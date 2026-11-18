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

import json
import random
import threading
import time
from collections import deque
from typing import Callable

from ..errors import AuthError, IncorrectState, ResourceNotFound, ThrottledError, TransientError


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
        # one system disk per instance + a few unattached data disks
        self._disks = [{"DiskId": f"d-bp1{i:015d}", "DiskName": f"{x['InstanceName']}-sys", "Size": 40,
                        "Category": "cloud_essd", "Type": "system", "Status": "In_use",
                        "InstanceId": x["InstanceId"], "RegionId": "cn-hangzhou"}
                       for i, x in enumerate(self._instances)]
        self._disks += [{"DiskId": f"d-bp9{i:015d}", "DiskName": f"data-{i}", "Size": rng.choice([100, 500]),
                         "Category": "cloud_efficiency", "Type": "data", "Status": "Available",
                         "InstanceId": "", "RegionId": "cn-hangzhou"} for i in range(3)]
        self._vpcs = [{"VpcId": f"vpc-bp1{i:012d}", "VpcName": name, "CidrBlock": cidr, "Status": "Available",
                       "RegionId": "cn-hangzhou", "IsDefault": i == 0}
                      for i, (name, cidr) in enumerate([("default", "172.16.0.0/12"), ("prod", "10.10.0.0/16")])]
        self._eips = [{"AllocationId": f"eip-bp1{i:012d}", "IpAddress": _rand_ip(rng, "47.110"),
                       "Bandwidth": "5", "RegionId": "cn-hangzhou",
                       "Status": "InUse" if i < min(2, count) else "Available",
                       "InstanceId": self._instances[i]["InstanceId"] if i < min(2, count) else ""}
                      for i in range(4)]

    def _check(self) -> None:
        self.faults.before_call()
        if self._creds != self._valid:
            raise AuthError("Specified access key is not found.", "aliyun", "InvalidAccessKeyId.NotFound")

    @staticmethod
    def _page(items: list, page: int, size: int, outer: str, inner: str) -> dict:
        if size > 100:
            raise TransientError("PageSize must be <= 100", "aliyun", "InvalidParameter")
        start = (page - 1) * size
        return {"RequestId": f"req-{page}", "TotalCount": len(items), "PageNumber": page, "PageSize": size,
                outer: {inner: items[start:start + size]}}

    def describe_instances(self, RegionId: str, PageNumber: int = 1, PageSize: int = 10,
                           InstanceIds: str | None = None) -> dict:
        """InstanceIds is a JSON array string, exactly like the real API."""
        self._check()
        items = [x for x in self._instances if x["RegionId"] == RegionId]
        if InstanceIds:
            wanted = set(json.loads(InstanceIds))
            items = [x for x in items if x["InstanceId"] in wanted]
        return self._page(items, PageNumber, PageSize, "Instances", "Instance")

    def describe_disks(self, RegionId: str, PageNumber: int = 1, PageSize: int = 10) -> dict:
        self._check()
        return self._page([d for d in self._disks if d["RegionId"] == RegionId], PageNumber, PageSize,
                          "Disks", "Disk")

    def describe_vpcs(self, RegionId: str, PageNumber: int = 1, PageSize: int = 10) -> dict:
        self._check()
        return self._page([v for v in self._vpcs if v["RegionId"] == RegionId], PageNumber, PageSize,
                          "Vpcs", "Vpc")

    def describe_eip_addresses(self, RegionId: str, PageNumber: int = 1, PageSize: int = 10) -> dict:
        self._check()
        return self._page([e for e in self._eips if e["RegionId"] == RegionId], PageNumber, PageSize,
                          "EipAddresses", "EipAddress")

    # --- lifecycle (real API: StartInstance / StopInstance, asynchronous) ---
    def _find(self, instance_id: str) -> dict:
        for inst in self._instances:
            if inst["InstanceId"] == instance_id:
                return inst
        raise ResourceNotFound(f"The specified InstanceId {instance_id} does not exist.",
                               "aliyun", "InvalidInstanceId.NotFound")

    def _auth(self) -> None:
        self.faults.before_call()
        if self._creds != self._valid:
            raise AuthError("Specified access key is not found.", "aliyun", "InvalidAccessKeyId.NotFound")

    def start_instance(self, InstanceId: str) -> dict:
        self._auth()
        inst = self._find(InstanceId)
        if inst["Status"] != "Stopped":
            raise IncorrectState(f"The current status of the resource does not support this operation: "
                                 f"{inst['Status']}", "aliyun", "IncorrectInstanceStatus")
        inst["Status"] = "Starting"
        return {"RequestId": "req-start"}

    def stop_instance(self, InstanceId: str) -> dict:
        self._auth()
        inst = self._find(InstanceId)
        if inst["Status"] != "Running":
            raise IncorrectState(f"The current status of the resource does not support this operation: "
                                 f"{inst['Status']}", "aliyun", "IncorrectInstanceStatus")
        inst["Status"] = "Stopping"
        return {"RequestId": "req-stop"}

    def reboot_instance(self, InstanceId: str) -> dict:
        self._auth()
        inst = self._find(InstanceId)
        if inst["Status"] != "Running":
            raise IncorrectState(f"The current status of the resource does not support this operation: "
                                 f"{inst['Status']}", "aliyun", "IncorrectInstanceStatus")
        inst["Status"] = "Starting"
        return {"RequestId": "req-reboot"}

    def settle(self) -> None:
        """Finish in-flight transitions (what the real cloud does after a few seconds)."""
        for inst in self._instances:
            inst["Status"] = {"Starting": "Running", "Stopping": "Stopped"}.get(inst["Status"], inst["Status"])

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
        self._volumes = [{"VolumeId": f"vol-0{i:016x}", "Size": 30, "VolumeType": "gp3", "State": "in-use",
                          "AvailabilityZone": f"{region}a",
                          "Attachments": [{"InstanceId": x["InstanceId"], "Device": "/dev/xvda", "State": "attached"}],
                          "Tags": [{"Key": "Name", "Value": f"{x['Tags'][0]['Value']}-root"}]}
                         for i, x in enumerate(self._instances)]
        self._volumes += [{"VolumeId": f"vol-9{i:016x}", "Size": 200, "VolumeType": "st1", "State": "available",
                           "AvailabilityZone": f"{region}a", "Attachments": [], "Tags": []} for i in range(2)]
        self._vpcs = [{"VpcId": "vpc-0default000000", "CidrBlock": "172.31.0.0/16", "State": "available",
                       "IsDefault": True, "Tags": []},
                      {"VpcId": "vpc-0prod00000000", "CidrBlock": "10.0.0.0/16", "State": "available",
                       "IsDefault": False, "Tags": [{"Key": "Name", "Value": "prod"}]}]
        self._addresses = [{"AllocationId": f"eipalloc-0{i:08x}", "PublicIp": _rand_ip(rng, "13.54"),
                            "Domain": "vpc", **({"InstanceId": self._instances[i]["InstanceId"],
                                                 "AssociationId": f"eipassoc-{i}"} if i == 0 and count else {})}
                           for i in range(3)]

    def _check(self) -> None:
        self.faults.before_call()
        if not self._ok:
            raise AuthError("AWS was not able to validate the provided access credentials", "aws", "AuthFailure")

    def describe_instances(self, MaxResults: int = 5, NextToken: str | None = None,
                           InstanceIds: list[str] | None = None) -> dict:
        self._check()
        if InstanceIds:
            found = [i for i in self._instances if i["InstanceId"] in InstanceIds]
            if not found:
                raise ResourceNotFound(f"The instance ID '{InstanceIds[0]}' does not exist", "aws",
                                       "InvalidInstanceID.NotFound")
            return {"Reservations": [{"ReservationId": "r-x", "Instances": found}]}
        start = int(NextToken) if NextToken else 0
        page = self._instances[start:start + MaxResults]
        # AWS groups instances into reservations; make it 2 per reservation
        reservations = [{"ReservationId": f"r-{start + j}", "Instances": page[j:j + 2]}
                        for j in range(0, len(page), 2)]
        resp = {"Reservations": reservations}
        if start + MaxResults < len(self._instances):
            resp["NextToken"] = str(start + MaxResults)
        return resp

    def describe_volumes(self, MaxResults: int = 5, NextToken: str | None = None) -> dict:
        self._check()
        start = int(NextToken) if NextToken else 0
        resp = {"Volumes": self._volumes[start:start + MaxResults]}
        if start + MaxResults < len(self._volumes):
            resp["NextToken"] = str(start + MaxResults)
        return resp

    def describe_vpcs(self) -> dict:
        self._check()
        return {"Vpcs": list(self._vpcs)}

    def describe_addresses(self) -> dict:
        """Real API: not paginated."""
        self._check()
        return {"Addresses": list(self._addresses)}

    def describe_instance_types(self, InstanceTypes: list[str]) -> dict:
        self.faults.before_call()
        self.type_calls += 1
        return {"InstanceTypes": [{"InstanceType": t, **self.TYPES[t]} for t in InstanceTypes if t in self.TYPES]}

    # --- lifecycle (real API: start_instances / stop_instances, batch + async) ---
    def _change(self, ids: list[str], allowed_from: str, to: str, key: str) -> dict:
        self.faults.before_call()
        if not self._ok:
            raise AuthError("AWS was not able to validate the provided access credentials", "aws", "AuthFailure")
        by_id = {i["InstanceId"]: i for i in self._instances}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise ResourceNotFound(f"The instance ID '{missing[0]}' does not exist", "aws",
                                   "InvalidInstanceID.NotFound")
        out = []
        for iid in ids:
            inst = by_id[iid]
            prev = inst["State"]["Name"]
            if prev not in (allowed_from, to):
                raise IncorrectState(f"The instance '{iid}' is not in a state from which it can be changed.",
                                     "aws", "IncorrectInstanceState")
            if prev == allowed_from:
                inst["State"] = {"Code": 0, "Name": to}
            out.append({"InstanceId": iid, "PreviousState": {"Name": prev},
                        "CurrentState": dict(inst["State"])})
        return {key: out}

    def start_instances(self, InstanceIds: list[str]) -> dict:
        return self._change(InstanceIds, "stopped", "pending", "StartingInstances")

    def stop_instances(self, InstanceIds: list[str]) -> dict:
        return self._change(InstanceIds, "running", "stopping", "StoppingInstances")

    def reboot_instances(self, InstanceIds: list[str]) -> dict:
        """Real API returns an empty body; the instance stays 'running'."""
        self._check()
        for iid in InstanceIds:
            inst = next((i for i in self._instances if i["InstanceId"] == iid), None)
            if inst is None:
                raise ResourceNotFound(f"The instance ID '{iid}' does not exist", "aws", "InvalidInstanceID.NotFound")
            if inst["State"]["Name"] != "running":
                raise IncorrectState(f"The instance '{iid}' is not in a state from which it can be rebooted.",
                                     "aws", "IncorrectInstanceState")
        return {}

    def settle(self) -> None:
        for inst in self._instances:
            name = {"pending": "running", "stopping": "stopped"}.get(inst["State"]["Name"])
            if name:
                inst["State"] = {"Code": 16 if name == "running" else 80, "Name": name}


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
        rg = f"/subscriptions/{subscription_id}/resourceGroups/rg-demo/providers"
        self._disks = [{"id": f"{rg}/Microsoft.Compute/disks/{v['name']}_OsDisk", "name": f"{v['name']}_OsDisk",
                        "location": "australiaeast", "managedBy": v["id"], "tags": {},
                        "sku": {"name": "Premium_LRS"},
                        "properties": {"diskSizeGB": 128, "diskState": "Attached"}} for v in self._vms]
        self._disks.append({"id": f"{rg}/Microsoft.Compute/disks/orphan-data", "name": "orphan-data",
                            "location": "australiaeast", "tags": {"note": "left over"},
                            "sku": {"name": "Standard_LRS"},
                            "properties": {"diskSizeGB": 512, "diskState": "Unattached"}})
        self._vnets = [{"id": f"{rg}/Microsoft.Network/virtualNetworks/vnet-prod", "name": "vnet-prod",
                        "location": "australiaeast", "tags": {},
                        "properties": {"addressSpace": {"addressPrefixes": ["10.1.0.0/16"]},
                                       "provisioningState": "Succeeded"}}]
        self._pips = [{"id": f"{rg}/Microsoft.Network/publicIPAddresses/pip-{i}", "name": f"pip-{i}",
                       "location": "australiaeast", "tags": {},
                       "properties": {"ipAddress": _rand_ip(rng, "20.70"), "publicIPAllocationMethod": "Static",
                                      **({"ipConfiguration": {"id": self._vms[i]["id"] + "/nic"}} if i == 0 and count else {})}}
                      for i in range(2)]

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

    def _list(self, token: str, items: list, kind: str, skip_token: int | None, top: int) -> dict:
        self._token_ok(token)
        start = skip_token or 0
        resp = {"value": items[start:start + top]}
        if start + top < len(items):
            resp["nextLink"] = (f"https://management.azure.com/subscriptions/{self.subscription_id}/providers/"
                                f"{kind}?api-version=2023-09-01&$skiptoken={start + top}")
        return resp

    def _token_ok(self, token: str) -> None:
        self.faults.before_call()
        if self._tokens.get(token, 0) < time.time():
            raise AuthError("The access token expiry UTC time is earlier than current UTC time.",
                            "azure", "ExpiredAuthenticationToken")

    def list_disks(self, token: str, skip_token: int | None = None, top: int = 5) -> dict:
        return self._list(token, self._disks, "Microsoft.Compute/disks", skip_token, top)

    def list_vnets(self, token: str, skip_token: int | None = None, top: int = 5) -> dict:
        return self._list(token, self._vnets, "Microsoft.Network/virtualNetworks", skip_token, top)

    def list_public_ips(self, token: str, skip_token: int | None = None, top: int = 5) -> dict:
        return self._list(token, self._pips, "Microsoft.Network/publicIPAddresses", skip_token, top)

    def get_vm(self, token: str, vm_id: str) -> dict:
        """GET .../virtualMachines/{name}?$expand=instanceView"""
        self._token_ok(token)
        vm = next((v for v in self._vms if v["id"] == vm_id), None)
        if vm is None:
            raise ResourceNotFound(f"The Resource '{vm_id}' was not found.", "azure", "ResourceNotFound")
        return vm

    def list_sizes(self, token: str, location: str) -> dict:
        self.faults.before_call()
        return {"value": [{"name": n, "numberOfCores": c, "memoryInMB": m} for n, (c, m) in self.SIZES.items()]}

    # --- lifecycle (real API: POST .../start and .../deallocate, return 202 Accepted) ---
    def _power_action(self, token: str, vm_id: str, new_power: str) -> dict:
        self.faults.before_call()
        if self._tokens.get(token, 0) < time.time():
            raise AuthError("The access token expiry UTC time is earlier than current UTC time.",
                            "azure", "ExpiredAuthenticationToken")
        vm = next((v for v in self._vms if v["id"] == vm_id), None)
        if vm is None:
            raise ResourceNotFound(f"The Resource '{vm_id}' was not found.", "azure", "ResourceNotFound")
        statuses = vm["properties"]["instanceView"]["statuses"]
        statuses[:] = [s for s in statuses if not s["code"].startswith("PowerState/")]
        statuses.append({"code": f"PowerState/{new_power}"})
        return {"status": 202}

    def start(self, token: str, vm_id: str) -> dict:
        return self._power_action(token, vm_id, "starting")

    def deallocate(self, token: str, vm_id: str) -> dict:
        return self._power_action(token, vm_id, "deallocating")

    def restart(self, token: str, vm_id: str) -> dict:
        vm = self.get_vm(token, vm_id)
        power = next(s["code"] for s in vm["properties"]["instanceView"]["statuses"] if s["code"].startswith("PowerState/"))
        if power != "PowerState/running":
            raise IncorrectState("Operation 'restart' is not allowed since the VM is not running.",
                                 "azure", "OperationNotAllowed")
        return {"status": 202}

    def settle(self) -> None:
        for vm in self._vms:
            for s in vm["properties"]["instanceView"]["statuses"]:
                s["code"] = {"PowerState/starting": "PowerState/running",
                             "PowerState/deallocating": "PowerState/deallocated"}.get(s["code"], s["code"])


def azure_throttle() -> Exception:
    return ThrottledError("Too many requests (429). Retry-After: 2", "azure", "429")
