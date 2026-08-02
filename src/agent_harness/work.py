"""Work items and claims: which agent is doing what, and what survives a crash.

Claims are **leases, not locks.** A lock held by a process that died is a
lock nobody can release, and the usual workaround — a human clearing stale
state — is exactly the unattended-operation failure this exists to prevent.
A lease expires on its own, so a worker that is killed mid-item releases it
by doing nothing at all.

The lease is kept alive by a heartbeat while work is genuinely in progress.
That distinction matters: "slow" and "dead" look identical from outside, and
a fleet that cannot tell them apart either kills healthy long-running work
or waits forever on a corpse. A heartbeat separates them, because only a
live process can keep stamping one.

This table is mutable, unlike `events` — a claim's whole purpose is to
change. The event stream still records every transition, so history stays
append-only even though current state does not.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Lease length. Long enough that an agent thinking hard about a hard problem
# is not evicted; short enough that a crashed worker's item is picked up in
# the same session rather than the next day.
DEFAULT_LEASE_SECONDS = 900.0

PENDING = "pending"
CLAIMED = "claimed"
DONE = "done"
FAILED = "failed"
BLOCKED = "blocked"

# Fleet control. Deliberately only three states, and none of them kill
# anything: stopping work mid-item destroys an agent's context and leaves a
# half-finished worktree, which is worse than waiting for it to finish.
#
#   running   claim freely
#   paused    stop claiming; in-flight work continues to completion
#   draining  same as paused, and the intent is to stop once it is quiet
#
# `paused` and `draining` behave identically to a worker. The difference is
# what the operator meant, which matters when someone else looks at the fleet
# and has to decide whether to resume it.
RUNNING = "running"
PAUSED = "paused"
DRAINING = "draining"
CONTROL_STATES = (RUNNING, PAUSED, DRAINING)

SCHEMA = """
CREATE TABLE IF NOT EXISTS work (
    item_id     TEXT PRIMARY KEY,
    issue       INTEGER,
    title       TEXT NOT NULL,
    brief       TEXT NOT NULL DEFAULT '',
    depends_on  TEXT NOT NULL DEFAULT '[]',
    state       TEXT NOT NULL DEFAULT 'pending',
    owner       TEXT,
    lease_until REAL NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    branch      TEXT,
    pr_url      TEXT,
    updated_at  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS work_state ON work (state, lease_until);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS control (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    state   TEXT NOT NULL DEFAULT 'running',
    reason  TEXT,
    changed_at REAL NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO control (id, state) VALUES (1, 'running');
"""


class ClaimLost(Exception):
    """This worker no longer owns the item it is working on.

    Raised when a heartbeat is refused: the lease expired while the work was
    still running and someone else re-claimed the row. Continuing past this
    point spends tokens on work that will be thrown away, and finishing would
    overwrite the new owner's claim.

    The correct response is to stop and release NOTHING -- the item is not
    ours to release.
    """


def worker_identity() -> str:
    """Who holds a claim. Host and pid, so a stale claim can be traced to a
    specific process rather than to an anonymous 'someone'."""
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass
class WorkRecord:
    item_id: str
    title: str
    brief: str = ""
    issue: int | None = None
    depends_on: list[str] = field(default_factory=list)
    state: str = PENDING
    owner: str | None = None
    lease_until: float = 0.0
    attempts: int = 0
    last_error: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    updated_at: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> WorkRecord:
        data = dict(row)
        data["depends_on"] = json.loads(data.get("depends_on") or "[]")
        return cls(**data)


class WorkQueue:
    """Claimable work, backed by the same SQLite file as the event store."""

    def __init__(
        self, path: str, lease_seconds: float = DEFAULT_LEASE_SECONDS, now: Any = time.time
    ) -> None:
        self.path = path
        self.lease_seconds = lease_seconds
        self.now = now
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ------------------------------------------------------------ loading

    def add(self, records: Iterable[WorkRecord]) -> int:
        """Add work, leaving anything already present untouched.

        Re-adding is how a re-synced plan reaches the queue, so it must not
        reset progress: an item already claimed or done stays that way, and
        only its description is refreshed.
        """
        conn = self._connect()
        added = 0
        for record in records:
            existing = conn.execute(
                "SELECT state FROM work WHERE item_id = ?", (record.item_id,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO work (item_id, issue, title, brief, depends_on, "
                    "state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.item_id,
                        record.issue,
                        record.title,
                        record.brief,
                        json.dumps(record.depends_on),
                        record.state,
                        self.now(),
                    ),
                )
                added += 1
            else:
                conn.execute(
                    "UPDATE work SET title = ?, brief = ?, depends_on = ?, issue = ?, "
                    "updated_at = ? WHERE item_id = ?",
                    (
                        record.title,
                        record.brief,
                        json.dumps(record.depends_on),
                        record.issue,
                        self.now(),
                        record.item_id,
                    ),
                )
        conn.close()
        return added

    # ------------------------------------------------------------ control

    def control(self) -> tuple[str, str | None]:
        """Current fleet control state and why it was set."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT state, reason FROM control WHERE id = 1").fetchone()
            return (row["state"], row["reason"]) if row else (RUNNING, None)
        finally:
            conn.close()

    def set_control(self, state: str, reason: str | None = None) -> None:
        """Pause, drain or resume the fleet.

        Takes effect at the next claim. Nothing in flight is interrupted:
        killing an agent mid-item destroys its context and leaves a
        half-finished worktree, which is worse than waiting.
        """
        if state not in CONTROL_STATES:
            raise ValueError(f"unknown control state {state!r}; expected {CONTROL_STATES}")
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE control SET state = ?, reason = ?, changed_at = ? WHERE id = 1",
                (state, reason, self.now()),
            )
        finally:
            conn.close()

    # ----------------------------------------------------------- settings

    def get_setting(self, key: str) -> Any | None:
        """Read a shared setting. Shared because the API process and the
        worker process are different processes: an in-memory value could
        never be changed from outside the loop that uses it."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return json.loads(row["value"]) if row else None
        finally:
            conn.close()

    def set_setting(self, key: str, value: Any) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, json.dumps(value), self.now()),
            )
        finally:
            conn.close()

    # ------------------------------------------------------------- claims

    def claim(self, owner: str | None = None) -> WorkRecord | None:
        """Take the next available item, or None.

        Available means: pending (or a lease that has expired), and every
        dependency done. The whole selection and claim happens inside one
        IMMEDIATE transaction, so two workers racing cannot both win — the
        loser sees the row already claimed and picks something else.
        """
        owner = owner or worker_identity()
        # Checked before anything is taken, so a pause stops the fleet at the
        # next item boundary rather than part-way through one.
        if self.control()[0] != RUNNING:
            return None
        now = self.now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM work WHERE (state = ? OR (state = ? AND lease_until < ?)) "
                "ORDER BY attempts, item_id",
                (PENDING, CLAIMED, now),
            ).fetchall()
            for candidate in row:
                record = WorkRecord.from_row(candidate)
                if not self._dependencies_met(conn, record):
                    continue
                conn.execute(
                    "UPDATE work SET state = ?, owner = ?, lease_until = ?, "
                    "attempts = attempts + 1, updated_at = ? WHERE item_id = ?",
                    (CLAIMED, owner, now + self.lease_seconds, now, record.item_id),
                )
                conn.execute("COMMIT")
                record.state = CLAIMED
                record.owner = owner
                record.lease_until = now + self.lease_seconds
                record.attempts += 1
                return record
            conn.execute("COMMIT")
            return None
        finally:
            conn.close()

    def _dependencies_met(self, conn: sqlite3.Connection, record: WorkRecord) -> bool:
        for dependency in record.depends_on:
            row = conn.execute("SELECT state FROM work WHERE item_id = ?", (dependency,)).fetchone()
            # A dependency on something not in the queue is not a blocker:
            # plans routinely reference work tracked elsewhere, and refusing
            # to start would strand the item forever.
            if row is not None and row["state"] != DONE:
                return False
        return True

    def heartbeat(self, item_id: str, owner: str) -> bool:
        """Extend the lease. Returns False if the claim was lost — which is
        the signal to stop working, because someone else now owns it."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE work SET lease_until = ?, updated_at = ? "
                "WHERE item_id = ? AND owner = ? AND state = ?",
                (self.now() + self.lease_seconds, self.now(), item_id, owner, CLAIMED),
            )
            return cursor.rowcount > 0
        finally:
            conn.close()

    def release(
        self,
        item_id: str,
        state: str,
        *,
        error: str | None = None,
        branch: str | None = None,
        pr_url: str | None = None,
        owner: str | None = None,
    ) -> bool:
        """Finish with an item. `state` is done, failed, blocked or pending
        (pending puts it back for another attempt).

        Returns True if the item was actually updated.

        **A worker must pass its `owner`.** A worker that stalled past its
        lease is not dead, only slow: it will surface eventually and report a
        result for an item that now belongs to someone else. Accepting that
        late report marks the item finished from work the new owner never did,
        and leaves the new owner running with nothing left to release. The
        guard makes the late report a no-op, which is what it is.

        `heartbeat` has always guarded on owner. Without the same guard here,
        the lease only held against the half of the race that asked politely.

        Omitting `owner` is an **administrative override** — the operator
        retrying a stuck item through the API has no worker identity, and
        guarding that would remove the one lever a human has over a wedged
        row.
        """
        conn = self._connect()
        try:
            sql = (
                "UPDATE work SET state = ?, owner = NULL, lease_until = 0, "
                "last_error = ?, branch = COALESCE(?, branch), "
                "pr_url = COALESCE(?, pr_url), updated_at = ? WHERE item_id = ?"
            )
            params: list[Any] = [state, error, branch, pr_url, self.now(), item_id]
            if owner is not None:
                sql += " AND owner = ?"
                params.append(owner)
            cursor = conn.execute(sql, params)
            return cursor.rowcount > 0
        finally:
            conn.close()

    # --------------------------------------------------------- projections

    def get(self, item_id: str) -> WorkRecord | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM work WHERE item_id = ?", (item_id,)).fetchone()
            return WorkRecord.from_row(row) if row else None
        finally:
            conn.close()

    def all(self) -> list[WorkRecord]:
        conn = self._connect()
        try:
            return [
                WorkRecord.from_row(r) for r in conn.execute("SELECT * FROM work ORDER BY item_id")
            ]
        finally:
            conn.close()

    def counts(self) -> dict[str, int]:
        conn = self._connect()
        try:
            return {
                r["state"]: r["n"]
                for r in conn.execute("SELECT state, COUNT(*) AS n FROM work GROUP BY state")
            }
        finally:
            conn.close()

    def stale(self) -> list[WorkRecord]:
        """Claims whose lease has expired: work that was being done by
        something that is no longer doing it."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM work WHERE state = ? AND lease_until < ?",
                (CLAIMED, self.now()),
            )
            return [WorkRecord.from_row(r) for r in rows]
        finally:
            conn.close()
