# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.6.0] - 2026-11-23

### Added
- Liveness and readiness probes: `GET /healthz`, `GET /readyz`.
- Graceful shutdown on SIGTERM / SIGINT (stop the scheduler, finish in-flight requests, close the database).
- `deploy/`: docker-compose stack with Prometheus and a provisioned Grafana dashboard; Kubernetes manifests (Kustomize) with probes, resource limits, a hardened security context and a NetworkPolicy.
- Supply chain: Dependabot, CodeQL, bandit, pip-audit, Trivy image scan, release workflow with CycloneDX SBOM, build provenance and a multi-arch GHCR image.
- Repository hygiene: SECURITY.md, CONTRIBUTING.md, CODEOWNERS, PR and issue templates, pre-commit, EditorConfig.
- Docs: architecture overview, architecture decision records, enterprise roadmap.

### Changed
- CI runs on pushes to `main` / `dev` and on every pull request, cancels superseded runs, reports coverage and smoke-tests the Docker image.
- Container image: health check, read-only friendly, `PYTHONDONTWRITEBYTECODE`.

## [1.5.0]
### Added
- Real Alibaba Cloud (ECS + VPC SDK) and Azure (identity + mgmt SDK) clients with error translation.
- API audit trail (`GET /audit`) and an OpenAPI 3 document (`GET /openapi.json`).
- ruff lint in CI.

## [1.4.0]
### Added
- Web dashboard served at `GET /`.
- Real AWS client (boto3) with error translation and a least-privilege IAM guide.

## [1.3.0]
### Added
- Cost estimates and waste report, governance policies (dry run, mark, stop), resource graph and orphans, inventory queries with CSV export, Prometheus metrics.

## [1.2.0]
### Added
- Disks, VPCs and public IPs in the inventory; live refresh and reboot; change history.
- Platform persistence, template catalog, distributed firewall with security tags.
- API keys with system_admin / org_admin / viewer roles.

## [1.1.0]
### Added
- Start / stop across clouds, scheduled sync, inventory summary, CI, Docker image.

## [1.0.0]
### Added
- Platform core (org, VDC, network, VM, edge gateway) and multi-cloud integration service with adapters for Alibaba Cloud, AWS and Azure.
