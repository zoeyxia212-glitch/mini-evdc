"""Azure adapter.

Real SDK equivalent: azure.identity.ClientSecretCredential +
azure.mgmt.compute.ComputeManagementClient (virtual_machines, disks) and
azure.mgmt.network.NetworkManagementClient (virtual_networks, public_ip_addresses).
Pagination follows "nextLink", which is treated as an opaque continuation token.

Azure uses short-lived OAuth bearer tokens, so this adapter
- refreshes the token shortly before it expires, and
- if a call still fails with an expired token, re-authenticates once.
"""
from __future__ import annotations

import time
from collections.abc import Iterator

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
DISK_STATE = {"Attached": ResourceStatus.IN_USE, "Reserved": ResourceStatus.IN_USE,
              "Unattached": ResourceStatus.AVAILABLE}
REFRESH_MARGIN_S = 60

LIST_CALLS = {
    "instance": ("ListVirtualMachines", "list_vms"),
    "disk": ("ListDisks", "list_disks"),
    "vpc": ("ListVirtualNetworks", "list_vnets"),
    "eip": ("ListPublicIPAddresses", "list_public_ips"),
}


@register("azure")
class AzureAdapter(ProviderAdapter):
    supports_reauth = True
    resource_types = ("instance", "disk", "vpc", "eip")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._token: str | None = None
        self._expires_at = 0.0
        self._sizes: dict[tuple[str, str], tuple[int, int]] = {}
        self._size_locations: set[str] = set()

    def authenticate(self, force: bool = False) -> None:
        if not force and self._token and time.time() < self._expires_at - REFRESH_MARGIN_S:
            return
        # the token request itself uses retry, but not the re-auth path (avoids recursion)
        resp = run_with_retry(self.client.get_token, self.policy, op="GetToken",
                              ctx={"provider": self.provider, "account": self.account}, sleep=self._sleep)
        self._token = resp["access_token"]
        self._expires_at = time.time() + float(resp["expires_in"])
        self.log.info("token acquired", extra={"ctx": {"provider": self.provider, "account": self.account,
                                                       "expires_in": resp["expires_in"]}})

    def iter_raw(self, kind: str) -> Iterator[dict]:
        op, method = LIST_CALLS[kind]
        next_link = None
        while True:
            self.authenticate()  # cheap; refreshes only near expiry
            # the nextLink is opaque: hand it back unchanged (real skiptokens are not numbers)
            resp = self.call(op, lambda link=next_link: getattr(self.client, method)(
                self._token, next_link=link, top=self.page_size))
            for item in resp["value"]:
                if kind == "instance":
                    self._load_sizes(item["location"])
                yield item
            next_link = resp.get("nextLink")
            if not next_link:
                return

    def _load_sizes(self, location: str) -> None:
        if location in self._size_locations:
            return
        resp = self.call("ListVmSizes", lambda: self.client.list_sizes(self._token, location))
        for s in resp["value"]:
            self._sizes[(location, s["name"])] = (s["numberOfCores"], s["memoryInMB"])
        self._size_locations.add(location)

    # -- mapping ------------------------------------------------------------
    def normalize(self, kind: str, raw: dict) -> CloudResource:
        return getattr(self, f"_normalize_{kind}")(raw)

    def _normalize_instance(self, raw: dict) -> CloudResource:
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
        return self.resource(
            "instance", raw["id"], raw["name"], POWER.get(power, ResourceStatus.UNKNOWN), raw["location"],
            vcpu=cores, memory_gb=round(mem_mb / 1024, 2) if mem_mb else None, private_ip=private_ip,
            tags=dict(raw.get("tags", {})), attributes={"vm_size": size, "power_state": power})

    def _normalize_disk(self, raw: dict) -> CloudResource:
        props = raw["properties"]
        return self.resource(
            "disk", raw["id"], raw["name"], DISK_STATE.get(props.get("diskState"), ResourceStatus.UNKNOWN),
            raw["location"], size_gb=float(props["diskSizeGB"]), tags=dict(raw.get("tags", {})),
            attributes={"category": raw.get("sku", {}).get("name"), "attached_to": raw.get("managedBy")})

    def _normalize_vpc(self, raw: dict) -> CloudResource:
        props = raw["properties"]
        prefixes = props.get("addressSpace", {}).get("addressPrefixes", [])
        return self.resource(
            "vpc", raw["id"], raw["name"],
            ResourceStatus.AVAILABLE if props.get("provisioningState") == "Succeeded" else ResourceStatus.PENDING,
            raw["location"], tags=dict(raw.get("tags", {})),
            attributes={"cidr": prefixes[0] if prefixes else None, "address_prefixes": prefixes})

    def _normalize_eip(self, raw: dict) -> CloudResource:
        props = raw["properties"]
        attached = props.get("ipConfiguration", {}).get("id")
        return self.resource(
            "eip", raw["id"], raw["name"], ResourceStatus.IN_USE if attached else ResourceStatus.AVAILABLE,
            raw["location"], public_ip=props.get("ipAddress"), tags=dict(raw.get("tags", {})),
            attributes={"allocation": props.get("publicIPAllocationMethod"), "attached_to": attached})

    # -- live query & lifecycle ------------------------------------------------
    def get_instance(self, native_id: str) -> CloudResource:
        self.authenticate()
        raw = self.call("GetVirtualMachine", lambda: self.client.get_vm(self._token, native_id))
        self._load_sizes(raw["location"])
        return self._normalize_instance(raw)

    def start(self, native_id: str) -> ResourceStatus:
        self.authenticate()
        self.call("StartVM", lambda: self.client.start(self._token, native_id))
        return ResourceStatus.PENDING

    def stop(self, native_id: str) -> ResourceStatus:
        """Azure 'stop' still bills compute; deallocate is what actually stops charges."""
        self.authenticate()
        self.call("DeallocateVM", lambda: self.client.deallocate(self._token, native_id))
        return ResourceStatus.STOPPING

    def reboot(self, native_id: str) -> ResourceStatus:
        self.authenticate()
        self.call("RestartVM", lambda: self.client.restart(self._token, native_id))
        return ResourceStatus.PENDING
