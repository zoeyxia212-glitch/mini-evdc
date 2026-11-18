import json
import threading
import unittest
import urllib.error
import urllib.request

from tests.testmanage import first, make_app
from evdc.api import serve
from evdc.errors import EvdcError
from evdc.models import CloudResource, ResourceStatus
from evdc.policy import Policy, matches
from evdc.pricing import cost_report, monthly_cost


def res(**kw) -> CloudResource:
    base = dict(provider="aws", account="a", region="r", native_id="x", name="n", status=ResourceStatus.RUNNING)
    base.update(kw)
    return CloudResource(**base)


class PricingTests(unittest.TestCase):
    def test_instance_only_billed_while_running(self):
        running = res(vcpu=2, memory_gb=4)
        self.assertAlmostEqual(monthly_cost(running), round((2 * 0.024 + 4 * 0.003) * 730, 2))
        self.assertEqual(monthly_cost(res(vcpu=2, memory_gb=4, status=ResourceStatus.STOPPED)), 0)

    def test_disk_priced_by_category_attached_or_not(self):
        d = res(resource_type="disk", size_gb=100, status=ResourceStatus.AVAILABLE, attributes={"category": "gp3"})
        self.assertAlmostEqual(monthly_cost(d), 9.6)
        unknown = res(resource_type="disk", size_gb=100, attributes={"category": "weird"})
        self.assertAlmostEqual(monthly_cost(unknown), 10.0)

    def test_report_totals_and_waste(self):
        items = [res(native_id="i", vcpu=1, memory_gb=1),
                 res(native_id="d", resource_type="disk", size_gb=50, status=ResourceStatus.AVAILABLE),
                 res(native_id="e", resource_type="eip", status=ResourceStatus.AVAILABLE),
                 res(native_id="gone", resource_type="disk", size_gb=999, status=ResourceStatus.DELETED)]
        rep = cost_report(items)
        self.assertAlmostEqual(rep["total_monthly"], sum(rep["by_provider"].values()))
        self.assertEqual({w["reason"] for w in rep["waste"]}, {"unattached disk", "unattached public IP"})
        self.assertNotIn("aws:a:gone", [t["uid"] for t in rep["top"]])


class PolicyUnitTests(unittest.TestCase):
    def test_filter_operators(self):
        r = res(vcpu=4, tags={"env": "prod"}, attributes={"attached_to": None, "category": "gp3"})
        cases = [
            ([{"key": "vcpu", "op": "gte", "value": 4}], True),
            ([{"key": "vcpu", "op": "gt", "value": 4}], False),
            ([{"key": "tag:env", "op": "eq", "value": "prod"}], True),
            ([{"key": "tag:owner", "op": "absent"}], True),
            ([{"key": "attributes.attached_to", "op": "absent"}], True),
            ([{"key": "status", "op": "in", "value": ["running", "pending"]}], True),
            ([{"key": "name", "op": "regex", "value": "^n$"}], True),
            ([{"key": "monthly_cost", "op": "gt", "value": 0}], True),
            ([{"key": "status", "op": "eq", "value": "running"}, {"key": "vcpu", "op": "lt", "value": 2}], False),
        ]
        for filters, expected in cases:
            with self.subTest(filters=filters):
                self.assertEqual(matches(r, filters), expected)

    def test_validation(self):
        bad = [
            {"name": "x y", "resource": "disk"},
            {"name": "ok", "resource": "bucket"},
            {"name": "ok", "resource": "disk", "filters": [{"key": "status", "op": "like"}]},
            {"name": "ok", "resource": "disk", "actions": ["delete"]},
            {"name": "ok", "resource": "disk", "actions": ["stop"]},
            {"name": "ok", "resource": "disk", "actions": [{"type": "mark"}]},
            {"name": "ok", "resource": "disk", "surprise": 1},
        ]
        for d in bad:
            with self.subTest(policy=d), self.assertRaises(EvdcError):
                Policy.from_dict(d)


class PolicyEngineTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.addCleanup(self.app.close)

    def test_dry_run_changes_nothing(self):
        out = self.app.policies.run("unattached-disks")
        self.assertTrue(out["dry_run"])
        self.assertGreater(out["matched"], 0)
        self.assertTrue(all(r["actions"] == ["would mark delete-in-30d"] for r in out["results"]))
        self.assertEqual(self.app.store.marks(), {})

    def test_mark_then_select_marked(self):
        self.app.policies.run("unattached-disks", dry_run=False)
        marks = self.app.store.marks()
        self.assertTrue(marks and all("delete-in-30d" in v for v in marks.values()))
        follow_up = Policy.from_dict({"name": "marked-disks", "resource": "disk",
                                      "filters": [{"key": "marked", "op": "contains", "value": "delete-in-30d"}]})
        self.assertEqual(self.app.policies.run(follow_up)["matched"], len(marks))

    def test_stop_action_uses_manager(self):
        out = self.app.policies.run("stop-test-instances", dry_run=False)
        self.assertGreater(out["matched"], 0)
        for r in out["results"]:
            self.assertEqual(self.app.store.get(r["uid"]).status, ResourceStatus.STOPPING)
            self.assertEqual(r["actions"], ["stop"])

    def test_runs_are_recorded(self):
        self.app.policies.run("idle-public-ips")
        self.assertEqual(self.app.store.policy_runs()[0]["policy"], "idle-public-ips")


class QueryAndGraphTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.addCleanup(self.app.close)

    def test_query_filters_sort_paging(self):
        page, total = self.app.store.query(resource_type="instance", sort="-vcpu", limit=5)
        self.assertEqual(len(page), 5)
        self.assertGreater(total, 5)
        self.assertEqual([r.vcpu for r in page], sorted([r.vcpu for r in page], reverse=True))
        prod, _ = self.app.store.query(tag="env:prod", limit=1000)
        self.assertTrue(prod and all(r.tags["env"] == "prod" for r in prod))
        named, _ = self.app.store.query(name="WEB", limit=1000)
        self.assertTrue(named and all("web" in r.name.lower() for r in named))
        with self.assertRaises(ValueError):
            self.app.store.query(sort="password")

    def test_instance_graph(self):
        inst = self.app.store.list_resources(provider="aliyun", resource_type="instance")[0]
        rel = self.app.store.relations(inst.uid)
        kinds = {e["relation"] for e in rel["edges"]}
        self.assertIn("has_disk", kinds)
        disk_uid = next(e["uid"] for e in rel["edges"] if e["relation"] == "has_disk")
        back = self.app.store.relations(disk_uid)["edges"]
        self.assertEqual(back[0]["uid"], inst.uid)

    def test_azure_public_ip_links_to_vm(self):
        pip = next(r for r in self.app.store.list_resources(provider="azure", resource_type="eip")
                   if r.status == ResourceStatus.IN_USE)
        self.assertEqual(self.app.store.relations(pip.uid)["edges"][0]["type"], "instance")

    def test_orphans_after_instance_deleted(self):
        inst = first(self.app, "aws", ResourceStatus.RUNNING)
        self.assertEqual(self.app.store.orphans(), [])
        self.app.store.set_status(inst.uid, ResourceStatus.DELETED)
        orphans = self.app.store.orphans()
        self.assertTrue(orphans and all(o.attributes["attached_to"] == inst.native_id for o in orphans))


class GovernanceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = make_app()
        cls.key = cls.app.bootstrap_admin()
        cls.server = serve(cls.app, port=0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.close()

    def get(self, path, key="admin", method="GET", body=None):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {self.key if key == 'admin' else key}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def test_paging_header_and_csv(self):
        s, h, body = self.get("/resources?type=disk&limit=3&sort=-size_gb")
        self.assertEqual((s, len(json.loads(body))), (200, 3))
        self.assertGreater(int(h["X-Total-Count"]), 3)
        s, h, body = self.get("/resources.csv?provider=aws&type=instance")
        self.assertTrue(h["Content-Type"].startswith("text/csv"))
        lines = body.strip().splitlines()
        self.assertTrue(lines[0].startswith("uid,provider"))
        self.assertEqual(len(lines), 1 + 17)

    def test_costs_relations_orphans(self):
        s, _, body = self.get("/costs?top=3")
        rep = json.loads(body)
        self.assertEqual((s, len(rep["top"])), (200, 3))
        self.assertGreater(rep["waste_monthly"], 0)
        inst = self.app.store.list_resources(provider="aws", resource_type="instance")[0]
        s, _, body = self.get(f"/resources/relations?uid={inst.uid}")
        self.assertEqual(json.loads(body)["uid"], inst.uid)
        self.assertEqual(self.get("/resources/relations")[0], 400)
        self.assertEqual(self.get("/resources/orphans")[0], 200)

    def test_policies_over_http(self):
        s, _, body = self.get("/policies")
        self.assertIn("unattached-disks", [p["name"] for p in json.loads(body)])
        s, _, body = self.get("/policies/run", method="POST", body={"name": "idle-public-ips"})
        self.assertTrue(json.loads(body)["dry_run"])
        adhoc = {"name": "big-instances", "resource": "instance",
                 "filters": [{"key": "vcpu", "op": "gte", "value": 8}]}
        s, _, body = self.get("/policies/run", method="POST", body={"policy": adhoc})
        self.assertEqual(s, 200)
        self.assertEqual(self.get("/policies", method="POST", body=adhoc)[0], 201)
        bad = {"name": "Bad Name", "resource": "disk"}
        self.assertEqual(self.get("/policies", method="POST", body=bad)[0], 400)

    def test_tenant_may_preview_but_not_act(self):
        org = self.app.platform.create_org("govern-org")
        key = self.app.auth.create_user("gov-viewer", "viewer", org.id)
        self.assertEqual(self.get("/policies/run", key=key, method="POST", body={"name": "idle-public-ips"})[0], 200)
        self.assertEqual(self.get("/policies/run", key=key, method="POST",
                                  body={"name": "idle-public-ips", "dry_run": False})[0], 403)

    def test_dashboard_served_without_key(self):
        for path in ("/", "/dashboard"):
            s, h, body = self.get(path, key=None)
            self.assertEqual(s, 200)
            self.assertTrue(h["Content-Type"].startswith("text/html"))
            self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
            self.assertIn("Mini EvDC", body)
            self.assertNotIn(self.key, body)   # the page never embeds credentials

    def test_metrics_public_prometheus_text(self):
        s, h, body = self.get("/metrics", key=None)
        self.assertEqual(s, 200)
        self.assertTrue(h["Content-Type"].startswith("text/plain"))
        self.assertIn("# TYPE evdc_resources gauge", body)
        self.assertIn('evdc_sync_success{provider="aws",account="aws"} 1', body)
        self.assertIn("evdc_monthly_cost_usd", body)


if __name__ == "__main__":
    unittest.main()
