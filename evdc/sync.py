"""Resource synchronisation: pull every account through its adapter into the store.

Rules:
- Providers are synced in parallel; one failing provider does not stop the rest.
- Records are upserted by uid (provider:account:native_id).
- Only after a *complete* successful listing are missing records marked
  deleted. A run that failed half-way never deletes anything, otherwise a
  timeout on page 3 would wrongly "delete" everything after page 2.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .adapters.base import ProviderAdapter
from .logs import get_logger
from .models import now
from .store import Store

log = get_logger("evdc.sync")


@dataclass
class SyncReport:
    provider: str
    account: str
    status: str = "running"
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    skipped: int = 0
    error: str | None = None
    duration_s: float = 0.0
    started_at: str = field(default_factory=now)


class SyncService:
    def __init__(self, store: Store, adapters: list[ProviderAdapter], max_workers: int = 4):
        self.store = store
        self.adapters = adapters
        self.max_workers = max_workers

    def sync_one(self, adapter: ProviderAdapter) -> SyncReport:
        report = SyncReport(provider=adapter.provider, account=adapter.account)
        t0 = time.monotonic()
        seen: set[str] = set()
        ctx = {"provider": adapter.provider, "account": adapter.account}
        log.info("sync started", extra={"ctx": ctx})
        try:
            for resource in adapter.list_resources():
                seen.add(resource.uid)
                outcome = self.store.upsert(resource)
                setattr(report, outcome, getattr(report, outcome) + 1)
            report.deleted = self.store.mark_missing_deleted(adapter.provider, adapter.account, seen)
            report.status = "success"
        except Exception as exc:  # noqa: BLE001 - report, never crash the whole run
            report.status, report.error = "failed", f"{type(exc).__name__}: {exc}"
        report.skipped = adapter.skipped
        report.duration_s = round(time.monotonic() - t0, 3)
        self.store.record_sync(provider=report.provider, account=report.account,
                               started_at=report.started_at, finished_at=now(), status=report.status,
                               created=report.created, updated=report.updated, deleted=report.deleted,
                               error=report.error)
        level = log.info if report.status == "success" else log.error
        level("sync finished", extra={"ctx": {**ctx, **{k: v for k, v in report.__dict__.items()
                                                        if k not in ("provider", "account")}}})
        return report

    def sync_all(self) -> list[SyncReport]:
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="sync") as pool:
            return list(pool.map(self.sync_one, self.adapters))


class SyncScheduler:
    """Runs a job every `interval` seconds in a background thread.

    The job itself is responsible for refusing overlapping runs (App.sync
    does this with a lock), so a slow sync is skipped, never stacked.
    """

    def __init__(self, job, interval: float):
        if interval <= 0:
            raise ValueError("interval must be > 0")
        self.job = job
        self.interval = interval
        self.runs = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="sync-scheduler", daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.job()
                self.runs += 1
            except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
                log.warning("scheduled sync skipped", extra={"ctx": {"error": str(exc)}})
            self._stop.wait(self.interval)

    def start(self) -> "SyncScheduler":
        self._thread.start()
        log.info("scheduler started", extra={"ctx": {"interval_s": self.interval}})
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout)
