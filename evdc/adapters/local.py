"""Adapter for Mini EvDC's own platform, so the inventory shows EvDC next to the public clouds.

`client` is an EvdcPlatform instance; it already produces CloudResource objects.
"""
from __future__ import annotations

from typing import Iterator

from ..errors import NotFound, ResourceNotFound
from ..models import CloudResource, ResourceStatus
from .base import ProviderAdapter, register


@register("evdc")
class EvdcLocalAdapter(ProviderAdapter):
    resource_types = ("instance", "vpc")

    def iter_raw(self, kind: str) -> Iterator[CloudResource]:  # type: ignore[override]
        yield from (r for r in self.client.as_cloud_resources(account=self.account) if r.resource_type == kind)

    def normalize(self, kind: str, raw: CloudResource) -> CloudResource:  # type: ignore[override]
        return raw

    def get_instance(self, native_id: str) -> CloudResource:
        for r in self.client.as_cloud_resources(account=self.account):
            if r.native_id == native_id:
                return r
        raise ResourceNotFound(f"VM {native_id} not found", self.provider, "NotFound")

    def _power(self, native_id: str, action: str) -> None:
        try:
            self.client.power(native_id, action)
        except NotFound as exc:
            raise ResourceNotFound(str(exc), self.provider, "NotFound") from None

    def start(self, native_id: str) -> ResourceStatus:
        self._power(native_id, "power_on")
        return ResourceStatus.RUNNING

    def stop(self, native_id: str) -> ResourceStatus:
        self._power(native_id, "shutdown_guest")
        return ResourceStatus.STOPPED

    def reboot(self, native_id: str) -> ResourceStatus:
        self._power(native_id, "reset")
        return ResourceStatus.RUNNING
