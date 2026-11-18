"""Monthly cost estimates per resource (inspired by Komiser's per-resource cost breakdown).

The numbers below are ILLUSTRATIVE list prices in USD, rounded, so the
feature can be demonstrated offline. Replace PRICES with real price-list data
(AWS Price List API, Aliyun BSS, Azure Retail Prices API) for real use.

Rules
- instance: (vCPU * per_vcpu + GB * per_gb) * 730 h, only while running/pending.
  A stopped instance costs nothing for compute (its disks still cost money).
- disk:     size_gb * per_gb_month by category, whether attached or not.
- eip:      flat monthly fee; an *unattached* public IP is pure waste.
- vpc:      free.
"""
from __future__ import annotations

from .models import CloudResource, ResourceStatus

HOURS_PER_MONTH = 730

PRICES: dict[str, dict] = {
    "aliyun": {"vcpu_hour": 0.021, "gb_hour": 0.0028, "eip_month": 3.0,
               "disk_gb_month": {"cloud_essd": 0.14, "cloud_efficiency": 0.05, "default": 0.08}},
    "aws": {"vcpu_hour": 0.024, "gb_hour": 0.0030, "eip_month": 3.65,
            "disk_gb_month": {"gp3": 0.096, "st1": 0.054, "default": 0.10}},
    "azure": {"vcpu_hour": 0.023, "gb_hour": 0.0031, "eip_month": 3.65,
              "disk_gb_month": {"Premium_LRS": 0.15, "Standard_LRS": 0.05, "default": 0.10}},
    "evdc": {"vcpu_hour": 0.018, "gb_hour": 0.0025, "eip_month": 0.0,
             "disk_gb_month": {"default": 0.06}},
}

_BILLED_COMPUTE = {ResourceStatus.RUNNING, ResourceStatus.PENDING, ResourceStatus.STOPPING}


def monthly_cost(r: CloudResource) -> float:
    price = PRICES.get(r.provider)
    if price is None or r.status == ResourceStatus.DELETED:
        return 0.0
    if r.resource_type == "instance":
        if r.status not in _BILLED_COMPUTE:
            return 0.0
        hourly = (r.vcpu or 0) * price["vcpu_hour"] + (r.memory_gb or 0) * price["gb_hour"]
        cost = hourly * HOURS_PER_MONTH
        if r.provider == "evdc":  # EvDC VMs carry their own disk
            cost += (r.attributes.get("disk_gb") or 0) * price["disk_gb_month"]["default"]
        return round(cost, 2)
    if r.resource_type == "disk":
        table = price["disk_gb_month"]
        rate = table.get(r.attributes.get("category") or "", table["default"])
        return round((r.size_gb or 0) * rate, 2)
    if r.resource_type == "eip":
        return round(price["eip_month"], 2)
    return 0.0


def waste_reason(r: CloudResource) -> str | None:
    """Why this resource is probably money for nothing, or None."""
    if r.resource_type == "disk" and r.status == ResourceStatus.AVAILABLE:
        return "unattached disk"
    if r.resource_type == "eip" and r.status == ResourceStatus.AVAILABLE:
        return "unattached public IP"
    return None


def cost_report(resources: list[CloudResource], top: int = 10) -> dict:
    live = [r for r in resources if r.status != ResourceStatus.DELETED]
    rows = [(r, monthly_cost(r)) for r in live]
    by_provider: dict[str, float] = {}
    by_type: dict[str, float] = {}
    for r, c in rows:
        by_provider[r.provider] = round(by_provider.get(r.provider, 0) + c, 2)
        by_type[r.resource_type] = round(by_type.get(r.resource_type, 0) + c, 2)
    waste = [{"uid": r.uid, "name": r.name, "provider": r.provider, "type": r.resource_type,
              "reason": waste_reason(r), "monthly_cost": c} for r, c in rows if waste_reason(r)]
    waste.sort(key=lambda w: -w["monthly_cost"])
    top_rows = sorted(rows, key=lambda rc: -rc[1])[:top]
    return {
        "currency": "USD",
        "note": "illustrative list prices; see evdc/pricing.py",
        "total_monthly": round(sum(c for _, c in rows), 2),
        "by_provider": by_provider,
        "by_type": by_type,
        "waste_monthly": round(sum(w["monthly_cost"] for w in waste), 2),
        "waste": waste,
        "top": [{"uid": r.uid, "name": r.name, "provider": r.provider, "type": r.resource_type,
                 "monthly_cost": c} for r, c in top_rows],
    }
