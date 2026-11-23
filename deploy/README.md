# Deployment

## docker compose (one machine: app + Prometheus + Grafana)

```bash
cd deploy
cp .env.example .env          # set EVDC_ADMIN_KEY and GRAFANA_ADMIN_PASSWORD
docker compose up -d --build
```

| Service | URL | Notes |
|---|---|---|
| Mini EvDC API + dashboard | http://localhost:8080 | sign in with `EVDC_ADMIN_KEY` |
| Prometheus | http://localhost:9090 | scrapes `/metrics`; alert rules in `prometheus/alerts.yml` |
| Grafana | http://localhost:3000 | dashboard "Mini EvDC – multi-cloud estate" is provisioned automatically |

The app container is read-only, drops all Linux capabilities and runs as UID 10001.
`docker compose stop` sends SIGTERM: the app reports not-ready, finishes in-flight requests,
stops the scheduler and closes the database before exiting.

Alerts shipped: `EvdcDown`, `CloudSyncFailing` (an account failed to sync for 15 min),
`CloudSyncStale` (no sync for an hour), `CloudCostJump` (estimated cost up 20% in a day).
Wire them to email / Slack / PagerDuty with Alertmanager.

## Kubernetes (Kustomize)

```bash
cp deploy/k8s/base/secrets.env.example deploy/k8s/base/secrets.env   # fill in, never commit
kubectl apply -k deploy/k8s/overlays/staging      # or overlays/production
kubectl -n mini-evdc-staging get pods,svc
```

What the manifests do:

- **Pod Security "restricted"** enforced on the namespace; the pod runs as non-root UID 10001,
  read-only root filesystem, no privilege escalation, all capabilities dropped, seccomp RuntimeDefault,
  no service-account token.
- **Probes:** startup and liveness on `/healthz`, readiness on `/readyz` (database reachable,
  scheduler alive, not draining).
- **Graceful rollout:** `--shutdown-grace=10` is shorter than `terminationGracePeriodSeconds: 30`,
  so Kubernetes removes the pod from the Service before the process stops accepting connections.
- **NetworkPolicy:** default deny; ingress only from the ingress controller and Prometheus,
  egress only to DNS and HTTPS (cloud APIs).
- **Config vs secrets:** account definitions in a ConfigMap, credentials in a Secret
  (`secretGenerator`); use External Secrets or Sealed Secrets in a real cluster.
- **Overlays:** `staging` (own namespace, slower sync, `:dev` image) and `production`
  (pinned release tag, 10 Gi volume, TLS Ingress with cert-manager and edge rate limiting).

One replica with a `Recreate` strategy because the store is SQLite on a ReadWriteOnce volume.
Horizontal scaling needs the PostgreSQL backend described in [docs/ROADMAP.md](../docs/ROADMAP.md).

Replace `OWNER` in the image names with your GitHub username (the `release` workflow publishes
`ghcr.io/<owner>/mini-evdc`).
