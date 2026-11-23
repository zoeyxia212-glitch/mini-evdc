# Security policy

## Supported versions

Only the latest release on `main` receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private
[security advisory](../../security/advisories/new) form instead ("Report a vulnerability"
on the Security tab). You will get an acknowledgement within 5 working days and a fix or
mitigation plan within 30 days for confirmed issues.

Useful details: affected version or commit, how to reproduce, impact, and any proposed fix.

## What is in scope

- the REST API and its access control (API keys, roles, org scoping)
- the dashboard (`evdc/web/dashboard.html`)
- the real cloud clients (`evdc/clouds/*real.py`) and how they handle credentials
- the container image and deployment manifests in `deploy/`

## How this project protects itself

| Control | Where |
|---|---|
| API keys stored as SHA-256 hashes, shown once | `evdc/auth.py` |
| Role- and org-scoped authorisation on every route | `evdc/api.py` |
| Audit trail of every write and every refused request | `GET /audit` |
| Cloud credentials only from the environment (`env:VAR`), never in files | `evdc/app.py` |
| Cloud clients expose an allow-list of API calls (no terminate / delete) | `evdc/clouds/*real.py` |
| Dashboard served with CSP, `X-Content-Type-Options`, no external assets | `evdc/api.py` |
| Static analysis (bandit, CodeQL), dependency audit (pip-audit), image scan (Trivy) | `.github/workflows/` |
| Non-root container, read-only root filesystem, dropped capabilities | `Dockerfile`, `deploy/k8s/` |
| Dependabot updates for pip, GitHub Actions and the base image | `.github/dependabot.yml` |
