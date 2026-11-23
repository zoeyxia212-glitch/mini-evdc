# ADR 0003: Standard library for v1, FastAPI + PostgreSQL for the enterprise edition

- Status: accepted (v1); migration planned
- Date: 2026-11-23

## Context
v1 had to run anywhere with no installs, so a reviewer can clone, run the demo and the tests
in seconds. That rules out frameworks and database servers. Enterprise use needs things the
standard library does not provide well: request validation and generated API docs, connection
pooling, schema migrations, horizontal scaling, OIDC, tracing.

## Decision
- v1 (current): `http.server` + `sqlite3`, layered so services never import the HTTP layer.
- Enterprise edition: FastAPI (validation, OpenAPI, dependency-injected auth), SQLAlchemy 2 +
  Alembic on PostgreSQL, a separate worker process for sync and long-running actions,
  OpenTelemetry. Service and adapter code is reused unchanged; the store gets a repository
  interface with SQLite and PostgreSQL implementations.

## Consequences
- The migration replaces `api.py` and `store.py`, not the business logic.
- The test suite keeps a fast in-process mode (SQLite) and gains an integration mode
  (PostgreSQL in CI via a service container).
