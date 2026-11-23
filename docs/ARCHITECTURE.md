# Architecture

Mini EvDC is two products in one process, sharing auth, storage and observability:

1. **Platform** – a multi-tenant virtual data center (orgs, VDCs, networks, VMs, edge gateway,
   distributed firewall), modelled on EvDC 1.0 / VMware vCloud Director.
2. **Multi-cloud integration service** – keeps a unified inventory of Alibaba Cloud, AWS, Azure
   and the platform itself, and lets operators query, govern and act on it.

```
 clients ──► HTTP API (api.py) ──► AuthService (auth.py) ── API key → Principal(role, org)
               │   audit trail, error mapping, OpenAPI, dashboard, /metrics, /healthz, /readyz
               │
   ┌───────────┼──────────────────────────────┬─────────────────────────────┐
   ▼           ▼                              ▼                             ▼
 EvdcPlatform  SyncService ─► ProviderAdapter  ResourceManager              PolicyEngine / pricing
 (core.py)     (sync.py)      (adapters/)      (manage.py)                  (policy.py, pricing.py)
   │             │              │ retry.py: timeout, backoff, re-auth        │
   │             │              ▼                                            │
   │             │        cloud clients: fake (tests/demo) or real SDK       │
   │             │        (clouds/awsreal.py, aliyunreal.py, azurereal.py)   │
   ▼             ▼                                                           ▼
 Store (store.py, SQLite): platform state · inventory · change history · marks · sync / policy runs · users · audit
```

## Layers

| Layer | Modules | Responsibility | Depends on |
|---|---|---|---|
| Interface | `api.py`, `web/dashboard.html`, `__main__.py` | HTTP, authentication, request validation, error → status, CLI | services |
| Services | `core.py`, `sync.py`, `manage.py`, `policy.py`, `pricing.py`, `auth.py` | business rules | domain, ports |
| Domain | `models.py`, `errors.py` | data classes, enums, error types | nothing |
| Ports | `adapters/base.py` (`ProviderAdapter`), `store.py` | interfaces to clouds and storage | domain |
| Adapters | `adapters/*.py`, `clouds/*.py` | provider-specific mapping, SDK calls | ports, SDKs |
| Cross-cutting | `retry.py`, `logs.py`, `metrics.py` | resilience, logging, metrics | – |

Services never import the HTTP layer, so the API can be replaced (see ADR 0003) without
touching business rules.

## Key flows

**Sync.** `SyncService` runs one thread per account. Each adapter authenticates, pages through
every resource type, maps raw records to `CloudResource` and the store upserts by
`uid = provider:account:native_id`, recording field-level diffs as change events. Missing
resources are marked deleted **only after a complete listing** (ADR 0002). A lock refuses
overlapping runs; `SyncScheduler` triggers runs on an interval.

**Action on a cloud resource.** `POST /resources/stop {uid}` → system-admin check →
`ResourceManager` looks the uid up, refuses invalid transitions before calling the cloud,
finds the adapter that owns the account, calls the provider through the retry path, records the
transitional status and an `action` event. The next sync records the final state.

**Tenant request on the platform.** `POST /vdcs/{vdc}/vms` → the caller's org must own the VDC
and the role must allow writes → `EvdcPlatform.create_vm` validates name, size and quota,
allocates an IP, records a Task and persists the platform state.

## Multi-tenancy and authorisation

Org → VDC → network / VM; templates per org. Every request carries an API key that resolves to
a `Principal(name, role, org_id)`. `AuthService.require` enforces three rules on every route:
system-only operations, org ownership of the target object, and read-only for viewers.
Lists are filtered by the caller's org. Every write and every refused request is audited.

## Resilience

| Failure | Behaviour |
|---|---|
| Provider throttling / 5xx / timeout | exponential backoff with full jitter, per-call timeout |
| Expired OAuth token (Azure) | refresh before expiry; one re-auth on 401 |
| One account down | other accounts keep syncing; failure recorded and exported as a metric |
| Sync fails half-way | nothing is marked deleted |
| Malformed record | skipped and counted, sync continues |
| SIGTERM | readiness off → grace period → stop accepting → finish in-flight → close store |

## Observability

JSON-lines logs with provider / account / operation / attempt; Prometheus metrics for
inventory, cost and sync health; alert rules and a Grafana dashboard in `deploy/`; probes for
orchestrators; an API audit trail and per-resource change history.

## Decisions

See [docs/adr](adr/). Planned evolution: [docs/ROADMAP.md](ROADMAP.md).
