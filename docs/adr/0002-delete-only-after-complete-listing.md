# ADR 0002: Mark resources deleted only after a complete, successful listing

- Status: accepted
- Date: 2026-11-18

## Context
A sync that times out on page 3 has seen only part of an account. Treating everything it did
not see as deleted would wipe the inventory and trigger false alerts and policy actions.

## Decision
`SyncService` upserts as it goes but calls `mark_missing_deleted` only when the adapter has
listed every resource type without error. A failed run records its error and deletes nothing.

## Consequences
- Deletions show up one successful sync late, which is acceptable for inventory.
- Live status queries (`refresh`) can still mark a single instance deleted when the provider
  answers "not found" for that exact ID.
