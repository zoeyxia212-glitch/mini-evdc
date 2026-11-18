# Running Mini EvDC against a real Azure subscription

`evdc/clouds/azurereal.py` wraps `azure-identity`, `azure-mgmt-compute` and
`azure-mgmt-network`. Credentials stay in your environment.

1. **Install the SDKs:** `pip install ".[azure]"` (from the repo root).
2. **Create a service principal with read access** to the subscription:

   ```bash
   az ad sp create-for-rbac --name mini-evdc --role Reader --scopes /subscriptions/<subscription-id>
   ```

   The output gives `appId` (client id), `password` (client secret) and `tenant`. For
   start / deallocate / restart from Mini EvDC, also assign a role that includes
   `Microsoft.Compute/virtualMachines/start/action`, `.../deallocate/action` and
   `.../restart/action` (for example *Virtual Machine Contributor* on one test resource group,
   or a custom role with just those actions).
3. **Export and start:**

   ```bash
   export AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=... AZURE_SUBSCRIPTION_ID=...
   export EVDC_ADMIN_KEY=choose-a-long-random-string
   python -m evdc serve --config accounts.azure.json --db evdc.db
   ```

How the wrapper maps the SDK onto the adapter: VM lists use `list_all(status_only="true")` so the
power state comes back without one call per VM; models are converted with
`serialize(keep_readonly=True)` to the REST JSON the adapter reads; pagination uses
`by_page(continuation_token)`; start / deallocate / restart call the `begin_*` long-running
operations without waiting (Azure answers 202). Note that the VM list returns NIC references,
not IP addresses, so private IPs show as empty for real Azure VMs.

Errors: 429 and 5xx are retried; 401 / 403 and `ClientAuthenticationError` are not; 404 becomes
404 and 409 / `OperationNotAllowed` becomes 409.

A *stopped* VM is still billed for compute; Mini EvDC's stop action uses **deallocate**, which
is what actually stops the charge.
