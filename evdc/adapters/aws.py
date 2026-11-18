"""AWS EC2 adapter.

Real SDK equivalent: boto3.client("ec2", region_name=...).describe_instances(
    MaxResults=..., NextToken=...)  and  describe_instance_types(InstanceTypes=[...]).

EC2 does not return CPU/memory per instance, only the instance type, so the
adapter looks the types up once and caches them.
"""
from __future__ import annotations

from typing import Iterator

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


@register("aws")
class AwsAdapter(ProviderAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._type_cache: dict[str, dict] = {}

    def iter_raw(self) -> Iterator[dict]:
        token = None
        while True:
            kwargs = {"MaxResults": self.page_size}
            if token:
                kwargs["NextToken"] = token
            resp = self.call("DescribeInstances", lambda kw=kwargs: self.client.describe_instances(**kw))
            page = [inst for res in resp.get("Reservations", []) for inst in res.get("Instances", [])]
            self._load_types({i["InstanceType"] for i in page})
            yield from page
            token = resp.get("NextToken")
            if not token:
                return

    def _load_types(self, types: set[str]) -> None:
        missing = sorted(t for t in types if t not in self._type_cache)
        if not missing:
            return
        resp = self.call("DescribeInstanceTypes",
                         lambda: self.client.describe_instance_types(InstanceTypes=missing))
        for t in resp["InstanceTypes"]:
            self._type_cache[t["InstanceType"]] = t

    def normalize(self, raw: dict) -> CloudResource:
        tags = {t["Key"]: t["Value"] for t in raw.get("Tags", [])}
        name = tags.pop("Name", raw["InstanceId"])
        tags["instance_type"] = raw["InstanceType"]
        spec = self._type_cache.get(raw["InstanceType"], {})
        az = raw.get("Placement", {}).get("AvailabilityZone", "")
        return CloudResource(
            provider=self.provider, account=self.account,
            region=az[:-1] if az and az[-1].isalpha() else az,
            native_id=raw["InstanceId"], name=name,
            status=STATUS.get(raw["State"]["Name"], ResourceStatus.UNKNOWN),
            vcpu=spec.get("VCpuInfo", {}).get("DefaultVCpus"),
            memory_gb=round(spec["MemoryInfo"]["SizeInMiB"] / 1024, 2) if spec else None,
            private_ip=raw.get("PrivateIpAddress"), public_ip=raw.get("PublicIpAddress"),
            tags=tags,
        )
