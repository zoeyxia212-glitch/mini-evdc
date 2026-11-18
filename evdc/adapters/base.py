"""Adapter pattern for cloud providers.

Each provider returns instances in its own format. An adapter does three jobs:

1. authenticate()  - get / refresh credentials (AK/SK, OAuth token, ...)
2. iter_raw()      - walk every page of the provider's list API
                     (page numbers, NextToken, nextLink ... all differ)
3. normalize(raw)  - map one raw record into the unified CloudResource

The sync engine only talks to this interface, so adding a new provider means
writing one new subclass and registering it; nothing else changes.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Iterator, TypeVar

from ..errors import AuthError
from ..logs import get_logger
from ..models import CloudResource, ResourceStatus
from ..retry import RetryPolicy, run_with_retry

T = TypeVar("T")

ADAPTERS: dict[str, type["ProviderAdapter"]] = {}


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

    def __init__(self, account: str, client: Any, policy: RetryPolicy | None = None, page_size: int = 10):
        self.account = account
        self.client = client
        self.policy = policy or RetryPolicy()
        self.page_size = page_size
        self.log = get_logger(f"evdc.adapter.{self.provider}")
        self.skipped = 0

    # -- hooks -----------------------------------------------------------
    def authenticate(self, force: bool = False) -> None:
        """Default: credentials are static keys, nothing to do."""

    @abstractmethod
    def iter_raw(self) -> Iterator[dict]:
        ...

    @abstractmethod
    def normalize(self, raw: dict) -> CloudResource:
        ...

    # -- lifecycle actions (unified management) ---------------------------
    # Return the status the instance moves to right away (usually a
    # transitional one such as PENDING / STOPPING); the next sync records
    # the final state. Adapters that cannot do this keep the default.
    def start(self, native_id: str) -> ResourceStatus:
        raise NotImplementedError(f"{self.provider} adapter does not support start")

    def stop(self, native_id: str) -> ResourceStatus:
        raise NotImplementedError(f"{self.provider} adapter does not support stop")

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

    def list_resources(self) -> Iterator[CloudResource]:
        self.authenticate()
        for raw in self.iter_raw():
            try:
                yield self.normalize(raw)
            except (KeyError, TypeError, ValueError) as exc:
                # one malformed record must not kill the whole sync
                self.skipped += 1
                self.log.warning("skipped malformed record", extra={"ctx": {
                    "provider": self.provider, "account": self.account, "error": repr(exc)}})
