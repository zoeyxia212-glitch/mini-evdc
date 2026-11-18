import unittest

from evdc.adapters.aliyun import AliyunAdapter
from evdc.adapters.aws import AwsAdapter
from evdc.clouds.fake import FakeAliyunEcs, FakeEc2, aliyun_throttle
from evdc.store import Store
from evdc.sync import SyncService
from tests.helpers import fast_policy, no_sleep


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.ali = no_sleep(AliyunAdapter("ali", FakeAliyunEcs("LTAI-demo", "secret-demo", count=12),
                                          fast_policy(max_attempts=2), page_size=5))
        self.aws = no_sleep(AwsAdapter("aws", FakeEc2("AKIA-demo", "secret-demo", count=7),
                                       fast_policy(), page_size=5))
        self.svc = SyncService(self.store, [self.ali, self.aws])

    def test_first_sync_creates_everything(self):
        reports = {r.provider: r for r in self.svc.sync_all()}
        # aliyun: 12 instances + 15 disks + 2 vpcs + 4 eips; aws: 7 + 9 volumes + 2 vpcs + 3 eips
        self.assertEqual((reports["aliyun"].created, reports["aws"].created), (33, 21))
        self.assertEqual(len(self.store.list_resources(resource_type="instance")), 19)
        self.assertEqual({r.resource_type for r in self.store.list_resources()}, {"instance", "disk", "vpc", "eip"})

    def test_second_sync_is_idempotent(self):
        self.svc.sync_all()
        r = self.svc.sync_one(self.ali)
        self.assertEqual((r.created, r.updated, r.unchanged, r.deleted), (0, 0, 33, 0))

    def test_changes_and_deletions_detected(self):
        self.svc.sync_all()
        inst = self.ali.client._instances
        inst[0]["Status"] = "Stopped" if inst[0]["Status"] != "Stopped" else "Running"
        gone = inst[1]["InstanceId"]
        self.ali.client.remove(gone)
        r = self.svc.sync_one(self.ali)
        self.assertEqual((r.updated, r.deleted), (1, 1))
        deleted = self.store.list_resources(provider="aliyun", status="deleted")
        self.assertEqual([d.native_id for d in deleted], [gone])

    def test_history_records_changes(self):
        self.svc.sync_all()
        disk = self.ali.client._disks[-1]
        disk["Status"] = "In_use"
        disk["InstanceId"] = self.ali.client._instances[0]["InstanceId"]
        self.svc.sync_one(self.ali)
        uid = f"aliyun:ali:{disk['DiskId']}"
        events = self.store.history(uid)
        self.assertEqual([e["kind"] for e in events], ["updated", "created"])
        self.assertEqual(events[0]["detail"]["status"], ["available", "in_use"])
        self.assertIn("attributes", events[0]["detail"])

    def test_failed_sync_never_deletes(self):
        self.svc.sync_all()
        # page 1 ok, page 2 throttled past max_attempts -> listing incomplete
        self.ali.client.faults.fail_next(lambda: None, aliyun_throttle, aliyun_throttle)
        r = self.svc.sync_one(self.ali)
        self.assertEqual((r.status, r.deleted), ("failed", 0))
        self.assertEqual(len(self.store.list_resources(provider="aliyun", status="deleted")), 0)

    def test_one_provider_failing_does_not_stop_others(self):
        self.ali.client._creds = ("bad", "bad")
        reports = {r.provider: r for r in self.svc.sync_all()}
        self.assertEqual(reports["aliyun"].status, "failed")
        self.assertEqual(reports["aws"].status, "success")
        history = self.store.sync_history()
        self.assertEqual(len(history), 2)


if __name__ == "__main__":
    unittest.main()
