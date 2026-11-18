"""AWS EC2 adapter.

Real SDK equivalent: boto3.client("ec2"):
    describe_instances / describe_volumes   NextToken pagination
    describe_vpcs / describe_addresses       (addresses are not paginated)
    describe_instance_types                  CPU / memory lookup, cached
    start_instances / stop_instances / reboot_instances
"""
from __future__ import annotations

from typing import Iterator

from ..errors import ResourceNotFound
from ..models import CloudResource, ResourceStatus
from .base import ProviderAdapter, register

STATUS = {
    "running": ResourceStatus.RUNNING,
    "stopped": ResourceStatus.STOPPED,
    "pending": ResourceStatus.PENDING,
    "stopping": ResourceStatus.STOPPING,
    "shutting-down": ResourceStatus.STOPPING,
    "terminated": ResourceStatus.DELETED,
}
VOLUME_STATUS = {"in-use": ResourceStatus.IN_USE, "available": ResourceStatus.AVAILABLE,
                 "creating": ResourceStatus.PENDING, "deleting": ResourceStatus.STOPPING}


def _region(az: str) -> str:
    return az[:-1] if az and az[-1].isalpha() else az


def _tags(raw: dict) -> dict:
    return {t["Key"]: t["Value"] for t in raw.get("Tags", [])}


@register("aws")
class AwsAdapter(ProviderAdapter):
    resource_types = ("instance", "disk", "vpc", "eip")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._type_cache: dict[str, dict] = {}

    @property
    def region(self) -> str:
        return getattr(self.client, "region", "")

    # -- listing ----------------------------------------------------------
    def _paged(self, op: str, method: str, key: str) -> Iterator[dict]:
        token = None
        while True:
            kwargs = {"MaxResults": self.page_size}
            if token:
                kwargs["NextToken"] = token
            resp = self.call(op, lambda kw=kwargs: getattr(self.client, method)(**kw))
            yield resp
            token = resp.get("NextToken")
            if not token:
                return

    def iter_raw(self, kind: str) -> Iterator[dict]:
        if kind == "instance":
            for resp in self._paged("DescribeInstances", "describe_instances", "Reservations"):
                page = [inst for res in resp.get("Reservations", []) for inst in res.get("Instances", [])]
                self._load_types({i["InstanceType"] for i in page})
                yield from page
        elif kind == "disk":
            for resp in self._paged("DescribeVolumes", "describe_volumes", "Volumes"):
                yield from resp["Volumes"]
        elif kind == "vpc":
            yield from self.call("DescribeVpcs", self.client.describe_vpcs)["Vpcs"]
        elif kind == "eip":
            yield from self.call("DescribeAddresses", self.client.describe_addresses)["Addresses"]

    def _load_types(self, types: set[str]) -> None:
        missing = sorted(t for t in types if t not in self._type_cache)
        if not missing:
            return
        resp = self.call("DescribeInstanceTypes",
                         lambda: self.client.describe_instance_types(InstanceTypes=missing))
        for t in resp["InstanceTypes"]:
            self._type_cache[t["InstanceType"]] = t

    # -- mapping ------------------------------------------------------------
    def normalize(self, kind: str, raw: dict) -> CloudResource:
        return getattr(self, f"_normalize_{kind}")(raw)

    def _normalize_instance(self, raw: dict) -> CloudResource:
        tags = _tags(raw)
        name = tags.pop("Name", raw["InstanceId"])
        spec = self._type_cache.get(raw["InstanceType"], {})
        az = raw.get("Placement", {}).get("AvailabilityZone", "")
        return self.resource(
            "instance", raw["InstanceId"], name, STATUS.get(raw["State"]["Name"], ResourceStatus.UNKNOWN),
            _region(az), vcpu=spec.get("VCpuInfo", {}).get("DefaultVCpus"),
            memory_gb=round(spec["MemoryInfo"]["SizeInMiB"] / 1024, 2) if spec else None,
            private_ip=raw.get("PrivateIpAddress"), public_ip=raw.get("PublicIpAddress"), tags=tags,
            attributes={"instance_type": raw["InstanceType"], "zone": az})

    def _normalize_disk(self, raw: dict) -> CloudResource:
        tags = _tags(raw)
        attached = [a["InstanceId"] for a in raw.get("Attachments", [])]
        return self.resource(
            "disk", raw["VolumeId"], tags.pop("Name", raw["VolumeId"]),
            VOLUME_STATUS.get(raw["State"], ResourceStatus.UNKNOWN), _region(raw.get("AvailabilityZone", "")),
            size_gb=float(raw["Size"]), tags=tags,
            attributes={"category": raw.get("VolumeType"), "attached_to": attached[0] if attached else None})

    def _normalize_vpc(self, raw: dict) -> CloudResource:
        tags = _tags(raw)
        return self.resource(
            "vpc", raw["VpcId"], tags.pop("Name", raw["VpcId"]),
            ResourceStatus.AVAILABLE if raw["State"] == "available" else ResourceStatus.PENDING,
            self.region, tags=tags, attributes={"cidr": raw["CidrBlock"], "is_default": raw.get("IsDefault", False)})

    def _normalize_eip(self, raw: dict) -> CloudResource:
        attached = raw.get("InstanceId")
        return self.resource(
            "eip", raw["AllocationId"], raw["PublicIp"],
            ResourceStatus.IN_USE if attached else ResourceStatus.AVAILABLE, self.region,
            public_ip=raw["PublicIp"], tags=_tags(raw), attributes={"attached_to": attached})

    # -- live query & lifecycle ----------------------------------------------
    def get_instance(self, native_id: str) -> CloudResource:
        self.authenticate()
        resp = self.call("DescribeInstances", lambda: self.client.describe_instances(InstanceIds=[native_id]))
        page = [i for r in resp.get("Reservations", []) for i in r.get("Instances", [])]
        if not page:
            raise ResourceNotFound(f"instance {native_id} not found", self.provider, "InvalidInstanceID.NotFound")
        self._load_types({page[0]["InstanceType"]})
        return self._normalize_instance(page[0])

    def _change(self, op: str, method: str, key: str, native_id: str) -> ResourceStatus:
        self.authenticate()
        resp = self.call(op, lambda: getattr(self.client, method)(InstanceIds=[native_id]))
        state = resp[key][0]["CurrentState"]["Name"]
        return STATUS.get(state, ResourceStatus.UNKNOWN)

    def start(self, native_id: str) -> ResourceStatus:
        return self._change("StartInstances", "start_instances", "StartingInstances", native_id)

    def stop(self, native_id: str) -> ResourceStatus:
        return self._change("StopInstances", "stop_instances", "StoppingInstances", native_id)

    def reboot(self, native_id: str) -> ResourceStatus:
        self.authenticate()
        self.call("RebootInstances", lambda: self.client.reboot_instances(InstanceIds=[native_id]))
        return ResourceStatus.RUNNING  # EC2 reboots in place; the state stays "running"
