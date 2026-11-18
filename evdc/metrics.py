"""Prometheus metrics in the text exposition format (scrape GET /metrics).

Exposes inventory counts, estimated monthly cost and the result of the last
sync per account, so Grafana can chart the multi-cloud estate and alert when
a provider's sync starts failing.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

from .models import ResourceStatus
from .pricing import monthly_cost


def _esc(v: str) -> str:
    """Escape a label value: backslash, double quote, newline (Prometheus text format)."""
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**kv) -> str:
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in kv.items()) + "}"


def render(app) -> str:
    lines: list[str] = []

    def metric(name: str, kind: str, help_text: str, samples: list[tuple[dict, float]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            lines.append(f"{name}{_labels(**labels) if labels else ''} {value}")

    resources = [r for r in app.store.list_resources() if r.status != ResourceStatus.DELETED]
    counts = Counter((r.provider, r.account, r.resource_type, r.status.value) for r in resources)
    metric("evdc_resources", "gauge", "Resources in the unified inventory",
           [({"provider": p, "account": a, "type": t, "status": s}, n) for (p, a, t, s), n in sorted(counts.items())])

    cost: Counter = Counter()
    for r in resources:
        cost[(r.provider, r.resource_type)] += monthly_cost(r)
    metric("evdc_monthly_cost_usd", "gauge", "Estimated monthly cost (illustrative prices)",
           [({"provider": p, "type": t}, round(v, 2)) for (p, t), v in sorted(cost.items())])

    latest: dict[tuple[str, str], dict] = {}
    for run in app.store.sync_history(limit=1000):
        latest.setdefault((run["provider"], run["account"]), run)
    ok, ts = [], []
    for (p, a), run in sorted(latest.items()):
        ok.append(({"provider": p, "account": a}, 1 if run["status"] == "success" else 0))
        try:
            ts.append(({"provider": p, "account": a}, int(datetime.fromisoformat(run["finished_at"]).timestamp())))
        except (TypeError, ValueError):
            pass
    metric("evdc_sync_success", "gauge", "1 if the last sync of this account succeeded", ok)
    metric("evdc_sync_last_timestamp_seconds", "gauge", "When the last sync of this account finished", ts)

    p = app.platform
    metric("evdc_platform_objects", "gauge", "Objects on the Mini EvDC platform",
           [({"kind": k}, len(v)) for k, v in (("org", p.orgs), ("vdc", p.vdcs), ("network", p.networks),
                                                ("vm", p.vms), ("template", p.templates))])
    return "\n".join(lines) + "\n"
