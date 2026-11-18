"""Data models for Mini EvDC.

Two families of models live here:

1. Platform models (Org -> VDC -> Network / VM), a simplified version of the
   vCloud Director object model that EvDC 1.0 exposed to tenants.
2. CloudResource, the *unified resource model* that every provider adapter
   maps its own data format into.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Platform models
# --------------------------------------------------------------------------

class PowerState(str, Enum):
    POWERED_OFF = "POWERED_OFF"
    POWERED_ON = "POWERED_ON"
    SUSPENDED = "SUSPENDED"


class NetworkType(str, Enum):
    ISOLATED = "isolated"   # only reachable inside the VDC
    ROUTED = "routed"       # goes out through the edge gateway (NAT / firewall)


@dataclass
class Quota:
    vcpu: int
    memory_gb: int
    storage_gb: int


@dataclass
class Org:
    name: str
    id: str = field(default_factory=lambda: new_id("org"))
    created_at: str = field(default_factory=now)


@dataclass
class VDC:
    org_id: str
    name: str
    region: str
    quota: Quota
    id: str = field(default_factory=lambda: new_id("vdc"))
    created_at: str = field(default_factory=now)


@dataclass
class Network:
    vdc_id: str
    name: str
    type: NetworkType
    cidr: str
    gateway: str
    id: str = field(default_factory=lambda: new_id("net"))
    allocated_ips: list[str] = field(default_factory=list)


@dataclass
class Snapshot:
    id: str
    created_at: str
    vcpu: int
    memory_gb: int
    disk_gb: int


@dataclass
class VM:
    vdc_id: str
    name: str
    vcpu: int
    memory_gb: int
    disk_gb: int
    os: str
    network_id: str
    ip: str
    id: str = field(default_factory=lambda: new_id("vm"))
    power_state: PowerState = PowerState.POWERED_OFF
    snapshots: list[Snapshot] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)   # security tags, used by the distributed firewall
    template_id: str | None = None
    created_at: str = field(default_factory=now)


@dataclass
class Template:
    """Catalog item: a VM image + default size, like a vApp template in EvDC."""
    org_id: str
    name: str
    os: str
    vcpu: int
    memory_gb: int
    disk_gb: int
    id: str = field(default_factory=lambda: new_id("tpl"))


@dataclass
class FirewallRule:
    """Edge gateway firewall rule. First match wins, like NSX Edge."""
    name: str
    source: str          # CIDR or "any"
    destination: str     # CIDR or "any"
    port: int | None     # None = any port
    action: str          # "allow" | "deny"
    id: str = field(default_factory=lambda: new_id("fw"))


@dataclass
class DistributedRule:
    """East-west rule enforced at every VM's NIC (NSX distributed firewall).

    source / destination: "any", a CIDR, or "tag:<security tag>".
    """
    name: str
    source: str
    destination: str
    port: int | None
    action: str
    id: str = field(default_factory=lambda: new_id("dfw"))


@dataclass
class NatRule:
    """DNAT (inbound) or SNAT (outbound) rule on the edge gateway."""
    kind: str            # "DNAT" | "SNAT"
    original_ip: str
    translated_ip: str
    original_port: int | None = None
    translated_port: int | None = None
    id: str = field(default_factory=lambda: new_id("nat"))


@dataclass
class Task:
    """Every state-changing call is recorded as a task, like the EvDC portal."""
    operation: str
    target: str
    status: str = "running"   # running | success | error
    detail: str = ""
    id: str = field(default_factory=lambda: new_id("task"))
    started_at: str = field(default_factory=now)
    finished_at: str | None = None


# --------------------------------------------------------------------------
# Unified multi-cloud resource model
# --------------------------------------------------------------------------

class ResourceStatus(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    PENDING = "pending"
    STOPPING = "stopping"
    DELETED = "deleted"
    AVAILABLE = "available"   # disk / eip not attached, vpc ready
    IN_USE = "in_use"         # disk / eip attached to an instance
    UNKNOWN = "unknown"


RESOURCE_TYPES = ("instance", "disk", "vpc", "eip")


@dataclass
class CloudResource:
    """One consistent structure for a cloud resource from any provider.

    Every adapter must produce this shape. Downstream code (sync, API,
    reports) never has to know which cloud an instance came from.
    """
    provider: str            # "evdc" | "aliyun" | "aws" | "azure"
    account: str             # which configured account it was synced from
    region: str
    native_id: str           # the provider's own ID
    name: str
    status: ResourceStatus
    vcpu: int | None = None          # instances
    memory_gb: float | None = None   # instances
    private_ip: str | None = None    # instances
    public_ip: str | None = None     # instances, eips
    size_gb: float | None = None     # disks
    tags: dict[str, str] = field(default_factory=dict)
    resource_type: str = "instance"  # instance | disk | vpc | eip
    attributes: dict[str, Any] = field(default_factory=dict)  # type-specific extras (cidr, attached_to ...)
    synced_at: str = field(default_factory=now)

    @property
    def uid(self) -> str:
        """Globally unique key used for upserts."""
        return f"{self.provider}:{self.account}:{self.native_id}"


def to_dict(obj: Any) -> Any:
    """JSON-friendly conversion for dataclasses and enums."""
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        data = {k: to_dict(v) for k, v in asdict(obj).items()}
        if isinstance(obj, CloudResource):
            data["uid"] = obj.uid
        return data
    if isinstance(obj, list):
        return [to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj
