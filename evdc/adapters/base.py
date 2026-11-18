"""Adapter pattern for cloud providers.

Each provider returns resources in its own format. An adapter does:

1. authenticate()          get / refresh credentials (AK/SK, OAuth token, ...)
2. iter_raw(kind)          walk every page of the provider's list API for one
                           resource type (page numbers, NextToken, nextLink ...)
3. normalize(kind, raw)    map one raw record into the unified CloudResource
4. get_instance(id)        live status query for one instance
5. start / stop / reboot   lifecycle actions

The sync engine and the manager only talk to this interface, so adding a new
provider means writing one new subclass and registering it.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

from ..errors import AuthError
from ..logs import get_logger
from ..models import CloudResource, ResourceStatus
from ..retry import RetryPolicy, run_with_retry

T = TypeVar("T")

ADAPTERS: dict[str, type[ProviderAdapter]] = {}


def register(name: str):
    """Class decorator: @register("aws") adds the adapter to the registry."""
    def deco(cls):
        cls.provider = name
        ADAPTERS[name] = cls
        return cls
    return deco


class ProviderAdapter(ABC):
    provider: str = "base"
    supports_reauth: bool = False
    resource_types: tuple[str, ...] = ("instance",)

    def __init__(self, account: str, client: Any, policy: RetryPolicy | None = None, page_size: int = 10):
        self.account = account
        self.client = client
        self.policy = policy or RetryPolicy()
        self.page_size = page_size
        self.log = get_logger(f"evdc.adapter.{self.provider}")
        self.skipped = 0

    # -- hooks -----------------------------------------------------------
    def authenticate(self, force: bool = False) -> None:  # noqa: B027 - optional hook, not abstract
        """Default: credentials are static keys, nothing to do."""

    @abstractmethod
    def iter_raw(self, kind: str) -> Iterator[dict]:
        ...

    @abstractmethod
    def normalize(self, kind: str, raw: dict) -> CloudResource:
        ...

    # live query + lifecycle; adapters that cannot do these keep the defaults
    # (the API turns NotImplementedError into HTTP 501)
    def get_instance(self, native_id: str) -> CloudResource:
        raise NotImplementedError(f"{self.provider} adapter does not support live status queries")

    def start(self, native_id: str) -> ResourceStatus:
        raise NotImplementedError(f"{self.provider} adapter does not support start")

    def stop(self, native_id: str) -> ResourceStatus:
        raise NotImplementedError(f"{self.provider} adapter does not support stop")

    def reboot(self, native_id: str) -> ResourceStatus:
        raise NotImplementedError(f"{self.provider} adapter does not support reboot")

    # -- shared machinery -------------------------------------------------
    def call(self, op: str, fn: Callable[[], T]) -> T:
        """Every API call goes through here: timeout + retry + one re-auth."""
        ctx = {"provider": self.provider, "account": self.account}
        try:
            return run_with_retry(fn, self.policy, op=op, ctx=ctx, sleep=self._sleep)
        except AuthError:
            if not self.supports_reauth:
                raise
            self.log.warning("auth failed, refreshing credentials once", extra={"ctx": {**ctx, "op": op}})
            self.authenticate(force=True)
            return run_with_retry(fn, self.policy, op=op, ctx=ctx, sleep=self._sleep)

    # tests swap this out so backoff does not actually wait
    _sleep: Callable[[float], None] = staticmethod(time.sleep)

    def list_resources(self, kinds: tuple[str, ...] | None = None) -> Iterator[CloudResource]:
        self.authenticate()
        for kind in kinds or self.resource_types:
            for raw in self.iter_raw(kind):
                try:
                    yield self.normalize(kind, raw)
                except (KeyError, TypeError, ValueError, IndexError) as exc:
                    # one malformed record must not kill the whole sync
                    self.skipped += 1
                    self.log.warning("skipped malformed record", extra={"ctx": {
                        "provider": self.provider, "account": self.account, "type": kind, "error": repr(exc)}})

    def resource(self, kind: str, native_id: str, name: str, status: ResourceStatus, region: str,
                 **fields) -> CloudResource:
        """Small helper so every adapter fills provider/account the same way."""
        return CloudResource(provider=self.provider, account=self.account, region=region, native_id=native_id,
                             name=name or native_id, status=status, resource_type=kind, **fields)
