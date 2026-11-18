"""Adapter for Mini EvDC's own VMs, so the inventory shows EvDC next to the public clouds."""
from __future__ import annotations

from typing import Iterator

from ..models import CloudResource, ResourceStatus
from .base import ProviderAdapter, register


@register("evdc")
class EvdcLocalAdapter(ProviderAdapter):
    """`client` is an EvdcPlatform instance. Its VMs are already unified."""

    def iter_raw(self) -> Iterator[CloudResource]:  # type: ignore[override]
        yield from self.client.as_cloud_resources(account=self.account)

    def normalize(self, raw: CloudResource) -> CloudResource:  # type: ignore[override]
        return raw

    def start(self, native_id: str) -> ResourceStatus:
        self.client.power(native_id, "power_on")
        return ResourceStatus.RUNNING

    def stop(self, native_id: str) -> ResourceStatus:
        self.client.power(native_id, "shutdown_guest")
        return ResourceStatus.STOPPED
