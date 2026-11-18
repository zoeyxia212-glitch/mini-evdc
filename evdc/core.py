"""Mini EvDC platform core.

A simplified version of the EvDC 1.0 tenant model (which was built on VMware
vCloud Director + NSX):

    Org ──< VDC (quota, region) ──< Network (isolated / routed, IP pool)
                               └──< VM (power state machine, snapshot)
    VDC ── Edge gateway: firewall rules (first match wins, default deny)
                         NAT rules (DNAT inbound / SNAT outbound)

Every state-changing call is recorded as a Task, like the portal's task panel.
State is kept in memory; the multi-cloud inventory (store.py) is in SQLite.
"""
from __future__ import annotations

import ipaddress
import re
import threading
from contextlib import contextmanager

from .errors import EvdcError, InvalidState, NotFound, QuotaExceeded
from .logs import get_logger
from .models import (VDC, VM, CloudResource, FirewallRule, NatRule, Network, NetworkType,
                     Org, PowerState, Quota, ResourceStatus, Snapshot, Task, new_id, now)

log = get_logger("evdc.core")

# computer name rule from the EvDC user guide: letters, digits, hyphens,
# not digits only, no spaces; Windows limits it to 15 chars.
_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,15}$")

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


class EdgeGateway:
    """Per-VDC edge: north-south firewall + NAT. Deny-all by default (like NSX Edge)."""

    def __init__(self, vdc_id: str):
        self.vdc_id = vdc_id
        self.firewall: list[FirewallRule] = []
        self.nat: list[NatRule] = []
        self.default_action = "deny"

    @staticmethod
    def _match(rule_value: str, ip: str) -> bool:
        if rule_value == "any":
            return True
        return ipaddress.ip_address(ip) in ipaddress.ip_network(rule_value, strict=False)

    def evaluate(self, source_ip: str, dest_ip: str, port: int) -> tuple[str, str]:
        """Return (action, rule_name). First matching rule wins."""
        for rule in self.firewall:
            if (self._match(rule.source, source_ip) and self._match(rule.destination, dest_ip)
                    and (rule.port is None or rule.port == port)):
                return rule.action, rule.name
        return self.default_action, "default"

    def translate_inbound(self, public_ip: str, port: int) -> tuple[str, int] | None:
        for rule in self.nat:
            if rule.kind == "DNAT" and rule.original_ip == public_ip and \
                    (rule.original_port is None or rule.original_port == port):
                return rule.translated_ip, rule.translated_port or port
        return None


class EvdcPlatform:
    def __init__(self):
        self.orgs: dict[str, Org] = {}
        self.vdcs: dict[str, VDC] = {}
        self.networks: dict[str, Network] = {}
        self.vms: dict[str, VM] = {}
        self.edges: dict[str, EdgeGateway] = {}
        self.tasks: list[Task] = []
        self._lock = threading.RLock()

    # -- task tracking ----------------------------------------------------
    @contextmanager
    def _task(self, operation: str, target: str):
        task = Task(operation=operation, target=target)
        self.tasks.append(task)
        try:
            yield task
            task.status = "success"
        except Exception as exc:
            task.status, task.detail = "error", str(exc)
            log.warning("task failed", extra={"ctx": {"op": operation, "target": target, "error": str(exc)}})
            raise
        finally:
            task.finished_at = now()

    # -- lookups ------------------------------------------------------------
    def _get(self, table: dict, key: str, kind: str):
        try:
            return table[key]
        except KeyError:
            raise NotFound(f"{kind} {key} not found") from None

    def get_vm(self, vm_id: str) -> VM:
        return self._get(self.vms, vm_id, "VM")

    def edge(self, vdc_id: str) -> EdgeGateway:
        return self._get(self.edges, vdc_id, "edge gateway for VDC")

    # -- org / vdc ----------------------------------------------------------
    def create_org(self, name: str) -> Org:
        with self._lock, self._task("create_org", name):
            if any(o.name == name for o in self.orgs.values()):
                raise EvdcError(f"org '{name}' already exists")
            org = Org(name=name)
            self.orgs[org.id] = org
            return org

    def create_vdc(self, org_id: str, name: str, region: str, quota: Quota) -> VDC:
        with self._lock, self._task("create_vdc", name):
            self._get(self.orgs, org_id, "org")
            vdc = VDC(org_id=org_id, name=name, region=region, quota=quota)
            self.vdcs[vdc.id] = vdc
            self.edges[vdc.id] = EdgeGateway(vdc.id)
            return vdc

    def usage(self, vdc_id: str) -> dict:
        vdc = self._get(self.vdcs, vdc_id, "VDC")
        vms = [v for v in self.vms.values() if v.vdc_id == vdc_id]
        used = {
            "vcpu": sum(v.vcpu for v in vms),
            "memory_gb": sum(v.memory_gb for v in vms),
            "storage_gb": sum(v.disk_gb * (1 + len(v.snapshots)) for v in vms),
        }
        return {"quota": vdc.quota.__dict__, "used": used, "vm_count": len(vms),
                "running": sum(v.power_state == PowerState.POWERED_ON for v in vms)}

    def _check_quota(self, vdc_id: str, add_vcpu: int, add_mem: int, add_disk: int) -> None:
        u = self.usage(vdc_id)
        q, used = u["quota"], u["used"]
        for key, add in (("vcpu", add_vcpu), ("memory_gb", add_mem), ("storage_gb", add_disk)):
            if used[key] + add > q[key]:
                raise QuotaExceeded(f"{key}: used {used[key]} + requested {add} > quota {q[key]}")

    # -- networks -----------------------------------------------------------
    def create_network(self, vdc_id: str, name: str, cidr: str,
                       type: NetworkType = NetworkType.ROUTED) -> Network:
        with self._lock, self._task("create_network", name):
            self._get(self.vdcs, vdc_id, "VDC")
            net = ipaddress.ip_network(cidr, strict=True)
            for other in self.networks.values():
                if other.vdc_id == vdc_id and net.overlaps(ipaddress.ip_network(other.cidr)):
                    raise EvdcError(f"{cidr} overlaps {other.name} ({other.cidr})")
            gateway = str(next(net.hosts()))
            network = Network(vdc_id=vdc_id, name=name, type=NetworkType(type), cidr=cidr, gateway=gateway)
            self.networks[network.id] = network
            return network

    def _allocate_ip(self, network: Network) -> str:
        taken = set(network.allocated_ips) | {network.gateway}
        for host in ipaddress.ip_network(network.cidr).hosts():
            if str(host) not in taken:
                network.allocated_ips.append(str(host))
                return str(host)
        raise QuotaExceeded(f"network {network.name} has no free IPs")

    # -- VMs ------------------------------------------------------------------
    def create_vm(self, vdc_id: str, name: str, vcpu: int, memory_gb: int, disk_gb: int,
                  os: str, network_id: str, power_on: bool = False) -> VM:
        with self._lock, self._task("create_vm", name):
            self._get(self.vdcs, vdc_id, "VDC")
            network = self._get(self.networks, network_id, "network")
            if network.vdc_id != vdc_id:
                raise EvdcError("network belongs to another VDC")
            if not _NAME_RE.match(name) or name.isdigit():
                raise EvdcError("name must be 1-15 letters/digits/hyphens and not digits only")
            if vcpu < 1 or memory_gb < 1 or disk_gb < 20:
                raise EvdcError("minimum size is 1 vCPU, 1 GB RAM, 20 GB disk")
            self._check_quota(vdc_id, vcpu, memory_gb, disk_gb)
            vm = VM(vdc_id=vdc_id, name=name, vcpu=vcpu, memory_gb=memory_gb, disk_gb=disk_gb,
                    os=os, network_id=network_id, ip=self._allocate_ip(network))
            if power_on:
                vm.power_state = PowerState.POWERED_ON
            self.vms[vm.id] = vm
            log.info("vm created", extra={"ctx": {"vm": vm.id, "vdc": vdc_id, "ip": vm.ip}})
            return vm

    def power(self, vm_id: str, action: str) -> VM:
        with self._lock, self._task(action, vm_id):
            vm = self.get_vm(vm_id)
            new_state = _POWER_TRANSITIONS.get((vm.power_state, action))
            if new_state is None:
                raise InvalidState(f"cannot {action} a VM that is {vm.power_state.value}")
            vm.power_state = new_state
            return vm

    def resize(self, vm_id: str, vcpu: int, memory_gb: int) -> VM:
        with self._lock, self._task("resize", vm_id):
            vm = self.get_vm(vm_id)
            if vm.power_state != PowerState.POWERED_OFF:
                raise InvalidState("power off the VM before changing CPU/memory")
            self._check_quota(vm.vdc_id, vcpu - vm.vcpu, memory_gb - vm.memory_gb, 0)
            vm.vcpu, vm.memory_gb = vcpu, memory_gb
            return vm

    def create_snapshot(self, vm_id: str) -> Snapshot:
        """EvDC (vCD) keeps one snapshot per VM: a new one replaces the old one."""
        with self._lock, self._task("create_snapshot", vm_id):
            vm = self.get_vm(vm_id)
            if not vm.snapshots:
                self._check_quota(vm.vdc_id, 0, 0, vm.disk_gb)
            snap = Snapshot(id=new_id("snap"), created_at=now(),
                            vcpu=vm.vcpu, memory_gb=vm.memory_gb, disk_gb=vm.disk_gb)
            vm.snapshots = [snap]
            return snap

    def revert_snapshot(self, vm_id: str) -> VM:
        with self._lock, self._task("revert_snapshot", vm_id):
            vm = self.get_vm(vm_id)
            if not vm.snapshots:
                raise InvalidState("VM has no snapshot")
            snap = vm.snapshots[0]
            vm.vcpu, vm.memory_gb, vm.disk_gb = snap.vcpu, snap.memory_gb, snap.disk_gb
            vm.power_state = PowerState.POWERED_OFF
            return vm

    def remove_snapshot(self, vm_id: str) -> VM:
        with self._lock, self._task("remove_snapshot", vm_id):
            vm = self.get_vm(vm_id)
            vm.snapshots = []
            return vm

    def delete_vm(self, vm_id: str) -> None:
        with self._lock, self._task("delete_vm", vm_id):
            vm = self.get_vm(vm_id)
            if vm.power_state != PowerState.POWERED_OFF:
                raise InvalidState("power off the VM before deleting it")
            network = self.networks.get(vm.network_id)
            if network and vm.ip in network.allocated_ips:
                network.allocated_ips.remove(vm.ip)
            del self.vms[vm_id]

    # -- edge gateway ----------------------------------------------------------
    def add_firewall_rule(self, vdc_id: str, rule: FirewallRule, position: int | None = None) -> FirewallRule:
        with self._lock, self._task("add_firewall_rule", vdc_id):
            if rule.action not in ("allow", "deny"):
                raise EvdcError("action must be allow or deny")
            for value in (rule.source, rule.destination):
                if value != "any":
                    ipaddress.ip_network(value, strict=False)  # validates
            rules = self.edge(vdc_id).firewall
            rules.insert(len(rules) if position is None else position, rule)
            return rule

    def add_nat_rule(self, vdc_id: str, rule: NatRule) -> NatRule:
        with self._lock, self._task("add_nat_rule", vdc_id):
            if rule.kind not in ("DNAT", "SNAT"):
                raise EvdcError("kind must be DNAT or SNAT")
            ipaddress.ip_address(rule.original_ip)
            ipaddress.ip_address(rule.translated_ip)
            self.edge(vdc_id).nat.append(rule)
            return rule

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

    # -- expose own VMs in the unified model ----------------------------------
    def as_cloud_resources(self, account: str = "local") -> list[CloudResource]:
        status_map = {PowerState.POWERED_ON: ResourceStatus.RUNNING,
                      PowerState.POWERED_OFF: ResourceStatus.STOPPED,
                      PowerState.SUSPENDED: ResourceStatus.STOPPED}
        out = []
        for vm in self.vms.values():
            vdc = self.vdcs[vm.vdc_id]
            out.append(CloudResource(
                provider="evdc", account=account, region=vdc.region, native_id=vm.id, name=vm.name,
                status=status_map[vm.power_state], vcpu=vm.vcpu, memory_gb=float(vm.memory_gb),
                private_ip=vm.ip, public_ip=None, tags={"vdc": vdc.name, "os": vm.os}))
        return out
