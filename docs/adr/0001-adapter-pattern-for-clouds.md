# ADR 0001: One adapter per cloud behind a common interface

- Status: accepted
- Date: 2026-11-18

## Context
Alibaba Cloud, AWS and Azure return different shapes (nested wrappers, camelCase vs
PascalCase, MB vs MiB), paginate differently (page number + total, continuation token,
next-link URL) and authenticate differently (AK/SK, IAM keys, short-lived OAuth tokens).

## Decision
Every provider implements `ProviderAdapter`: `authenticate`, `iter_raw(kind)`,
`normalize(kind, raw)`, `get_instance`, `start`, `stop`, `reboot`. Adapters register by name.
All calls go through one `call()` that adds timeouts, retries and one re-auth. Sync, actions,
policies and the API only see `CloudResource`.

## Consequences
- A new provider is one file plus its fake for tests; nothing else changes.
- Provider quirks stay inside the adapter (e.g. EC2 has no CPU/memory per instance, so the
  AWS adapter looks up instance types and caches them).
- Same pattern as Apache Libcloud drivers, so it is familiar to reviewers.
