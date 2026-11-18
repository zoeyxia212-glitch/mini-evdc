import unittest

from tests import helpers  # noqa: F401  (quiet logs)
from evdc.core import EvdcPlatform
from evdc.errors import EvdcError, InvalidState, NotFound, QuotaExceeded
from evdc.models import FirewallRule, NatRule, PowerState, Quota


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
            self.vm(vcpu=99)
        last = self.p.tasks[-1]
        self.assertEqual((last.operation, last.status), ("create_vm", "error"))
        self.assertIn("quota", last.detail)


if __name__ == "__main__":
    unittest.main()
