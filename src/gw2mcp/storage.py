"""SQLite persistence: delta-encoded account history plus a cache of public game data.

History model
-------------
Every snapshot gets a row in ``snapshots``. Tracked values are stored in
``observations`` as ``(kind, key, snapshot_id, value)`` but only when the value
*changed* compared to the previous observation of the same ``(kind, key)``. A
JSON ``null`` value marks that the key disappeared (e.g. an item was used up).
The value of a series at snapshot ``S`` is therefore the most recent
observation with ``snapshot_id <= S``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at     TEXT NOT NULL,
    account_id   TEXT,
    account_name TEXT,
    sections     TEXT NOT NULL DEFAULT '[]',
    errors       TEXT NOT NULL DEFAULT '{}',
    duration     REAL,
    complete     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS snapshots_taken_at ON snapshots(taken_at);
CREATE TABLE IF NOT EXISTS observations (
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    value       TEXT,
    PRIMARY KEY (kind, key, snapshot_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS observations_snapshot ON observations(snapshot_id);
CREATE TABLE IF NOT EXISTS catalog (
    endpoint   TEXT NOT NULL,
    id         TEXT NOT NULL,
    name       TEXT,
    data       TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (endpoint, id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS catalog_name ON catalog(endpoint, name);
CREATE TABLE IF NOT EXISTS catalog_index (
    endpoint TEXT PRIMARY KEY,
    built_at REAL NOT NULL,
    count    INTEGER NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Snapshot:
    id: int
    taken_at: datetime
    account_id: str | None
    account_name: str | None
    sections: list[str]
    errors: dict[str, str]
    duration: float | None
    complete: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "taken_at": to_iso(self.taken_at),
            "account_name": self.account_name,
            "sections": self.sections,
            "errors": self.errors,
            "duration_seconds": round(self.duration, 2) if self.duration is not None else None,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class Observation:
    snapshot_id: int
    taken_at: datetime
    key: str
    value: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "taken_at": to_iso(self.taken_at),
            "key": self.key,
            "value": self.value,
        }


class Storage:
    """Thread-safe wrapper around a single SQLite connection."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 10000")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # ------------------------------------------------------------- snapshots
    def save_snapshot(
        self,
        data: Mapping[str, Mapping[str, Any]],
        *,
        taken_at: datetime | None = None,
        account_id: str | None = None,
        account_name: str | None = None,
        errors: Mapping[str, str] | None = None,
        duration: float | None = None,
    ) -> Snapshot:
        """Persist a snapshot atomically.

        ``data`` maps a *kind* to the complete ``{key: value}`` state of that kind.
        Kinds that are absent (e.g. because their API call failed) are left untouched,
        so their previous values carry forward.
        """
        taken_at = taken_at or utcnow()
        errors = dict(errors or {})
        sections = sorted(data)
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO snapshots(taken_at, account_id, account_name, sections, errors, duration, complete)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    to_iso(taken_at),
                    account_id,
                    account_name,
                    _dump(sections),
                    _dump(errors),
                    duration,
                    0 if errors else 1,
                ),
            )
            sid = int(cur.lastrowid or 0)
            for kind, values in data.items():
                previous = self._state_locked(kind, None)
                new = {str(k): _dump(v) for k, v in values.items() if v is not None}
                rows = [(kind, k, sid, v) for k, v in new.items() if previous.get(k) != v]
                rows += [(kind, k, sid, None) for k in previous if k not in new]
                c.executemany("INSERT INTO observations(kind, key, snapshot_id, value) VALUES (?, ?, ?, ?)", rows)
        snap = self.get_snapshot(sid)
        assert snap is not None
        return snap

    def _row_to_snapshot(self, row: sqlite3.Row) -> Snapshot:
        return Snapshot(
            id=row["id"],
            taken_at=from_iso(row["taken_at"]),
            account_id=row["account_id"],
            account_name=row["account_name"],
            sections=json.loads(row["sections"]),
            errors=json.loads(row["errors"]),
            duration=row["duration"],
            complete=bool(row["complete"]),
        )

    def get_snapshot(self, snapshot_id: int) -> Snapshot | None:
        rows = self._query("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,))
        return self._row_to_snapshot(rows[0]) if rows else None

    def latest_snapshot(self) -> Snapshot | None:
        rows = self._query("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1")
        return self._row_to_snapshot(rows[0]) if rows else None

    def first_snapshot(self) -> Snapshot | None:
        rows = self._query("SELECT * FROM snapshots ORDER BY id ASC LIMIT 1")
        return self._row_to_snapshot(rows[0]) if rows else None

    def snapshot_at(self, when: datetime) -> Snapshot | None:
        """The last snapshot taken at or before ``when`` (falls back to the first one after)."""
        rows = self._query(
            "SELECT * FROM snapshots WHERE taken_at <= ? ORDER BY taken_at DESC, id DESC LIMIT 1",
            (to_iso(when),),
        )
        if not rows:
            rows = self._query(
                "SELECT * FROM snapshots WHERE taken_at > ? ORDER BY taken_at ASC, id ASC LIMIT 1",
                (to_iso(when),),
            )
        return self._row_to_snapshot(rows[0]) if rows else None

    def list_snapshots(
        self, *, since: datetime | None = None, until: datetime | None = None, limit: int = 50
    ) -> list[Snapshot]:
        sql = "SELECT * FROM snapshots WHERE 1=1"
        params: list[Any] = []
        if since:
            sql += " AND taken_at >= ?"
            params.append(to_iso(since))
        if until:
            sql += " AND taken_at <= ?"
            params.append(to_iso(until))
        sql += " ORDER BY taken_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [self._row_to_snapshot(r) for r in self._query(sql, params)]

    def count_snapshots(self) -> int:
        return int(self._query("SELECT COUNT(*) AS n FROM snapshots")[0]["n"])

    # ---------------------------------------------------------- observations
    def _state_locked(self, kind: str, snapshot_id: int | None) -> dict[str, str]:
        params: list[Any] = [kind]
        cond = ""
        if snapshot_id is not None:
            cond = " AND snapshot_id <= ?"
            params.append(snapshot_id)
        params.append(kind)
        rows = self._conn.execute(
            "SELECT o.key, o.value FROM observations o JOIN ("
            f"  SELECT key, MAX(snapshot_id) AS sid FROM observations WHERE kind = ?{cond} GROUP BY key"
            ") m ON o.key = m.key AND o.snapshot_id = m.sid WHERE o.kind = ?",
            params,
        ).fetchall()
        return {r["key"]: r["value"] for r in rows if r["value"] is not None}

    def state(self, kind: str, snapshot_id: int | None = None) -> dict[str, Any]:
        """All present ``{key: value}`` pairs of ``kind`` as of ``snapshot_id`` (default: latest)."""
        with self._lock:
            raw = self._state_locked(kind, snapshot_id)
        return {k: json.loads(v) for k, v in raw.items()}

    def value_at(self, kind: str, key: str, snapshot_id: int | None = None) -> Any:
        sql = "SELECT value FROM observations WHERE kind = ? AND key = ?"
        params: list[Any] = [kind, str(key)]
        if snapshot_id is not None:
            sql += " AND snapshot_id <= ?"
            params.append(snapshot_id)
        sql += " ORDER BY snapshot_id DESC LIMIT 1"
        rows = self._query(sql, params)
        if not rows or rows[0]["value"] is None:
            return None
        return json.loads(rows[0]["value"])

    def series(
        self,
        kind: str,
        key: str | None = None,
        *,
        key_prefix: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Observation]:
        """Change points of a series (or of all keys sharing a prefix), oldest first."""
        sql = (
            "SELECT o.key, o.value, o.snapshot_id, s.taken_at FROM observations o"
            " JOIN snapshots s ON s.id = o.snapshot_id WHERE o.kind = ?"
        )
        params: list[Any] = [kind]
        if key is not None:
            sql += " AND o.key = ?"
            params.append(str(key))
        if key_prefix is not None:
            sql += " AND substr(o.key, 1, ?) = ?"
            params += [len(key_prefix), key_prefix]
        if since:
            sql += " AND s.taken_at >= ?"
            params.append(to_iso(since))
        if until:
            sql += " AND s.taken_at <= ?"
            params.append(to_iso(until))
        sql += " ORDER BY s.taken_at ASC, o.snapshot_id ASC, o.key ASC"
        return [
            Observation(
                snapshot_id=r["snapshot_id"],
                taken_at=from_iso(r["taken_at"]),
                key=r["key"],
                value=json.loads(r["value"]) if r["value"] is not None else None,
            )
            for r in self._query(sql, params)
        ]

    def kinds(self) -> list[str]:
        return [r["kind"] for r in self._query("SELECT DISTINCT kind FROM observations ORDER BY kind")]

    def keys(self, kind: str) -> list[str]:
        return [
            r["key"] for r in self._query("SELECT DISTINCT key FROM observations WHERE kind = ? ORDER BY key", (kind,))
        ]

    # --------------------------------------------------------------- catalog
    def catalog_get(self, endpoint: str, ids: Iterable[str], max_age: float | None = None) -> dict[str, Any]:
        ids = [str(i) for i in ids]
        if not ids:
            return {}
        out: dict[str, Any] = {}
        cutoff = time.time() - max_age if max_age is not None else None
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            sql = f"SELECT id, data, fetched_at FROM catalog WHERE endpoint = ? AND id IN ({marks})"
            for r in self._query(sql, [endpoint, *chunk]):
                if cutoff is None or r["fetched_at"] >= cutoff:
                    out[r["id"]] = json.loads(r["data"])
        return out

    def catalog_put(self, endpoint: str, objects: Iterable[Mapping[str, Any]]) -> int:
        now = time.time()
        rows = [
            (endpoint, str(o["id"]), _name_of(o), json.dumps(o, separators=(",", ":")), now)
            for o in objects
            if "id" in o
        ]
        with self._tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO catalog(endpoint, id, name, data, fetched_at) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def catalog_mark_indexed(self, endpoint: str, count: int) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO catalog_index(endpoint, built_at, count) VALUES (?, ?, ?)",
                (endpoint, time.time(), count),
            )

    def catalog_index_info(self, endpoint: str) -> tuple[float, int] | None:
        rows = self._query("SELECT built_at, count FROM catalog_index WHERE endpoint = ?", (endpoint,))
        return (rows[0]["built_at"], rows[0]["count"]) if rows else None

    def catalog_search(self, endpoint: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Case-insensitive name search: exact matches first, then prefix, then substring."""
        q = query.strip().lower()
        if not q:
            return []
        words = [w for w in q.split() if w]
        cond = " AND ".join(["instr(lower(name), ?) > 0"] * len(words))
        rows = self._query(
            f"SELECT data, lower(name) AS lname FROM catalog WHERE endpoint = ? AND name IS NOT NULL AND {cond}",
            [endpoint, *words],
        )

        def rank(row: sqlite3.Row) -> tuple[int, int, str]:
            name = row["lname"]
            if name == q:
                tier = 0
            elif name.startswith(q):
                tier = 1
            elif q in name:
                tier = 2
            else:
                tier = 3
            return (tier, len(name), name)

        rows = sorted(rows, key=rank)[:limit]
        return [json.loads(r["data"]) for r in rows]


def _name_of(obj: Mapping[str, Any]) -> str | None:
    for field in ("name", "title", "label"):
        value = obj.get(field)
        if isinstance(value, str) and value:
            return value
    return None
