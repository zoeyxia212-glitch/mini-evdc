# Mini EvDC

![tests](https://github.com/<your-github-username>/mini-evdc/actions/workflows/tests.yml/badge.svg)

A simplified, runnable re-creation of **EvDC (Enterprise Virtual Data Center)**, the enterprise cloud
platform I worked on at VNET, together with the **multi-cloud integration service** that connected
Alibaba Cloud, AWS and Azure to it.

- **Platform:** multi-tenant orgs, VDCs with quotas, networks with IP allocation, VMs, templates,
  snapshots, an edge gateway (firewall + NAT) and a distributed firewall driven by security tags.
- **Multi-cloud integration:** instances, disks, VPCs and public IPs from three clouds mapped into one
  resource model, kept in sync, queryable live, and manageable (start / stop / reboot) through one API.
- **Engineering:** API keys with role-based access, SQLite persistence, change history, timeouts and
  retries, structured JSON logs, 71 tests, CI on Python 3.10 – 3.13, Docker image.

Pure Python standard library: no dependencies and no cloud account needed (realistic fake clouds are built in).

```
python -m evdc demo                                        # end-to-end walkthrough in the terminal
python -m unittest discover -s tests -p "test*.py" -t .    # 71 tests
python -m evdc serve --config accounts.example.json --sync-interval 300   # REST API on :8080
```

## Architecture

```
         ┌──────────────────────────── REST API (api.py) ────────────────────────────┐
         │  API key ─► AuthService (system_admin / org_admin / viewer, org scoping)    │
         └───────────────┬───────────────────────────────────────┬─────────────────────┘
                         │                                       │
   ┌──────── Platform core (core.py) ─────────┐    ┌── Multi-cloud ───────────────────────────┐
   │ Org ─< VDC (quota) ─< Network (IPAM)      │    │ SyncService  parallel, upsert by uid,    │
   │  │             └─< VM (power, snapshot,   │    │              delete only after a full    │
   │  │                    security tags)      │    │              successful listing          │
   │  └─< Template (catalog)                   │    │ SyncScheduler  every N s, no overlap     │
   │ VDC ─ Edge: firewall (default deny), NAT  │    │ ResourceManager refresh / start / stop / │
   │     └ Distributed FW: east-west, by tag   │    │                 reboot any instance      │
   │ Task log; state saved after every change  │    └───────────────┬──────────────────────────┘
   └───────────────────────────────────────────┘                    │ ProviderAdapter interface
                         │                     ┌────────────────────┼───────────────────┐
                 EvdcLocalAdapter        AliyunAdapter         AwsAdapter          AzureAdapter
                         │               page no. + total      NextToken           nextLink + OAuth
                         ▼                     ▼                    ▼                   ▼
                    CloudResource (instance | disk | vpc | eip) ─────────► SQLite store (store.py)
                                                                           inventory · change history
                                                                           sync runs · platform state · users
```

Cross-cutting: `retry.py` (per-call timeout, exponential backoff with full jitter, retry only on
throttling / 5xx / timeout) and `logs.py` (JSON-lines logging with provider, account, operation and
attempt on every line).

## Feature list

### Multi-cloud integration service

| Feature | Details |
|---|---|
| **Unified resource model** | `CloudResource`: provider, account, region, native_id, type, name, status, vCPU, memory, IPs, size, tags, attributes. `uid = provider:account:native_id` |
| **Resource types** | `instance`, `disk`, `vpc`, `eip` on every provider (EvDC itself exposes its VMs and networks) |
| **Provider adapters** | One class per cloud, registered with `@register("aws")`. Adding a provider = one new file |
| **Authentication** | Aliyun AK/SK, AWS access keys, Azure OAuth client-credentials token with proactive refresh and one re-auth on expiry. Secrets come from `env:VAR` in config |
| **Pagination** | Page number + total count (Aliyun), continuation token (AWS), next-link URL (Azure), unpaged (AWS addresses) |
| **Timeout & retry** | `RetryPolicy(max_attempts, base_delay, max_delay, timeout)`; auth errors are not retried blindly |
| **Synchronisation** | Parallel per account; one failing provider does not stop the others; a sync that fails half-way never marks anything deleted; overlapping runs refused |
| **Scheduled sync** | `--sync-interval 300` runs a background sync every 5 minutes |
| **Live status query** | `POST /resources/refresh {uid}` asks the provider for one instance now (Aliyun `DescribeInstances(InstanceIds)`, EC2 `describe_instances(InstanceIds)`, Azure `GET vm?$expand=instanceView`) |
| **Unified management** | `start` / `stop` / `reboot` any instance by uid. Already-running is a no-op; invalid transitions are refused before calling the cloud; the provider's own refusal is surfaced as 409 |
| **Change history** | Every create, field change (with old → new values), deletion and action is recorded per resource |
| **Inventory summary** | Totals by provider, type and status; running vCPU / memory; total disk |

### Platform core (EvDC rules)

| Feature | Details |
|---|---|
| **Tenancy** | Org → VDC (region, quota) → networks, VMs; templates per org |
| **Quotas** | vCPU, memory, storage (snapshots count). Quota can be raised or lowered, never below current usage |
| **Networks** | Routed or isolated, IPv4 /29 or larger, no overlaps inside a VDC, gateway = first host, IPs allocated and released automatically |
| **VMs** | Name rules from the EvDC user guide, size limits, power state machine (power_on / power_off / shutdown_guest / reset / suspend / discard_suspend), resize only when off, disks grow but never shrink |
| **Catalog** | Create templates, deploy VMs from them, capture a powered-off VM as a new template |
| **Snapshots** | One per VM (as in vCloud Director), revert, remove |
| **Edge gateway** | North-south firewall, first match wins, default deny; DNAT / SNAT. Inbound traffic needs **both** a DNAT rule and an allow rule |
| **Distributed firewall** | East-west rules by CIDR or `tag:<security tag>`, default allow (switchable to deny). Changing a VM's role = changing its tag, no rule edits |
| **Traffic checks** | `check-inbound` and `check-east-west` explain which rule delivered or dropped a packet |
| **Persistence** | The whole platform is saved to SQLite after every successful change and reloaded on start |
| **Task log** | Every change is a task with status, error detail and timestamps |

### Access control

| Role | Can do |
|---|---|
| `system_admin` | Everything: orgs, VDCs and quotas, multi-cloud sync and actions, task log, any user |
| `org_admin` | Full control inside its own org (networks, VMs, templates, rules, users of that org); read multi-cloud inventory |
| `viewer` | Read-only inside its own org |

Keys are random tokens shown once; only the SHA-256 hash is stored. Send them as
`Authorization: Bearer <key>` or `X-API-Key: <key>`. The first `serve` creates a system admin
(key from `EVDC_ADMIN_KEY`, or printed once). More users: `POST /users` or
`python -m evdc create-user NAME --role org_admin --org ORG_ID`.

### How each cloud's data is mapped

| | Alibaba Cloud | AWS | Azure |
|---|---|---|---|
| instances | `DescribeInstances` → `Instances.Instance[]`, `Status: "Running"`, `Memory` in MB | `describe_instances` → `Reservations[].Instances[]`, `State.Name`; CPU/memory via `describe_instance_types` (cached) | `virtualMachines` → `value[]`, power in `instanceView.statuses`, size via VM sizes list (cached) |
| disks | `DescribeDisks`, `Status: In_use / Available`, `Size` GB | `describe_volumes`, `State: in-use / available`, `Attachments[]` | `disks`, `diskState: Attached / Unattached`, `managedBy` |
| VPCs | `DescribeVpcs`, `CidrBlock` | `describe_vpcs`, name from tag `Name` | `virtualNetworks`, `addressSpace.addressPrefixes` |
| public IPs | `DescribeEipAddresses`, `InstanceId` | `describe_addresses` (not paginated), `InstanceId` | `publicIPAddresses`, `ipConfiguration.id` |
| paging | `PageNumber` + `TotalCount` | `NextToken` | `nextLink` |
| start / stop / reboot | `StartInstance` / `StopInstance` / `RebootInstance` | `start_instances` / `stop_instances` / `reboot_instances` | `start` / `deallocate` / `restart` |

## REST API

Full route list with access rules is in the docstring of [`evdc/api.py`](evdc/api.py). A quick tour:

```bash
export K="Authorization: Bearer $EVDC_ADMIN_KEY"
curl -H "$K" -X POST localhost:8080/orgs -d '{"name": "acme"}'
curl -H "$K" -X POST localhost:8080/orgs/<org>/vdcs -d '{"name": "prod", "region": "huabei1",
     "quota": {"vcpu": 16, "memory_gb": 64, "storage_gb": 500}}'
curl -H "$K" -X POST localhost:8080/users -d '{"name": "acme-admin", "role": "org_admin", "org_id": "<org>"}'
curl -H "$K" -X POST localhost:8080/vdcs/<vdc>/networks -d '{"name": "app", "cidr": "192.168.10.0/24"}'
curl -H "$K" -X POST localhost:8080/vdcs/<vdc>/vms -d '{"name": "web-01", "vcpu": 2, "memory_gb": 4,
     "disk_gb": 40, "network_id": "<net>", "tags": ["web"], "power_on": true}'
curl -H "$K" -X POST localhost:8080/vdcs/<vdc>/dfw -d '{"name": "web-db", "source": "tag:web",
     "destination": "tag:db", "port": 5432, "action": "allow"}'

curl -H "$K" -X POST localhost:8080/sync
curl -H "$K" "localhost:8080/resources?provider=aws&type=disk&status=available"   # unattached disks
curl -H "$K" localhost:8080/resources/summary
curl -H "$K" -X POST localhost:8080/resources/start -d '{"uid": "aws:aws-syd:i-00000000000000001"}'
curl -H "$K" "localhost:8080/resources/history?uid=aws:aws-syd:i-00000000000000001"
```

Errors: 400 bad input · 401 no/invalid key · 403 role or org not allowed · 404 not found ·
405 wrong method · 409 quota / wrong state / sync running · 501 unsupported action ·
502 provider still failing after retries.

## Running with Docker

```
docker build -t mini-evdc .
docker run -p 8080:8080 \
  -e EVDC_ADMIN_KEY=change-me -e ALIYUN_AK=... -e ALIYUN_SK=... \
  -v $PWD/accounts.example.json:/config/accounts.json:ro -v evdc-data:/data mini-evdc
```

The image runs as a non-root user, keeps its database in the `/data` volume and syncs every 5 minutes.

## Project layout

```
evdc/
  models.py      platform models + unified CloudResource
  core.py        platform: orgs, VDCs, networks, VMs, templates, edge gateway, distributed firewall
  auth.py        API keys, roles, org scoping
  adapters/      base.py (interface, retry + re-auth), aliyun.py, aws.py, azure.py, local.py
  clouds/fake.py fake Aliyun / EC2 / Azure APIs in real response shapes, with fault injection
  retry.py       timeout + backoff + retry
  sync.py        sync engine + scheduler
  manage.py      refresh / start / stop / reboot any instance by uid
  store.py       SQLite: inventory, change history, sync runs, platform state, users
  logs.py        JSON logging
  app.py         config → clients → adapters; wiring
  api.py         REST API (http.server)
tests/           71 unittest cases
.github/workflows/tests.yml   CI on Python 3.10 – 3.13
Dockerfile       non-root image, data volume, scheduled sync
```

## Using real clouds

The fake clients use the same method names and response shapes as the real SDK calls, so a real
client only needs a thin wrapper that also translates SDK exceptions into `ThrottledError` /
`TransientError` / `AuthError` / `ResourceNotFound` / `IncorrectState`, so the retry policy and the
API know what to do. Example for AWS:

```python
import boto3
from botocore.exceptions import ClientError
from evdc.app import CLIENT_FACTORIES
from evdc.errors import AuthError, IncorrectState, ResourceNotFound, ThrottledError, TransientError

class Ec2Client:
    def __init__(self, c):
        self.region = c["region"]
        self._ec2 = boto3.client("ec2", region_name=c["region"],
                                 aws_access_key_id=c["access_key"], aws_secret_access_key=c["secret_key"])

    def __getattr__(self, method):          # describe_instances, describe_volumes, start_instances ...
        fn = getattr(self._ec2, method)
        def call(**kw):
            try:
                return fn(**kw)
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code in ("RequestLimitExceeded", "Throttling"):
                    raise ThrottledError(str(e), "aws", code)
                if code in ("AuthFailure", "UnauthorizedOperation"):
                    raise AuthError(str(e), "aws", code)
                if code.endswith(".NotFound"):
                    raise ResourceNotFound(str(e), "aws", code)
                if code == "IncorrectInstanceState":
                    raise IncorrectState(str(e), "aws", code)
                raise TransientError(str(e), "aws", code)
        return call

CLIENT_FACTORIES["aws"] = Ec2Client     # then set "fake": false in the config
```

## Relation to the real EvDC

The real EvDC 1.0 was a multi-tenant IaaS built on VMware vCloud Director and NSX (organisations,
organisation VDCs, vApps, catalogs, edge gateways, distributed firewall with security tags and
groups). Mini EvDC keeps the same object model and rules in a few thousand lines of Python.

Out of scope on purpose: real hypervisors and storage, vApps, multi-site, VPN, load balancers, billing.
