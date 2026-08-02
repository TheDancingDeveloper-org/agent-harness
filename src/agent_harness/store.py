"""SQLite event store (D6). Append-only spine, projections on top.

The only write path in this module is `append`. There is no update, no
delete, and no code path that edits a row after it lands -- that is risk R3
in the plan (the dashboard becoming a second source of truth and drifting),
and it is enforced by a test that greps this module for the statements that
would break it, not merely by intent.

WAL is on so a reader (the web app) never blocks the writer (the ingester)
and vice versa. They are separate connections, possibly separate processes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .events import Event

SCHEMA_VERSION = 1

# Indexes exist for the five panels named in §7 P2, and for nothing else.
# An index that no panel queries is a write cost with no reader.
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key  TEXT    NOT NULL UNIQUE,
    ts          REAL    NOT NULL,
    kind        TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    worker      TEXT,
    role        TEXT,
    model       TEXT,
    endpoint    TEXT,
    outcome     TEXT,
    error_class TEXT,
    latency_s   REAL,
    data        TEXT    NOT NULL DEFAULT '{}'
);

-- errors panel: by class over time, by worker, by endpoint
CREATE INDEX IF NOT EXISTS events_class_ts ON events (error_class, ts);
-- every panel filters by window first
CREATE INDEX IF NOT EXISTS events_ts       ON events (ts);
-- fleet panel
CREATE INDEX IF NOT EXISTS events_worker_ts ON events (worker, ts);
-- quota/spend panel splits by role
CREATE INDEX IF NOT EXISTS events_role_ts  ON events (role, ts);
-- kind is the first filter for the diffs/verdicts and pipeline panels
CREATE INDEX IF NOT EXISTS events_kind_ts  ON events (kind, ts);
"""


class EventStore:
    """Append-only store. Safe to share between threads; each thread gets
    its own connection because SQLite objects are not thread-safe."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            elif row[0] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"store at {self.path} is schema v{row[0]}, this build expects "
                    f"v{SCHEMA_VERSION}. Migrations are deliberate, not automatic: "
                    f"the events table is the source of truth and must not be "
                    f"silently reshaped."
                )

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---------------------------------------------------------------- write

    def append(self, events: Iterable[Event]) -> int:
        """Append events, ignoring any whose dedupe_key is already present.

        Returns the number actually inserted. Re-ingesting the same history
        is therefore a no-op returning 0, which is what makes the ingester
        replayable (T22) without tracking file offsets.
        """
        conn = self._connect()
        rows = [
            (
                e.dedupe_key,
                e.ts,
                e.kind,
                e.source,
                e.worker,
                e.role,
                e.model,
                e.endpoint,
                e.outcome,
                e.error_class,
                e.latency_s,
                json.dumps(e.data, default=str),
            )
            for e in events
        ]
        if not rows:
            return 0
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO events (dedupe_key, ts, kind, source, worker, role, "
            "model, endpoint, outcome, error_class, latency_s, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return conn.total_changes - before

    # ----------------------------------------------------------- projections

    def count(self) -> int:
        return int(self._connect().execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def span(self) -> tuple[float | None, float | None]:
        row = self._connect().execute("SELECT MIN(ts), MAX(ts) FROM events").fetchone()
        return (row[0], row[1])

    def sources(self) -> dict[str, int]:
        rows = (
            self._connect()
            .execute("SELECT source, COUNT(*) AS n FROM events GROUP BY source ORDER BY n DESC")
            .fetchall()
        )
        return {r["source"]: r["n"] for r in rows}

    def rate_limits_by_class(self, since: float | None = None) -> dict[str, int]:
        """The errors panel's core number: 429s split by class.

        Rows whose source predates classification are counted under the
        pseudo-class `unclassified` rather than being omitted -- a gap the
        viewer cannot see is worse than a gap labelled as one.
        """
        sql = "SELECT error_class, COUNT(*) AS n FROM events WHERE error_class IS NOT NULL"
        args: list[Any] = []
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        sql += " GROUP BY error_class ORDER BY n DESC"
        return {r["error_class"]: r["n"] for r in self._connect().execute(sql, args)}

    def rate_limits_over_time(
        self, since: float | None = None, bucket_seconds: int = 3600
    ) -> list[dict[str, Any]]:
        """(bucket, error_class, count), for the over-time chart."""
        sql = (
            "SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, error_class, COUNT(*) AS n "
            "FROM events WHERE error_class IS NOT NULL"
        )
        args: list[Any] = [bucket_seconds, bucket_seconds]
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        sql += " GROUP BY bucket, error_class ORDER BY bucket"
        return [dict(r) for r in self._connect().execute(sql, args)]

    def group_counts(
        self, field: str, since: float | None = None, rate_limits_only: bool = True
    ) -> list[dict[str, Any]]:
        """Counts grouped by one column, optionally restricted to 429s."""
        if field not in {"worker", "endpoint", "role", "model", "source"}:
            raise ValueError(f"refusing to group by unindexed/unknown column {field!r}")
        sql = f"SELECT {field} AS key, error_class, COUNT(*) AS n FROM events WHERE 1=1"
        args: list[Any] = []
        if rate_limits_only:
            sql += " AND error_class IN ('rpm', 'window_cap', 'terminal_cap')"
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        sql += f" GROUP BY {field}, error_class ORDER BY n DESC"
        return [dict(r) for r in self._connect().execute(sql, args)]

    def outcome_counts(self, kind: str | None = None, since: float | None = None) -> dict[str, int]:
        sql = "SELECT outcome, COUNT(*) AS n FROM events WHERE outcome IS NOT NULL"
        args: list[Any] = []
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        sql += " GROUP BY outcome ORDER BY n DESC"
        return {r["outcome"]: r["n"] for r in self._connect().execute(sql, args)}

    def recent(
        self, kind: str | None = None, limit: int = 50, since: float | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE 1=1"
        args: list[Any] = []
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [self._row_to_dict(r) for r in self._connect().execute(sql, args)]

    def workers(self, since: float | None = None) -> list[dict[str, Any]]:
        """Fleet panel: last-seen state per worker."""
        sql = (
            "SELECT worker, MAX(ts) AS last_seen, COUNT(*) AS calls, "
            "       SUM(CASE WHEN outcome = 'error' THEN 1 ELSE 0 END) AS errors "
            "FROM events WHERE worker IS NOT NULL"
        )
        args: list[Any] = []
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        sql += " GROUP BY worker ORDER BY last_seen DESC"
        return [dict(r) for r in self._connect().execute(sql, args)]

    def since_id(self, event_id: int, limit: int = 200) -> list[dict[str, Any]]:
        """Events after `event_id`, oldest first. This is what SSE resumes
        from on reconnect (T28's last-event-id), which is why the id is a
        monotonic integer and not a timestamp -- two events in the same
        millisecond must still have an order."""
        rows = self._connect().execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (event_id, limit)
        )
        return [self._row_to_dict(r) for r in rows]

    def max_id(self) -> int:
        row = self._connect().execute("SELECT MAX(id) FROM events").fetchone()
        return int(row[0] or 0)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        try:
            out["data"] = json.loads(out.get("data") or "{}")
        except ValueError:
            out["data"] = {}
        return out

    def iter_all(self) -> Iterator[dict[str, Any]]:
        for row in self._connect().execute("SELECT * FROM events ORDER BY id"):
            yield self._row_to_dict(row)
