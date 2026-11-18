"""Tiny REST API on the standard library (no framework needed).

    GET    /health
    GET    /orgs                         POST /orgs                {name}
    POST   /orgs/{org}/vdcs              {name, region, quota:{vcpu, memory_gb, storage_gb}}
    GET    /vdcs/{vdc}/usage
    POST   /vdcs/{vdc}/networks          {name, cidr, type}
    POST   /vdcs/{vdc}/vms               {name, vcpu, memory_gb, disk_gb, os, network_id, power_on}
    GET    /vms
    POST   /vms/{vm}/power               {action}
    POST   /vms/{vm}/snapshot
    DELETE /vms/{vm}
    POST   /vdcs/{vdc}/firewall          {name, source, destination, port, action}
    POST   /vdcs/{vdc}/nat               {kind, original_ip, translated_ip, original_port, translated_port}
    POST   /vdcs/{vdc}/check-inbound     {client_ip, public_ip, port}
    GET    /tasks
    POST   /sync                         run multi-cloud sync now
    GET    /sync/history
    GET    /resources?provider=&status=  unified inventory across all clouds
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .app import App
from .errors import EvdcError, InvalidState, NotFound, QuotaExceeded
from .logs import get_logger
from .models import FirewallRule, NatRule, NetworkType, Quota, to_dict

log = get_logger("evdc.api")


def build_routes(app: App):
    p = app.platform

    def orgs_list(_b, _q):
        return 200, list(p.orgs.values())

    def orgs_create(b, _q):
        return 201, p.create_org(b["name"])

    def vdc_create(b, _q, org):
        return 201, p.create_vdc(org, b["name"], b.get("region", "huabei1"), Quota(**b["quota"]))

    def vdc_usage(_b, _q, vdc):
        return 200, p.usage(vdc)

    def net_create(b, _q, vdc):
        return 201, p.create_network(vdc, b["name"], b["cidr"], NetworkType(b.get("type", "routed")))

    def vm_create(b, _q, vdc):
        return 201, p.create_vm(vdc, b["name"], int(b["vcpu"]), int(b["memory_gb"]), int(b["disk_gb"]),
                                b.get("os", "Ubuntu 22.04"), b["network_id"], bool(b.get("power_on")))

    def vm_list(_b, _q):
        return 200, list(p.vms.values())

    def vm_power(b, _q, vm):
        return 200, p.power(vm, b["action"])

    def vm_snapshot(_b, _q, vm):
        return 201, p.create_snapshot(vm)

    def vm_delete(_b, _q, vm):
        p.delete_vm(vm)
        return 204, None

    def fw_add(b, _q, vdc):
        return 201, p.add_firewall_rule(vdc, FirewallRule(**b))

    def nat_add(b, _q, vdc):
        return 201, p.add_nat_rule(vdc, NatRule(**b))

    def check_inbound(b, _q, vdc):
        return 200, p.check_inbound(vdc, b["client_ip"], b["public_ip"], int(b["port"]))

    def tasks(_b, _q):
        return 200, p.tasks[-50:]

    def sync_now(_b, _q):
        return 200, [r.__dict__ for r in app.sync()]

    def sync_history(_b, _q):
        return 200, app.store.sync_history()

    def resources(_b, q):
        return 200, app.store.list_resources(q.get("provider"), q.get("status"))

    def health(_b, _q):
        return 200, {"status": "ok", "accounts": [f"{a.provider}:{a.account}" for a in app.adapters]}

    return [
        ("GET", r"/health", health),
        ("GET", r"/orgs", orgs_list),
        ("POST", r"/orgs", orgs_create),
        ("POST", r"/orgs/(?P<org>[\w-]+)/vdcs", vdc_create),
        ("GET", r"/vdcs/(?P<vdc>[\w-]+)/usage", vdc_usage),
        ("POST", r"/vdcs/(?P<vdc>[\w-]+)/networks", net_create),
        ("POST", r"/vdcs/(?P<vdc>[\w-]+)/vms", vm_create),
        ("POST", r"/vdcs/(?P<vdc>[\w-]+)/firewall", fw_add),
        ("POST", r"/vdcs/(?P<vdc>[\w-]+)/nat", nat_add),
        ("POST", r"/vdcs/(?P<vdc>[\w-]+)/check-inbound", check_inbound),
        ("GET", r"/vms", vm_list),
        ("POST", r"/vms/(?P<vm>[\w-]+)/power", vm_power),
        ("POST", r"/vms/(?P<vm>[\w-]+)/snapshot", vm_snapshot),
        ("DELETE", r"/vms/(?P<vm>[\w-]+)", vm_delete),
        ("GET", r"/tasks", tasks),
        ("POST", r"/sync", sync_now),
        ("GET", r"/sync/history", sync_history),
        ("GET", r"/resources", resources),
    ]


def make_handler(app: App):
    routes = [(m, re.compile(f"^{pat}$"), fn) for m, pat, fn in build_routes(app)]

    class Handler(BaseHTTPRequestHandler):
        server_version = "MiniEvDC/1.0"

        def _send(self, status: int, payload) -> None:
            body = b"" if payload is None else json.dumps(to_dict(payload), ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            for m, pattern, fn in routes:
                match = pattern.match(url.path)
                if m == method and match:
                    break
            else:
                return self._send(404, {"error": "route not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                status, payload = fn(body, query, **match.groupdict())
                self._send(status, payload)
            except NotFound as exc:
                self._send(404, {"error": str(exc)})
            except (QuotaExceeded, InvalidState) as exc:
                self._send(409, {"error": str(exc)})
            except (EvdcError, KeyError, ValueError, TypeError) as exc:
                self._send(400, {"error": f"{type(exc).__name__}: {exc}"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def log_message(self, fmt, *args):  # route access logs through JSON logger
            log.info("http", extra={"ctx": {"client": self.client_address[0], "line": fmt % args}})

    return Handler


def serve(app: App, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(app))
