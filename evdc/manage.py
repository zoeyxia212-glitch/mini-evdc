"""Unified management: act on any instance by its uid, whatever cloud it lives on.

    manager.start("aws:aws-syd:i-0000000000000003")
    manager.stop("aliyun:ali-prod:i-bp1000000000000004")

The manager looks the resource up in the inventory, finds the adapter that
owns its account, calls the provider (through the same timeout / retry /
re-auth path as sync), and records the new status straight away so the
inventory does not show stale data until the next sync.
"""
from __future__ import annotations

from dataclasses import dataclass

from .adapters.base import ProviderAdapter
from .errors import EvdcError, NotFound
from .logs import get_logger
from .models import ResourceStatus
from .store import Store

log = get_logger("evdc.manage")

# no-op when the instance is already there (or on its way)
_ALREADY = {
    "start": {ResourceStatus.RUNNING, ResourceStatus.PENDING},
    "stop": {ResourceStatus.STOPPED, ResourceStatus.STOPPING},
}


@dataclass
class ActionResult:
    uid: str
    action: str
    previous: str
    status: str
    changed: bool


class ResourceManager:
    def __init__(self, store: Store, adapters: list[ProviderAdapter]):
        self.store = store
        self.adapters = adapters

    def _adapter_for(self, provider: str, account: str) -> ProviderAdapter:
        for a in self.adapters:
            if a.provider == provider and a.account == account:
                return a
        raise NotFound(f"no configured account {provider}:{account}")

    def _run(self, uid: str, action: str) -> ActionResult:
        res = self.store.get(uid)
        if res is None:
            raise NotFound(f"resource {uid} not in inventory (run a sync first)")
        if res.status == ResourceStatus.DELETED:
            raise EvdcError(f"resource {uid} has been deleted")
        if res.status in _ALREADY[action]:
            return ActionResult(uid, action, res.status.value, res.status.value, changed=False)
        adapter = self._adapter_for(res.provider, res.account)
        new_status = getattr(adapter, action)(res.native_id)
        self.store.set_status(uid, new_status)
        log.info("resource action", extra={"ctx": {"uid": uid, "action": action,
                                                   "from": res.status.value, "to": new_status.value}})
        return ActionResult(uid, action, res.status.value, new_status.value, changed=True)

    def start(self, uid: str) -> ActionResult:
        return self._run(uid, "start")

    def stop(self, uid: str) -> ActionResult:
        return self._run(uid, "stop")
