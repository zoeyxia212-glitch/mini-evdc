import threading
import time
import unittest

from evdc.app import App
from evdc.errors import EvdcError, IncorrectState, NotFound, SyncInProgress
from evdc.models import NetworkType, Quota, ResourceStatus
from evdc.sync import SyncScheduler
from tests.helpers import FAST

ACCOUNTS = [
    {"provider": "aliyun", "account": "ali", "fake": True, "retry": FAST,
     "credentials": {"access_key_id": "LTAI-demo", "access_key_secret": "secret-demo"}},
    {"provider": "aws", "account": "aws", "fake": True, "retry": FAST,
     "credentials": {"access_key": "AKIA-demo", "secret_key": "secret-demo"}},
    {"provider": "azure", "account": "az", "fake": True, "retry": FAST,
     "credentials": {"tenant_id": "tenant-demo", "client_id": "app-demo", "client_secret": "secret-demo"}},
]


def make_app() -> App:
    app = App()
    app.load_accounts(ACCOUNTS)
    for a in app.adapters:
        a._sleep = lambda _s: None
    p = app.platform
    org = p.create_org("o")
    vdc = p.create_vdc(org.id, "v", "huabei1", Quota(8, 16, 200))
    net = p.create_network(vdc.id, "n", "10.9.0.0/24", NetworkType.ROUTED)
    p.create_vm(vdc.id, "local-01", 2, 4, 40, "Ubuntu", net.id)
    app.sync()
    return app


def first(app: App, provider: str, status: ResourceStatus):
    return next(r for r in app.store.list_resources(provider=provider) if r.status == status)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.addCleanup(self.app.close)
        self.clients = {a.provider: a.client for a in self.app.adapters}

    def test_start_and_stop_on_every_cloud(self):
        for provider in ("evdc", "aliyun", "aws", "azure"):
            with self.subTest(provider=provider):
                stopped = first(self.app, provider, ResourceStatus.STOPPED)
                r = self.app.manager.start(stopped.uid)
                self.assertTrue(r.changed)
                self.assertIn(r.status, ("pending", "running"))
                self.assertEqual(self.app.store.get(stopped.uid).status.value, r.status)

                running = first(self.app, provider, ResourceStatus.RUNNING)
                r = self.app.manager.stop(running.uid)
                self.assertIn(r.status, ("stopping", "stopped"))

    def test_final_state_arrives_with_next_sync(self):
        res = first(self.app, "aws", ResourceStatus.STOPPED)
        self.app.manager.start(res.uid)
        self.clients["aws"].settle()
        self.app.sync()
        self.assertEqual(self.app.store.get(res.uid).status, ResourceStatus.RUNNING)

    def test_already_running_is_a_no_op(self):
        res = first(self.app, "aliyun", ResourceStatus.RUNNING)
        calls = self.clients["aliyun"].faults.calls
        r = self.app.manager.start(res.uid)
        self.assertFalse(r.changed)
        self.assertEqual(self.clients["aliyun"].faults.calls, calls)  # cloud not even called

    def test_cloud_side_state_conflict_surfaces(self):
        # inventory says stopped, but on the cloud it is already stopping -> provider refuses
        res = first(self.app, "aliyun", ResourceStatus.RUNNING)
        self.clients["aliyun"]._find(res.native_id)["Status"] = "Stopping"
        self.app.store.set_status(res.uid, ResourceStatus.STOPPED)
        with self.assertRaises(IncorrectState):
            self.app.manager.start(res.uid)

    def test_unknown_or_deleted(self):
        with self.assertRaises(NotFound):
            self.app.manager.start("aws:aws:i-nope")
        res = first(self.app, "aliyun", ResourceStatus.STOPPED)
        self.app.store.set_status(res.uid, ResourceStatus.DELETED)
        with self.assertRaises(EvdcError):
            self.app.manager.start(res.uid)

    def test_refresh_reads_live_state(self):
        res = first(self.app, "aliyun", ResourceStatus.STOPPED)
        self.app.manager.start(res.uid)
        self.clients["aliyun"].settle()
        self.assertEqual(self.app.manager.refresh(res.uid).status, ResourceStatus.RUNNING)
        kinds = [e["kind"] for e in self.app.store.history(res.uid)]
        self.assertEqual(kinds, ["updated", "action", "created"])

    def test_refresh_marks_vanished_instance_deleted(self):
        res = first(self.app, "aliyun", ResourceStatus.RUNNING)
        self.clients["aliyun"].remove(res.native_id)
        self.assertEqual(self.app.manager.refresh(res.uid).status, ResourceStatus.DELETED)

    def test_reboot_only_when_running(self):
        for provider in ("aliyun", "aws", "azure", "evdc"):
            with self.subTest(provider=provider):
                stopped = first(self.app, provider, ResourceStatus.STOPPED)
                with self.assertRaises(EvdcError):
                    self.app.manager.reboot(stopped.uid)
        running = first(self.app, "azure", ResourceStatus.RUNNING)
        self.assertTrue(self.app.manager.reboot(running.uid).changed)

    def test_actions_only_on_instances(self):
        disk = self.app.store.list_resources(provider="aws", resource_type="disk")[0]
        with self.assertRaises(EvdcError):
            self.app.manager.start(disk.uid)

    def test_azure_action_reauths_on_expired_token(self):
        res = first(self.app, "azure", ResourceStatus.STOPPED)
        self.clients["azure"].expire_all_tokens()
        self.assertTrue(self.app.manager.start(res.uid).changed)


class SummaryTests(unittest.TestCase):
    def test_summary_matches_inventory(self):
        app = make_app()
        self.addCleanup(app.close)
        s = app.store.summary()
        live = [r for r in app.store.list_resources() if r.status != ResourceStatus.DELETED]
        self.assertEqual(s["total"], len(live))
        running = [r for r in live if r.status == ResourceStatus.RUNNING and r.resource_type == "instance"]
        self.assertEqual(s["running_vcpu"], sum(r.vcpu or 0 for r in running))
        self.assertEqual(sum(s["by_type"].values()), s["total"])
        self.assertEqual(set(s["by_provider"]), {"evdc", "aliyun", "aws", "azure"})


class SchedulerTests(unittest.TestCase):
    def test_runs_repeatedly_and_stops(self):
        hits = []
        sched = SyncScheduler(lambda: hits.append(1), interval=0.02).start()
        time.sleep(0.15)
        sched.stop()
        n = len(hits)
        self.assertGreaterEqual(n, 3)
        time.sleep(0.06)
        self.assertEqual(len(hits), n)

    def test_survives_failing_job(self):
        def boom():
            raise RuntimeError("x")
        sched = SyncScheduler(boom, interval=0.01).start()
        time.sleep(0.05)
        self.assertTrue(sched._thread.is_alive())
        sched.stop()

    def test_overlapping_sync_refused(self):
        app = make_app()
        self.addCleanup(app.close)
        aliyun = next(a for a in app.adapters if a.provider == "aliyun")
        aliyun.client.faults.latency = 0.3
        t = threading.Thread(target=app.sync)
        t.start()
        time.sleep(0.05)
        with self.assertRaises(SyncInProgress):
            app.sync()
        t.join()

    def test_bad_interval(self):
        with self.assertRaises(ValueError):
            SyncScheduler(lambda: None, 0)


if __name__ == "__main__":
    unittest.main()
