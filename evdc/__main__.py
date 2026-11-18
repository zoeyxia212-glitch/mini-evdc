"""Command line.

    python -m evdc demo                      # end-to-end walkthrough in the terminal
    python -m evdc serve --config accounts.json --port 8080 --db evdc.db
"""
from __future__ import annotations

import argparse
import sys

from .app import App
from .clouds.fake import aliyun_throttle, aws_throttle
from .errors import TransientError
from .logs import setup_logging
from .models import FirewallRule, NatRule, Quota


def demo() -> None:
    setup_logging("WARNING")  # keep the demo output readable; retries still show
    app = App()
    p = app.platform

    print("== 1. Tenant set-up (org -> VDC -> network -> VMs) ==")
    org = p.create_org("acme")
    vdc = p.create_vdc(org.id, "acme-prod", "huabei1", Quota(vcpu=16, memory_gb=64, storage_gb=500))
    net = p.create_network(vdc.id, "web-net", "192.168.10.0/24")
    web = p.create_vm(vdc.id, "web-01", 2, 4, 40, "Ubuntu 22.04", net.id, power_on=True)
    db = p.create_vm(vdc.id, "db-01", 4, 16, 200, "CentOS 7", net.id, power_on=True)
    print(f"  {web.name} {web.ip}  |  {db.name} {db.ip}")
    print("  usage:", p.usage(vdc.id)["used"], "of", p.usage(vdc.id)["quota"])

    print("\n== 2. Quota check ==")
    try:
        p.create_vm(vdc.id, "big-01", 16, 64, 100, "Ubuntu 22.04", net.id)
    except Exception as exc:
        print("  refused:", exc)

    print("\n== 3. Edge gateway: NAT + firewall (both are needed) ==")
    p.add_nat_rule(vdc.id, NatRule("DNAT", "203.0.113.10", web.ip, 443, 443))
    print("  NAT only     ->", p.check_inbound(vdc.id, "8.8.8.8", "203.0.113.10", 443))
    p.add_firewall_rule(vdc.id, FirewallRule("allow-https", "any", f"{web.ip}/32", 443, "allow"))
    print("  NAT + allow  ->", p.check_inbound(vdc.id, "8.8.8.8", "203.0.113.10", 443))

    print("\n== 4. Multi-cloud sync with injected faults ==")
    app.load_accounts([
        {"provider": "aliyun", "account": "ali-prod", "fake": True, "page_size": 10,
         "credentials": {"access_key_id": "LTAI-demo", "access_key_secret": "secret-demo"},
         "retry": {"base_delay": 0.05, "max_attempts": 4}},
        {"provider": "aws", "account": "aws-syd", "fake": True, "page_size": 5,
         "credentials": {"access_key": "AKIA-demo", "secret_key": "secret-demo"},
         "retry": {"base_delay": 0.05}},
        {"provider": "azure", "account": "az-au", "fake": True, "page_size": 5,
         "credentials": {"tenant_id": "tenant-demo", "client_id": "app-demo", "client_secret": "secret-demo"},
         "retry": {"base_delay": 0.05}},
    ])
    ali, aws, az = (a.client for a in app.adapters[1:])
    ali.faults.fail_next(aliyun_throttle, aliyun_throttle)              # throttled twice, then OK
    aws.faults.fail_next(lambda: TransientError("503 Service Unavailable", "aws"))
    print("  (retry warnings above/below are expected: throttling and a 503 were injected)")
    for r in app.sync():
        print(f"  {r.provider:7} {r.account:9} {r.status:8} created={r.created:<3} "
              f"deleted={r.deleted} skipped={r.skipped} {r.duration_s}s")

    print("\n== 5. Unified inventory (same shape for every cloud) ==")
    rows = app.store.list_resources()
    by_provider: dict[str, list] = {}
    for r in rows:
        by_provider.setdefault(r.provider, []).append(r)
    for provider, items in by_provider.items():
        running = sum(r.status.value == "running" for r in items)
        print(f"  {provider:7} {len(items):3} instances, {running} running")
    print("  sample:")
    for provider, items in by_provider.items():
        r = items[0]
        print(f"    {r.uid[:45]:45} {r.name:12} {r.status.value:8} {r.vcpu}C/{r.memory_gb}G {r.private_ip}")

    print("\n== 6. Second sync after an instance disappears from Aliyun ==")
    ali.remove(next(r.native_id for r in rows if r.provider == "aliyun"))
    az.expire_all_tokens()  # forces the Azure adapter to re-authenticate
    for r in app.sync():
        print(f"  {r.provider:7} {r.status:8} created={r.created} updated={r.updated} "
              f"unchanged={r.unchanged} deleted={r.deleted}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evdc", description="Mini EvDC")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    s = sub.add_parser("serve")
    s.add_argument("--config", help="accounts JSON file")
    s.add_argument("--db", default="evdc.db")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    if args.cmd == "demo":
        demo()
        return 0

    from .api import serve
    setup_logging("INFO")
    app = App(args.db)
    if args.config:
        app.load_config(args.config)
    server = serve(app, args.host, args.port)
    print(f"Mini EvDC API on http://{args.host}:{args.port}  (Ctrl+C to stop)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
