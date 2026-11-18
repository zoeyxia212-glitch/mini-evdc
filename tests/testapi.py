import json
import threading
import unittest
import urllib.error
import urllib.request

from tests.helpers import FAST
from evdc.api import serve
from evdc.app import App


class ApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = App()
        cls.app.load_accounts([{"provider": "aws", "account": "aws-syd", "fake": True, "retry": FAST,
                                "credentials": {"access_key": "AKIA-demo", "secret_key": "secret-demo"}}])
        cls.admin = cls.app.bootstrap_admin()
        cls.server = serve(cls.app, port=0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.close()

    def call(self, method, path, body=None, key="admin"):
        key = self.admin if key == "admin" else key
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            raw = err.read()
            return err.code, json.loads(raw) if raw else None

    def make_tenant(self, name):
        _, org = self.call("POST", "/orgs", {"name": name})
        _, vdc = self.call("POST", f"/orgs/{org['id']}/vdcs",
                           {"name": "v1", "quota": {"vcpu": 8, "memory_gb": 16, "storage_gb": 200}})
        _, net = self.call("POST", f"/vdcs/{vdc['id']}/networks", {"name": "n1", "cidr": "172.20.0.0/24"})
        _, admin = self.call("POST", "/users", {"name": f"{name}-admin", "role": "org_admin", "org_id": org["id"]})
        _, viewer = self.call("POST", "/users", {"name": f"{name}-viewer", "role": "viewer", "org_id": org["id"]})
        return org, vdc, net, admin["api_key"], viewer["api_key"]


class AuthTests(ApiBase):
    def test_health_is_public_everything_else_needs_a_key(self):
        self.assertEqual(self.call("GET", "/health", key=None)[0], 200)
        self.assertEqual(self.call("GET", "/orgs", key=None)[0], 401)
        self.assertEqual(self.call("GET", "/orgs", key="evdc_wrong")[0], 401)
        s, me = self.call("GET", "/me")
        self.assertEqual((s, me["role"]), (200, "system_admin"))

    def test_x_api_key_header_also_works(self):
        req = urllib.request.Request(self.base + "/me", headers={"X-API-Key": self.admin})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)

    def test_roles_and_org_scoping(self):
        org, vdc, net, a_key, v_key = self.make_tenant("alpha")
        _, _, other_net, b_key, _ = self.make_tenant("beta")

        # org admin can build inside its own org
        s, vm = self.call("POST", f"/vdcs/{vdc['id']}/vms", {"name": "app-01", "vcpu": 2, "memory_gb": 4,
                                                            "disk_gb": 40, "network_id": net["id"]}, key=a_key)
        self.assertEqual(s, 201)
        # viewer can read but not write
        self.assertEqual(self.call("GET", f"/vms/{vm['id']}", key=v_key)[0], 200)
        self.assertEqual(self.call("POST", f"/vms/{vm['id']}/power", {"action": "power_on"}, key=v_key)[0], 403)
        # another org's admin cannot see or touch it
        self.assertEqual(self.call("GET", f"/vms/{vm['id']}", key=b_key)[0], 403)
        self.assertEqual(self.call("DELETE", f"/vms/{vm['id']}", key=b_key)[0], 403)
        # lists are scoped
        _, vms = self.call("GET", "/vms", key=b_key)
        self.assertNotIn(vm["id"], [v["id"] for v in vms])
        _, orgs = self.call("GET", "/orgs", key=a_key)
        self.assertEqual([o["id"] for o in orgs], [org["id"]])
        # provider-only operations
        self.assertEqual(self.call("POST", "/orgs", {"name": "x"}, key=a_key)[0], 403)
        self.assertEqual(self.call("PUT", f"/vdcs/{vdc['id']}/quota",
                                   {"vcpu": 99, "memory_gb": 99, "storage_gb": 999}, key=a_key)[0], 403)
        self.assertEqual(self.call("POST", "/sync", key=a_key)[0], 403)
        self.assertEqual(self.call("GET", "/tasks", key=a_key)[0], 403)
        # org admin can add a viewer to its own org but not to another one
        self.assertEqual(self.call("POST", "/users", {"name": "alpha-ro", "role": "viewer",
                                                      "org_id": org["id"]}, key=a_key)[0], 201)
        self.assertEqual(self.call("POST", "/users", {"name": "sneaky", "role": "system_admin"}, key=a_key)[0], 403)

    def test_user_key_shown_once_and_deletable(self):
        org, *_ = self.make_tenant("gamma")
        s, created = self.call("POST", "/users", {"name": "gamma-x", "role": "viewer", "org_id": org["id"]})
        self.assertEqual(s, 201)
        _, users = self.call("GET", "/users")
        self.assertNotIn("api_key", next(u for u in users if u["name"] == "gamma-x"))
        self.assertEqual(self.call("DELETE", "/users/gamma-x")[0], 204)
        self.assertEqual(self.call("GET", "/me", key=created["api_key"])[0], 401)
        self.assertEqual(self.call("DELETE", "/users/admin")[0], 400)  # cannot delete yourself


class PlatformApiTests(ApiBase):
    def test_full_vm_lifecycle(self):
        org, vdc, net, key, _ = self.make_tenant("delta")
        s, tpl = self.call("POST", f"/orgs/{org['id']}/templates",
                           {"name": "small", "os": "Ubuntu 22.04", "vcpu": 2, "memory_gb": 4, "disk_gb": 40}, key=key)
        s, vm = self.call("POST", f"/vdcs/{vdc['id']}/vms", {"name": "web-01", "template_id": tpl["id"],
                                                            "network_id": net["id"], "tags": ["web"]}, key=key)
        self.assertEqual((s, vm["ip"], vm["tags"]), (201, "172.20.0.2", ["web"]))
        self.assertEqual(self.call("PUT", f"/vms/{vm['id']}/size", {"vcpu": 4, "memory_gb": 8, "disk_gb": 60},
                                   key=key)[1]["disk_gb"], 60)
        self.assertEqual(self.call("POST", f"/vms/{vm['id']}/snapshot", key=key)[0], 201)
        self.assertEqual(self.call("POST", f"/vms/{vm['id']}/snapshot/revert", key=key)[1]["vcpu"], 4)
        self.assertEqual(self.call("DELETE", f"/vms/{vm['id']}/snapshot", key=key)[0], 204)
        s, vm = self.call("POST", f"/vms/{vm['id']}/power", {"action": "power_on"}, key=key)
        self.assertEqual(vm["power_state"], "POWERED_ON")
        self.assertEqual(self.call("DELETE", f"/vms/{vm['id']}", key=key)[0], 409)      # power off first
        self.assertEqual(self.call("POST", f"/vms/{vm['id']}/power", {"action": "fly"}, key=key)[0], 409)
        self.call("POST", f"/vms/{vm['id']}/power", {"action": "power_off"}, key=key)
        self.assertEqual(self.call("DELETE", f"/vms/{vm['id']}", key=key)[0], 204)
        s, info = self.call("GET", f"/vdcs/{vdc['id']}", key=key)
        self.assertEqual(info["usage"]["vm_count"], 0)

    def test_edge_and_dfw(self):
        org, vdc, net, key, _ = self.make_tenant("eps")
        mk = lambda n, t: self.call("POST", f"/vdcs/{vdc['id']}/vms", {  # noqa: E731
            "name": n, "vcpu": 1, "memory_gb": 1, "disk_gb": 20, "network_id": net["id"], "tags": [t],
            "power_on": True}, key=key)[1]
        web, db = mk("web-01", "web"), mk("db-01", "db")
        self.call("POST", f"/vdcs/{vdc['id']}/nat", {"kind": "DNAT", "original_ip": "203.0.113.7",
                                                     "translated_ip": web["ip"], "original_port": 80}, key=key)
        s, fw = self.call("POST", f"/vdcs/{vdc['id']}/firewall", {"name": "http", "source": "any",
                                                                   "destination": "any", "port": 80,
                                                                   "action": "allow"}, key=key)
        s, r = self.call("POST", f"/vdcs/{vdc['id']}/check-inbound",
                         {"client_ip": "1.2.3.4", "public_ip": "203.0.113.7", "port": 80}, key=key)
        self.assertEqual(r["result"], "delivered")
        self.call("POST", f"/vdcs/{vdc['id']}/dfw", {"name": "web-db", "source": "tag:web",
                                                     "destination": "tag:db", "port": 5432, "action": "allow"}, key=key)
        self.call("PUT", f"/vdcs/{vdc['id']}/dfw/default", {"action": "deny"}, key=key)
        ew = lambda a, b, port: self.call("POST", f"/vdcs/{vdc['id']}/check-east-west",  # noqa: E731
                                          {"src_vm": a["id"], "dst_vm": b["id"], "port": port}, key=key)[1]["result"]
        self.assertEqual((ew(web, db, 5432), ew(db, web, 22)), ("delivered", "dropped"))
        s, edge = self.call("GET", f"/vdcs/{vdc['id']}/edge", key=key)
        self.assertEqual((len(edge["firewall"]), len(edge["nat"]), len(edge["dfw"]), edge["dfw_default"]),
                         (1, 1, 1, "deny"))
        self.assertEqual(self.call("DELETE", f"/vdcs/{vdc['id']}/rules/{fw['id']}", key=key)[0], 204)

    def test_validation_errors(self):
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("PUT", "/orgs")[0], 405)
        self.assertEqual(self.call("POST", "/vms/vm-missing/power", {"action": "power_on"})[0], 404)
        s, err = self.call("POST", "/orgs", {})
        self.assertEqual((s, "name" in err["error"]), (400, True))


class MultiCloudApiTests(ApiBase):
    def test_sync_inventory_and_actions(self):
        s, reports = self.call("POST", "/sync")
        self.assertTrue(all(r["status"] == "success" for r in reports))
        s, items = self.call("GET", "/resources?provider=aws&type=instance")
        self.assertEqual(len(items), 17)
        self.assertEqual(len(self.call("GET", "/resources?provider=aws&type=disk")[1]), 19)
        _, summary = self.call("GET", "/resources/summary")
        self.assertEqual(summary["by_type"]["eip"], 3)

        uid = next(i["uid"] for i in items if i["status"] == "stopped")
        s, r = self.call("POST", "/resources/start", {"uid": uid})
        self.assertEqual((s, r["status"], r["changed"]), (202, "pending", True))
        s, r = self.call("POST", "/resources/start", {"uid": uid})
        self.assertEqual((s, r["changed"]), (200, False))
        s, live = self.call("POST", "/resources/refresh", {"uid": uid})
        self.assertEqual((s, live["uid"]), (200, uid))
        s, hist = self.call("GET", f"/resources/history?uid={uid}")
        self.assertIn("action", [h["kind"] for h in hist])
        self.assertEqual(self.call("POST", "/resources/start", {"uid": "aws:aws-syd:i-nope"})[0], 404)
        self.assertEqual(self.call("POST", "/resources/fly", {"uid": uid})[0], 404)

    def test_tenants_read_inventory_but_cannot_act(self):
        *_, key, viewer = self.make_tenant("zeta")
        self.call("POST", "/sync")
        self.assertEqual(self.call("GET", "/resources/summary", key=viewer)[0], 200)
        uid = self.call("GET", "/resources?type=instance")[1][0]["uid"]
        self.assertEqual(self.call("POST", "/resources/stop", {"uid": uid}, key=key)[0], 403)


if __name__ == "__main__":
    unittest.main()
