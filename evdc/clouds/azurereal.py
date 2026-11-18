"""Real Azure client for the AzureAdapter, built on the official SDKs:

    pip install azure-identity azure-mgmt-compute azure-mgmt-network   (or: pip install ".[azure]")

The adapter speaks in REST shapes: camelCase dicts with "properties", a bearer
token it refreshes, and opaque "nextLink" pagination. This wrapper maps that
onto the SDK:

- get_token()           ClientSecretCredential.get_token(".../.default") -> {access_token, expires_in}
- list_*(next_link=)    pager.by_page(continuation_token).next() -> {"value": [...], "nextLink": token}
- items                 model.serialize(keep_readonly=True) -> the REST JSON (camelCase, with ids)
- start / deallocate / restart   begin_*() long-running operations; we do not wait (Azure returns 202)

The SDK clients authenticate with the credential object themselves; the token
the adapter passes in is only used for expiry tracking. SDK errors
(azure.core.exceptions) are translated by HTTP status and class name.
"""
from __future__ import annotations

import time
from typing import Any

from ..errors import AuthError, CloudApiError, IncorrectState, ResourceNotFound, ThrottledError, TransientError

SCOPE = "https://management.azure.com/.default"
NETWORK_ERRORS = {"ServiceRequestError", "ServiceResponseError", "ServiceRequestTimeoutError",
                  "ServiceResponseTimeoutError"}


def translate(exc: Exception) -> Exception:
    if isinstance(exc, CloudApiError):
        return exc
    name = type(exc).__name__
    if name in NETWORK_ERRORS:
        return TransientError(str(exc), "azure", name)
    if name == "ClientAuthenticationError":
        return AuthError(str(exc), "azure", "ClientAuthenticationError")
    status = getattr(exc, "status_code", None)
    if status is None:
        return exc
    err = getattr(exc, "error", None)
    code = getattr(err, "code", None) or name
    msg = getattr(exc, "message", None) or str(exc)
    if status == 429:
        return ThrottledError(msg, "azure", code)
    if status in (401, 403):
        return AuthError(msg, "azure", code)
    if status == 404:
        return ResourceNotFound(msg, "azure", code)
    if status == 409 or code == "OperationNotAllowed":
        return IncorrectState(msg, "azure", code)
    if status >= 500:
        return TransientError(msg, "azure", code)
    return CloudApiError(msg, "azure", code)


def split_id(resource_id: str) -> tuple[str, str]:
    """/subscriptions/x/resourceGroups/RG/providers/.../virtualMachines/NAME -> (RG, NAME)."""
    parts = resource_id.strip("/").split("/")
    lowered = [p.lower() for p in parts]
    try:
        rg = parts[lowered.index("resourcegroups") + 1]
    except (ValueError, IndexError):
        raise ResourceNotFound(f"not an Azure resource id: {resource_id}", "azure", "InvalidResourceId") from None
    return rg, parts[-1]


class AzureClient:
    """Drop-in replacement for FakeAzure that talks to the real Azure Resource Manager."""

    def __init__(self, credentials: dict | None = None, *, credential: Any = None,
                 compute: Any = None, network: Any = None):
        credentials = credentials or {}
        if credential is None or compute is None or network is None:
            try:
                from azure.identity import ClientSecretCredential
                from azure.mgmt.compute import ComputeManagementClient
                from azure.mgmt.network import NetworkManagementClient
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise RuntimeError('Azure SDK not installed: pip install ".[azure]"') from exc
            credential = ClientSecretCredential(credentials["tenant_id"], credentials["client_id"],
                                                credentials["client_secret"])
            sub = credentials["subscription_id"]
            compute = ComputeManagementClient(credential, sub)
            network = NetworkManagementClient(credential, sub)
        self._credential, self._compute, self._network = credential, compute, network

    @staticmethod
    def _guard(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - translated or re-raised as is
            raise translate(exc) from exc

    # -- auth ----------------------------------------------------------------
    def get_token(self) -> dict:
        tok = self._guard(self._credential.get_token, SCOPE)
        return {"access_token": tok.token, "expires_in": max(0, int(tok.expires_on - time.time()))}

    # -- paged lists ------------------------------------------------------------
    def _page(self, make_pager, next_link: str | None) -> dict:
        def first_page():
            pages = make_pager().by_page(continuation_token=next_link)  # pager built inside the guard too
            items = [item.serialize(keep_readonly=True) for item in next(pages)]
            return {"value": items, "nextLink": pages.continuation_token}
        return self._guard(first_page)

    def list_vms(self, token: str, next_link: str | None = None, top: int = 5) -> dict:
        # status_only gives instanceView (power state) without a call per VM
        return self._page(lambda: self._compute.virtual_machines.list_all(status_only="true"), next_link)

    def list_disks(self, token: str, next_link: str | None = None, top: int = 5) -> dict:
        return self._page(self._compute.disks.list, next_link)

    def list_vnets(self, token: str, next_link: str | None = None, top: int = 5) -> dict:
        return self._page(self._network.virtual_networks.list_all, next_link)

    def list_public_ips(self, token: str, next_link: str | None = None, top: int = 5) -> dict:
        return self._page(self._network.public_ip_addresses.list_all, next_link)

    def list_sizes(self, token: str, location: str) -> dict:
        sizes = self._guard(lambda: [s.serialize(keep_readonly=True)
                                     for s in self._compute.virtual_machine_sizes.list(location)])
        return {"value": sizes}

    # -- single VM ----------------------------------------------------------------
    def get_vm(self, token: str, vm_id: str) -> dict:
        rg, name = split_id(vm_id)
        vm = self._guard(self._compute.virtual_machines.get, rg, name, expand="instanceView")
        return vm.serialize(keep_readonly=True)

    def _begin(self, op: str, vm_id: str) -> dict:
        rg, name = split_id(vm_id)
        self._guard(getattr(self._compute.virtual_machines, f"begin_{op}"), rg, name)
        return {"status": 202}

    def start(self, token: str, vm_id: str) -> dict:
        return self._begin("start", vm_id)

    def deallocate(self, token: str, vm_id: str) -> dict:
        return self._begin("deallocate", vm_id)

    def restart(self, token: str, vm_id: str) -> dict:
        return self._begin("restart", vm_id)


def factory(credentials: dict) -> AzureClient:
    return AzureClient(credentials)
