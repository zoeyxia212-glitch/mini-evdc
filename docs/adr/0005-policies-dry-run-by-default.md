# ADR 0005: Governance policies run as a dry run by default

- Status: accepted
- Date: 2026-11-18

## Context
Policies can stop instances and mark resources for deletion across several clouds. A wrong
filter applied blindly is an outage.

## Decision
`POST /policies/run` previews unless `dry_run: false` is sent explicitly; any signed-in user may
preview, only a system admin may act. Destructive clean-up is two-step (mark, then act on
`marked`), as in Cloud Custodian's mark-for-op.

## Consequences
- Operators see the blast radius (matches and their monthly cost) before acting.
- Automation must opt in to changes explicitly.
