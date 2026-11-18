"""In-memory fake cloud APIs.

They return data in the *real* response shapes of each provider, so the
adapters do the same mapping work they would against the real SDKs:

- Alibaba Cloud ECS  DescribeInstances      PageNumber / PageSize / TotalCount, Memory in MB,
                                             nested {"Instances": {"Instance": [...]}}
- AWS EC2            describe_instances      NextToken, Reservations -> Instances,
                                             memory only via describe_instance_types
- Azure Compute      virtualMachines list    "value" + "nextLink", OAuth bearer token that expires,
                                             power state hidden in instanceView.statuses

They can also inject faults (throttling, 5xx, slow calls, expired tokens) so
the retry / timeout / re-auth logic can be exercised without a real account.
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from typing import Callable

from ..errors import AuthError, ThrottledError, TransientError


class FaultInjector:
    """Queue of faults to raise on the next calls, plus optional latency."""

    def __init__(self):
        self._queue: deque[Callable[[], Exception] | None] = deque()
        self.latency: float = 0.0
        self.calls = 0
        self._lock = threading.Lock()

    def fail_next(self, *factories: Callable[[], Exception]) -> None:
        self._queue.extend(factories)

    def before_call(self) -> None:
        with self._lock:
            self.calls += 1
            factory = self._queue.popleft() if self._queue else None
        if self.latency:
            time.sleep(self.latency)
        exc = factory() if factory else None   # a factory returning None = "this call succeeds"
        if exc is not None:
            raise exc


def _rand_ip(rng: random.Random, prefix: str) -> str:
    return f"{prefix}.{rng.randint(0, 255)}.{rng.randint(2, 254)}"


# ---------------------------------------------------------------------------
# Alibaba Cloud
# ---------------------------------------------------------------------------
class FakeAliyunEcs:
    STATUSES = ["Running", "Stopped", "Starting", "Stopping", "Pending"]

    def __init__(self, access_key_id: str, access_key_secret: str, count: int = 23, seed: int = 1):
        self._valid = ("LTAI-demo", "secret-demo")
        self._creds = (access_key_id, access_key_secret)
        self.faults = FaultInjector()
        rng = random.Random(seed)
        self._instances = []
        for i in range(count):
            cpu = rng.choice([2, 4, 8])
            self._instances.append({
                "InstanceId": f"i-bp1{i:015d}",
                "InstanceName": f"ali-web-{i:02d}",
                "RegionId": "cn-hangzhou",
                "ZoneId": "cn-hangzhou-h",
                "Status": rng.choice(self.STATUSES[:2] * 4 + self.STATUSES),
                "Cpu": cpu,
                "Memory": cpu * 2 * 1024,          # MB
                "InstanceType": "ecs.g6." + {2: "large", 4: "xlarge", 8: "2xlarge"}[cpu],
                "VpcAttributes": {"PrivateIpAddress": {"IpAddress": [_rand_ip(rng, "172.16")]}},
                "PublicIpAddress": {"IpAddress": [_rand_ip(rng, "47.98")] if i % 3 == 0 else []},
                "EipAddress": {"IpAddress": ""},
                "Tags": {"Tag": [{"TagKey": "env", "TagValue": rng.choice(["prod", "test"])}]},
            })

    def describe_instances(self, RegionId: str, PageNumber: int = 1, PageSize: int = 10) -> dict:
        self.faults.before_call()
        if self._creds != self._valid:
            raise AuthError("Specified access key is not found.", "aliyun", "InvalidAccessKeyId.NotFound")
        if PageSize > 100:
            raise TransientError("PageSize must be <= 100", "aliyun", "InvalidParameter")
        items = [x for x in self._instances if x["RegionId"] == RegionId]
        start = (PageNumber - 1) * PageSize
        return {
            "RequestId": f"req-{PageNumber}",
            "TotalCount": len(items),
            "PageNumber": PageNumber,
            "PageSize": PageSize,
            "Instances": {"Instance": items[start:start + PageSize]},
        }

    # test helpers
    def remove(self, instance_id: str) -> None:
        self._instances = [x for x in self._instances if x["InstanceId"] != instance_id]


def aliyun_throttle() -> Exception:
    return ThrottledError("Request was denied due to user flow control.", "aliyun", "Throttling.User")


# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------
class FakeEc2:
    TYPES = {
        "t3.medium": {"VCpuInfo": {"DefaultVCpus": 2}, "MemoryInfo": {"SizeInMiB": 4096}},
        "m5.large": {"VCpuInfo": {"DefaultVCpus": 2}, "MemoryInfo": {"SizeInMiB": 8192}},
        "c5.xlarge": {"VCpuInfo": {"DefaultVCpus": 4}, "MemoryInfo": {"SizeInMiB": 8192}},
    }

    def __init__(self, access_key: str, secret_key: str, region: str = "ap-southeast-2",
                 count: int = 17, seed: int = 2):
        self._ok = (access_key, secret_key) == ("AKIA-demo", "secret-demo")
        self.region = region
        self.faults = FaultInjector()
        self.type_calls = 0
        rng = random.Random(seed)
        self._instances = []
        for i in range(count):
            itype = rng.choice(list(self.TYPES))
            inst = {
                "InstanceId": f"i-0{i:016x}",
                "InstanceType": itype,
                "State": {"Code": 16, "Name": rng.choice(["running", "running", "stopped", "pending", "stopping"])},
                "PrivateIpAddress": _rand_ip(rng, "10.0"),
                "Placement": {"AvailabilityZone": f"{region}a"},
                "Tags": [{"Key": "Name", "Value": f"aws-app-{i:02d}"}, {"Key": "team", "Value": "platform"}],
            }
            if i % 2 == 0:
                inst["PublicIpAddress"] = _rand_ip(rng, "54.79")
            self._instances.append(inst)

    def describe_instances(self, MaxResults: int = 5, NextToken: str | None = None) -> dict:
        self.faults.before_call()
        if not self._ok:
            raise AuthError("AWS was not able to validate the provided access credentials", "aws", "AuthFailure")
        start = int(NextToken) if NextToken else 0
        page = self._instances[start:start + MaxResults]
        # AWS groups instances into reservations; make it 2 per reservation
        reservations = [{"ReservationId": f"r-{start + j}", "Instances": page[j:j + 2]}
                        for j in range(0, len(page), 2)]
        resp = {"Reservations": reservations}
        if start + MaxResults < len(self._instances):
            resp["NextToken"] = str(start + MaxResults)
        return resp

    def describe_instance_types(self, InstanceTypes: list[str]) -> dict:
        self.faults.before_call()
        self.type_calls += 1
        return {"InstanceTypes": [{"InstanceType": t, **self.TYPES[t]} for t in InstanceTypes if t in self.TYPES]}


def aws_throttle() -> Exception:
    return ThrottledError("Request limit exceeded.", "aws", "RequestLimitExceeded")


# ---------------------------------------------------------------------------
# Azure
# ---------------------------------------------------------------------------
class FakeAzure:
    SIZES = {"Standard_B2s": (2, 4096), "Standard_D2s_v3": (2, 8192), "Standard_D4s_v3": (4, 16384)}

    def __init__(self, tenant_id: str, client_id: str, client_secret: str,
                 subscription_id: str = "sub-demo", count: int = 12, seed: int = 3, token_ttl: float = 3600):
        self._ok = (tenant_id, client_id, client_secret) == ("tenant-demo", "app-demo", "secret-demo")
        self.subscription_id = subscription_id
        self.token_ttl = token_ttl
        self.faults = FaultInjector()
        self.tokens_issued = 0
        self._tokens: dict[str, float] = {}
        rng = random.Random(seed)
        self._vms = []
        for i in range(count):
            size = rng.choice(list(self.SIZES))
            power = rng.choice(["running", "running", "deallocated", "stopped", "starting"])
            self._vms.append({
                "id": f"/subscriptions/{subscription_id}/resourceGroups/rg-demo/providers/"
                      f"Microsoft.Compute/virtualMachines/az-vm-{i:02d}",
                "name": f"az-vm-{i:02d}",
                "location": "australiaeast",
                "tags": {"owner": "zoey"} if i % 2 else {},
                "properties": {
                    "hardwareProfile": {"vmSize": size},
                    "instanceView": {"statuses": [
                        {"code": "ProvisioningState/succeeded"},
                        {"code": f"PowerState/{power}"},
                    ]},
                    "networkProfile": {"networkInterfaces": [{"properties": {"ipConfigurations": [
                        {"properties": {"privateIPAddress": _rand_ip(rng, "10.1")}}]}}]},
                },
            })

    # OAuth2 client-credentials flow
    def get_token(self) -> dict:
        self.faults.before_call()
        if not self._ok:
            raise AuthError("AADSTS7000215: Invalid client secret provided.", "azure", "invalid_client")
        self.tokens_issued += 1
        token = f"tok-{self.tokens_issued}"
        self._tokens[token] = time.time() + self.token_ttl
        return {"access_token": token, "expires_in": self.token_ttl, "token_type": "Bearer"}

    def expire_all_tokens(self) -> None:
        for t in self._tokens:
            self._tokens[t] = 0

    def list_vms(self, token: str, skip_token: int | None = None, top: int = 5) -> dict:
        self.faults.before_call()
        if self._tokens.get(token, 0) < time.time():
            raise AuthError("The access token expiry UTC time is earlier than current UTC time.",
                            "azure", "ExpiredAuthenticationToken")
        start = skip_token or 0
        resp = {"value": self._vms[start:start + top]}
        if start + top < len(self._vms):
            resp["nextLink"] = (f"https://management.azure.com/subscriptions/{self.subscription_id}/providers/"
                                f"Microsoft.Compute/virtualMachines?api-version=2023-09-01&$skiptoken={start + top}")
        return resp

    def list_sizes(self, token: str, location: str) -> dict:
        self.faults.before_call()
        return {"value": [{"name": n, "numberOfCores": c, "memoryInMB": m} for n, (c, m) in self.SIZES.items()]}


def azure_throttle() -> Exception:
    return ThrottledError("Too many requests (429). Retry-After: 2", "azure", "429")
