"""Alibaba Cloud adapter (primary provider).

Real SDK equivalents (alibabacloud_ecs20140526 / alibabacloud_vpc20160428):
    DescribeInstances, DescribeDisks, DescribeVpcs, DescribeEipAddresses,
    StartInstance, StopInstance, RebootInstance
All list calls use PageNumber / PageSize and return TotalCount, and wrap the
records twice, e.g. {"Disks": {"Disk": [...]}}.
"""
from __future__ import annotations

import json
from typing import Iterator

from ..errors import ResourceNotFound
from ..models import CloudResource, ResourceStatus
from .base import ProviderAdapter, register

INSTANCE_STATUS = {
    "Running": ResourceStatus.RUNNING,
    "Stopped": ResourceStatus.STOPPED,
    "Starting": ResourceStatus.PENDING,
    "Pending": ResourceStatus.PENDING,
    "Stopping": ResourceStatus.STOPPING,
}
DISK_STATUS = {"In_use": ResourceStatus.IN_USE, "Available": ResourceStatus.AVAILABLE,
               "Attaching": ResourceStatus.PENDING, "Creating": ResourceStatus.PENDING}
EIP_STATUS = {"InUse": ResourceStatus.IN_USE, "Available": ResourceStatus.AVAILABLE,
              "Associating": ResourceStatus.PENDING}

# kind -> (API op name, client method, outer key, inner key)
LIST_CALLS = {
    "instance": ("DescribeInstances", "describe_instances", "Instances", "Instance"),
    "disk": ("DescribeDisks", "describe_disks", "Disks", "Disk"),
    "vpc": ("DescribeVpcs", "describe_vpcs", "Vpcs", "Vpc"),
    "eip": ("DescribeEipAddresses", "describe_eip_addresses", "EipAddresses", "EipAddress"),
}


def _tags(raw: dict) -> dict:
    return {t["TagKey"]: t["TagValue"] for t in raw.get("Tags", {}).get("Tag", [])}


@register("aliyun")
class AliyunAdapter(ProviderAdapter):
    resource_types = ("instance", "disk", "vpc", "eip")

    def __init__(self, *args, region: str = "cn-hangzhou", **kwargs):
        super().__init__(*args, **kwargs)
        self.region = region

    def iter_raw(self, kind: str) -> Iterator[dict]:
        op, method, outer, inner = LIST_CALLS[kind]
        page, seen = 1, 0
        while True:
            resp = self.call(op, lambda p=page: getattr(self.client, method)(
                RegionId=self.region, PageNumber=p, PageSize=self.page_size))
            items = resp[outer][inner]
            yield from items
            seen += len(items)
            # stop on the last page; the empty-page check guards against a bad TotalCount
            if not items or seen >= resp["TotalCount"]:
                return
            page += 1

    def normalize(self, kind: str, raw: dict) -> CloudResource:
        return getattr(self, f"_normalize_{kind}")(raw)

    def _normalize_instance(self, raw: dict) -> CloudResource:
        private = raw.get("VpcAttributes", {}).get("PrivateIpAddress", {}).get("IpAddress", [])
        # public IP is either a classic public IP or an attached EIP
        public = list(raw.get("PublicIpAddress", {}).get("IpAddress", []))
        eip = raw.get("EipAddress", {}).get("IpAddress")
        if not public and eip:
            public = [eip]
        return self.resource(
            "instance", raw["InstanceId"], raw.get("InstanceName"),
            INSTANCE_STATUS.get(raw["Status"], ResourceStatus.UNKNOWN), raw["RegionId"],
            vcpu=int(raw["Cpu"]), memory_gb=round(raw["Memory"] / 1024, 2),
            private_ip=private[0] if private else None, public_ip=public[0] if public else None,
            tags=_tags(raw), attributes={"instance_type": raw.get("InstanceType"), "zone": raw.get("ZoneId")})

    def _normalize_disk(self, raw: dict) -> CloudResource:
        return self.resource(
            "disk", raw["DiskId"], raw.get("DiskName"), DISK_STATUS.get(raw["Status"], ResourceStatus.UNKNOWN),
            raw["RegionId"], size_gb=float(raw["Size"]), tags=_tags(raw),
            attributes={"category": raw.get("Category"), "role": raw.get("Type"),
                        "attached_to": raw.get("InstanceId") or None})

    def _normalize_vpc(self, raw: dict) -> CloudResource:
        return self.resource(
            "vpc", raw["VpcId"], raw.get("VpcName"),
            ResourceStatus.AVAILABLE if raw["Status"] == "Available" else ResourceStatus.PENDING,
            raw["RegionId"], tags=_tags(raw),
            attributes={"cidr": raw["CidrBlock"], "is_default": raw.get("IsDefault", False)})

    def _normalize_eip(self, raw: dict) -> CloudResource:
        return self.resource(
            "eip", raw["AllocationId"], raw["IpAddress"], EIP_STATUS.get(raw["Status"], ResourceStatus.UNKNOWN),
            raw["RegionId"], public_ip=raw["IpAddress"],
            attributes={"bandwidth_mbps": int(raw.get("Bandwidth") or 0),
                        "attached_to": raw.get("InstanceId") or None})

    # -- live query & lifecycle -------------------------------------------------
    def get_instance(self, native_id: str) -> CloudResource:
        self.authenticate()
        resp = self.call("DescribeInstances", lambda: self.client.describe_instances(
            RegionId=self.region, InstanceIds=json.dumps([native_id])))
        items = resp["Instances"]["Instance"]
        if not items:
            raise ResourceNotFound(f"instance {native_id} not found", self.provider, "InvalidInstanceId.NotFound")
        return self._normalize_instance(items[0])

    def start(self, native_id: str) -> ResourceStatus:
        self.authenticate()
        self.call("StartInstance", lambda: self.client.start_instance(InstanceId=native_id))
        return ResourceStatus.PENDING

    def stop(self, native_id: str) -> ResourceStatus:
        self.authenticate()
        self.call("StopInstance", lambda: self.client.stop_instance(InstanceId=native_id))
        return ResourceStatus.STOPPING

    def reboot(self, native_id: str) -> ResourceStatus:
        self.authenticate()
        self.call("RebootInstance", lambda: self.client.reboot_instance(InstanceId=native_id))
        return ResourceStatus.PENDING
