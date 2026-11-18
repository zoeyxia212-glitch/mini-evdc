"""Governance policies, in the spirit of Cloud Custodian: select resources with
filters, then run actions on what matches.

A policy is plain JSON:

    {
      "name": "unattached-disks",
      "resource": "disk",                         # instance | disk | vpc | eip
      "description": "Disks nobody is using",
      "filters": [
        {"key": "status", "op": "eq", "value": "available"},
        {"key": "monthly_cost", "op": "gt", "value": 5}
      ],
      "actions": ["report", {"type": "mark", "label": "delete-in-30d"}]
    }

Filter keys: any CloudResource field (status, provider, region, name, vcpu ...),
`tag:<key>`, `attributes.<key>`, `monthly_cost`, `marked` (labels set by the mark action).
Operators: eq ne in not_in gt gte lt lte contains regex present absent.

Actions:
    report                      always; lists the matches
    mark   {label}              store a label on the resource (Custodian's mark-for-op)
    unmark {label}
    stop                        stop matching instances through the ResourceManager

Policies run as a **dry run by default**: matches are reported and nothing is
changed until the caller passes dry_run=False.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .errors import EvdcError
from .logs import get_logger
from .models import RESOURCE_TYPES, CloudResource, ResourceStatus
from .pricing import monthly_cost

log = get_logger("evdc.policy")

OPS = {"eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "contains", "regex", "present", "absent"}
ACTIONS = {"report", "mark", "unmark", "stop"}


@dataclass
class Policy:
    name: str
    resource: str
    filters: list[dict] = field(default_factory=list)
    actions: list[Any] = field(default_factory=lambda: ["report"])
    description: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> Policy:
        if not isinstance(d, dict):
            raise EvdcError("a policy must be a JSON object")
        unknown = set(d) - {"name", "resource", "filters", "actions", "description"}
        if unknown:
            raise EvdcError(f"unknown policy field(s): {sorted(unknown)}")
        p = cls(name=d.get("name", ""), resource=d.get("resource", ""), filters=list(d.get("filters", [])),
                actions=list(d.get("actions", ["report"])), description=d.get("description", ""))
        p.validate()
        return p

    def validate(self) -> None:
        if not re.match(r"^[a-z0-9][a-z0-9-]{1,63}$", self.name):
            raise EvdcError("policy name: lowercase letters, digits and hyphens")
        if self.resource not in RESOURCE_TYPES:
            raise EvdcError(f"resource must be one of {RESOURCE_TYPES}")
        for f in self.filters:
            if not isinstance(f, dict) or "key" not in f or f.get("op", "eq") not in OPS:
                raise EvdcError(f"bad filter {f!r}; need key and op in {sorted(OPS)}")
            if f.get("op") == "regex":
                re.compile(str(f.get("value", "")))
        for a in self.actions:
            kind = a if isinstance(a, str) else a.get("type") if isinstance(a, dict) else None
            if kind not in ACTIONS:
                raise EvdcError(f"bad action {a!r}; use one of {sorted(ACTIONS)}")
            if kind in ("mark", "unmark") and not (isinstance(a, dict) and a.get("label")):
                raise EvdcError(f"{kind} needs a label")
            if kind == "stop" and self.resource != "instance":
                raise EvdcError("stop only applies to instances")


def _value(r: CloudResource, key: str, marks: dict[str, list[str]]) -> Any:
    if key == "monthly_cost":
        return monthly_cost(r)
    if key == "marked":
        return marks.get(r.uid) or None
    if key.startswith("tag:"):
        return r.tags.get(key[4:])
    if key.startswith("attributes."):
        return r.attributes.get(key[len("attributes."):])
    if key == "uid":
        return r.uid
    v = getattr(r, key, None)
    return v.value if isinstance(v, ResourceStatus) else v


def matches(r: CloudResource, filters: list[dict], marks: dict[str, list[str]] | None = None) -> bool:
    marks = marks or {}
    for f in filters:
        op, want = f.get("op", "eq"), f.get("value")
        got = _value(r, f["key"], marks)
        if op == "present":
            ok = got not in (None, "", [], {})
        elif op == "absent":
            ok = got in (None, "", [], {})
        elif op == "eq":
            ok = got == want
        elif op == "ne":
            ok = got != want
        elif op == "in":
            ok = got in (want or [])
        elif op == "not_in":
            ok = got not in (want or [])
        elif op == "contains":
            ok = got is not None and str(want) in (got if isinstance(got, list) else str(got))
        elif op == "regex":
            ok = got is not None and re.search(str(want), str(got)) is not None
        else:  # numeric comparisons
            if got is None:
                return False
            try:
                g, w = float(got), float(want)
            except (TypeError, ValueError):
                return False
            ok = {"gt": g > w, "gte": g >= w, "lt": g < w, "lte": g <= w}[op]
        if not ok:
            return False
    return True


class PolicyEngine:
    def __init__(self, store, manager_factory):
        self.store = store
        self.manager_factory = manager_factory  # callable -> ResourceManager (fresh adapters list)
        self.policies: dict[str, Policy] = {}

    def load(self, items: list[dict]) -> list[Policy]:
        loaded = [Policy.from_dict(d) for d in items]
        for p in loaded:
            self.policies[p.name] = p
        return loaded

    def load_file(self, path: str) -> list[Policy]:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return self.load(data["policies"] if isinstance(data, dict) else data)

    def run(self, policy: Policy | str, dry_run: bool = True) -> dict:
        if isinstance(policy, str):
            if policy not in self.policies:
                raise EvdcError(f"no policy called {policy}")
            policy = self.policies[policy]
        resources = [r for r in self.store.list_resources(resource_type=policy.resource)
                     if r.status != ResourceStatus.DELETED]
        marks = self.store.marks()
        hits = [r for r in resources if matches(r, policy.filters, marks)]
        results = []
        for r in hits:
            done = []
            for a in policy.actions:
                kind = a if isinstance(a, str) else a["type"]
                if kind == "report":
                    continue
                if dry_run:
                    done.append(f"would {kind}" + (f" {a['label']}" if kind in ("mark", "unmark") else ""))
                    continue
                try:
                    if kind == "mark":
                        self.store.add_mark(r.uid, a["label"], policy.name)
                    elif kind == "unmark":
                        self.store.remove_mark(r.uid, a["label"])
                    elif kind == "stop":
                        if r.status == ResourceStatus.RUNNING:
                            self.manager_factory().stop(r.uid)
                        else:
                            done.append("stop skipped (not running)")
                            continue
                    done.append(kind + (f" {a['label']}" if kind in ("mark", "unmark") else ""))
                except Exception as exc:  # noqa: BLE001 - one failure must not stop the run
                    done.append(f"{kind} failed: {exc}")
            results.append({"uid": r.uid, "name": r.name, "provider": r.provider, "status": r.status.value,
                            "monthly_cost": monthly_cost(r), "actions": done})
        self.store.record_policy_run(policy.name, dry_run, len(resources), len(results))
        log.info("policy run", extra={"ctx": {"policy": policy.name, "dry_run": dry_run,
                                              "scanned": len(resources), "matched": len(results)}})
        return {"policy": policy.name, "resource": policy.resource, "dry_run": dry_run,
                "scanned": len(resources), "matched": len(results),
                "monthly_cost_of_matches": round(sum(x["monthly_cost"] for x in results), 2),
                "results": results}


DEFAULT_POLICIES = [
    {"name": "unattached-disks", "resource": "disk",
     "description": "Disks not attached to any instance (Custodian's classic EBS garbage-collect)",
     "filters": [{"key": "status", "op": "eq", "value": "available"}],
     "actions": ["report", {"type": "mark", "label": "delete-in-30d"}]},
    {"name": "idle-public-ips", "resource": "eip",
     "description": "Public IPs that are paid for but not attached",
     "filters": [{"key": "status", "op": "eq", "value": "available"}],
     "actions": ["report", {"type": "mark", "label": "release"}]},
    {"name": "untagged-instances", "resource": "instance",
     "description": "Instances without an env tag (tag compliance)",
     "filters": [{"key": "tag:env", "op": "absent"}, {"key": "provider", "op": "ne", "value": "evdc"}],
     "actions": ["report"]},
    {"name": "stop-test-instances", "resource": "instance",
     "description": "Off-hours: stop running instances tagged env=test",
     "filters": [{"key": "tag:env", "op": "eq", "value": "test"}, {"key": "status", "op": "eq", "value": "running"}],
     "actions": ["report", "stop"]},
]
