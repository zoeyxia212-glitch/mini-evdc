"""REST API on the standard library (no framework needed).

Every route except GET /health needs an API key:
    Authorization: Bearer <key>      or      X-API-Key: <key>

Access: S = system_admin only, T = tenant (org_admin / viewer of the owning org, or system_admin),
        W = write (viewer refused), R = any authenticated user.

  Identity & users
    GET    /health                                    public
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
    POST   /vms/{vm}/power   {action}                 T W  power_on | power_off | shutdown_guest | reset | suspend | discard_suspend
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
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .app import App
from .auth import AuthService, Forbidden, Principal, Unauthorized
from .errors import (CloudApiError, EvdcError, IncorrectState, InvalidState, NotFound, QuotaExceeded,
                     ResourceNotFound, SyncInProgress)
from .logs import get_logger
from .models import DistributedRule, FirewallRule, NatRule, NetworkType, Quota, to_dict

log = get_logger("evdc.api")
MAX_BODY = 1_000_000


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

    def resources(c):
        q = c.query
        return 200, store.list_resources(q.get("provider"), q.get("status"), q.get("type"), q.get("account"))

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
    return [
        ("GET", r"/health", health, False),
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
    ]


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
            body = b"" if payload is None else json.dumps(to_dict(payload), ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
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
            for m, pattern, fn, needs_auth in routes:
                match = pattern.match(url.path)
                if not match:
                    continue
                path_matched = True
                if m == method:
                    break
            else:
                return self._send(405 if path_matched else 404,
                                  {"error": "method not allowed" if path_matched else "route not found"})
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
