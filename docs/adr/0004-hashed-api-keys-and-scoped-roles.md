# ADR 0004: Hashed API keys with role and org scoping

- Status: accepted
- Date: 2026-11-18

## Context
Tenants must never see or change other tenants' objects; the provider needs full control;
some users need read-only access. Keys leak through logs, screenshots and backups.

## Decision
Random keys (`evdc_` + 32 bytes, URL-safe), shown once, stored as SHA-256. Three roles
(system_admin, org_admin, viewer) modelled on CloudStack / vCloud Director. Authorisation is
checked per route against the owning org of the target object. Every write and every 401 /
403 is audited.

## Consequences
- Lost keys cannot be recovered, only replaced.
- Roles are fixed in v1; custom roles, expiring and scoped keys and OIDC single sign-on are on
  the roadmap.
