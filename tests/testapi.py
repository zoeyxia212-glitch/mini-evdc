import json
import threading
import unittest
import urllib.error
import urllib.request

from tests import helpers  # noqa: F401
from evdc.api import serve
from evdc.app import App


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = App()
        cls.app.load_accounts([{"provider": "aws", "account": "aws-syd", "fake": True,
                                "credentials": {"access_key": "AKIA-demo", "secret_key": "secret-demo"}}])
        cls.server = serve(cls.app, port=0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_full_flow(self):
        s, org = self.call("POST", "/orgs", {"name": "api-org"})
        self.assertEqual(s, 201)
        s, vdc = self.call("POST", f"/orgs/{org['id']}/vdcs",
                           {"name": "v1", "quota": {"vcpu": 4, "memory_gb": 8, "storage_gb": 100}})
        s, net = self.call("POST", f"/vdcs/{vdc['id']}/networks", {"name": "n1", "cidr": "172.20.0.0/24"})
        s, vm = self.call("POST", f"/vdcs/{vdc['id']}/vms", {"name": "app-01", "vcpu": 2, "memory_gb": 4,
                                                            "disk_gb": 40, "network_id": net["id"]})
        self.assertEqual((s, vm["ip"], vm["power_state"]), (201, "172.20.0.2", "POWERED_OFF"))

        s, vm = self.call("POST", f"/vms/{vm['id']}/power", {"action": "power_on"})
        self.assertEqual(vm["power_state"], "POWERED_ON")
        s, err = self.call("DELETE", f"/vms/{vm['id']}")
        self.assertEqual(s, 409)  # must power off first
        s, err = self.call("POST", f"/vdcs/{vdc['id']}/vms", {"name": "big", "vcpu": 8, "memory_gb": 4,
                                                             "disk_gb": 40, "network_id": net["id"]})
        self.assertEqual(s, 409)
        self.assertIn("quota", err["error"])

    def test_sync_and_inventory(self):
        s, reports = self.call("POST", "/sync")
        self.assertEqual(s, 200)
        self.assertTrue(all(r["status"] == "success" for r in reports))
        s, items = self.call("GET", "/resources?provider=aws")
        self.assertEqual(len(items), 17)
        self.assertEqual(set(items[0]), {"provider", "account", "region", "native_id", "name", "status", "vcpu",
                                         "memory_gb", "private_ip", "public_ip", "tags", "resource_type",
                                         "synced_at", "uid"})

    def test_errors(self):
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("POST", "/vms/vm-missing/power", {"action": "power_on"})[0], 404)
        self.assertEqual(self.call("POST", "/orgs", {})[0], 400)


if __name__ == "__main__":
    unittest.main()
