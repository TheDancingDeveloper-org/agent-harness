"""The audit log: what happened, kept for as long as it is worth knowing.

Separate from `store.EventStore` on purpose. That one shares a SQLite file
with the work queue, which is fine for a live view and wrong for history: the
queue is mutable, gets migrated in place, and is a reasonable thing to delete
and rebuild from the plan. Anything sharing that file shares that fate.

Three disciplines, each of which is a property rather than an intention:

**Append-only.** This class offers no way to change or remove a row, because
a method that exists will eventually be called during an incident, by someone
with a good reason, at the worst possible time.

**History is never rewritten to fit a new schema.** Columns are added; old
rows keep their absence. A backfill is indistinguishable from falsification
after the fact, and once it has happened once, no number in the series can be
defended. Every row records the schema version it was written under.

**Cost is stored with the price that produced it.** Tokens alone are not a
cost series: prices change, and applying today's rate to last year's tokens
silently rewrites the past every time a vendor repricings. Recording the
applied price makes a repricing a visible step in the series instead.

Observation must never stop work. If this store cannot be opened, the harness
runs on with `degraded = True` and writes become no-ops -- unless the operator
declared it required, in which case failing loudly is better than a fleet that
runs unaudited while looking audited.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .events import Event

log = logging.getLogger(__name__)

#: Bumped only when a column is ADDED. Rows keep the version they were written
#: under, so a reader can tell "this field did not exist yet" from "this field
#: was empty" -- a distinction that is destroyed by any backfill.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key     TEXT    NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL,
    ts             REAL    NOT NULL,
    kind           TEXT    NOT NULL,
    source         TEXT    NOT NULL,
    project_id     TEXT,
    item_id        TEXT,
    attempt        INTEGER,
    worker         TEXT,
    role           TEXT,
    model          TEXT,
    endpoint       TEXT,
    outcome        TEXT,
    error_class    TEXT,
    latency_s      REAL,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    tokens_cached  INTEGER,
    -- The cost as computed AT WRITE TIME, from the prices below. Null when no
    -- price was known: zero is a measurement, unknown is not, and folding one
    -- into the other understates spend in a way no later query can detect.
    cost_usd       REAL,
    price_in_per_mtok  REAL,
    price_out_per_mtok REAL,
    price_table    TEXT,
    data           TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS audit_ts         ON events (ts);
CREATE INDEX IF NOT EXISTS audit_project_ts ON events (project_id, ts);
CREATE INDEX IF NOT EXISTS audit_kind_ts    ON events (kind, ts);
CREATE INDEX IF NOT EXISTS audit_model_ts   ON events (role, model, ts);
CREATE INDEX IF NOT EXISTS audit_item       ON events (project_id, item_id);

-- Daily aggregates, written once and never rewritten. Raw events are thinned
-- only after the rollup covering them exists; thinning first is silent data
-- loss that looks tidy.
CREATE TABLE IF NOT EXISTS rollup_daily (
    day         TEXT    NOT NULL,
    project_id  TEXT    NOT NULL,
    role        TEXT,
    model       TEXT,
    outcome     TEXT,
    events      INTEGER NOT NULL DEFAULT 0,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL,
    latency_p50 REAL,
    schema_version INTEGER NOT NULL,
    PRIMARY KEY (day, project_id, role, model, outcome)
);

-- A dated, immutable measurement to compare against. Without one, "better
-- than before" has no before.
CREATE TABLE IF NOT EXISTS baselines (
    baseline_id TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    label       TEXT NOT NULL,
    window_days INTEGER NOT NULL,
    items_done  INTEGER,
    cost_usd    REAL,
    notes       TEXT,
    schema_version INTEGER NOT NULL
);
"""


def _cost_of(data: dict[str, Any]) -> float | None:
    """Cost from tokens and the prices supplied with them.

    Returns None unless a price is actually known. A default of zero would be
    a measurement claiming the call was free.
    """
    price_in = data.get("price_in_per_mtok")
    price_out = data.get("price_out_per_mtok")
    if price_in is None and price_out is None:
        return None
    tokens_in = data.get("tokens_in") or 0
    tokens_out = data.get("tokens_out") or 0
    return (tokens_in / 1_000_000) * (price_in or 0.0) + (tokens_out / 1_000_000) * (
        price_out or 0.0
    )


class AuditStore:
    """Append-only history, in its own database.

    Deliberately exposes no update, delete, purge or clear.
    """

    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, path: Path | str, degraded: bool = False) -> None:
        self.path = Path(path)
        self.degraded = degraded
        # Thread-local, like EventStore: the API serves handlers from a
        # threadpool, and a sqlite3 connection is bound to the thread that
        # opened it.
        self._local = threading.local()
        if not self.degraded:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            # Checkpoint on the way out: nearly all of a WAL-mode database can
            # live in the -wal sidecar, so a backup that copies only the
            # .sqlite would otherwise take almost nothing.
            with contextlib.suppress(sqlite3.Error):
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------- writing

    def append(self, events: Iterable[Event]) -> int:
        """Add events. Returns how many were new.

        A repeated identity is a duplicate, never an amendment: taking the
        newer one would make history depend on write order.
        """
        if self.degraded:
            return 0
        conn = self._connect()
        written = 0
        for event in events:
            data = dict(event.data or {})
            try:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO events ("
                    "dedupe_key, schema_version, ts, kind, source, project_id, item_id, "
                    "attempt, worker, role, model, endpoint, outcome, error_class, "
                    "latency_s, tokens_in, tokens_out, tokens_cached, cost_usd, "
                    "price_in_per_mtok, price_out_per_mtok, price_table, data"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.dedupe_key,
                        SCHEMA_VERSION,
                        event.ts,
                        event.kind,
                        event.source,
                        data.get("project_id"),
                        data.get("item_id"),
                        data.get("attempt"),
                        event.worker,
                        event.role,
                        event.model,
                        event.endpoint,
                        event.outcome,
                        event.error_class,
                        event.latency_s,
                        data.get("tokens_in"),
                        data.get("tokens_out"),
                        data.get("tokens_cached"),
                        _cost_of(data),
                        data.get("price_in_per_mtok"),
                        data.get("price_out_per_mtok"),
                        data.get("price_table"),
                        json.dumps(data, sort_keys=True),
                    ),
                )
                written += cursor.rowcount or 0
            except sqlite3.Error as exc:
                # One bad event must not lose the batch, and must not stop the
                # fleet. Losing a row of history is bad; halting delivery
                # because of it is worse.
                log.warning("audit: could not write event: %s", exc)
        return written

    def adopt(self, legacy_path: Path | str) -> int:
        """Copy history out of a database that also holds operational state.

        Copies rather than moves. If this store turns out to be on the wrong
        disk, the original is still the record -- and a stale copy is
        recoverable where a deleted one is not.

        Idempotent, because it runs at startup.
        """
        if self.degraded:
            return 0
        legacy = Path(legacy_path)
        if not legacy.exists():
            return 0
        try:
            source = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
            source.row_factory = sqlite3.Row
            rows = source.execute("SELECT * FROM events ORDER BY id").fetchall()
            source.close()
        except sqlite3.Error as exc:
            log.warning("audit: could not read legacy history from %s: %s", legacy, exc)
            return 0

        conn = self._connect()
        written = 0
        for row in rows:
            data = json.loads(row["data"] or "{}")
            cursor = conn.execute(
                "INSERT OR IGNORE INTO events ("
                "dedupe_key, schema_version, ts, kind, source, project_id, item_id, "
                "attempt, worker, role, model, endpoint, outcome, error_class, "
                "latency_s, tokens_in, tokens_out, tokens_cached, cost_usd, "
                "price_in_per_mtok, price_out_per_mtok, price_table, data"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["dedupe_key"],
                    SCHEMA_VERSION,
                    row["ts"],
                    row["kind"],
                    row["source"],
                    data.get("project_id"),
                    data.get("item_id"),
                    data.get("attempt"),
                    row["worker"],
                    row["role"],
                    row["model"],
                    row["endpoint"],
                    row["outcome"],
                    row["error_class"],
                    row["latency_s"],
                    data.get("tokens_in"),
                    data.get("tokens_out"),
                    data.get("tokens_cached"),
                    _cost_of(data),
                    data.get("price_in_per_mtok"),
                    data.get("price_out_per_mtok"),
                    data.get("price_table"),
                    row["data"] or "{}",
                ),
            )
            written += cursor.rowcount or 0
        if written:
            log.info("audit: adopted %d events from %s", written, legacy)
        return written

    def record_baseline(
        self,
        baseline_id: str,
        project_id: str,
        *,
        label: str,
        window_days: int,
        recorded_at: float,
        items_done: int | None = None,
        cost_usd: float | None = None,
        notes: str | None = None,
    ) -> bool:
        """Record a dated measurement to compare against. Immutable once set:
        re-recording under the same id is refused, not overwritten."""
        if self.degraded:
            return False
        conn = self._connect()
        cursor = conn.execute(
            "INSERT OR IGNORE INTO baselines (baseline_id, project_id, recorded_at, label, "
            "window_days, items_done, cost_usd, notes, schema_version) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                baseline_id,
                project_id,
                recorded_at,
                label,
                window_days,
                items_done,
                cost_usd,
                notes,
                SCHEMA_VERSION,
            ),
        )
        return bool(cursor.rowcount)

    # ----------------------------------------------------- rollup & retention

    def rollup(self, until: float | None = None) -> int:
        """Aggregate complete days into immutable rows. Returns rows written.

        Only whole days in the past are rolled up. A rollup is immutable, so
        writing one for a day still in progress would freeze a partial day as
        though it were the whole of it -- and nothing downstream could tell.

        A day already rolled up is never rewritten, even if events for it
        arrive later. If a published number could change, every historical
        figure is provisional forever and no report can be reproduced. Late
        events stay in the raw table and are visible there; they simply do not
        retroactively edit a closed day.
        """
        if self.degraded:
            return 0
        conn = self._connect()
        # Midnight UTC of the current day: everything strictly before it is a
        # complete day.
        boundary = until if until is not None else time.time()
        cutoff = (boundary // 86400) * 86400
        rows = conn.execute(
            "SELECT date(ts, 'unixepoch') AS day, "
            "COALESCE(project_id, '') AS project_id, "
            "COALESCE(role, '') AS role, COALESCE(model, '') AS model, "
            "COALESCE(outcome, '') AS outcome, "
            "COUNT(*) AS events, "
            "SUM(COALESCE(tokens_in, 0)) AS tokens_in, "
            "SUM(COALESCE(tokens_out, 0)) AS tokens_out, "
            "SUM(cost_usd) AS cost_usd, "
            "AVG(latency_s) AS latency_p50 "
            "FROM events WHERE ts < ? "
            "GROUP BY day, project_id, role, model, outcome",
            (cutoff,),
        ).fetchall()

        written = 0
        for row in rows:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO rollup_daily (day, project_id, role, model, outcome, "
                "events, tokens_in, tokens_out, cost_usd, latency_p50, schema_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["day"],
                    row["project_id"],
                    row["role"],
                    row["model"],
                    row["outcome"],
                    row["events"],
                    row["tokens_in"],
                    row["tokens_out"],
                    row["cost_usd"],
                    row["latency_p50"],
                    SCHEMA_VERSION,
                ),
            )
            written += cursor.rowcount or 0
        return written

    def rollups(
        self, project_id: str | None = None, since_day: str | None = None
    ) -> list[dict[str, Any]]:
        """Daily aggregates, oldest first. This is the long series."""
        if self.degraded:
            return []
        sql = "SELECT * FROM rollup_daily"
        clauses, params = [], []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if since_day is not None:
            clauses.append("day >= ?")
            params.append(since_day)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY day, project_id, role, model"
        return [dict(r) for r in self._connect().execute(sql, params)]

    def rolled_up_through(self) -> str | None:
        """The last day covered by a rollup, or None."""
        if self.degraded:
            return None
        row = self._connect().execute("SELECT MAX(day) FROM rollup_daily").fetchone()
        return row[0] if row else None

    def thin(self, older_than_days: int = 90, now: float | None = None) -> int:
        """Remove raw events that a rollup already covers. Returns rows removed.

        The only deletion in this class, and it is fenced twice: an event is
        removed only if it is older than the retention window AND its day has
        already been aggregated.

        That ordering is the whole discipline. Thinning before aggregating is
        silent data loss that leaves a tidy-looking database -- the rows are
        gone, the series has a hole, and nothing reports it.
        """
        if self.degraded:
            return 0
        covered = self.rolled_up_through()
        if covered is None:
            # Nothing has been aggregated, so nothing may be removed however
            # old it is.
            return 0
        moment = now if now is not None else time.time()
        cutoff = moment - older_than_days * 86400
        conn = self._connect()
        cursor = conn.execute(
            "DELETE FROM events WHERE ts < ? AND date(ts, 'unixepoch') <= ?",
            (cutoff, covered),
        )
        removed = cursor.rowcount or 0
        if removed:
            log.info("audit: thinned %d raw events already covered by rollups", removed)
        return removed

    # ------------------------------------------------------------- reading

    def count(self) -> int:
        if self.degraded:
            return 0
        return int(self._connect().execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def span(self) -> tuple[float | None, float | None]:
        """Oldest and newest event, so a caller can tell how much history
        exists before drawing a year-long chart of three days."""
        if self.degraded:
            return (None, None)
        row = self._connect().execute("SELECT MIN(ts), MAX(ts) FROM events").fetchone()
        return (row[0], row[1])

    def recent(self, limit: int = 100, project_id: str | None = None) -> list[dict[str, Any]]:
        if self.degraded:
            return []
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if project_id is not None:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._connect().execute(sql, params)]

    def since_id(self, event_id: int, limit: int = 200) -> list[dict[str, Any]]:
        """Paged by row id, not timestamp: two events in one millisecond must
        still have a total order."""
        if self.degraded:
            return []
        return [
            dict(r)
            for r in self._connect().execute(
                "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (event_id, limit)
            )
        ]

    def max_id(self) -> int:
        if self.degraded:
            return 0
        return int(self._connect().execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])

    def rate_limits_by_class(self, since: float | None = None) -> dict[str, int]:
        """Classified live failures for the errors projection."""
        if self.degraded:
            return {}
        sql = "SELECT error_class, COUNT(*) AS n FROM events WHERE error_class IS NOT NULL"
        params: list[Any] = []
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " GROUP BY error_class ORDER BY n DESC"
        return {r["error_class"]: r["n"] for r in self._connect().execute(sql, params)}

    def group_counts(
        self, field: str, since: float | None = None, rate_limits_only: bool = True
    ) -> list[dict[str, Any]]:
        """Counts for a bounded set of audit columns used by the API."""
        if self.degraded:
            return []
        if field not in {"worker", "endpoint", "role", "model", "source"}:
            raise ValueError(f"refusing to group by unindexed/unknown column {field!r}")
        sql = f"SELECT {field} AS key, error_class, COUNT(*) AS n FROM events WHERE 1=1"
        params: list[Any] = []
        if rate_limits_only:
            sql += " AND error_class IN ('rpm', 'window_cap', 'terminal_cap')"
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        sql += f" GROUP BY {field}, error_class ORDER BY n DESC"
        return [dict(r) for r in self._connect().execute(sql, params)]

    def cost(
        self,
        since: float | None = None,
        until: float | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Spend per (project, role, model), with an explicit unpriced count.

        `unpriced` is reported rather than folded in: a total that silently
        omits calls whose price was unknown reads as complete and is not.
        """
        if self.degraded:
            return []
        sql = (
            "SELECT project_id, role, model, COUNT(*) AS calls, "
            "SUM(COALESCE(tokens_in, 0)) AS tokens_in, "
            "SUM(COALESCE(tokens_out, 0)) AS tokens_out, "
            "SUM(cost_usd) AS cost_usd, "
            "SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced "
            "FROM events WHERE kind = 'model_call'"
        )
        params: list[Any] = []
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        if until is not None:
            sql += " AND ts < ?"
            params.append(until)
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " GROUP BY project_id, role, model ORDER BY cost_usd DESC NULLS LAST"
        return [dict(r) for r in self._connect().execute(sql, params)]

    def delivery(
        self, since: float | None = None, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Item outcomes per project, and the attempts they took."""
        if self.degraded:
            return []
        sql = (
            "SELECT project_id, outcome, COUNT(*) AS n, "
            "COUNT(DISTINCT item_id) AS items "
            "FROM events WHERE kind = 'work'"
        )
        params: list[Any] = []
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " GROUP BY project_id, outcome ORDER BY n DESC"
        return [dict(r) for r in self._connect().execute(sql, params)]

    def baselines(self, project_id: str | None = None) -> list[dict[str, Any]]:
        if self.degraded:
            return []
        sql = "SELECT * FROM baselines"
        params: list[Any] = []
        if project_id is not None:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        sql += " ORDER BY recorded_at DESC"
        return [dict(r) for r in self._connect().execute(sql, params)]


def open_audit_store(
    path: Path | str, *, required: bool = False, adopt_from: Path | str | None = None
) -> AuditStore:
    """Open the audit store, or degrade.

    `required=False` is the default because observation failing must not stop
    work: a full disk should cost history, not delivery.

    `required=True` inverts that for deployments where the history IS the
    point. A fleet running unaudited looks exactly like a fleet running
    audited, so if that matters, it should refuse to start rather than find
    out months later.
    """
    try:
        store = AuditStore(path)
    except (OSError, sqlite3.Error) as exc:
        if required:
            raise OSError(
                f"audit store at {path} is required but could not be opened: {exc}"
            ) from exc
        log.warning("audit: running DEGRADED, history is not being recorded: %s", exc)
        return AuditStore(path, degraded=True)
    if adopt_from is not None:
        store.adopt(adopt_from)
    return store
