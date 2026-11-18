import unittest

from evdc.core import EvdcPlatform
from evdc.errors import EvdcError, InvalidState, NotFound, QuotaExceeded
from evdc.models import DistributedRule, FirewallRule, NatRule, PowerState, Quota
from tests import helpers  # noqa: F401  (quiet logs)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.p = EvdcPlatform()
        org = self.p.create_org("acme")
        self.vdc = self.p.create_vdc(org.id, "prod", "huabei1", Quota(8, 32, 300))
        self.net = self.p.create_network(self.vdc.id, "web", "10.0.0.0/29")  # 6 hosts, .1 = gateway

    def vm(self, name="web-01", **kw):
        args = dict(vcpu=2, memory_gb=4, disk_gb=40, os="Ubuntu", network_id=self.net.id)
        args.update(kw)
        return self.p.create_vm(self.vdc.id, name, **args)

    def test_ip_allocation_skips_gateway_and_reuses_released(self):
        a, b = self.vm("a"), self.vm("b")
        self.assertEqual((self.net.gateway, a.ip, b.ip), ("10.0.0.1", "10.0.0.2", "10.0.0.3"))
        self.p.delete_vm(a.id)
        self.assertEqual(self.vm("c").ip, "10.0.0.2")

    def test_network_runs_out_of_ips(self):
        for i in range(5):
            self.vm(f"v{i}", vcpu=1, memory_gb=1, disk_gb=20)
        with self.assertRaises(QuotaExceeded):
            self.vm("v6", vcpu=1, memory_gb=1, disk_gb=20)

    def test_overlapping_network_rejected(self):
        with self.assertRaises(EvdcError):
            self.p.create_network(self.vdc.id, "dup", "10.0.0.0/24")

    def test_quota_enforced_on_create_and_resize(self):
        vm = self.vm(vcpu=6)
        with self.assertRaises(QuotaExceeded):
            self.vm("b", vcpu=4)
        with self.assertRaises(QuotaExceeded):
            self.p.resize(vm.id, 10, 4)

    def test_snapshot_counts_toward_storage(self):
        vm = self.vm(disk_gb=150)
        self.p.create_snapshot(vm.id)
        self.assertEqual(self.p.usage(self.vdc.id)["used"]["storage_gb"], 300)
        with self.assertRaises(QuotaExceeded):
            self.vm("b", disk_gb=20)

    def test_name_rules(self):
        for bad in ("12345", "has space", "x" * 16):
            with self.assertRaises(EvdcError):
                self.vm(bad)

    def test_power_state_machine(self):
        vm = self.vm()
        self.p.power(vm.id, "power_on")
        self.p.power(vm.id, "suspend")
        self.assertEqual(vm.power_state, PowerState.SUSPENDED)
        with self.assertRaises(InvalidState):
            self.p.power(vm.id, "reset")
        with self.assertRaises(InvalidState):
            self.p.delete_vm(vm.id)
        self.p.power(vm.id, "discard_suspend")
        self.p.delete_vm(vm.id)
        with self.assertRaises(NotFound):
            self.p.get_vm(vm.id)

    def test_resize_requires_power_off(self):
        vm = self.vm(power_on=True)
        with self.assertRaises(InvalidState):
            self.p.resize(vm.id, 4, 8)

    def test_snapshot_revert(self):
        vm = self.vm()
        self.p.create_snapshot(vm.id)
        self.p.resize(vm.id, 4, 8)
        self.p.revert_snapshot(vm.id)
        self.assertEqual((vm.vcpu, vm.memory_gb), (2, 4))

    def test_edge_needs_nat_and_firewall(self):
        vm = self.vm()
        self.assertEqual(self.p.check_inbound(self.vdc.id, "1.1.1.1", "203.0.113.5", 80)["reason"], "no DNAT rule")
        self.p.add_nat_rule(self.vdc.id, NatRule("DNAT", "203.0.113.5", vm.ip, 80, 8080))
        self.assertEqual(self.p.check_inbound(self.vdc.id, "1.1.1.1", "203.0.113.5", 80)["result"], "dropped")
        self.p.add_firewall_rule(self.vdc.id, FirewallRule("web", "any", "10.0.0.0/29", 8080, "allow"))
        res = self.p.check_inbound(self.vdc.id, "1.1.1.1", "203.0.113.5", 80)
        self.assertEqual((res["result"], res["to"]), ("delivered", f"{vm.ip}:8080"))

    def test_firewall_first_match_wins(self):
        self.p.add_firewall_rule(self.vdc.id, FirewallRule("allow-all", "any", "any", None, "allow"))
        self.p.add_firewall_rule(self.vdc.id, FirewallRule("block-bad", "6.6.6.0/24", "any", None, "deny"),
                                 position=0)
        edge = self.p.edge(self.vdc.id)
        self.assertEqual(edge.evaluate("6.6.6.6", "10.0.0.2", 22), ("deny", "block-bad"))
        self.assertEqual(edge.evaluate("7.7.7.7", "10.0.0.2", 22), ("allow", "allow-all"))

    def test_tasks_recorded_with_errors(self):
        with self.assertRaises(QuotaExceeded):
            self.vm(vcpu=20)
        last = self.p.tasks[-1]
        self.assertEqual((last.operation, last.status), ("create_vm", "error"))
        self.assertIn("quota", last.detail)


class PlatformFeatureTests(unittest.TestCase):
    def setUp(self):
        self.saved = []
        self.p = EvdcPlatform(on_change=self.saved.append)
        self.org = self.p.create_org("acme")
        self.vdc = self.p.create_vdc(self.org.id, "prod", "huabei1", Quota(16, 64, 1000))
        self.net = self.p.create_network(self.vdc.id, "app", "10.1.0.0/24")

    def test_template_create_and_capture(self):
        tpl = self.p.create_template(self.org.id, "small", "Ubuntu", 2, 4, 40)
        vm = self.p.create_vm_from_template(self.vdc.id, "web-01", tpl.id, self.net.id, tags=["web"])
        self.assertEqual((vm.vcpu, vm.os, vm.template_id), (2, "Ubuntu", tpl.id))
        self.p.resize(vm.id, 4, 8, 80)
        cap = self.p.capture_template(vm.id, "web-golden")
        self.assertEqual((cap.vcpu, cap.disk_gb), (4, 80))
        other = self.p.create_org("other")
        with self.assertRaises(EvdcError):
            self.p.create_vm_from_template(self.p.create_vdc(other.id, "v", "r", Quota(4, 8, 100)).id,
                                           "x-01", tpl.id, self.net.id)

    def test_distributed_firewall_by_tag(self):
        web = self.p.create_vm(self.vdc.id, "web-01", 1, 1, 20, "u", self.net.id, power_on=True, tags=["web"])
        db = self.p.create_vm(self.vdc.id, "db-01", 1, 1, 20, "u", self.net.id, power_on=True, tags=["db"])
        self.assertEqual(self.p.check_east_west(db.id, web.id, 22)["result"], "delivered")  # default allow
        self.p.add_dfw_rule(self.vdc.id, DistributedRule("web-db", "tag:web", "tag:db", 5432, "allow"))
        self.p.set_dfw_default(self.vdc.id, "deny")
        self.assertEqual(self.p.check_east_west(web.id, db.id, 5432)["result"], "delivered")
        self.assertEqual(self.p.check_east_west(web.id, db.id, 22)["result"], "dropped")
        # moving db's role = changing the tag, no rule edits
        self.p.set_tags(db.id, ["cache"])
        self.assertEqual(self.p.check_east_west(web.id, db.id, 5432)["result"], "dropped")
        with self.assertRaises(EvdcError):
            self.p.add_dfw_rule(self.vdc.id, DistributedRule("bad", "tag:Bad Tag", "any", None, "allow"))

    def test_rules_can_be_deleted(self):
        rule = self.p.add_firewall_rule(self.vdc.id, FirewallRule("r", "any", "any", None, "allow"))
        nat = self.p.add_nat_rule(self.vdc.id, NatRule("SNAT", "10.1.0.5", "203.0.113.1"))
        self.p.delete_rule(self.vdc.id, rule.id)
        self.p.delete_rule(self.vdc.id, nat.id)
        self.assertEqual((self.p.edge(self.vdc.id).firewall, self.p.edge(self.vdc.id).nat), ([], []))
        with self.assertRaises(NotFound):
            self.p.delete_rule(self.vdc.id, rule.id)

    def test_delete_order_is_enforced(self):
        vm = self.p.create_vm(self.vdc.id, "a-01", 1, 1, 20, "u", self.net.id)
        with self.assertRaises(InvalidState):
            self.p.delete_network(self.net.id)
        with self.assertRaises(InvalidState):
            self.p.delete_vdc(self.vdc.id)
        self.p.delete_vm(vm.id)
        self.p.delete_network(self.net.id)
        self.p.delete_vdc(self.vdc.id)
        self.p.delete_org(self.org.id)
        self.assertEqual(self.p.orgs, {})

    def test_quota_update_cannot_go_below_usage(self):
        self.p.create_vm(self.vdc.id, "a-01", 8, 8, 20, "u", self.net.id)
        with self.assertRaises(QuotaExceeded):
            self.p.update_quota(self.vdc.id, Quota(4, 64, 1000))
        self.p.update_quota(self.vdc.id, Quota(32, 64, 1000))

    def test_disk_grows_never_shrinks(self):
        vm = self.p.create_vm(self.vdc.id, "a-01", 1, 1, 50, "u", self.net.id)
        with self.assertRaises(EvdcError):
            self.p.resize(vm.id, 1, 1, 30)

    def test_state_round_trip(self):
        vm = self.p.create_vm(self.vdc.id, "a-01", 2, 2, 40, "u", self.net.id, power_on=True, tags=["web"])
        self.p.create_snapshot(vm.id)
        self.p.add_dfw_rule(self.vdc.id, DistributedRule("x", "tag:web", "any", 80, "deny"))
        self.p.add_nat_rule(self.vdc.id, NatRule("DNAT", "203.0.113.9", vm.ip, 80, 8080))
        clone = EvdcPlatform()
        clone.load_state(self.saved[-1])
        self.assertEqual(clone.to_state(), self.p.to_state())
        self.assertEqual(clone.get_vm(vm.id).power_state, PowerState.POWERED_ON)
        self.assertEqual(clone.check_inbound(self.vdc.id, "1.1.1.1", "203.0.113.9", 80)["result"], "dropped")

    def test_failed_change_is_not_saved(self):
        before = len(self.saved)
        with self.assertRaises(EvdcError):
            self.p.create_network(self.vdc.id, "dup", "10.1.0.0/25")
        self.assertEqual(len(self.saved), before)


if __name__ == "__main__":
    unittest.main()
