"""REST API on the standard library (no framework needed).

Every route except GET /health needs an API key:
    Authorization: Bearer <key>      or      X-API-Key: <key>

Access: S = system_admin only, T = tenant (org_admin / viewer of the owning org, or system_admin),
        W = write (viewer refused), R = any authenticated user.

  Identity & users
    GET    /health                                    public
    GET    /healthz                                   public  liveness: the process answers
    GET    /readyz                                    public  readiness: db, scheduler, not draining (else 503)
    GET    /me                                        R
    GET    /users                                     T  (system: all users)
    POST   /users            {name, role, org_id}     T W  -> returns the new API key once
    DELETE /users/{name}                              T W
  Orgs, VDCs, networks
    GET    /orgs                                      R  (tenants see their own org)
    POST   /orgs             {name}                   S
    DELETE /orgs/{org}                                S
    GET    /orgs/{org}/vdcs                           T
    POST   /orgs/{org}/vdcs  {name, region, quota}    S  (the provider allocates quota)
    GET    /vdcs/{vdc}                                T  (VDC + usage)
    PUT    /vdcs/{vdc}/quota {vcpu, memory_gb, storage_gb}   S
    DELETE /vdcs/{vdc}                                S
    GET    /vdcs/{vdc}/networks                       T
    POST   /vdcs/{vdc}/networks {name, cidr, type}    T W
    DELETE /networks/{net}                            T W
  Catalog
    GET    /orgs/{org}/templates                      T
    POST   /orgs/{org}/templates {name, os, vcpu, memory_gb, disk_gb}   T W
    DELETE /templates/{tpl}                           T W
    POST   /vms/{vm}/capture {name}                   T W  (save VM as template)
  VMs
    GET    /vms?vdc=&tag=                             R  (scoped to the caller's org)
    POST   /vdcs/{vdc}/vms   {name, network_id, template_id | vcpu+memory_gb+disk_gb+os, tags, power_on}  T W
    GET    /vms/{vm}                                  T
    DELETE /vms/{vm}                                  T W
    POST   /vms/{vm}/power   {action}                 T W
           action: power_on | power_off | shutdown_guest | reset | suspend | discard_suspend
    PUT    /vms/{vm}/size    {vcpu, memory_gb, disk_gb}  T W
    PUT    /vms/{vm}/tags    {tags: [...]}            T W
    POST   /vms/{vm}/snapshot                         T W
    POST   /vms/{vm}/snapshot/revert                  T W
    DELETE /vms/{vm}/snapshot                         T W
  Edge gateway + distributed firewall
    GET    /vdcs/{vdc}/edge                           T  (all rules)
    POST   /vdcs/{vdc}/firewall {name, source, destination, port, action, position?}   T W
    POST   /vdcs/{vdc}/nat      {kind, original_ip, translated_ip, original_port, translated_port}  T W
    POST   /vdcs/{vdc}/dfw      {name, source, destination, port, action, position?}   T W  (tag:<name> allowed)
    PUT    /vdcs/{vdc}/dfw/default {action}           T W
    DELETE /vdcs/{vdc}/rules/{rule}                   T W
    POST   /vdcs/{vdc}/check-inbound    {client_ip, public_ip, port}   T
    POST   /vdcs/{vdc}/check-east-west  {src_vm, dst_vm, port}         T
  Audit
    GET    /tasks                                     S
  Multi-cloud
    POST   /sync                                      S
    GET    /sync/history                              R
    GET    /resources?provider=&status=&type=&account=   R
    GET    /resources/summary                         R
    GET    /resources/history?uid=&limit=             R  (no uid = latest events everywhere)
    POST   /resources/{refresh|start|stop|reboot} {uid}   S
      query params for GET /resources: provider status type account region
      tag=env | tag=env:prod   name=<substring>   sort=name|-vcpu|...   limit offset
      (total count in the X-Total-Count header)
    GET    /resources.csv?<same filters>              R  CSV export
    GET    /resources/relations?uid=                  R  disks / IPs of an instance, or what a disk is attached to
    GET    /resources/orphans                         R  disks / IPs pointing at instances that no longer exist
  Cost & governance
    GET    /costs?top=10                              R  estimated monthly cost, by provider / type, waste
    GET    /policies                                  R
    POST   /policies         {policy JSON}            S  add or replace a policy
    POST   /policies/run     {name | policy, dry_run=true}   R for dry runs, S to act
    GET    /policies/runs                             R
  Audit
    GET    /audit?limit=&user=                        S  every write and every refused request: who, what, result
  Monitoring, docs & UI
    GET    /openapi.json                              public, OpenAPI 3 description of this API
    GET    /metrics                                   public, Prometheus text format
    GET    / or /dashboard                            public page; it asks for an API key and calls the API
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .app import App
from .auth import AuthService, Forbidden, Principal, Unauthorized
from .errors import (
    CloudApiError,
    EvdcError,
    IncorrectState,
    InvalidState,
    NotFound,
    QuotaExceeded,
    ResourceNotFound,
    SyncInProgress,
)
from .logs import get_logger
from .metrics import render as render_metrics
from .models import DistributedRule, FirewallRule, NatRule, NetworkType, Quota, to_dict
from .policy import Policy
from .pricing import cost_report, monthly_cost

log = get_logger("evdc.api")
MAX_BODY = 1_000_000


@dataclass
class Raw:
    """A non-JSON response body (CSV, Prometheus text) or JSON with extra headers."""
    body: object
    content_type: str = "application/json; charset=utf-8"
    headers: dict = field(default_factory=dict)


@dataclass
class Ctx:
    who: Principal | None
    body: dict
    query: dict


def build_routes(app: App):
    p, auth, store = app.platform, app.auth, app.store
    need = AuthService.require

    def tenant(c: Ctx, kind: str, obj_id: str, write: bool = False) -> None:
        need(c.who, write=write, org_id=p.org_of(kind, obj_id))

    def system(c: Ctx) -> None:
        need(c.who, system=True)

    def fields(body: dict, *names: str) -> dict:
        missing = [n for n in names if n not in body]
        if missing:
            raise EvdcError(f"missing field(s): {', '.join(missing)}")
        return {n: body[n] for n in names}

    # -- identity & users ----------------------------------------------------
    def health(c):
        return 200, {"status": "ok", "accounts": [f"{a.provider}:{a.account}" for a in app.adapters]}

    def healthz(c):
        return 200, {"status": "alive"}

    def readyz(c):
        ready, checks = app.readiness()
        return (200 if ready else 503), {"status": "ready" if ready else "not ready", "checks": checks}

    def me(c):
        return 200, c.who.__dict__

    def users_list(c):
        if c.who.is_system:
            return 200, store.list_users()
        need(c.who, org_id=c.who.org_id)
        return 200, store.list_users(c.who.org_id)

    def users_create(c):
        b = c.body
        role, org_id = b.get("role", "viewer"), b.get("org_id")
        if role == "system_admin":
            system(c)
        else:
            if org_id is None:
                raise EvdcError("org_id is required for org_admin / viewer")
            p.get_org(org_id)
            need(c.who, write=True, org_id=org_id)
            if c.who.role != "system_admin" and c.who.role != "org_admin":
                raise Forbidden("only admins can create users")
        key = auth.create_user(fields(b, "name")["name"], role, org_id)
        return 201, {"name": b["name"], "role": role, "org_id": org_id, "api_key": key,
                     "note": "store this key now; it cannot be shown again"}

    def users_delete(c, name):
        target = next((u for u in store.list_users() if u["name"] == name), None)
        if target is None:
            raise NotFound(f"user {name} not found")
        if name == c.who.name:
            raise EvdcError("you cannot delete yourself")
        if target["role"] == "system_admin":
            system(c)
        else:
            need(c.who, write=True, org_id=target["org_id"])
            if c.who.role == "viewer":
                raise Forbidden("viewer role is read-only")
        store.delete_user(name)
        return 204, None

    # -- orgs / vdcs / networks ------------------------------------------------
    def orgs_list(c):
        if c.who.is_system:
            return 200, list(p.orgs.values())
        return 200, [p.get_org(c.who.org_id)]

    def orgs_create(c):
        system(c)
        return 201, p.create_org(fields(c.body, "name")["name"])

    def orgs_delete(c, org):
        system(c)
        p.delete_org(org)
        return 204, None

    def vdcs_list(c, org):
        tenant(c, "org", org)
        return 200, p.list_vdcs(org)

    def vdc_create(c, org):
        system(c)
        b = fields(c.body, "name", "quota")
        return 201, p.create_vdc(org, b["name"], c.body.get("region", "huabei1"), Quota(**b["quota"]))

    def vdc_get(c, vdc):
        tenant(c, "vdc", vdc)
        return 200, {**to_dict(p.get_vdc(vdc)), "usage": p.usage(vdc)}

    def vdc_quota(c, vdc):
        system(c)
        return 200, p.update_quota(vdc, Quota(**fields(c.body, "vcpu", "memory_gb", "storage_gb")))

    def vdc_delete(c, vdc):
        system(c)
        p.delete_vdc(vdc)
        return 204, None

    def nets_list(c, vdc):
        tenant(c, "vdc", vdc)
        return 200, p.list_networks(vdc)

    def net_create(c, vdc):
        tenant(c, "vdc", vdc, write=True)
        b = fields(c.body, "name", "cidr")
        return 201, p.create_network(vdc, b["name"], b["cidr"], NetworkType(c.body.get("type", "routed")))

    def net_delete(c, net):
        tenant(c, "network", net, write=True)
        p.delete_network(net)
        return 204, None

    # -- templates ------------------------------------------------------------------
    def tpl_list(c, org):
        tenant(c, "org", org)
        return 200, p.list_templates(org)

    def tpl_create(c, org):
        tenant(c, "org", org, write=True)
        b = fields(c.body, "name", "os", "vcpu", "memory_gb", "disk_gb")
        return 201, p.create_template(org, b["name"], b["os"], int(b["vcpu"]), int(b["memory_gb"]),
                                      int(b["disk_gb"]))

    def tpl_delete(c, tpl):
        tenant(c, "template", tpl, write=True)
        p.delete_template(tpl)
        return 204, None

    def vm_capture(c, vm):
        tenant(c, "vm", vm, write=True)
        return 201, p.capture_template(vm, fields(c.body, "name")["name"])

    # -- VMs ---------------------------------------------------------------------------
    def vms_list(c):
        org = None if c.who.is_system else c.who.org_id
        return 200, p.list_vms(vdc_id=c.query.get("vdc"), org_id=org, tag=c.query.get("tag"))

    def vm_create(c, vdc):
        tenant(c, "vdc", vdc, write=True)
        b = c.body
        common = fields(b, "name", "network_id")
        if b.get("template_id"):
            return 201, p.create_vm_from_template(vdc, common["name"], b["template_id"], common["network_id"],
                                                  power_on=bool(b.get("power_on")), tags=b.get("tags"))
        size = fields(b, "vcpu", "memory_gb", "disk_gb")
        return 201, p.create_vm(vdc, common["name"], int(size["vcpu"]), int(size["memory_gb"]),
                                int(size["disk_gb"]), b.get("os", "Ubuntu 22.04"), common["network_id"],
                                power_on=bool(b.get("power_on")), tags=b.get("tags"))

    def vm_get(c, vm):
        tenant(c, "vm", vm)
        return 200, p.get_vm(vm)

    def vm_delete(c, vm):
        tenant(c, "vm", vm, write=True)
        p.delete_vm(vm)
        return 204, None

    def vm_power(c, vm):
        tenant(c, "vm", vm, write=True)
        return 200, p.power(vm, fields(c.body, "action")["action"])

    def vm_size(c, vm):
        tenant(c, "vm", vm, write=True)
        b = fields(c.body, "vcpu", "memory_gb")
        disk = c.body.get("disk_gb")
        return 200, p.resize(vm, int(b["vcpu"]), int(b["memory_gb"]), None if disk is None else int(disk))

    def vm_tags(c, vm):
        tenant(c, "vm", vm, write=True)
        tags = fields(c.body, "tags")["tags"]
        if not isinstance(tags, list):
            raise EvdcError("tags must be a list")
        return 200, p.set_tags(vm, tags)

    def snap_create(c, vm):
        tenant(c, "vm", vm, write=True)
        return 201, p.create_snapshot(vm)

    def snap_revert(c, vm):
        tenant(c, "vm", vm, write=True)
        return 200, p.revert_snapshot(vm)

    def snap_delete(c, vm):
        tenant(c, "vm", vm, write=True)
        p.remove_snapshot(vm)
        return 204, None

    # -- edge gateway + DFW ---------------------------------------------------------------
    def edge_get(c, vdc):
        tenant(c, "vdc", vdc)
        e = p.edge(vdc)
        return 200, {"firewall": e.firewall, "firewall_default": e.default_action, "nat": e.nat,
                     "dfw": e.dfw, "dfw_default": e.dfw_default_action}

    def _rule_args(b: dict) -> tuple[dict, int | None]:
        rule = fields(b, "name", "source", "destination", "action")
        rule["port"] = None if b.get("port") in (None, "", "any") else int(b["port"])
        return rule, b.get("position")

    def fw_add(c, vdc):
        tenant(c, "vdc", vdc, write=True)
        rule, pos = _rule_args(c.body)
        return 201, p.add_firewall_rule(vdc, FirewallRule(**rule), pos)

    def dfw_add(c, vdc):
        tenant(c, "vdc", vdc, write=True)
        rule, pos = _rule_args(c.body)
        return 201, p.add_dfw_rule(vdc, DistributedRule(**rule), pos)

    def dfw_default(c, vdc):
        tenant(c, "vdc", vdc, write=True)
        p.set_dfw_default(vdc, fields(c.body, "action")["action"])
        return 200, {"dfw_default": p.edge(vdc).dfw_default_action}

    def nat_add(c, vdc):
        tenant(c, "vdc", vdc, write=True)
        b = fields(c.body, "kind", "original_ip", "translated_ip")
        return 201, p.add_nat_rule(vdc, NatRule(original_port=c.body.get("original_port"),
                                                translated_port=c.body.get("translated_port"), **b))

    def rule_delete(c, vdc, rule):
        tenant(c, "vdc", vdc, write=True)
        p.delete_rule(vdc, rule)
        return 204, None

    def check_inbound(c, vdc):
        tenant(c, "vdc", vdc)
        b = fields(c.body, "client_ip", "public_ip", "port")
        return 200, p.check_inbound(vdc, b["client_ip"], b["public_ip"], int(b["port"]))

    def check_east_west(c, vdc):
        tenant(c, "vdc", vdc)
        b = fields(c.body, "src_vm", "dst_vm", "port")
        for vm_id in (b["src_vm"], b["dst_vm"]):
            tenant(c, "vm", vm_id)
        return 200, p.check_east_west(b["src_vm"], b["dst_vm"], int(b["port"]))

    def tasks(c):
        system(c)
        return 200, p.tasks[-100:]

    # -- multi-cloud -----------------------------------------------------------------------
    def sync_now(c):
        system(c)
        return 200, [r.__dict__ for r in app.sync()]

    def sync_history(c):
        return 200, store.sync_history()

    def _query(q: dict):
        return store.query(provider=q.get("provider"), status=q.get("status"), resource_type=q.get("type"),
                           account=q.get("account"), region=q.get("region"), tag=q.get("tag"),
                           name=q.get("name"), sort=q.get("sort", "name"), limit=int(q.get("limit", 100)),
                           offset=int(q.get("offset", 0)))

    def resources(c):
        page, total = _query(c.query)
        return 200, Raw(page, headers={"X-Total-Count": str(total)})

    def resources_csv(c):
        q = dict(c.query)
        q.setdefault("limit", "1000")
        page, _ = _query(q)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["uid", "provider", "account", "region", "type", "name", "status", "vcpu", "memory_gb",
                    "size_gb", "private_ip", "public_ip", "monthly_cost_usd", "tags"])
        for r in page:
            w.writerow([r.uid, r.provider, r.account, r.region, r.resource_type, r.name, r.status.value, r.vcpu,
                        r.memory_gb, r.size_gb, r.private_ip, r.public_ip, monthly_cost(r),
                        ";".join(f"{k}={v}" for k, v in sorted(r.tags.items()))])
        return 200, Raw(out.getvalue(), "text/csv; charset=utf-8",
                        {"Content-Disposition": 'attachment; filename="inventory.csv"'})

    def resources_relations(c):
        uid = c.query.get("uid")
        if not uid:
            raise EvdcError("uid query parameter is required")
        rel = store.relations(uid)
        if not rel:
            raise NotFound(f"resource {uid} not in inventory")
        return 200, rel

    def resources_orphans(c):
        return 200, store.orphans()

    # -- cost & governance ------------------------------------------------------------------
    def costs(c):
        return 200, cost_report(store.list_resources(), top=int(c.query.get("top", 10)))

    def policies_list(c):
        return 200, [p.__dict__ for p in app.policies.policies.values()]

    def policies_add(c):
        system(c)
        policy = app.policies.load([c.body])[0]
        return 201, policy.__dict__

    def policies_run(c):
        b = c.body
        dry_run = b.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise EvdcError("dry_run must be true or false")
        if not dry_run:
            system(c)          # anyone may preview, only the provider may act
        if "policy" in b:
            target = Policy.from_dict(b["policy"])
        else:
            target = fields(b, "name")["name"]
        return 200, app.policies.run(target, dry_run=dry_run)

    def policies_runs(c):
        return 200, store.policy_runs()

    def audit(c):
        system(c)
        return 200, store.audit_log(int(c.query.get("limit", 100)), c.query.get("user"))

    def openapi(c):
        return 200, openapi_spec(routes_table, __doc__)

    def dashboard(c):
        html = (Path(__file__).parent / "web" / "dashboard.html").read_text(encoding="utf-8")
        return 200, Raw(html, "text/html; charset=utf-8",
                        {"Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; "
                                                    "style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'",
                         "Cache-Control": "no-store"})

    def metrics(c):
        return 200, Raw(render_metrics(app), "text/plain; version=0.0.4; charset=utf-8")

    def resources_summary(c):
        return 200, store.summary()

    def resources_history(c):
        return 200, store.history(c.query.get("uid"), int(c.query.get("limit", 100)))

    def resource_action(c, action):
        system(c)
        uid = fields(c.body, "uid")["uid"]
        if action == "refresh":
            return 200, app.manager.refresh(uid)
        result = getattr(app.manager, action)(uid)
        return (202 if result.changed else 200), result.__dict__

    ID = r"(?P<{}>[\w-]+)"
    routes_table = [
        ("GET", r"/health", health, False),
        ("GET", r"/healthz", healthz, False),
        ("GET", r"/readyz", readyz, False),
        ("GET", r"/me", me, True),
        ("GET", r"/users", users_list, True),
        ("POST", r"/users", users_create, True),
        ("DELETE", r"/users/(?P<name>[\w.-]+)", users_delete, True),
        ("GET", r"/orgs", orgs_list, True),
        ("POST", r"/orgs", orgs_create, True),
        ("DELETE", rf"/orgs/{ID.format('org')}", orgs_delete, True),
        ("GET", rf"/orgs/{ID.format('org')}/vdcs", vdcs_list, True),
        ("POST", rf"/orgs/{ID.format('org')}/vdcs", vdc_create, True),
        ("GET", rf"/orgs/{ID.format('org')}/templates", tpl_list, True),
        ("POST", rf"/orgs/{ID.format('org')}/templates", tpl_create, True),
        ("DELETE", rf"/templates/{ID.format('tpl')}", tpl_delete, True),
        ("GET", rf"/vdcs/{ID.format('vdc')}", vdc_get, True),
        ("PUT", rf"/vdcs/{ID.format('vdc')}/quota", vdc_quota, True),
        ("DELETE", rf"/vdcs/{ID.format('vdc')}", vdc_delete, True),
        ("GET", rf"/vdcs/{ID.format('vdc')}/networks", nets_list, True),
        ("POST", rf"/vdcs/{ID.format('vdc')}/networks", net_create, True),
        ("DELETE", rf"/networks/{ID.format('net')}", net_delete, True),
        ("POST", rf"/vdcs/{ID.format('vdc')}/vms", vm_create, True),
        ("GET", rf"/vdcs/{ID.format('vdc')}/edge", edge_get, True),
        ("POST", rf"/vdcs/{ID.format('vdc')}/firewall", fw_add, True),
        ("POST", rf"/vdcs/{ID.format('vdc')}/nat", nat_add, True),
        ("POST", rf"/vdcs/{ID.format('vdc')}/dfw", dfw_add, True),
        ("PUT", rf"/vdcs/{ID.format('vdc')}/dfw/default", dfw_default, True),
        ("DELETE", rf"/vdcs/{ID.format('vdc')}/rules/{ID.format('rule')}", rule_delete, True),
        ("POST", rf"/vdcs/{ID.format('vdc')}/check-inbound", check_inbound, True),
        ("POST", rf"/vdcs/{ID.format('vdc')}/check-east-west", check_east_west, True),
        ("GET", r"/vms", vms_list, True),
        ("GET", rf"/vms/{ID.format('vm')}", vm_get, True),
        ("DELETE", rf"/vms/{ID.format('vm')}", vm_delete, True),
        ("POST", rf"/vms/{ID.format('vm')}/power", vm_power, True),
        ("PUT", rf"/vms/{ID.format('vm')}/size", vm_size, True),
        ("PUT", rf"/vms/{ID.format('vm')}/tags", vm_tags, True),
        ("POST", rf"/vms/{ID.format('vm')}/capture", vm_capture, True),
        ("POST", rf"/vms/{ID.format('vm')}/snapshot", snap_create, True),
        ("POST", rf"/vms/{ID.format('vm')}/snapshot/revert", snap_revert, True),
        ("DELETE", rf"/vms/{ID.format('vm')}/snapshot", snap_delete, True),
        ("GET", r"/tasks", tasks, True),
        ("POST", r"/sync", sync_now, True),
        ("GET", r"/sync/history", sync_history, True),
        ("GET", r"/resources", resources, True),
        ("GET", r"/resources/summary", resources_summary, True),
        ("GET", r"/resources/history", resources_history, True),
        ("POST", r"/resources/(?P<action>refresh|start|stop|reboot)", resource_action, True),
        ("GET", r"/resources\.csv", resources_csv, True),
        ("GET", r"/resources/relations", resources_relations, True),
        ("GET", r"/resources/orphans", resources_orphans, True),
        ("GET", r"/costs", costs, True),
        ("GET", r"/policies", policies_list, True),
        ("POST", r"/policies", policies_add, True),
        ("POST", r"/policies/run", policies_run, True),
        ("GET", r"/policies/runs", policies_runs, True),
        ("GET", r"/metrics", metrics, False),
        ("GET", r"/(?:dashboard)?", dashboard, False),
        ("GET", r"/audit", audit, True),
        ("GET", r"/openapi\.json", openapi, False),
    ]
    return routes_table


_DOC_LINE = re.compile(r"^\s{4}(GET|POST|PUT|DELETE)\s+(/\S*)\s*(.*)$")
_PARAM = re.compile(r"\(\?P<(\w+)>[^)]*\)")
_ACCESS = {"S": "system admin only", "T": "users of the owning org (or a system admin)", "W": "write: viewers refused",
           "R": "any signed-in user", "public": "no API key needed"}


SUMMARIES = {
    "health": "Service health and configured accounts", "healthz": "Liveness probe",
    "readyz": "Readiness probe", "me": "The caller's identity and role",
    "users_list": "List users", "users_create": "Create a user and return its API key once",
    "users_delete": "Delete a user", "orgs_list": "List orgs", "orgs_create": "Create an org",
    "orgs_delete": "Delete an empty org", "vdcs_list": "List an org's VDCs", "vdc_create": "Create a VDC with a quota",
    "vdc_get": "Get a VDC and its usage", "vdc_quota": "Change a VDC's quota", "vdc_delete": "Delete an empty VDC",
    "nets_list": "List a VDC's networks", "net_create": "Create a network", "net_delete": "Delete an unused network",
    "tpl_list": "List an org's templates", "tpl_create": "Create a template", "tpl_delete": "Delete a template",
    "vm_capture": "Save a powered-off VM as a template", "vms_list": "List VMs (scoped to the caller's org)",
    "vm_create": "Create a VM from a size or a template", "vm_get": "Get a VM", "vm_delete": "Delete a powered-off VM",
    "vm_power": "Power action on a VM", "vm_size": "Resize a powered-off VM", "vm_tags": "Set a VM's security tags",
    "snap_create": "Take a snapshot", "snap_revert": "Revert to the snapshot", "snap_delete": "Remove the snapshot",
    "edge_get": "Edge gateway and distributed firewall rules", "fw_add": "Add an edge firewall rule",
    "nat_add": "Add a NAT rule", "dfw_add": "Add a distributed firewall rule",
    "dfw_default": "Set the distributed firewall default action", "rule_delete": "Delete a rule",
    "check_inbound": "Explain whether inbound traffic is delivered",
    "check_east_west": "Explain whether VM-to-VM traffic is delivered", "tasks": "Recent platform tasks",
    "sync_now": "Sync every cloud account now", "sync_history": "Sync history",
    "resources": "Query the unified inventory", "resources_csv": "Export the inventory as CSV",
    "resources_summary": "Inventory totals", "resources_history": "Change history",
    "resources_relations": "Linked resources", "resources_orphans": "Disks and IPs whose instance is gone",
    "resource_action": "Act on an instance in any cloud", "costs": "Estimated monthly cost and waste",
    "policies_list": "List governance policies", "policies_add": "Add or replace a policy",
    "policies_run": "Run a policy (dry run by default)", "policies_runs": "Policy run history",
    "audit": "API audit trail", "openapi": "This OpenAPI document", "dashboard": "Web dashboard",
    "metrics": "Prometheus metrics",
}


def _parse_doc(doc: str) -> dict[tuple[str, str], dict]:
    """Read the route list in this module's docstring: method, path, {body}, access codes, note."""
    out: dict[tuple[str, str], dict] = {}
    for line in (doc or "").replace("?<same filters>", "").replace("/ or /dashboard", "/").splitlines():
        m = _DOC_LINE.match(line)
        if not m:
            continue
        method, path, rest = m.group(1), m.group(2).split("?")[0], m.group(3).strip()
        body = ""
        if rest.startswith("{"):
            body, rest = rest[: rest.index("}") + 1], rest[rest.index("}") + 1:].strip()
        access = []
        while rest:
            word, _, tail = rest.partition(" ")
            if word.rstrip(",") in _ACCESS:
                access.append(_ACCESS[word.rstrip(",")])
                rest = tail.strip()
            else:
                break
        note = rest.strip(" ()-")
        paths = [path]
        alt = re.search(r"\{([\w|]+\|[\w|]+)\}", path)
        if alt:
            paths = [path.replace(alt.group(0), a) for a in alt.group(1).split("|")]
        for pth in paths:
            out[(method, pth)] = {"summary": note, "body": body, "access": access}
    return out


def openapi_spec(routes: list, doc: str) -> dict:
    """Build an OpenAPI 3.0 document from the route table; summaries come from the module docstring."""
    notes = _parse_doc(doc)
    paths: dict[str, dict] = {}
    for method, pattern, fn, needs_auth in routes:
        alternation = re.search(r"\(\?P<action>([\w|]+)\)", pattern)
        variants = [pattern.replace(alternation.group(0), a) for a in alternation.group(1).split("|")] \
            if alternation else [pattern]
        for variant in variants:
            if variant == r"/(?:dashboard)?":
                variant = "/"
            path = _PARAM.sub(lambda m: "{" + m.group(1) + "}", variant).replace("\\.", ".")
            params = re.findall(r"\{(\w+)\}", path)
            note = notes.get((method, path), {"summary": "", "body": "", "access": []})
            summary = SUMMARIES.get(fn.__name__, f"{method} {path}")
            if fn.__name__ == "resource_action":
                summary = f"{path.rsplit('/', 1)[1].capitalize()} an instance in any cloud"
            description = "Access: " + ("; ".join(note["access"]) or "see summary") + "."
            if note["body"]:
                description += f" Body: `{note['body']}`."
            if note["summary"] and not note["summary"].startswith(method):
                description += f" {note['summary'][0].upper()}{note['summary'][1:]}."
            op = {
                "summary": summary,
                "description": description,
                "operationId": f"{method.lower()}{re.sub(r'[^A-Za-z0-9]+', '_', path).rstrip('_')}",
                "parameters": [{"name": p, "in": "path", "required": True, "schema": {"type": "string"}}
                               for p in params],
                "responses": {"200": {"description": "OK"}, "400": {"description": "Bad input"},
                              "401": {"description": "Missing or invalid API key"},
                              "403": {"description": "Role or org not allowed"},
                              "404": {"description": "Not found"}, "409": {"description": "Conflict"}},
            }
            if method in ("POST", "PUT"):
                op["requestBody"] = {"required": False, "content": {"application/json": {"schema": {"type": "object"}}}}
            if not needs_auth:
                op["security"] = []
            paths.setdefault(path, {})[method.lower()] = op
    return {
        "openapi": "3.0.3",
        "info": {"title": "Mini EvDC API", "version": __import__("evdc").__version__,
                 "description": "Simplified enterprise virtual data center + multi-cloud integration service."},
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"},
                                           "apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}},
        "security": [{"bearer": []}, {"apiKey": []}],
        "paths": dict(sorted(paths.items())),
    }


# exception -> HTTP status (first match wins, so subclasses go first)
ERROR_STATUS = [
    (Unauthorized, 401), (Forbidden, 403),
    (NotFound, 404), (ResourceNotFound, 404),
    (QuotaExceeded, 409), (InvalidState, 409), (SyncInProgress, 409), (IncorrectState, 409),
    (NotImplementedError, 501),
    (CloudApiError, 502),          # provider still failing after retries
    (EvdcError, 400), (KeyError, 400), (ValueError, 400), (TypeError, 400),
]


def make_handler(app: App):
    routes = [(m, re.compile(f"^{pat}$"), fn, needs_auth) for m, pat, fn, needs_auth in build_routes(app)]

    class Handler(BaseHTTPRequestHandler):
        server_version = "MiniEvDC/1.2"

        def _send(self, status: int, payload) -> None:
            ctype, extra = "application/json; charset=utf-8", {}
            if isinstance(payload, Raw):
                ctype, extra = payload.content_type, payload.headers
                payload = payload.body
            if payload is None:
                body = b""
            elif isinstance(payload, str):
                body = payload.encode()
            else:
                body = json.dumps(to_dict(payload), ensure_ascii=False).encode()
            self._status = status
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _key(self) -> str | None:
            header = self.headers.get("Authorization", "")
            if header.lower().startswith("bearer "):
                return header[7:].strip()
            return self.headers.get("X-API-Key")

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            path_matched = False
            for m, pattern, fn, needs_auth in routes:  # noqa: B007 - fn / needs_auth used after break
                match = pattern.match(url.path)
                if not match:
                    continue
                path_matched = True
                if m == method:
                    break
            else:
                return self._send(405 if path_matched else 404,
                                  {"error": "method not allowed" if path_matched else "route not found"})
            who = None
            try:
                who = app.auth.authenticate(self._key()) if needs_auth else None
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise EvdcError("request body too large")
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if not isinstance(body, dict):
                    raise EvdcError("request body must be a JSON object")
                status, payload = fn(Ctx(who, body, query), **match.groupdict())
                self._send(status, payload)
            except Exception as exc:  # noqa: BLE001 - mapped to a status below
                for exc_type, status in ERROR_STATUS:
                    if isinstance(exc, exc_type):
                        detail = f"{exc.provider} {exc.code}: {exc}" if isinstance(exc, CloudApiError) else str(exc)
                        return self._send(status, {"error": detail, "type": type(exc).__name__})
                log.exception("unhandled error", extra={"ctx": {"path": url.path}})
                self._send(500, {"error": "internal error"})
            finally:
                # audit every change and every refused request (who, what, result); reads are not logged
                status = getattr(self, "_status", 0)
                if method != "GET" or status in (401, 403):
                    app.store.audit(who.name if who else None, who.role if who else None, method, url.path,
                                    status, self.client_address[0])

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def log_message(self, fmt, *args):  # route access logs through the JSON logger
            log.info("http", extra={"ctx": {"client": self.client_address[0], "line": fmt % args}})

    return Handler


def serve(app: App, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(app))
