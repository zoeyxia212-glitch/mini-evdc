"""Alibaba Cloud ECS adapter (primary provider).

Real SDK equivalent: alibabacloud_ecs20140526.Client.describe_instances(
    DescribeInstancesRequest(region_id=..., page_number=..., page_size=...))
"""
from __future__ import annotations

from typing import Iterator

from ..models import CloudResource, ResourceStatus
from .base import ProviderAdapter, register

STATUS = {
    "Running": ResourceStatus.RUNNING,
    "Stopped": ResourceStatus.STOPPED,
    "Starting": ResourceStatus.PENDING,
    "Pending": ResourceStatus.PENDING,
    "Stopping": ResourceStatus.STOPPING,
}


@register("aliyun")
class AliyunAdapter(ProviderAdapter):
    def __init__(self, *args, region: str = "cn-hangzhou", **kwargs):
        super().__init__(*args, **kwargs)
        self.region = region

    def iter_raw(self) -> Iterator[dict]:
        page, seen = 1, 0
        while True:
            resp = self.call("DescribeInstances", lambda p=page: self.client.describe_instances(
                RegionId=self.region, PageNumber=p, PageSize=self.page_size))
            items = resp["Instances"]["Instance"]
            yield from items
            seen += len(items)
            # stop on the last page; the empty-page check guards against a bad TotalCount
            if not items or seen >= resp["TotalCount"]:
                return
            page += 1

    def normalize(self, raw: dict) -> CloudResource:
        private = raw.get("VpcAttributes", {}).get("PrivateIpAddress", {}).get("IpAddress", [])
        # public IP is either a classic public IP or an attached EIP
        public = list(raw.get("PublicIpAddress", {}).get("IpAddress", []))
        eip = raw.get("EipAddress", {}).get("IpAddress")
        if not public and eip:
            public = [eip]
        tags = {t["TagKey"]: t["TagValue"] for t in raw.get("Tags", {}).get("Tag", [])}
        tags["instance_type"] = raw.get("InstanceType", "")
        return CloudResource(
            provider=self.provider, account=self.account, region=raw["RegionId"],
            native_id=raw["InstanceId"], name=raw.get("InstanceName") or raw["InstanceId"],
            status=STATUS.get(raw["Status"], ResourceStatus.UNKNOWN),
            vcpu=int(raw["Cpu"]), memory_gb=round(raw["Memory"] / 1024, 2),
            private_ip=private[0] if private else None,
            public_ip=public[0] if public else None,
            tags=tags,
        )
