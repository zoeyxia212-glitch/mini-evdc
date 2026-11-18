"""Mini EvDC platform core.

A simplified version of the EvDC 1.0 tenant model (which was built on VMware
vCloud Director + NSX):

    Org ──< VDC (quota, region) ──< Network (isolated / routed, IP pool)
     │                          └──< VM (power state machine, snapshot, security tags)
     └──< Template (catalog)
    VDC ── Edge gateway: north-south firewall (first match, default deny) + DNAT/SNAT
        └─ Distributed firewall: east-west rules on every VM NIC, by CIDR or security tag

Every state-changing call is recorded as a Task, like the portal's task panel,
and (when a store is attached) the whole platform state is saved to SQLite
after each successful change, so it survives a restart.
"""
from __future__ import annotations

import ipaddress
import re
import threading
from collections.abc import Callable
from contextlib import contextmanager

from .errors import EvdcError, InvalidState, NotFound, QuotaExceeded
from .logs import get_logger
from .models import (
    VDC,
    VM,
    CloudResource,
    DistributedRule,
    FirewallRule,
    NatRule,
    Network,
    NetworkType,
    Org,
    PowerState,
    Quota,
    ResourceStatus,
    Snapshot,
    Task,
    Template,
    new_id,
    now,
    to_dict,
)

log = get_logger("evdc.core")

# computer name rule from the EvDC user guide: letters, digits, hyphens,
# not digits only, no spaces; Windows limits it to 15 chars.
_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,15}$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MAX_TASKS = 500

# (current state, action) -> new state
_POWER_TRANSITIONS = {
    (PowerState.POWERED_OFF, "power_on"): PowerState.POWERED_ON,
    (PowerState.POWERED_ON, "power_off"): PowerState.POWERED_OFF,
    (PowerState.POWERED_ON, "shutdown_guest"): PowerState.POWERED_OFF,
    (PowerState.POWERED_ON, "reset"): PowerState.POWERED_ON,
    (PowerState.POWERED_ON, "suspend"): PowerState.SUSPENDED,
    (PowerState.SUSPENDED, "power_on"): PowerState.POWERED_ON,
    (PowerState.SUSPENDED, "discard_suspend"): PowerState.POWERED_OFF,
}
POWER_ACTIONS = sorted({a for _, a in _POWER_TRANSITIONS})


def _validate_endpoint(value: str, allow_tags: bool = False) -> None:
    if value == "any":
        return
    if allow_tags and value.startswith("tag:"):
        if not _TAG_RE.match(value[4:]):
            raise EvdcError(f"bad security tag in '{value}'")
        return
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        raise EvdcError(f"'{value}' is not 'any', a CIDR" + (" or tag:<name>" if allow_tags else "")) from None


class EdgeGateway:
    """Per-VDC gateway: north-south firewall + NAT (deny-all by default, like NSX Edge)
    and the distributed firewall for east-west traffic (allow-all by default, like NSX DFW)."""

    def __init__(self, vdc_id: str):
        self.vdc_id = vdc_id
        self.firewall: list[FirewallRule] = []
        self.nat: list[NatRule] = []
        self.dfw: list[DistributedRule] = []
        self.default_action = "deny"
        self.dfw_default_action = "allow"

    @staticmethod
    def _match(rule_value: str, ip: str) -> bool:
        if rule_value == "any":
            return True
        return ipaddress.ip_address(ip) in ipaddress.ip_network(rule_value, strict=False)

    def evaluate(self, source_ip: str, dest_ip: str, port: int) -> tuple[str, str]:
        """North-south. Return (action, rule_name); first matching rule wins."""
        for rule in self.firewall:
            if (self._match(rule.source, source_ip) and self._match(rule.destination, dest_ip)
                    and (rule.port is None or rule.port == port)):
                return rule.action, rule.name
        return self.default_action, "default"

    @staticmethod
    def _match_vm(rule_value: str, vm: VM) -> bool:
        if rule_value.startswith("tag:"):
            return rule_value[4:] in vm.tags
        return EdgeGateway._match(rule_value, vm.ip)

    def evaluate_east_west(self, src: VM, dst: VM, port: int) -> tuple[str, str]:
        for rule in self.dfw:
            if (self._match_vm(rule.source, src) and self._match_vm(rule.destination, dst)
                    and (rule.port is None or rule.port == port)):
                return rule.action, rule.name
        return self.dfw_default_action, "default"

    def translate_inbound(self, public_ip: str, port: int) -> tuple[str, int] | None:
        for rule in self.nat:
            if rule.kind == "DNAT" and rule.original_ip == public_ip and \
                    (rule.original_port is None or rule.original_port == port):
                return rule.translated_ip, rule.translated_port or port
        return None


class EvdcPlatform:
    def __init__(self, on_change: Callable[[dict], None] | None = None):
        self.orgs: dict[str, Org] = {}
        self.vdcs: dict[str, VDC] = {}
        self.networks: dict[str, Network] = {}
        self.vms: dict[str, VM] = {}
        self.templates: dict[str, Template] = {}
        self.edges: dict[str, EdgeGateway] = {}
        self.tasks: list[Task] = []
        self._lock = threading.RLock()
        self.on_change = on_change   # called with to_state() after every successful change

    # -- task tracking + persistence ------------------------------------------
    @contextmanager
    def _task(self, operation: str, target: str):
        task = Task(operation=operation, target=target)
        with self._lock:
            self.tasks.append(task)
            del self.tasks[:-MAX_TASKS]
            try:
                yield task
                task.status = "success"
            except Exception as exc:
                task.status, task.detail = "error", str(exc)
                log.warning("task failed", extra={"ctx": {"op": operation, "target": target, "error": str(exc)}})
                raise
            finally:
                task.finished_at = now()
            if self.on_change:
                self.on_change(self.to_state())

    # -- lookups ----------------------------------------------------------------
    @staticmethod
    def _get(table: dict, key: str, kind: str):
        try:
            return table[key]
        except KeyError:
            raise NotFound(f"{kind} {key} not found") from None

    def get_org(self, org_id: str) -> Org:
        return self._get(self.orgs, org_id, "org")

    def get_vdc(self, vdc_id: str) -> VDC:
        return self._get(self.vdcs, vdc_id, "VDC")

    def get_network(self, net_id: str) -> Network:
        return self._get(self.networks, net_id, "network")

    def get_vm(self, vm_id: str) -> VM:
        return self._get(self.vms, vm_id, "VM")

    def get_template(self, tpl_id: str) -> Template:
        return self._get(self.templates, tpl_id, "template")

    def edge(self, vdc_id: str) -> EdgeGateway:
        return self._get(self.edges, vdc_id, "edge gateway for VDC")

    def org_of(self, kind: str, obj_id: str) -> str:
        """Which org owns an object (used by access control)."""
        if kind == "org":
            return self.get_org(obj_id).id
        if kind == "vdc":
            return self.get_vdc(obj_id).org_id
        if kind == "vm":
            return self.get_vdc(self.get_vm(obj_id).vdc_id).org_id
        if kind == "network":
            return self.get_vdc(self.get_network(obj_id).vdc_id).org_id
        if kind == "template":
            return self.get_template(obj_id).org_id
        raise ValueError(kind)

    def list_vdcs(self, org_id: str | None = None) -> list[VDC]:
        return [v for v in self.vdcs.values() if org_id is None or v.org_id == org_id]

    def list_networks(self, vdc_id: str) -> list[Network]:
        self.get_vdc(vdc_id)
        return [n for n in self.networks.values() if n.vdc_id == vdc_id]

    def list_vms(self, vdc_id: str | None = None, org_id: str | None = None, tag: str | None = None) -> list[VM]:
        out = []
        for vm in self.vms.values():
            if vdc_id and vm.vdc_id != vdc_id:
                continue
            if org_id and self.vdcs[vm.vdc_id].org_id != org_id:
                continue
            if tag and tag not in vm.tags:
                continue
            out.append(vm)
        return out

    def list_templates(self, org_id: str | None = None) -> list[Template]:
        return [t for t in self.templates.values() if org_id is None or t.org_id == org_id]

    # -- org / vdc ----------------------------------------------------------------
    def create_org(self, name: str) -> Org:
        with self._task("create_org", name):
            if not name or any(o.name == name for o in self.orgs.values()):
                raise EvdcError(f"org name '{name}' is empty or already exists")
            org = Org(name=name)
            self.orgs[org.id] = org
            return org

    def delete_org(self, org_id: str) -> None:
        with self._task("delete_org", org_id):
            self.get_org(org_id)
            if self.list_vdcs(org_id) or self.list_templates(org_id):
                raise InvalidState("delete the org's VDCs and templates first")
            del self.orgs[org_id]

    def create_vdc(self, org_id: str, name: str, region: str, quota: Quota) -> VDC:
        with self._task("create_vdc", name):
            self.get_org(org_id)
            if min(quota.vcpu, quota.memory_gb, quota.storage_gb) <= 0:
                raise EvdcError("quota values must be positive")
            vdc = VDC(org_id=org_id, name=name, region=region, quota=quota)
            self.vdcs[vdc.id] = vdc
            self.edges[vdc.id] = EdgeGateway(vdc.id)
            return vdc

    def update_quota(self, vdc_id: str, quota: Quota) -> VDC:
        with self._task("update_quota", vdc_id):
            vdc = self.get_vdc(vdc_id)
            used = self.usage(vdc_id)["used"]
            for key, value in quota.__dict__.items():
                if value < used[key]:
                    raise QuotaExceeded(f"{key}: new quota {value} is below current usage {used[key]}")
            vdc.quota = quota
            return vdc

    def delete_vdc(self, vdc_id: str) -> None:
        with self._task("delete_vdc", vdc_id):
            self.get_vdc(vdc_id)
            if self.list_vms(vdc_id) or self.list_networks(vdc_id):
                raise InvalidState("delete the VDC's VMs and networks first")
            del self.vdcs[vdc_id]
            del self.edges[vdc_id]

    def usage(self, vdc_id: str) -> dict:
        vdc = self.get_vdc(vdc_id)
        vms = self.list_vms(vdc_id)
        used = {
            "vcpu": sum(v.vcpu for v in vms),
            "memory_gb": sum(v.memory_gb for v in vms),
            "storage_gb": sum(v.disk_gb * (1 + len(v.snapshots)) for v in vms),
        }
        return {"quota": dict(vdc.quota.__dict__), "used": used, "vm_count": len(vms),
                "running": sum(v.power_state == PowerState.POWERED_ON for v in vms)}

    def _check_quota(self, vdc_id: str, add_vcpu: int, add_mem: int, add_disk: int) -> None:
        u = self.usage(vdc_id)
        q, used = u["quota"], u["used"]
        for key, add in (("vcpu", add_vcpu), ("memory_gb", add_mem), ("storage_gb", add_disk)):
            if used[key] + add > q[key]:
                raise QuotaExceeded(f"{key}: used {used[key]} + requested {add} > quota {q[key]}")

    # -- networks -----------------------------------------------------------------
    def create_network(self, vdc_id: str, name: str, cidr: str,
                       type: NetworkType = NetworkType.ROUTED) -> Network:
        with self._task("create_network", name):
            self.get_vdc(vdc_id)
            try:
                net = ipaddress.ip_network(cidr, strict=True)
            except ValueError as exc:
                raise EvdcError(f"bad CIDR {cidr}: {exc}") from None
            if net.version != 4 or net.prefixlen > 29:
                raise EvdcError("use an IPv4 network of /29 or larger")
            for other in self.list_networks(vdc_id):
                if net.overlaps(ipaddress.ip_network(other.cidr)):
                    raise EvdcError(f"{cidr} overlaps {other.name} ({other.cidr})")
            network = Network(vdc_id=vdc_id, name=name, type=NetworkType(type), cidr=cidr,
                              gateway=str(next(net.hosts())))
            self.networks[network.id] = network
            return network

    def delete_network(self, net_id: str) -> None:
        with self._task("delete_network", net_id):
            self.get_network(net_id)
            if any(vm.network_id == net_id for vm in self.vms.values()):
                raise InvalidState("network still has VMs attached")
            del self.networks[net_id]

    def _allocate_ip(self, network: Network) -> str:
        taken = set(network.allocated_ips) | {network.gateway}
        for host in ipaddress.ip_network(network.cidr).hosts():
            if str(host) not in taken:
                network.allocated_ips.append(str(host))
                return str(host)
        raise QuotaExceeded(f"network {network.name} has no free IPs")

    # -- templates (catalog) ------------------------------------------------------
    def create_template(self, org_id: str, name: str, os: str, vcpu: int, memory_gb: int, disk_gb: int) -> Template:
        with self._task("create_template", name):
            self.get_org(org_id)
            self._validate_size(vcpu, memory_gb, disk_gb)
            tpl = Template(org_id=org_id, name=name, os=os, vcpu=vcpu, memory_gb=memory_gb, disk_gb=disk_gb)
            self.templates[tpl.id] = tpl
            return tpl

    def delete_template(self, tpl_id: str) -> None:
        with self._task("delete_template", tpl_id):
            self.get_template(tpl_id)
            del self.templates[tpl_id]

    def capture_template(self, vm_id: str, name: str) -> Template:
        """Save a powered-off VM as a catalog template ("add to catalog" in the EvDC portal)."""
        vm = self.get_vm(vm_id)
        if vm.power_state != PowerState.POWERED_OFF:
            raise InvalidState("power off the VM before capturing it as a template")
        return self.create_template(self.vdcs[vm.vdc_id].org_id, name, vm.os, vm.vcpu, vm.memory_gb, vm.disk_gb)

    # -- VMs ------------------------------------------------------------------------
    @staticmethod
    def _validate_size(vcpu: int, memory_gb: int, disk_gb: int) -> None:
        if vcpu < 1 or memory_gb < 1 or disk_gb < 20:
            raise EvdcError("minimum size is 1 vCPU, 1 GB RAM, 20 GB disk")
        if vcpu > 96 or memory_gb > 768:
            raise EvdcError("maximum size is 96 vCPU / 768 GB RAM")

    def create_vm(self, vdc_id: str, name: str, vcpu: int, memory_gb: int, disk_gb: int,
                  os: str, network_id: str, power_on: bool = False, tags: list[str] | None = None,
                  template_id: str | None = None) -> VM:
        with self._task("create_vm", name):
            self.get_vdc(vdc_id)
            network = self.get_network(network_id)
            if network.vdc_id != vdc_id:
                raise EvdcError("network belongs to another VDC")
            if not _NAME_RE.match(name) or name.isdigit():
                raise EvdcError("name must be 1-15 letters/digits/hyphens and not digits only")
            if any(v.name == name for v in self.list_vms(vdc_id)):
                raise EvdcError(f"a VM called {name} already exists in this VDC")
            self._validate_size(vcpu, memory_gb, disk_gb)
            tags = self._clean_tags(tags or [])
            self._check_quota(vdc_id, vcpu, memory_gb, disk_gb)
            vm = VM(vdc_id=vdc_id, name=name, vcpu=vcpu, memory_gb=memory_gb, disk_gb=disk_gb, os=os,
                    network_id=network_id, ip=self._allocate_ip(network), tags=tags, template_id=template_id)
            if power_on:
                vm.power_state = PowerState.POWERED_ON
            self.vms[vm.id] = vm
            log.info("vm created", extra={"ctx": {"vm": vm.id, "vdc": vdc_id, "ip": vm.ip}})
            return vm

    def create_vm_from_template(self, vdc_id: str, name: str, template_id: str, network_id: str,
                                power_on: bool = False, tags: list[str] | None = None) -> VM:
        tpl = self.get_template(template_id)
        if tpl.org_id != self.get_vdc(vdc_id).org_id:
            raise EvdcError("template belongs to another org")
        return self.create_vm(vdc_id, name, tpl.vcpu, tpl.memory_gb, tpl.disk_gb, tpl.os, network_id,
                              power_on=power_on, tags=tags, template_id=tpl.id)

    def power(self, vm_id: str, action: str) -> VM:
        with self._task(action, vm_id):
            vm = self.get_vm(vm_id)
            new_state = _POWER_TRANSITIONS.get((vm.power_state, action))
            if new_state is None:
                raise InvalidState(f"cannot {action} a VM that is {vm.power_state.value}")
            vm.power_state = new_state
            return vm

    def resize(self, vm_id: str, vcpu: int, memory_gb: int, disk_gb: int | None = None) -> VM:
        with self._task("resize", vm_id):
            vm = self.get_vm(vm_id)
            if vm.power_state != PowerState.POWERED_OFF:
                raise InvalidState("power off the VM before changing its size")
            disk_gb = vm.disk_gb if disk_gb is None else disk_gb
            if disk_gb < vm.disk_gb:
                raise EvdcError("disks can grow but not shrink")
            self._validate_size(vcpu, memory_gb, disk_gb)
            extra_disk = (disk_gb - vm.disk_gb) * (1 + len(vm.snapshots))
            self._check_quota(vm.vdc_id, vcpu - vm.vcpu, memory_gb - vm.memory_gb, extra_disk)
            vm.vcpu, vm.memory_gb, vm.disk_gb = vcpu, memory_gb, disk_gb
            return vm

    @staticmethod
    def _clean_tags(tags: list[str]) -> list[str]:
        clean = sorted({t.strip().lower() for t in tags})
        bad = [t for t in clean if not _TAG_RE.match(t)]
        if bad:
            raise EvdcError(f"security tags must be lowercase letters, digits, hyphens: {bad}")
        return clean

    def set_tags(self, vm_id: str, tags: list[str]) -> VM:
        with self._task("set_tags", vm_id):
            vm = self.get_vm(vm_id)
            vm.tags = self._clean_tags(tags)
            return vm

    def create_snapshot(self, vm_id: str) -> Snapshot:
        """EvDC (vCD) keeps one snapshot per VM: a new one replaces the old one."""
        with self._task("create_snapshot", vm_id):
            vm = self.get_vm(vm_id)
            if not vm.snapshots:
                self._check_quota(vm.vdc_id, 0, 0, vm.disk_gb)
            snap = Snapshot(id=new_id("snap"), created_at=now(),
                            vcpu=vm.vcpu, memory_gb=vm.memory_gb, disk_gb=vm.disk_gb)
            vm.snapshots = [snap]
            return snap

    def revert_snapshot(self, vm_id: str) -> VM:
        with self._task("revert_snapshot", vm_id):
            vm = self.get_vm(vm_id)
            if not vm.snapshots:
                raise InvalidState("VM has no snapshot")
            snap = vm.snapshots[0]
            vm.vcpu, vm.memory_gb, vm.disk_gb = snap.vcpu, snap.memory_gb, snap.disk_gb
            vm.power_state = PowerState.POWERED_OFF
            return vm

    def remove_snapshot(self, vm_id: str) -> VM:
        with self._task("remove_snapshot", vm_id):
            vm = self.get_vm(vm_id)
            if not vm.snapshots:
                raise InvalidState("VM has no snapshot")
            vm.snapshots = []
            return vm

    def delete_vm(self, vm_id: str) -> None:
        with self._task("delete_vm", vm_id):
            vm = self.get_vm(vm_id)
            if vm.power_state != PowerState.POWERED_OFF:
                raise InvalidState("power off the VM before deleting it")
            network = self.networks.get(vm.network_id)
            if network and vm.ip in network.allocated_ips:
                network.allocated_ips.remove(vm.ip)
            del self.vms[vm_id]

    # -- edge gateway (north-south) ---------------------------------------------
    def add_firewall_rule(self, vdc_id: str, rule: FirewallRule, position: int | None = None) -> FirewallRule:
        with self._task("add_firewall_rule", vdc_id):
            if rule.action not in ("allow", "deny"):
                raise EvdcError("action must be allow or deny")
            _validate_endpoint(rule.source)
            _validate_endpoint(rule.destination)
            rules = self.edge(vdc_id).firewall
            rules.insert(len(rules) if position is None else position, rule)
            return rule

    def add_nat_rule(self, vdc_id: str, rule: NatRule) -> NatRule:
        with self._task("add_nat_rule", vdc_id):
            if rule.kind not in ("DNAT", "SNAT"):
                raise EvdcError("kind must be DNAT or SNAT")
            for ip in (rule.original_ip, rule.translated_ip):
                try:
                    ipaddress.ip_address(ip)
                except ValueError:
                    raise EvdcError(f"'{ip}' is not an IP address") from None
            self.edge(vdc_id).nat.append(rule)
            return rule

    def delete_rule(self, vdc_id: str, rule_id: str) -> None:
        """Delete a firewall, NAT or distributed-firewall rule by id."""
        with self._task("delete_rule", rule_id):
            edge = self.edge(vdc_id)
            for rules in (edge.firewall, edge.nat, edge.dfw):
                for i, r in enumerate(rules):
                    if r.id == rule_id:
                        del rules[i]
                        return
            raise NotFound(f"rule {rule_id} not found")

    def check_inbound(self, vdc_id: str, client_ip: str, public_ip: str, port: int) -> dict:
        """Simulate a packet from the internet: NAT first, then the firewall.

        Mirrors the user-guide warning: you need BOTH a DNAT rule and a
        firewall allow rule, otherwise traffic is dropped.
        """
        edge = self.edge(vdc_id)
        translated = edge.translate_inbound(public_ip, port)
        if translated is None:
            return {"result": "dropped", "reason": "no DNAT rule"}
        private_ip, private_port = translated
        action, rule = edge.evaluate(client_ip, private_ip, private_port)
        return {"result": "delivered" if action == "allow" else "dropped",
                "reason": f"firewall rule '{rule}' -> {action}",
                "to": f"{private_ip}:{private_port}"}

    # -- distributed firewall (east-west) -----------------------------------------
    def add_dfw_rule(self, vdc_id: str, rule: DistributedRule, position: int | None = None) -> DistributedRule:
        with self._task("add_dfw_rule", vdc_id):
            if rule.action not in ("allow", "deny"):
                raise EvdcError("action must be allow or deny")
            _validate_endpoint(rule.source, allow_tags=True)
            _validate_endpoint(rule.destination, allow_tags=True)
            rules = self.edge(vdc_id).dfw
            rules.insert(len(rules) if position is None else position, rule)
            return rule

    def set_dfw_default(self, vdc_id: str, action: str) -> None:
        with self._task("set_dfw_default", vdc_id):
            if action not in ("allow", "deny"):
                raise EvdcError("action must be allow or deny")
            self.edge(vdc_id).dfw_default_action = action

    def check_east_west(self, src_vm_id: str, dst_vm_id: str, port: int) -> dict:
        src, dst = self.get_vm(src_vm_id), self.get_vm(dst_vm_id)
        if src.vdc_id != dst.vdc_id:
            return {"result": "dropped", "reason": "VMs are in different VDCs (no route between them)"}
        if dst.power_state != PowerState.POWERED_ON:
            return {"result": "dropped", "reason": f"{dst.name} is not powered on"}
        action, rule = self.edge(src.vdc_id).evaluate_east_west(src, dst, port)
        return {"result": "delivered" if action == "allow" else "dropped",
                "reason": f"distributed firewall rule '{rule}' -> {action}",
                "from": f"{src.name} ({src.ip})", "to": f"{dst.name} ({dst.ip}):{port}"}

    # -- expose platform in the unified model ---------------------------------------
    def as_cloud_resources(self, account: str = "local") -> list[CloudResource]:
        status_map = {PowerState.POWERED_ON: ResourceStatus.RUNNING,
                      PowerState.POWERED_OFF: ResourceStatus.STOPPED,
                      PowerState.SUSPENDED: ResourceStatus.STOPPED}
        out = []
        for vm in list(self.vms.values()):
            vdc = self.vdcs[vm.vdc_id]
            out.append(CloudResource(
                provider="evdc", account=account, region=vdc.region, native_id=vm.id, name=vm.name,
                status=status_map[vm.power_state], vcpu=vm.vcpu, memory_gb=float(vm.memory_gb),
                private_ip=vm.ip, tags={"vdc": vdc.name, "os": vm.os},
                attributes={"security_tags": vm.tags, "disk_gb": vm.disk_gb}))
        for net in list(self.networks.values()):
            vdc = self.vdcs[net.vdc_id]
            out.append(CloudResource(
                provider="evdc", account=account, region=vdc.region, native_id=net.id, name=net.name,
                status=ResourceStatus.AVAILABLE, resource_type="vpc", tags={"vdc": vdc.name},
                attributes={"cidr": net.cidr, "type": net.type.value, "used_ips": len(net.allocated_ips)}))
        return out

    # -- persistence -------------------------------------------------------------------
    def to_state(self) -> dict:
        return {
            "orgs": to_dict(list(self.orgs.values())),
            "vdcs": to_dict(list(self.vdcs.values())),
            "networks": to_dict(list(self.networks.values())),
            "vms": to_dict(list(self.vms.values())),
            "templates": to_dict(list(self.templates.values())),
            "edges": {vdc_id: {"firewall": to_dict(e.firewall), "nat": to_dict(e.nat), "dfw": to_dict(e.dfw),
                               "dfw_default_action": e.dfw_default_action}
                      for vdc_id, e in self.edges.items()},
        }

    def load_state(self, state: dict) -> None:
        with self._lock:
            self.orgs = {o["id"]: Org(**o) for o in state.get("orgs", [])}
            self.vdcs = {v["id"]: VDC(**{**v, "quota": Quota(**v["quota"])}) for v in state.get("vdcs", [])}
            self.networks = {n["id"]: Network(**{**n, "type": NetworkType(n["type"])})
                             for n in state.get("networks", [])}
            self.vms = {v["id"]: VM(**{**v, "power_state": PowerState(v["power_state"]),
                                       "snapshots": [Snapshot(**s) for s in v.get("snapshots", [])]})
                        for v in state.get("vms", [])}
            self.templates = {t["id"]: Template(**t) for t in state.get("templates", [])}
            self.edges = {}
            for vdc_id in self.vdcs:
                saved = state.get("edges", {}).get(vdc_id, {})
                edge = EdgeGateway(vdc_id)
                edge.firewall = [FirewallRule(**r) for r in saved.get("firewall", [])]
                edge.nat = [NatRule(**r) for r in saved.get("nat", [])]
                edge.dfw = [DistributedRule(**r) for r in saved.get("dfw", [])]
                edge.dfw_default_action = saved.get("dfw_default_action", "allow")
                self.edges[vdc_id] = edge
