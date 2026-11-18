"""Unified management: query and act on any instance by its uid, whatever cloud it lives on.

    manager.refresh("aws:aws-syd:i-0000000000000003")   # live status query, updates the inventory
    manager.start(uid) / manager.stop(uid) / manager.reboot(uid)

The manager looks the resource up in the inventory, finds the adapter that
owns its account, calls the provider (through the same timeout / retry /
re-auth path as sync), and records the new status straight away, with an
entry in the resource's change history.
"""
from __future__ import annotations

from dataclasses import dataclass

from .adapters.base import ProviderAdapter
from .errors import EvdcError, NotFound, ResourceNotFound
from .logs import get_logger
from .models import CloudResource, ResourceStatus
from .store import Store

log = get_logger("evdc.manage")

# no-op when the instance is already there (or on its way)
_ALREADY = {
    "start": {ResourceStatus.RUNNING, ResourceStatus.PENDING},
    "stop": {ResourceStatus.STOPPED, ResourceStatus.STOPPING},
}
# states an action may start from (checked against the inventory before calling the cloud)
_ALLOWED_FROM = {
    "start": {ResourceStatus.STOPPED, ResourceStatus.UNKNOWN},
    "stop": {ResourceStatus.RUNNING, ResourceStatus.UNKNOWN},
    "reboot": {ResourceStatus.RUNNING, ResourceStatus.UNKNOWN},
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

    def _instance(self, uid: str) -> CloudResource:
        res = self.store.get(uid)
        if res is None:
            raise NotFound(f"resource {uid} not in inventory (run a sync first)")
        if res.resource_type != "instance":
            raise EvdcError(f"{uid} is a {res.resource_type}; actions only apply to instances")
        if res.status == ResourceStatus.DELETED:
            raise EvdcError(f"resource {uid} has been deleted")
        return res

    def refresh(self, uid: str) -> CloudResource:
        """Ask the provider for the current state of one instance and update the inventory."""
        res = self._instance(uid)
        adapter = self._adapter_for(res.provider, res.account)
        try:
            live = adapter.get_instance(res.native_id)
        except ResourceNotFound:
            self.store.set_status(uid, ResourceStatus.DELETED, action="refresh")
            return self.store.get(uid)
        self.store.upsert(live)
        return live

    def _run(self, uid: str, action: str) -> ActionResult:
        res = self._instance(uid)
        if res.status in _ALREADY.get(action, ()):
            return ActionResult(uid, action, res.status.value, res.status.value, changed=False)
        if res.status not in _ALLOWED_FROM[action]:
            raise EvdcError(f"cannot {action} an instance that is {res.status.value}")
        adapter = self._adapter_for(res.provider, res.account)
        new_status = getattr(adapter, action)(res.native_id)
        self.store.set_status(uid, new_status, action=action)
        log.info("resource action", extra={"ctx": {"uid": uid, "action": action,
                                                   "from": res.status.value, "to": new_status.value}})
        return ActionResult(uid, action, res.status.value, new_status.value, changed=True)

    def start(self, uid: str) -> ActionResult:
        return self._run(uid, "start")

    def stop(self, uid: str) -> ActionResult:
        return self._run(uid, "stop")

    def reboot(self, uid: str) -> ActionResult:
        return self._run(uid, "reboot")
