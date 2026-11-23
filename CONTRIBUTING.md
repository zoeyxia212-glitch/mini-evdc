# Contributing

## Branches

This repository uses `main` + `dev` + `feature/*`:

| Branch | Purpose | How it changes |
|---|---|---|
| `main` | released, always deployable | pull request from `dev` only; tagged `vX.Y.Z` on release |
| `dev` | integration of finished features | pull requests from `feature/*` |
| `feature/<short-name>` | one change | branched from `dev`, deleted after merge |

Both `main` and `dev` are protected: a pull request and green checks (lint, tests on
Python 3.10–3.13, Docker, CodeQL, security) are required before merging.

## Local workflow

```bash
git switch dev && git pull
git switch -c feature/<short-name>
pip install pre-commit ruff && pre-commit install     # once
python -m unittest discover -s tests -p "test*.py" -t .
ruff check evdc tests
git push -u origin feature/<short-name>              # then open a PR into dev
```

## Commit messages

Imperative mood, short subject (≤ 72 characters), body explaining *why*:

```
Add lease expiry to VMs

Tenants forget test machines. A lease lets the platform stop them
automatically after N days and records the action in the audit trail.
```

Conventional prefixes (`feat:`, `fix:`, `docs:`, `ci:`, `refactor:`) are welcome and make
release notes easier to read.

## Pull requests

- one topic per PR; link the issue it closes
- add or update tests with every behaviour change
- update `CHANGELOG.md` under **Unreleased**
- update `README.md` / `docs/` when you change the API, configuration or deployment

## Releasing

1. Move the **Unreleased** notes in `CHANGELOG.md` under the new version.
2. Bump `version` in `pyproject.toml` and `evdc/__init__.py`.
3. Merge `dev` into `main` via PR, then tag: `git tag v1.6.0 && git push origin v1.6.0`.
   The `release` workflow builds the wheel, SBOM and multi-arch image and creates the GitHub release.
