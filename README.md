# Mini EvDC

A simplified, runnable re-creation of **EvDC (Enterprise Virtual Data Center)**, the enterprise cloud
platform I worked on at VNET, together with the **multi-cloud integration service** that connected
Alibaba Cloud, AWS and Azure to it.

Pure Python 3.10+ standard library. No dependencies, no cloud account needed (fake clouds are built in).

```
python -m evdc demo                 # end-to-end walkthrough in the terminal
python -m unittest discover -s tests -p "test*.py" -t .   # 35 tests
python -m evdc serve --config accounts.example.json      # REST API on :8080
```

## Architecture

```
                      ┌──────────────────── REST API (api.py) ────────────────────┐
                      │ /orgs /vdcs /vms /firewall /nat      /sync /resources      │
                      └──────────────┬─────────────────────────────┬──────────────┘
                                     │                             │
              ┌──────── Platform core (core.py) ───────┐   ┌── SyncService (sync.py) ──┐
              │ Org ─< VDC (quota) ─< Network (IPAM)    │   │ parallel per account       │
              │                └─< VM (power, snapshot) │   │ upsert by uid              │
              │ VDC ── Edge gateway: firewall + NAT     │   │ delete only after a full,  │
              │ every change recorded as a Task         │   │ successful listing         │
              └─────────────────────────────────────────┘   └────────────┬──────────────┘
                                                                         │ ProviderAdapter interface
                         ┌──────────────┬──────────────┬─────────────────┼──────────────┐
                    EvdcLocalAdapter AliyunAdapter   AwsAdapter       AzureAdapter
                         │          PageNumber/Size  NextToken        nextLink + OAuth token
                         │          Memory in MB     type lookup      power in instanceView
                         ▼                ▼               ▼                ▼
                   CloudResource  (one unified model) ──────────► SQLite store (store.py)
```

Cross-cutting: `retry.py` (per-call timeout, exponential backoff with full jitter, retry only on
throttling / 5xx / timeout) and `logs.py` (JSON-lines structured logging with provider, account,
operation and attempt on every line).

## What each part shows

| Area | Implementation |
|---|---|
| **Unified resource model** | `CloudResource` in `models.py`: provider, account, region, native_id, name, status, vCPU, memory, IPs, tags. `uid = provider:account:native_id` |
| **Provider adapters** | `adapters/` – one class per cloud, registered with `@register("aws")`. Adding a provider = one new file |
| **Authentication** | Aliyun AK/SK, AWS access keys, Azure OAuth client-credentials token with proactive refresh and one re-auth on expiry. Secrets come from `env:VAR` in config |
| **Pagination** | Three different styles: page number + total count (Aliyun), continuation token (AWS), next-link URL (Azure) |
| **Timeout & retry** | `RetryPolicy(max_attempts, base_delay, max_delay, timeout)`. Auth errors are not retried blindly |
| **Partial-failure safety** | A sync that fails half-way never marks resources as deleted; one failing provider does not stop the others |
| **Structured logging** | JSON lines, ready for ELK / CloudWatch / SLS |
| **Platform core** | Quotas, IP allocation, VM power state machine, single snapshot, edge firewall (first-match, default deny), DNAT/SNAT, task history |

### How each cloud's data is mapped

| Field | Alibaba Cloud ECS | AWS EC2 | Azure |
|---|---|---|---|
| list call | `DescribeInstances` | `describe_instances` | `virtualMachines` list |
| paging | `PageNumber`, `TotalCount` | `NextToken` | `nextLink` |
| records | `Instances.Instance[]` | `Reservations[].Instances[]` | `value[]` |
| status | `Status: "Running"` | `State.Name: "running"` | `instanceView.statuses[] "PowerState/running"` |
| CPU / memory | `Cpu`, `Memory` (MB) | only `InstanceType` → `describe_instance_types` (cached) | only `vmSize` → VM sizes list (cached) |
| name | `InstanceName` | tag `Name` | `name` |
| tags | `Tags.Tag[{TagKey,TagValue}]` | `Tags[{Key,Value}]` | `tags{}` |

## Project layout

```
evdc/
  models.py      platform models + unified CloudResource
  core.py        Mini EvDC platform (org, VDC, network, VM, edge gateway, tasks)
  adapters/      base.py (interface, retry + re-auth), aliyun.py, aws.py, azure.py, local.py
  clouds/fake.py fake Aliyun / EC2 / Azure APIs in real response shapes, with fault injection
  retry.py       timeout + backoff + retry
  sync.py        sync engine
  store.py       SQLite inventory + sync history
  logs.py        JSON logging
  app.py         config → clients → adapters
  api.py         REST API (http.server)
tests/           35 unittest cases: core rules, pagination, mapping, retries, timeouts, re-auth, sync, API
```

## Using real clouds

The fake clients use the same method names as the real SDK calls, so a real client only needs a thin
wrapper that also translates SDK exceptions into `ThrottledError` / `TransientError` / `AuthError`
(so the retry policy knows what to retry). Example for AWS:

```python
import boto3
from botocore.exceptions import ClientError
from evdc.app import CLIENT_FACTORIES
from evdc.errors import AuthError, ThrottledError, TransientError

class Ec2Client:
    def __init__(self, c):
        self._ec2 = boto3.client("ec2", region_name=c["region"],
                                 aws_access_key_id=c["access_key"], aws_secret_access_key=c["secret_key"])
    def _wrap(self, fn, **kw):
        try:
            return fn(**kw)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("RequestLimitExceeded", "Throttling"):
                raise ThrottledError(str(e), "aws", code)
            if code in ("AuthFailure", "UnauthorizedOperation"):
                raise AuthError(str(e), "aws", code)
            raise TransientError(str(e), "aws", code)
    def describe_instances(self, **kw):
        return self._wrap(self._ec2.describe_instances, **kw)
    def describe_instance_types(self, **kw):
        return self._wrap(self._ec2.describe_instance_types, **kw)

CLIENT_FACTORIES["aws"] = Ec2Client     # then set "fake": false in the config
```

## Relation to the real EvDC

The real EvDC 1.0 was a multi-tenant IaaS built on VMware vCloud Director and NSX
(organisations, organisation VDCs, vApps, edge gateways, distributed firewall). Mini EvDC keeps
the same object model and rules in a few hundred lines of Python, e.g.:

- an edge gateway denies everything by default, so inbound traffic needs **both** a DNAT rule and a
  firewall allow rule;
- VM computer names are 1–15 letters, digits or hyphens and cannot be digits only;
- a VM must be powered off before it is resized or deleted;
- one snapshot per VM, and snapshots count against the storage quota.

Out of scope on purpose: real hypervisors, storage, multi-site, vApps, the billing system.
