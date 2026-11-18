"""Application wiring: config -> clients -> adapters -> sync service.

Credentials are never hard-coded: a value written as "env:NAME" in the config
is read from the environment variable NAME.

`"fake": true` accounts use the in-memory fake clouds (for demos and tests).
To use a real cloud, register a factory in CLIENT_FACTORIES that returns the
real SDK client wrapped to the same method names (see README).
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable

from .adapters import ADAPTERS, ProviderAdapter
from .clouds.fake import FakeAliyunEcs, FakeAzure, FakeEc2
from .core import EvdcPlatform
from .errors import SyncInProgress
from .manage import ResourceManager
from .retry import RetryPolicy
from .store import Store
from .sync import SyncScheduler, SyncService


def resolve(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("env:"):
        name = value[4:]
        if name not in os.environ:
            raise KeyError(f"environment variable {name} is not set")
        return os.environ[name]
    if isinstance(value, dict):
        return {k: resolve(v) for k, v in value.items()}
    return value


FAKE_FACTORIES: dict[str, Callable[[dict], Any]] = {
    "aliyun": lambda c: FakeAliyunEcs(c["access_key_id"], c["access_key_secret"]),
    "aws": lambda c: FakeEc2(c["access_key"], c["secret_key"], region=c.get("region", "ap-southeast-2")),
    "azure": lambda c: FakeAzure(c["tenant_id"], c["client_id"], c["client_secret"]),
}

# provider -> callable(credentials) returning a real client; empty by default
CLIENT_FACTORIES: dict[str, Callable[[dict], Any]] = {}


class App:
    def __init__(self, db_path: str = ":memory:"):
        self.platform = EvdcPlatform()
        self.store = Store(db_path)
        self.adapters: list[ProviderAdapter] = []
        self.scheduler: SyncScheduler | None = None
        self._sync_lock = threading.Lock()
        self.add_account("evdc", "local", self.platform)

    def add_account(self, provider: str, account: str, client: Any, **options) -> ProviderAdapter:
        cls = ADAPTERS[provider]
        policy = RetryPolicy(**options.pop("retry", {}))
        adapter = cls(account, client, policy, **options)
        self.adapters.append(adapter)
        return adapter

    def load_accounts(self, accounts: list[dict]) -> None:
        for acc in accounts:
            provider = acc["provider"]
            creds = resolve(acc.get("credentials", {}))
            if acc.get("fake"):
                client = FAKE_FACTORIES[provider](creds)
            elif provider in CLIENT_FACTORIES:
                client = CLIENT_FACTORIES[provider](creds)
            else:
                raise NotImplementedError(f"no real client factory registered for {provider}")
            options = {k: v for k, v in acc.items() if k in ("page_size", "retry", "region")}
            if provider != "aliyun":
                options.pop("region", None)
            self.add_account(provider, acc["account"], client, **options)

    def load_config(self, path: str) -> None:
        with open(path, encoding="utf-8") as fh:
            self.load_accounts(json.load(fh)["accounts"])

    def sync(self):
        """Run one full sync. Refuses (SyncInProgress) if another sync is still running."""
        if not self._sync_lock.acquire(blocking=False):
            raise SyncInProgress("a sync is already running")
        try:
            return SyncService(self.store, self.adapters).sync_all()
        finally:
            self._sync_lock.release()

    @property
    def manager(self) -> ResourceManager:
        return ResourceManager(self.store, self.adapters)

    def start_scheduler(self, interval: float) -> SyncScheduler:
        self.scheduler = SyncScheduler(self.sync, interval).start()
        return self.scheduler
