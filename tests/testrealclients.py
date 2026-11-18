"""Tests for the real Alibaba Cloud and Azure wrappers, without the SDKs or an account.

Small stand-ins mimic the SDK surface the wrappers rely on (request models and
response.body.to_map() for Alibaba Cloud; pagers with by_page / continuation_token,
model.serialize() and begin_* operations for Azure) and delegate to the fake clouds.
"""
import re
import unittest
from types import SimpleNamespace

from evdc.adapters.aliyun import AliyunAdapter
from evdc.adapters.azure import AzureAdapter
from evdc.clouds import aliyunreal, azurereal
from evdc.clouds.aliyunreal import AliyunClient
from evdc.clouds.azurereal import AzureClient, split_id
from evdc.clouds.fake import FakeAliyunEcs, FakeAzure
from evdc.errors import AuthError, CloudApiError, IncorrectState, ResourceNotFound, ThrottledError, TransientError
from evdc.models import ResourceStatus
from tests.helpers import fast_policy, no_sleep


# ---------------------------------------------------------------- Alibaba Cloud stand-ins
class TeaException(Exception):
    """Same attributes as Tea.exceptions.TeaException."""

    def __init__(self, code, message="boom", status=400):
        super().__init__(f"Error: {code} message: {message}")
        self.code, self.message, self.data = code, message, {"statusCode": status}


class Models:
    """Stand-in for alibabacloud_ecs20140526.models: any *Request class, storing snake_case kwargs."""

    def __getattr__(self, name):
        return lambda **kw: SimpleNamespace(_name=name, **kw)


def pascal(name: str) -> str:
    return "".join(p.capitalize() if p != "ids" else "Ids" for p in name.split("_"))


class SdkClient:
    """Stand-in for the ECS / VPC SDK client: method(request) -> response.body.to_map()."""

    def __init__(self, fake, errors=()):
        self.fake, self.errors, self.calls = fake, list(errors), []

    def __getattr__(self, method):
        def call(request):
            self.calls.append(method)
            if self.errors:
                raise self.errors.pop(0)
            kwargs = {pascal(k): v for k, v in vars(request).items() if k != "_name"}
            data = getattr(self.fake, method)(**kwargs)
            return SimpleNamespace(body=SimpleNamespace(to_map=lambda: data))
        return call


def aliyun_client(errors=()):
    fake = FakeAliyunEcs("LTAI-demo", "secret-demo", count=7)
    ecs = SdkClient(fake, errors)
    return AliyunClient(ecs=ecs, vpc=SdkClient(fake), ecs_models=Models(), vpc_models=Models()), ecs


class AliyunRealTests(unittest.TestCase):
    def adapter(self, errors=()):
        client, ecs = aliyun_client(errors)
        return no_sleep(AliyunAdapter("real", client, fast_policy(), page_size=3)), ecs

    def test_snake_case_request_models(self):
        self.assertEqual(aliyunreal.snake("RegionId"), "region_id")
        self.assertEqual(aliyunreal.snake("InstanceIds"), "instance_ids")

    def test_full_listing_through_wrapper(self):
        a, _ = self.adapter()
        kinds = {}
        for r in a.list_resources():
            kinds[r.resource_type] = kinds.get(r.resource_type, 0) + 1
        self.assertEqual(kinds, {"instance": 7, "disk": 10, "vpc": 2, "eip": 4})

    def test_live_query_and_actions(self):
        a, _ = self.adapter()
        inst = next(r for r in a.list_resources(("instance",)) if r.status == ResourceStatus.STOPPED)
        self.assertEqual(a.get_instance(inst.native_id).uid, inst.uid)
        self.assertEqual(a.start(inst.native_id), ResourceStatus.PENDING)

    def test_throttling_retried_auth_not(self):
        a, ecs = self.adapter([TeaException("Throttling.User", status=400)])
        self.assertEqual(len(list(a.list_resources(("instance",)))), 7)
        self.assertEqual(ecs.calls[:2], ["describe_instances", "describe_instances"])
        a, ecs = self.adapter([TeaException("InvalidAccessKeyId.NotFound", status=404)])
        with self.assertRaises(AuthError):
            list(a.list_resources(("instance",)))
        self.assertEqual(len(ecs.calls), 1)

    def test_translate(self):
        cases = [("Throttling", 400, ThrottledError), ("Throttling.Api", 400, ThrottledError),
                 ("SignatureDoesNotMatch", 400, AuthError), ("Forbidden.RAM", 403, AuthError),
                 ("InvalidInstanceId.NotFound", 404, ResourceNotFound),
                 ("IncorrectInstanceStatus", 403, IncorrectState),
                 ("ServiceUnavailable", 503, TransientError), ("Whatever", 502, TransientError),
                 ("InvalidParameter", 400, CloudApiError)]
        for code, status, expected in cases:
            with self.subTest(code=code):
                self.assertIs(type(aliyunreal.translate(TeaException(code, status=status))), expected)

    def test_unknown_operation_refused(self):
        client, _ = aliyun_client()
        with self.assertRaises(AttributeError):
            client.delete_instance(InstanceId="i-1")


# ---------------------------------------------------------------- Azure stand-ins
class Model:
    def __init__(self, data):
        self.data = data

    def serialize(self, keep_readonly=False):
        return self.data


class Pager:
    """Stand-in for azure.core.paging.ItemPaged with by_page(continuation_token)."""

    def __init__(self, items, size=4):
        self.items, self.size = items, size

    def by_page(self, continuation_token=None):
        start = int(continuation_token or 0)
        pager = self

        class Pages:
            continuation_token = str(start + pager.size) if start + pager.size < len(pager.items) else None

            def __next__(self):
                return [Model(x) for x in pager.items[start:start + pager.size]]
        return Pages()


class HttpResponseError(Exception):
    def __init__(self, status, code="Err"):
        super().__init__(f"({code}) http {status}")
        self.status_code, self.error, self.message = status, SimpleNamespace(code=code), f"({code})"


class ClientAuthenticationError(Exception):
    pass


def azure_client(fake: FakeAzure, fail_list=()):
    token = fake.get_token()["access_token"]
    fails = list(fail_list)

    def list_all(status_only=None):
        if fails:
            raise fails.pop(0)
        return Pager(fake._vms)

    def get(rg, name, expand=None):
        vm = next((v for v in fake._vms if v["name"] == name), None)
        if vm is None:
            raise HttpResponseError(404, "ResourceNotFound")
        return Model(vm)

    def begin(op):
        def run(rg, name):
            vm_id = next(v["id"] for v in fake._vms if v["name"] == name)
            getattr(fake, op)(token, vm_id)
            return SimpleNamespace(done=lambda: False)
        return run

    compute = SimpleNamespace(
        virtual_machines=SimpleNamespace(list_all=list_all, get=get, begin_start=begin("start"),
                                         begin_deallocate=begin("deallocate"), begin_restart=begin("restart")),
        disks=SimpleNamespace(list=lambda: Pager(fake._disks)),
        virtual_machine_sizes=SimpleNamespace(list=lambda location: [
            Model({"name": n, "numberOfCores": c, "memoryInMB": m}) for n, (c, m) in fake.SIZES.items()]))
    network = SimpleNamespace(virtual_networks=SimpleNamespace(list_all=lambda: Pager(fake._vnets)),
                              public_ip_addresses=SimpleNamespace(list_all=lambda: Pager(fake._pips)))
    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="real-token", expires_on=10**10))
    return AzureClient(credential=credential, compute=compute, network=network)


class AzureRealTests(unittest.TestCase):
    def adapter(self, fails=()):
        fake = FakeAzure("tenant-demo", "app-demo", "secret-demo", count=10)
        return no_sleep(AzureAdapter("real", azure_client(fake, fails), fast_policy(), page_size=4)), fake

    def test_full_listing_with_opaque_next_link(self):
        a, _ = self.adapter()
        kinds = {}
        for r in a.list_resources():
            kinds[r.resource_type] = kinds.get(r.resource_type, 0) + 1
        self.assertEqual(kinds, {"instance": 10, "disk": 11, "vpc": 1, "eip": 2})
        self.assertTrue(all(r.vcpu for r in a.list_resources(("instance",))))

    def test_token_from_credential(self):
        a, _ = self.adapter()
        a.authenticate()
        self.assertEqual(a._token, "real-token")

    def test_live_query_and_actions(self):
        a, _ = self.adapter()
        running = next(r for r in a.list_resources(("instance",)) if r.status == ResourceStatus.RUNNING)
        self.assertEqual(a.get_instance(running.native_id).status, ResourceStatus.RUNNING)
        self.assertEqual(a.reboot(running.native_id), ResourceStatus.PENDING)
        self.assertEqual(a.stop(running.native_id), ResourceStatus.STOPPING)

    def test_throttling_retried(self):
        a, _ = self.adapter([HttpResponseError(429, "TooManyRequests")])
        self.assertEqual(len(list(a.list_resources(("instance",)))), 10)

    def test_translate(self):
        cases = [(HttpResponseError(429), ThrottledError), (HttpResponseError(401), AuthError),
                 (HttpResponseError(403, "AuthorizationFailed"), AuthError),
                 (HttpResponseError(404, "ResourceNotFound"), ResourceNotFound),
                 (HttpResponseError(409, "OperationNotAllowed"), IncorrectState),
                 (HttpResponseError(503), TransientError), (HttpResponseError(400, "BadRequest"), CloudApiError),
                 (ClientAuthenticationError("bad secret"), AuthError)]
        for exc, expected in cases:
            with self.subTest(exc=str(exc)):
                self.assertIs(type(azurereal.translate(exc)), expected)

    def test_split_id(self):
        vm_id = "/subscriptions/s/resourceGroups/rg-demo/providers/Microsoft.Compute/virtualMachines/vm-1"
        self.assertEqual(split_id(vm_id), ("rg-demo", "vm-1"))
        with self.assertRaises(ResourceNotFound):
            split_id("not-an-id")


class FactoryTests(unittest.TestCase):
    def test_missing_sdks_give_install_hint(self):
        from evdc.app import App
        app = App()
        self.addCleanup(app.close)
        for provider, creds, hint in (
                ("aliyun", {"access_key_id": "a", "access_key_secret": "b"}, "aliyun"),
                ("azure", {"tenant_id": "t", "client_id": "c", "client_secret": "s", "subscription_id": "x"},
                 "azure")):
            try:
                __import__({"aliyun": "alibabacloud_ecs20140526", "azure": "azure.mgmt.compute"}[provider])
                continue  # SDK present: would build a real client
            except ImportError:
                pass
            with self.subTest(provider=provider), self.assertRaises(RuntimeError) as ctx:
                app.load_accounts([{"provider": provider, "account": "r", "fake": False, "credentials": creds}])
            self.assertTrue(re.search(rf"\.\[{hint}\]", str(ctx.exception)))


if __name__ == "__main__":
    unittest.main()
