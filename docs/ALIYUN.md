# Running Mini EvDC against a real Alibaba Cloud account

`evdc/clouds/aliyunreal.py` wraps the official SDKs (ECS for instances and disks, VPC for
VPCs and EIPs). Credentials stay in your environment; nothing in the repository needs a key.

1. **Install the SDKs:** `pip install ".[aliyun]"` (from the repo root).
2. **Create a RAM user for programmatic access** (RAM console → Users → Create user,
   OpenAPI access only) and grant it the system policies `AliyunECSReadOnlyAccess` and
   `AliyunVPCReadOnlyAccess`. If you want start / stop / reboot from Mini EvDC, add a custom
   policy that allows `ecs:StartInstance`, `ecs:StopInstance` and `ecs:RebootInstance`,
   ideally limited to the instances you test with (`acs:ecs:*:*:instance/i-...`).
3. **Export the key and start:**

   ```bash
   export ALIBABA_CLOUD_ACCESS_KEY_ID=...  ALIBABA_CLOUD_ACCESS_KEY_SECRET=...
   export EVDC_ADMIN_KEY=choose-a-long-random-string
   python -m evdc serve --config accounts.aliyun.json --db evdc.db
   ```

   Change `region` in `accounts.aliyun.json` to the region you use (for example `ap-southeast-2`).

Error handling: `Throttling*`, `ServiceUnavailable` and 5xx are retried with backoff;
`InvalidAccessKeyId.NotFound`, `SignatureDoesNotMatch` and `Forbidden.RAM` are not;
`*.NotFound` becomes 404 and `IncorrectInstanceStatus` becomes 409. Only the seven calls the
adapter needs are exposed; anything else is refused in code.

Pay-as-you-go instances, disks and EIPs cost money even when idle. Release what you create
and set a budget alert before experimenting.
