"""Azure Compute adapter.

Real SDK equivalent: azure.identity.ClientSecretCredential +
azure.mgmt.compute.ComputeManagementClient.virtual_machines.list_all(),
which follows "nextLink" for you; here we do it by hand to show the pattern.

Azure uses short-lived OAuth bearer tokens, so this adapter:
- refreshes the token shortly before it expires, and
- if a call still fails with an expired token, re-authenticates once.
"""
from __future__ import annotations

import time
from typing import Iterator
from urllib.parse import parse_qs, urlparse

from ..models import CloudResource, ResourceStatus
from ..retry import run_with_retry
from .base import ProviderAdapter, register

POWER = {
    "running": ResourceStatus.RUNNING,
    "deallocated": ResourceStatus.STOPPED,
    "stopped": ResourceStatus.STOPPED,
    "starting": ResourceStatus.PENDING,
    "stopping": ResourceStatus.STOPPING,
    "deallocating": ResourceStatus.STOPPING,
}
REFRESH_MARGIN_S = 60


@register("azure")
class AzureAdapter(ProviderAdapter):
    supports_reauth = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._token: str | None = None
        self._expires_at = 0.0
        self._sizes: dict[tuple[str, str], tuple[int, int]] = {}
        self._size_locations: set[str] = set()

    def authenticate(self, force: bool = False) -> None:
        if not force and self._token and time.time() < self._expires_at - REFRESH_MARGIN_S:
            return
        # token request itself uses retry, but not the re-auth path (avoid recursion)
        resp = run_with_retry(self.client.get_token, self.policy, op="GetToken",
                              ctx={"provider": self.provider, "account": self.account}, sleep=self._sleep)
        self._token = resp["access_token"]
        self._expires_at = time.time() + float(resp["expires_in"])
        self.log.info("token acquired", extra={"ctx": {"provider": self.provider, "account": self.account,
                                                       "expires_in": resp["expires_in"]}})

    def iter_raw(self) -> Iterator[dict]:
        skip = None
        while True:
            self.authenticate()  # cheap; refreshes only near expiry
            resp = self.call("ListVirtualMachines",
                             lambda s=skip: self.client.list_vms(self._token, skip_token=s, top=self.page_size))
            for vm in resp["value"]:
                self._load_sizes(vm["location"])
                yield vm
            next_link = resp.get("nextLink")
            if not next_link:
                return
            skip = int(parse_qs(urlparse(next_link).query)["$skiptoken"][0])

    def _load_sizes(self, location: str) -> None:
        if location in self._size_locations:
            return
        resp = self.call("ListVmSizes", lambda: self.client.list_sizes(self._token, location))
        for s in resp["value"]:
            self._sizes[(location, s["name"])] = (s["numberOfCores"], s["memoryInMB"])
        self._size_locations.add(location)

    def normalize(self, raw: dict) -> CloudResource:
        props = raw["properties"]
        size = props["hardwareProfile"]["vmSize"]
        cores, mem_mb = self._sizes.get((raw["location"], size), (None, None))
        power = next((s["code"].split("/", 1)[1] for s in props.get("instanceView", {}).get("statuses", [])
                      if s["code"].startswith("PowerState/")), None)
        nics = props.get("networkProfile", {}).get("networkInterfaces", [])
        private_ip = None
        if nics:
            ipcfgs = nics[0].get("properties", {}).get("ipConfigurations", [])
            if ipcfgs:
                private_ip = ipcfgs[0]["properties"].get("privateIPAddress")
        return CloudResource(
            provider=self.provider, account=self.account, region=raw["location"],
            native_id=raw["id"], name=raw["name"],
            status=POWER.get(power, ResourceStatus.UNKNOWN),
            vcpu=cores, memory_gb=round(mem_mb / 1024, 2) if mem_mb else None,
            private_ip=private_ip, public_ip=None,
            tags={**raw.get("tags", {}), "vm_size": size},
        )
