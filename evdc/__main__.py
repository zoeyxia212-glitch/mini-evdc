"""Command line.

    python -m evdc demo                      end-to-end walkthrough in the terminal
    python -m evdc serve [--config accounts.json] [--db evdc.db] [--port 8080] [--sync-interval 300]
    python -m evdc create-user NAME --role org_admin --org ORG_ID [--db evdc.db]
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

from .app import App
from .clouds.fake import aliyun_throttle
from .errors import TransientError
from .logs import setup_logging
from .metrics import render as render_metrics
from .models import DistributedRule, FirewallRule, NatRule, Quota
from .pricing import cost_report


def _accounts(fast: bool = True) -> list[dict]:
    retry = {"base_delay": 0.05} if fast else {}
    return [
        {"provider": "aliyun", "account": "ali-prod", "fake": True, "page_size": 10, "retry": retry,
         "credentials": {"access_key_id": "LTAI-demo", "access_key_secret": "secret-demo"}},
        {"provider": "aws", "account": "aws-syd", "fake": True, "page_size": 5, "retry": retry,
         "credentials": {"access_key": "AKIA-demo", "secret_key": "secret-demo"}},
        {"provider": "azure", "account": "az-au", "fake": True, "page_size": 5, "retry": retry,
         "credentials": {"tenant_id": "tenant-demo", "client_id": "app-demo", "client_secret": "secret-demo"}},
    ]


def demo() -> None:
    setup_logging("ERROR")  # keep the demo output readable
    db = os.path.join(tempfile.mkdtemp(), "demo.db")
    app = App(db)
    p = app.platform

    print("== 1. Tenant set-up: org -> VDC -> network -> template -> VMs ==")
    org = p.create_org("acme")
    vdc = p.create_vdc(org.id, "acme-prod", "huabei1", Quota(vcpu=16, memory_gb=64, storage_gb=600))
    net = p.create_network(vdc.id, "app-net", "192.168.10.0/24")
    tpl = p.create_template(org.id, "ubuntu-small", "Ubuntu 22.04", 2, 4, 40)
    web = p.create_vm_from_template(vdc.id, "web-01", tpl.id, net.id, power_on=True, tags=["web"])
    db_vm = p.create_vm(vdc.id, "db-01", 4, 16, 200, "CentOS 7", net.id, power_on=True, tags=["db"])
    print(f"  {web.name} {web.ip} {web.tags}  |  {db_vm.name} {db_vm.ip} {db_vm.tags}")
    print("  usage:", p.usage(vdc.id)["used"], "of", p.usage(vdc.id)["quota"])

    print("\n== 2. Quota check ==")
    try:
        p.create_vm(vdc.id, "big-01", 16, 64, 100, "Ubuntu 22.04", net.id)
    except Exception as exc:
        print("  refused:", exc)

    print("\n== 3. Edge gateway (north-south): NAT + firewall are both needed ==")
    p.add_nat_rule(vdc.id, NatRule("DNAT", "203.0.113.10", web.ip, 443, 443))
    print("  NAT only     ->", p.check_inbound(vdc.id, "8.8.8.8", "203.0.113.10", 443)["result"])
    p.add_firewall_rule(vdc.id, FirewallRule("allow-https", "any", f"{web.ip}/32", 443, "allow"))
    print("  NAT + allow  ->", p.check_inbound(vdc.id, "8.8.8.8", "203.0.113.10", 443)["result"])

    print("\n== 4. Distributed firewall (east-west) by security tag ==")
    p.add_dfw_rule(vdc.id, DistributedRule("web-to-db", "tag:web", "tag:db", 5432, "allow"))
    p.set_dfw_default(vdc.id, "deny")
    print("  web -> db :5432 ->", p.check_east_west(web.id, db_vm.id, 5432)["result"])
    print("  db  -> web:22   ->", p.check_east_west(db_vm.id, web.id, 22)["result"])

    print("\n== 5. Persistence: a new process sees the same platform ==")
    again = App(db)
    print(f"  reloaded {len(again.platform.vms)} VMs, {len(again.platform.edges[vdc.id].dfw)} DFW rule(s)")

    print("\n== 6. Multi-cloud sync (instances, disks, VPCs, EIPs) with injected faults ==")
    app.load_accounts(_accounts())
    ali, aws, az = (a.client for a in app.adapters[1:])
    ali.faults.fail_next(aliyun_throttle, aliyun_throttle)              # throttled twice, then OK
    aws.faults.fail_next(lambda: TransientError("503 Service Unavailable", "aws"))
    for r in app.sync():
        print(f"  {r.provider:7} {r.account:9} {r.status:8} created={r.created:<3} skipped={r.skipped} {r.duration_s}s")

    print("\n== 7. Unified inventory ==")
    s = app.store.summary()
    print(f"  {s['total']} resources {s['by_type']}")
    print(f"  running capacity {s['running_vcpu']} vCPU / {s['running_memory_gb']} GB, disks {s['disk_gb']} GB")
    for provider in ("aliyun", "aws", "azure", "evdc"):
        for kind in ("instance", "disk", "vpc", "eip"):
            items = app.store.list_resources(provider=provider, resource_type=kind)
            if items:
                r = items[0]
                extra = r.attributes.get("cidr") or r.public_ip or (f"{r.size_gb:.0f} GB" if r.size_gb else "") \
                    or f"{r.vcpu}C/{r.memory_gb}G"
                print(f"    {provider:7} {kind:8} {r.name[:22]:22} {r.status.value:9} {extra}")

    print("\n== 8. Unified management + live status query ==")
    targets = {}
    for res in app.store.list_resources(status="stopped", resource_type="instance"):
        targets.setdefault(res.provider, res)
    for provider, res in targets.items():
        r = app.manager.start(res.uid)
        print(f"  start  {provider:7} {res.name:12} {r.previous} -> {r.status}")
    for client in (ali, aws, az):
        client.settle()  # the clouds finish booting
    for provider, res in targets.items():
        live = app.manager.refresh(res.uid)
        print(f"  refresh {provider:6} {res.name:12} -> {live.status.value}")
    running = app.store.list_resources(provider="aws", status="running", resource_type="instance")[0]
    print(f"  reboot aws     {running.name:12} -> {app.manager.reboot(running.uid).status}")

    print("\n== 9. Change history of one resource ==")
    uid = targets["aws"].uid
    for e in reversed(app.store.history(uid)):
        print(f"  {e['kind']:8} {e['detail']}")

    print("\n== 10. Second sync after a disk disappears from Aliyun ==")
    ali._disks.pop()
    az.expire_all_tokens()  # forces the Azure adapter to re-authenticate
    for r in app.sync():
        print(f"  {r.provider:7} {r.status:8} created={r.created} updated={r.updated} "
              f"unchanged={r.unchanged} deleted={r.deleted}")

    print("\n== 11. Cost estimate (Komiser-style, illustrative prices) ==")
    rep = cost_report(app.store.list_resources(), top=3)
    print(f"  total ≈ ${rep['total_monthly']}/month  by provider {rep['by_provider']}")
    print(f"  waste ≈ ${rep['waste_monthly']}/month on {len(rep['waste'])} idle disks / public IPs")
    for t in rep["top"]:
        print(f"    top: {t['provider']:7} {t['type']:8} {t['name'][:20]:20} ${t['monthly_cost']}")

    print("\n== 12. Governance policies (Cloud Custodian-style, dry run first) ==")
    for name in ("unattached-disks", "idle-public-ips", "untagged-instances", "stop-test-instances"):
        out = app.policies.run(name)
        print(f"  {name:20} scanned={out['scanned']:<3} matched={out['matched']:<3} "
              f"(${out['monthly_cost_of_matches']}/month)")
    out = app.policies.run("unattached-disks", dry_run=False)
    print(f"  applied unattached-disks: {out['matched']} disks marked delete-in-30d")

    print("\n== 13. Query + resource graph (CloudQuery / Cartography-style) ==")
    page, total = app.store.query(resource_type="instance", tag="env:prod", sort="-vcpu", limit=3)
    print(f"  env=prod instances: {total}; biggest: {[f'{r.name} {r.vcpu}C' for r in page]}")
    inst = app.store.list_resources(provider="aliyun", resource_type="instance")[0]
    for e in app.store.relations(inst.uid)["edges"]:
        print(f"  {inst.name} --{e['relation']}--> {e['name']} ({e['status']})")

    print("\n== 14. Prometheus metrics (GET /metrics), first lines ==")
    for line in render_metrics(app).splitlines()[:5]:
        print("  " + line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evdc", description="Mini EvDC")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    s = sub.add_parser("serve")
    s.add_argument("--config", help="accounts JSON file")
    s.add_argument("--db", default="evdc.db")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--sync-interval", type=float, default=0,
                   help="seconds between automatic syncs (0 = only on POST /sync)")
    u = sub.add_parser("create-user", help="create a user and print its API key")
    u.add_argument("name")
    u.add_argument("--role", default="viewer", choices=["system_admin", "org_admin", "viewer"])
    u.add_argument("--org", help="org id (not for system_admin)")
    u.add_argument("--db", default="evdc.db")
    args = parser.parse_args(argv)

    if args.cmd == "demo":
        demo()
        return 0

    if args.cmd == "create-user":
        key = App(args.db).auth.create_user(args.name, args.role, args.org)
        print(key)
        return 0

    from .api import serve
    setup_logging("INFO")
    app = App(args.db)
    key = app.bootstrap_admin()
    if key and os.environ.get("EVDC_ADMIN_KEY"):
        print("Created system admin 'admin' with the key from EVDC_ADMIN_KEY", file=sys.stderr)
    elif key:
        print(f"Created system admin 'admin'. API key (shown once): {key}", file=sys.stderr)
    if args.config:
        app.load_config(args.config)
    if args.sync_interval > 0:
        app.start_scheduler(args.sync_interval)
    server = serve(app, args.host, args.port)
    print(f"Mini EvDC API on http://{args.host}:{args.port}  (Ctrl+C to stop)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.scheduler:
            app.scheduler.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
