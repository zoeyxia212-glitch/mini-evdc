"""SQLite persistence for the unified resource inventory and sync history."""
from __future__ import annotations

import json
import sqlite3
import threading

from .models import CloudResource, ResourceStatus, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    uid         TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    account     TEXT NOT NULL,
    region      TEXT NOT NULL,
    native_id   TEXT NOT NULL,
    name        TEXT,
    status      TEXT NOT NULL,
    vcpu        INTEGER,
    memory_gb   REAL,
    private_ip  TEXT,
    public_ip   TEXT,
    tags        TEXT,
    resource_type TEXT,
    synced_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_res_provider ON resources(provider, account);

CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT, account TEXT,
    started_at  TEXT, finished_at TEXT,
    status      TEXT, created INTEGER, updated INTEGER, deleted INTEGER,
    error       TEXT
);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    # -- resources ---------------------------------------------------------
    def upsert(self, r: CloudResource) -> str:
        """Insert or update. Returns 'created', 'updated' or 'unchanged'."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM resources WHERE uid=?", (r.uid,)).fetchone()
            values = (r.provider, r.account, r.region, r.native_id, r.name, r.status.value,
                      r.vcpu, r.memory_gb, r.private_ip, r.public_ip,
                      json.dumps(r.tags, sort_keys=True), r.resource_type, r.synced_at)
            if row is None:
                self._conn.execute(
                    "INSERT INTO resources (provider, account, region, native_id, name, status, vcpu,"
                    " memory_gb, private_ip, public_ip, tags, resource_type, synced_at, uid)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*values, r.uid))
                self._conn.commit()
                return "created"
            old = (row["provider"], row["account"], row["region"], row["native_id"], row["name"],
                   row["status"], row["vcpu"], row["memory_gb"], row["private_ip"], row["public_ip"],
                   row["tags"], row["resource_type"])
            self._conn.execute(
                "UPDATE resources SET provider=?, account=?, region=?, native_id=?, name=?, status=?,"
                " vcpu=?, memory_gb=?, private_ip=?, public_ip=?, tags=?, resource_type=?, synced_at=?"
                " WHERE uid=?", (*values, r.uid))
            self._conn.commit()
            return "unchanged" if old == values[:-1] else "updated"

    def mark_missing_deleted(self, provider: str, account: str, seen_uids: set[str]) -> int:
        """Anything we had for this account but did not see this run is gone."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT uid FROM resources WHERE provider=? AND account=? AND status!=?",
                (provider, account, ResourceStatus.DELETED.value)).fetchall()
            gone = [r["uid"] for r in rows if r["uid"] not in seen_uids]
            for uid in gone:
                self._conn.execute("UPDATE resources SET status=?, synced_at=? WHERE uid=?",
                                   (ResourceStatus.DELETED.value, now(), uid))
            self._conn.commit()
            return len(gone)

    def list_resources(self, provider: str | None = None, status: str | None = None) -> list[CloudResource]:
        sql, args = "SELECT * FROM resources WHERE 1=1", []
        if provider:
            sql += " AND provider=?"
            args.append(provider)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY provider, account, name"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row_to_resource(r) for r in rows]

    @staticmethod
    def _row_to_resource(row: sqlite3.Row) -> CloudResource:
        return CloudResource(
            provider=row["provider"], account=row["account"], region=row["region"],
            native_id=row["native_id"], name=row["name"], status=ResourceStatus(row["status"]),
            vcpu=row["vcpu"], memory_gb=row["memory_gb"], private_ip=row["private_ip"],
            public_ip=row["public_ip"], tags=json.loads(row["tags"] or "{}"),
            resource_type=row["resource_type"], synced_at=row["synced_at"])

    # -- sync history ------------------------------------------------------
    def record_sync(self, **run) -> None:
        cols = ", ".join(run)
        marks = ", ".join("?" for _ in run)
        with self._lock:
            self._conn.execute(f"INSERT INTO sync_runs ({cols}) VALUES ({marks})", tuple(run.values()))
            self._conn.commit()

    def sync_history(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
