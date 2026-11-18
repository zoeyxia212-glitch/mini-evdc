"""SQLite persistence.

Tables
- resources      unified multi-cloud inventory (one row per uid)
- resource_events change history: created / updated (with field diff) / deleted / action
- sync_runs      one row per account per sync
- platform_state JSON snapshot of the Mini EvDC platform (orgs, VDCs, VMs ...)
- users          API users: hashed key, role, org scope
"""
from __future__ import annotations

import json
import sqlite3
import threading

from .models import CloudResource, ResourceStatus, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    uid           TEXT PRIMARY KEY,
    provider      TEXT NOT NULL,
    account       TEXT NOT NULL,
    region        TEXT NOT NULL,
    native_id     TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    name          TEXT,
    status        TEXT NOT NULL,
    vcpu          INTEGER,
    memory_gb     REAL,
    private_ip    TEXT,
    public_ip     TEXT,
    size_gb       REAL,
    tags          TEXT,
    attributes    TEXT,
    synced_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_res_provider ON resources(provider, account);
CREATE INDEX IF NOT EXISTS idx_res_type ON resources(resource_type, status);

CREATE TABLE IF NOT EXISTS resource_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    uid     TEXT NOT NULL,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,      -- created | updated | deleted | action
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_uid ON resource_events(uid);

CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT, account TEXT,
    started_at  TEXT, finished_at TEXT,
    status      TEXT, created INTEGER, updated INTEGER, deleted INTEGER,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS platform_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    saved_at   TEXT,
    state      TEXT
);

CREATE TABLE IF NOT EXISTS marks (
    uid     TEXT NOT NULL,
    label   TEXT NOT NULL,
    policy  TEXT,
    ts      TEXT NOT NULL,
    PRIMARY KEY (uid, label)
);

CREATE TABLE IF NOT EXISTS policy_runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    policy   TEXT, ts TEXT, dry_run INTEGER, scanned INTEGER, matched INTEGER
);

CREATE TABLE IF NOT EXISTS users (
    name      TEXT PRIMARY KEY,
    key_hash  TEXT UNIQUE NOT NULL,
    role      TEXT NOT NULL,
    org_id    TEXT,
    created_at TEXT
);
"""

# columns compared when deciding created / updated / unchanged
_FIELDS = ("provider", "account", "region", "native_id", "resource_type", "name", "status", "vcpu",
           "memory_gb", "private_ip", "public_ip", "size_gb", "tags", "attributes")


def _row_values(r: CloudResource) -> dict:
    return {"provider": r.provider, "account": r.account, "region": r.region, "native_id": r.native_id,
            "resource_type": r.resource_type, "name": r.name, "status": r.status.value, "vcpu": r.vcpu,
            "memory_gb": r.memory_gb, "private_ip": r.private_ip, "public_ip": r.public_ip,
            "size_gb": r.size_gb, "tags": json.dumps(r.tags, sort_keys=True),
            "attributes": json.dumps(r.attributes, sort_keys=True, default=str)}


class Store:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- resources ---------------------------------------------------------
    def upsert(self, r: CloudResource) -> str:
        """Insert or update. Returns 'created', 'updated' or 'unchanged' and logs an event."""
        new = _row_values(r)
        with self._lock:
            row = self._conn.execute("SELECT * FROM resources WHERE uid=?", (r.uid,)).fetchone()
            cols = list(new) + ["synced_at"]
            if row is None:
                self._conn.execute(
                    f"INSERT INTO resources ({', '.join(cols)}, uid) VALUES ({', '.join('?' * (len(cols) + 1))})",
                    (*new.values(), r.synced_at, r.uid))
                self._event(r.uid, "created", {"status": new["status"], "type": r.resource_type})
                outcome = "created"
            else:
                diff = {k: [row[k], v] for k, v in new.items() if row[k] != v}
                self._conn.execute(f"UPDATE resources SET {', '.join(c + '=?' for c in cols)} WHERE uid=?",
                                   (*new.values(), r.synced_at, r.uid))
                if diff:
                    self._event(r.uid, "updated", diff)
                outcome = "updated" if diff else "unchanged"
            self._conn.commit()
            return outcome

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
                self._event(uid, "deleted", {})
            self._conn.commit()
            return len(gone)

    def get(self, uid: str) -> CloudResource | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM resources WHERE uid=?", (uid,)).fetchone()
        return self._row_to_resource(row) if row else None

    def set_status(self, uid: str, status: ResourceStatus, action: str | None = None) -> None:
        with self._lock:
            old = self._conn.execute("SELECT status FROM resources WHERE uid=?", (uid,)).fetchone()
            self._conn.execute("UPDATE resources SET status=?, synced_at=? WHERE uid=?",
                               (status.value, now(), uid))
            if action:
                self._event(uid, "action", {"action": action, "status": [old["status"] if old else None,
                                                                         status.value]})
            self._conn.commit()

    def list_resources(self, provider: str | None = None, status: str | None = None,
                       resource_type: str | None = None, account: str | None = None) -> list[CloudResource]:
        sql, args = "SELECT * FROM resources WHERE 1=1", []
        for col, val in (("provider", provider), ("status", status), ("resource_type", resource_type),
                         ("account", account)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        sql += " ORDER BY provider, account, resource_type, name"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row_to_resource(r) for r in rows]

    def query(self, provider: str | None = None, status: str | None = None, resource_type: str | None = None,
              account: str | None = None, region: str | None = None, tag: str | None = None,
              name: str | None = None, sort: str = "name", limit: int = 100, offset: int = 0,
              include_deleted: bool = True) -> tuple[list[CloudResource], int]:
        """Flexible inventory query (CloudQuery / Steampipe style). Returns (page, total).

        tag:   "env" (tag present) or "env:prod" (tag has that value)
        name:  case-insensitive substring
        sort:  name | -name | vcpu | -vcpu | memory_gb | size_gb | synced_at | provider (prefix - = descending)
        """
        sortable = {"name", "vcpu", "memory_gb", "size_gb", "synced_at", "provider", "status", "region"}
        col = sort.lstrip("-")
        if col not in sortable:
            raise ValueError(f"sort must be one of {sorted(sortable)} (prefix - for descending)")
        if not (1 <= limit <= 1000) or offset < 0:
            raise ValueError("limit must be 1-1000 and offset >= 0")
        items = self.list_resources(provider, status, resource_type, account)
        if not include_deleted and not status:
            items = [r for r in items if r.status != ResourceStatus.DELETED]
        if region:
            items = [r for r in items if r.region == region]
        if tag:
            k, _, v = tag.partition(":")
            items = [r for r in items if k in r.tags and (not v or r.tags[k] == v)]
        if name:
            items = [r for r in items if name.lower() in (r.name or "").lower()]
        numeric = col in ("vcpu", "memory_gb", "size_gb")

        def sort_key(r: CloudResource):
            v = r.status.value if col == "status" else getattr(r, col)
            return (v or 0) if numeric else str(v or "").lower()

        with_value = [r for r in items if (r.status.value if col == "status" else getattr(r, col)) is not None]
        has = {id(r) for r in with_value}
        without = [r for r in items if id(r) not in has]
        with_value.sort(key=sort_key, reverse=sort.startswith("-"))
        items = with_value + without          # missing values always last
        return items[offset:offset + limit], len(items)

    def relations(self, uid: str) -> dict:
        """Resource graph around one resource (Cartography style), from attached_to links.

        instance -> its disks and public IPs; disk / eip -> the instance it is attached to.
        """
        res = self.get(uid)
        if res is None:
            return {}
        siblings = [r for r in self.list_resources(provider=res.provider, account=res.account)
                    if r.status != ResourceStatus.DELETED]
        by_native = {r.native_id: r for r in siblings}
        edges = []
        if res.resource_type == "instance":
            for r in siblings:
                if r.attributes.get("attached_to") and r.attributes["attached_to"].split("/nic")[0] == res.native_id:
                    edges.append({"relation": f"has_{r.resource_type}", "uid": r.uid, "name": r.name,
                                  "type": r.resource_type, "status": r.status.value})
        else:
            target = (res.attributes.get("attached_to") or "").split("/nic")[0]
            if target in by_native:
                t = by_native[target]
                edges.append({"relation": "attached_to", "uid": t.uid, "name": t.name, "type": t.resource_type,
                              "status": t.status.value})
        return {"uid": res.uid, "name": res.name, "type": res.resource_type, "edges": edges}

    def orphans(self) -> list[CloudResource]:
        """Disks / EIPs that point at an instance the inventory no longer has (or has deleted)."""
        live_ids = {(r.provider, r.account, r.native_id) for r in self.list_resources(resource_type="instance")
                    if r.status != ResourceStatus.DELETED}
        out = []
        for r in self.list_resources():
            target = (r.attributes.get("attached_to") or "").split("/nic")[0]
            if r.resource_type in ("disk", "eip") and r.status != ResourceStatus.DELETED and target \
                    and (r.provider, r.account, target) not in live_ids:
                out.append(r)
        return out

    def summary(self) -> dict:
        """Counts per provider / type / status, plus capacity of running instances (deleted excluded)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT provider, resource_type, status, COUNT(*) AS n, SUM(COALESCE(vcpu,0)) AS cpu,"
                " SUM(COALESCE(memory_gb,0)) AS mem, SUM(COALESCE(size_gb,0)) AS disk"
                " FROM resources WHERE status != ? GROUP BY provider, resource_type, status",
                (ResourceStatus.DELETED.value,)).fetchall()
        out: dict = {"total": 0, "running_vcpu": 0, "running_memory_gb": 0.0, "disk_gb": 0.0,
                     "by_type": {}, "by_provider": {}}
        for r in rows:
            prov = out["by_provider"].setdefault(r["provider"], {"total": 0})
            kind = prov.setdefault(r["resource_type"], {})
            kind[r["status"]] = r["n"]
            prov["total"] += r["n"]
            out["total"] += r["n"]
            out["by_type"][r["resource_type"]] = out["by_type"].get(r["resource_type"], 0) + r["n"]
            if r["resource_type"] == "instance" and r["status"] == ResourceStatus.RUNNING.value:
                out["running_vcpu"] += r["cpu"]
                out["running_memory_gb"] = round(out["running_memory_gb"] + r["mem"], 2)
            if r["resource_type"] == "disk":
                out["disk_gb"] = round(out["disk_gb"] + r["disk"], 2)
        return out

    @staticmethod
    def _row_to_resource(row: sqlite3.Row) -> CloudResource:
        return CloudResource(
            provider=row["provider"], account=row["account"], region=row["region"],
            native_id=row["native_id"], name=row["name"], status=ResourceStatus(row["status"]),
            vcpu=row["vcpu"], memory_gb=row["memory_gb"], private_ip=row["private_ip"],
            public_ip=row["public_ip"], size_gb=row["size_gb"], tags=json.loads(row["tags"] or "{}"),
            resource_type=row["resource_type"], attributes=json.loads(row["attributes"] or "{}"),
            synced_at=row["synced_at"])

    # -- change history ------------------------------------------------------
    def _event(self, uid: str, kind: str, detail: dict) -> None:
        self._conn.execute("INSERT INTO resource_events (uid, ts, kind, detail) VALUES (?,?,?,?)",
                           (uid, now(), kind, json.dumps(detail, default=str)))

    def history(self, uid: str | None = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM resource_events", []
        if uid:
            sql += " WHERE uid=?"
            args.append(uid)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]

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

    # -- platform state ------------------------------------------------------
    def save_platform(self, state: dict) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO platform_state (id, saved_at, state) VALUES (1, ?, ?)"
                               " ON CONFLICT(id) DO UPDATE SET saved_at=excluded.saved_at, state=excluded.state",
                               (now(), json.dumps(state)))
            self._conn.commit()

    def load_platform(self) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT state FROM platform_state WHERE id=1").fetchone()
        return json.loads(row["state"]) if row else None

    # -- policy marks + runs ---------------------------------------------------------
    def add_mark(self, uid: str, label: str, policy: str | None = None) -> None:
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO marks (uid, label, policy, ts) VALUES (?,?,?,?)",
                               (uid, label, policy, now()))
            self._event(uid, "action", {"action": "mark", "label": label, "policy": policy})
            self._conn.commit()

    def remove_mark(self, uid: str, label: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM marks WHERE uid=? AND label=?", (uid, label))
            self._event(uid, "action", {"action": "unmark", "label": label})
            self._conn.commit()

    def marks(self) -> dict[str, list[str]]:
        with self._lock:
            rows = self._conn.execute("SELECT uid, label FROM marks ORDER BY label").fetchall()
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["uid"], []).append(r["label"])
        return out

    def record_policy_run(self, policy: str, dry_run: bool, scanned: int, matched: int) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO policy_runs (policy, ts, dry_run, scanned, matched) VALUES (?,?,?,?,?)",
                               (policy, now(), int(dry_run), scanned, matched))
            self._conn.commit()

    def policy_runs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM policy_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "dry_run": bool(r["dry_run"])} for r in rows]

    # -- users -------------------------------------------------------------------
    def add_user(self, name: str, key_hash: str, role: str, org_id: str | None) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO users (name, key_hash, role, org_id, created_at) VALUES (?,?,?,?,?)",
                               (name, key_hash, role, org_id, now()))
            self._conn.commit()

    def user_by_hash(self, key_hash: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT name, role, org_id FROM users WHERE key_hash=?", (key_hash,)).fetchone()
        return dict(row) if row else None

    def list_users(self, org_id: str | None = None) -> list[dict]:
        sql, args = "SELECT name, role, org_id, created_at FROM users", []
        if org_id:
            sql += " WHERE org_id=?"
            args.append(org_id)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql + " ORDER BY name", args).fetchall()]

    def delete_user(self, name: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM users WHERE name=?", (name,))
            self._conn.commit()
            return cur.rowcount > 0
