import time
import unittest

from evdc.adapters.aliyun import AliyunAdapter
from evdc.adapters.aws import AwsAdapter
from evdc.adapters.azure import AzureAdapter
from evdc.clouds.fake import FakeAliyunEcs, FakeAzure, FakeEc2, aliyun_throttle, aws_throttle, azure_throttle
from evdc.errors import ApiTimeout, AuthError, TransientError
from evdc.models import CloudResource, ResourceStatus
from tests.helpers import fast_policy, no_sleep


def ali(count=23, **kw):
    return no_sleep(AliyunAdapter("ali", FakeAliyunEcs("LTAI-demo", "secret-demo", count=count),
                                  fast_policy(**kw), page_size=10))


def aws(count=17, **kw):
    return no_sleep(AwsAdapter("aws", FakeEc2("AKIA-demo", "secret-demo", count=count),
                               fast_policy(**kw), page_size=5))


def azure(**kw):
    ttl = kw.pop("ttl", 3600)
    client = FakeAzure("tenant-demo", "app-demo", "secret-demo", token_ttl=ttl)
    return no_sleep(AzureAdapter("az", client, fast_policy(**kw), page_size=5))


class PaginationAndMapping(unittest.TestCase):
    def test_aliyun_reads_every_page(self):
        a = ali(23)
        items = list(a.list_resources(("instance",)))
        self.assertEqual(len(items), 23)
        self.assertEqual(a.client.faults.calls, 3)  # 10 + 10 + 3
        self.assertEqual(len({r.uid for r in items}), 23)

    def test_aliyun_exact_page_boundary(self):
        a = ali(20)
        self.assertEqual(len(list(a.list_resources(("instance",)))), 20)
        self.assertEqual(a.client.faults.calls, 2)

    def test_aliyun_memory_is_converted_from_mb(self):
        r = next(ali(1).list_resources(("instance",)))
        self.assertEqual(r.memory_gb, r.vcpu * 2)
        self.assertEqual(r.provider, "aliyun")
        self.assertIn("env", r.tags)

    def test_aws_next_token_and_type_lookup_is_cached(self):
        a = aws(17)
        items = list(a.list_resources(("instance",)))
        self.assertEqual(len(items), 17)
        self.assertTrue(all(r.vcpu and r.memory_gb for r in items))
        self.assertLessEqual(a.client.type_calls, 3)  # only 3 distinct types exist
        self.assertTrue(all(not r.region.endswith("a") for r in items))
        self.assertNotIn("Name", items[0].tags)

    def test_azure_next_link_and_power_state(self):
        a = azure()
        items = list(a.list_resources(("instance",)))
        self.assertEqual(len(items), 12)
        self.assertTrue(all(isinstance(r.status, ResourceStatus) for r in items))
        self.assertTrue(all(r.vcpu for r in items))

    def test_all_providers_produce_same_shape(self):
        for adapter in (ali(3), aws(3), azure()):
            r = next(adapter.list_resources())
            self.assertIsInstance(r, CloudResource)
            self.assertTrue(r.uid.startswith(adapter.provider + ":"))

    def test_malformed_record_is_skipped_not_fatal(self):
        a = ali(3)
        a.client._instances[1].pop("Status")
        self.assertEqual(len(list(a.list_resources(("instance",)))), 2)
        self.assertEqual(a.skipped, 1)


class ResourceTypes(unittest.TestCase):
    def check_types(self, adapter, expected: dict):
        items = list(adapter.list_resources())
        counts = {}
        for r in items:
            counts[r.resource_type] = counts.get(r.resource_type, 0) + 1
        self.assertEqual(counts, expected)
        return items

    def test_aliyun_all_types(self):
        items = self.check_types(ali(5), {"instance": 5, "disk": 8, "vpc": 2, "eip": 4})
        disk = next(r for r in items if r.resource_type == "disk" and r.status == ResourceStatus.IN_USE)
        self.assertEqual((disk.size_gb, disk.attributes["category"]), (40.0, "cloud_essd"))
        self.assertTrue(disk.attributes["attached_to"].startswith("i-"))
        vpc = next(r for r in items if r.resource_type == "vpc")
        self.assertEqual(vpc.attributes["cidr"], "172.16.0.0/12")
        eips = [r for r in items if r.resource_type == "eip"]
        self.assertEqual(sum(r.status == ResourceStatus.IN_USE for r in eips), 2)

    def test_aws_all_types(self):
        items = self.check_types(aws(4), {"instance": 4, "disk": 6, "vpc": 2, "eip": 3})
        free = [r for r in items if r.resource_type == "disk" and r.status == ResourceStatus.AVAILABLE]
        self.assertEqual(len(free), 2)
        self.assertIsNone(free[0].attributes["attached_to"])
        self.assertEqual(next(r for r in items if r.native_id == "vpc-0prod00000000").name, "prod")

    def test_azure_all_types(self):
        items = self.check_types(azure(), {"instance": 12, "disk": 13, "vpc": 1, "eip": 2})
        orphan = next(r for r in items if r.name == "orphan-data")
        self.assertEqual((orphan.status, orphan.size_gb), (ResourceStatus.AVAILABLE, 512.0))

    def test_get_instance_live(self):
        for adapter in (ali(3), aws(3), azure()):
            first = next(adapter.list_resources(("instance",)))
            live = adapter.get_instance(first.native_id)
            self.assertEqual((live.uid, live.vcpu), (first.uid, first.vcpu))

    def test_get_instance_missing(self):
        from evdc.errors import ResourceNotFound
        for adapter in (ali(3), aws(3)):
            with self.assertRaises(ResourceNotFound):
                adapter.get_instance("i-doesnotexist")

    def test_reboot_requires_running(self):
        from evdc.errors import IncorrectState
        a = aws(6)
        items = list(a.list_resources(("instance",)))
        running = next(r for r in items if r.status == ResourceStatus.RUNNING)
        stopped = next(r for r in items if r.status == ResourceStatus.STOPPED)
        self.assertEqual(a.reboot(running.native_id), ResourceStatus.RUNNING)
        with self.assertRaises(IncorrectState):
            a.reboot(stopped.native_id)


class Reliability(unittest.TestCase):
    def test_retries_throttling_then_succeeds(self):
        a = ali(5)
        a.client.faults.fail_next(aliyun_throttle, aliyun_throttle)
        self.assertEqual(len(list(a.list_resources(("instance",)))), 5)
        self.assertEqual(a.client.faults.calls, 3)

    def test_gives_up_after_max_attempts(self):
        a = aws(5, max_attempts=3)
        a.client.faults.fail_next(*[aws_throttle] * 3)
        with self.assertRaises(Exception) as ctx:
            list(a.list_resources(("instance",)))
        self.assertEqual(type(ctx.exception).__name__, "ThrottledError")

    def test_transient_error_on_second_page_is_retried(self):
        a = aws(10)
        list(a.list_resources(("instance",)))  # warm the type cache
        a.client.faults.calls = 0
        a.client.faults.fail_next(lambda: None, lambda: TransientError("502", "aws"))
        self.assertEqual(len(list(a.list_resources(("instance",)))), 10)

    def test_auth_error_is_not_retried(self):
        a = no_sleep(AliyunAdapter("ali", FakeAliyunEcs("bad", "bad"), fast_policy()))
        with self.assertRaises(AuthError):
            list(a.list_resources(("instance",)))
        self.assertEqual(a.client.faults.calls, 1)

    def test_slow_call_times_out_and_is_retried(self):
        a = ali(3, timeout=0.05, max_attempts=2)
        a.client.faults.latency = 0.2
        t0 = time.monotonic()
        with self.assertRaises(ApiTimeout):
            list(a.list_resources(("instance",)))
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_azure_reauths_once_on_expired_token(self):
        a = azure()
        list(a.list_resources(("instance",)))
        a.client.expire_all_tokens()
        self.assertEqual(len(list(a.list_resources(("instance",)))), 12)
        self.assertEqual(a.client.tokens_issued, 2)

    def test_azure_refreshes_token_before_expiry(self):
        a = azure(ttl=30)  # inside the 60s refresh margin -> refresh on every page
        list(a.list_resources(("instance",)))
        self.assertGreater(a.client.tokens_issued, 1)

    def test_azure_throttle_retried(self):
        a = azure()
        a.authenticate()
        a.client.faults.fail_next(azure_throttle)
        self.assertEqual(len(list(a.list_resources(("instance",)))), 12)


if __name__ == "__main__":
    unittest.main()
