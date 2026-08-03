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
import logging
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
log = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 900.0

PENDING = "pending"
CLAIMED = "claimed"
DONE = "done"
FAILED = "failed"
BLOCKED = "blocked"
#: Tried too many times and given up on. Distinct from `failed`, which is one
#: attempt that did not work: `exhausted` says the harness will not try again
#: without a human. Without it, an item that reliably kills its worker is
#: re-claimed forever -- it sinks to the back of the queue on `attempts` and
#: returns every cycle, spending real money each time, looking identical to
#: an item that is merely busy.
EXHAUSTED = "exhausted"

#: Attempts before an item is given up on. Generous: a lease expiring because
#: a pod restarted is not the item's fault, and giving up on the first crash
#: would strand work a retry would have finished.
DEFAULT_MAX_ATTEMPTS = 5

# Fleet control, per project. None of these states kill anything: stopping
# work mid-item destroys an agent's context and leaves a half-finished
# worktree, which is worse than waiting for it to finish.
#
#   running   claim freely
#   paused    stop claiming; in-flight work continues to completion
#   draining  same as paused, and the intent is to stop once it is quiet
#   stopped   no workers exist for this project; only a human starts them
#
# `paused` and `draining` behave identically to a worker. The difference is
# what the operator meant, which matters when someone else looks at the fleet
# and has to decide whether to resume it.
#
# `stopped` is not a third flavour of pause. Pausing instructs a running
# fleet; `stopped` means there is no fleet to instruct. The distinction earns
# its place at boot, which stops every project regardless of what it was
# doing — so a project deliberately drained before a restart must not come
# back indistinguishable from one that was running happily.
RUNNING = "running"
PAUSED = "paused"
DRAINING = "draining"
STOPPED = "stopped"
CONTROL_STATES = (RUNNING, PAUSED, DRAINING, STOPPED)

#: How many candidate rows a single claim considers. The whole eligible
#: backlog used to be loaded into memory inside the write transaction on
#: every claim, which holds the write lock for longer the larger the backlog
#: gets. Ordering is by attempts then id, so the most deserving rows are in
#: the first page and a dependency-blocked page simply yields nothing.
CLAIM_SCAN_LIMIT = 200

#: Where work with no project of its own lives. Pre-project databases migrate
#: into this, so an upgrade never orphans a row.
DEFAULT_PROJECT = "default"

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    repo        TEXT,
    work_dir    TEXT,
    base_branch TEXT NOT NULL DEFAULT 'main',
    checks      TEXT NOT NULL DEFAULT '[]',
    plan_path   TEXT,
    roles       TEXT,
    max_workers INTEGER NOT NULL DEFAULT 1,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    created_at  REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0
);

-- (project_id, item_id), never item_id alone. Two plans that both name T1
-- are two items, and before this they were one row that silently overwrote
-- the other.
CREATE TABLE IF NOT EXISTS work (
    project_id  TEXT NOT NULL DEFAULT 'default',
    item_id     TEXT NOT NULL,
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
    updated_at  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, item_id)
);
CREATE INDEX IF NOT EXISTS work_state ON work (project_id, state, lease_until);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT 0
);

-- One row per project. `previous_state` is what it was doing before boot
-- stopped it, so "was running" and "was drained because we were deploying"
-- stay distinguishable to whoever decides whether to resume.
CREATE TABLE IF NOT EXISTS control (
    project_id     TEXT PRIMARY KEY,
    state          TEXT NOT NULL DEFAULT 'stopped',
    reason         TEXT,
    previous_state TEXT,
    changed_at     REAL NOT NULL DEFAULT 0
);

-- Sessions left running on purpose when an agent timed out. Killing one
-- destroys the context that makes the item resumable by a human, so they are
-- kept -- but kept means owned, not forgotten. Without this table nothing
-- knows they exist, and "preserved deliberately" and "leaked" become the
-- same thing after a week.
CREATE TABLE IF NOT EXISTS abandoned_sessions (
    session_id  TEXT PRIMARY KEY,
    item_id     TEXT NOT NULL,
    reason      TEXT,
    session_url TEXT,
    abandoned_at REAL NOT NULL
);
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
class Project:
    """A stream of work with its own queue, control state and configuration.

    Everything here was previously a CLI flag, which is why a restart meant
    re-supplying all of it: there was nowhere to persist what a project *is*.
    """

    project_id: str
    name: str
    repo: str | None = None
    work_dir: str | None = None
    base_branch: str = "main"
    checks: list[str] = field(default_factory=list)
    plan_path: str | None = None
    roles: dict[str, Any] | None = None
    max_workers: int = 1
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Project:
        data = dict(row)
        data["checks"] = json.loads(data.get("checks") or "[]")
        data["roles"] = json.loads(data["roles"]) if data.get("roles") else None
        return cls(**data)


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
    project_id: str = DEFAULT_PROJECT

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
        self._migrate()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ---------------------------------------------------------- migration

    def _migrate(self) -> None:
        """Bring a pre-project database up to the project-scoped schema.

        Runs on every open and must therefore be idempotent -- it decides by
        inspecting the table rather than by a version number, because a
        version number is one more thing that can disagree with reality.

        SQLite cannot alter a primary key, so `work` is rebuilt. Everything
        is inside one transaction: a migration that fails half way through a
        live queue would be far worse than one that refuses to start.
        """
        conn = self._connect()
        try:
            tables = {
                r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            # The migration writes rows that name a project, so the table it
            # names them into has to exist first. SCHEMA cannot simply run
            # before the migration: its index on work names project_id, which
            # the old table does not have.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    project_id  TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    repo        TEXT,
                    work_dir    TEXT,
                    base_branch TEXT NOT NULL DEFAULT 'main',
                    checks      TEXT NOT NULL DEFAULT '[]',
                    plan_path   TEXT,
                    roles       TEXT,
                    max_workers INTEGER NOT NULL DEFAULT 1,
                    created_at  REAL NOT NULL DEFAULT 0,
                    updated_at  REAL NOT NULL DEFAULT 0
                )
            """)
            if "work" in tables:
                columns = {r["name"] for r in conn.execute("PRAGMA table_info(work)")}
                if "project_id" not in columns:
                    self._migrate_work_to_projects(conn)
            if "control" in tables:
                columns = {r["name"] for r in conn.execute("PRAGMA table_info(control)")}
                if "project_id" not in columns:
                    self._migrate_control_to_projects(conn)
            self._add_missing_columns(conn)
        finally:
            conn.close()

    #: Columns added after a table first shipped. SQLite cannot add them via
    #: CREATE TABLE IF NOT EXISTS on an existing table, and the bootstrap in
    #: `_migrate` creates `projects` before SCHEMA runs -- so without this,
    #: every new column exists in the schema string and nowhere else. Additive
    #: only: nothing here drops or rewrites, so a rollback to an older build
    #: still reads its own columns.
    ADDED_COLUMNS = {
        "projects": {"max_attempts": "INTEGER NOT NULL DEFAULT 5"},
    }

    def _add_missing_columns(self, conn: sqlite3.Connection) -> None:
        for table, columns in self.ADDED_COLUMNS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue
            for name, declaration in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def _migrate_work_to_projects(self, conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""
                CREATE TABLE work_migrated (
                    project_id  TEXT NOT NULL DEFAULT 'default',
                    item_id     TEXT NOT NULL,
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
                    updated_at  REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (project_id, item_id)
                )
            """)
            conn.execute("""
                INSERT INTO work_migrated
                    (project_id, item_id, issue, title, brief, depends_on, state,
                     owner, lease_until, attempts, last_error, branch, pr_url, updated_at)
                SELECT 'default', item_id, issue, title, brief, depends_on, state,
                       owner, lease_until, attempts, last_error, branch, pr_url, updated_at
                FROM work
            """)
            conn.execute("DROP TABLE work")
            conn.execute("ALTER TABLE work_migrated RENAME TO work")
            # The rows now name a project. If that project does not exist,
            # every project-scoped query skips them: present in the table,
            # absent from the UI, which is worse than losing them loudly.
            conn.execute(
                "INSERT OR IGNORE INTO projects (project_id, name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (DEFAULT_PROJECT, "Default", self.now(), self.now()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _migrate_control_to_projects(self, conn: sqlite3.Connection) -> None:
        """The single global control row becomes the default project's."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            previous = conn.execute("SELECT state, reason FROM control WHERE id = 1").fetchone()
            conn.execute("DROP TABLE control")
            conn.execute("""
                CREATE TABLE control (
                    project_id     TEXT PRIMARY KEY,
                    state          TEXT NOT NULL DEFAULT 'stopped',
                    reason         TEXT,
                    previous_state TEXT,
                    changed_at     REAL NOT NULL DEFAULT 0
                )
            """)
            if previous is not None:
                # Deliberately `stopped`, not whatever it was: the process
                # that was running it is gone, so nothing is claiming. What it
                # was doing is kept in previous_state.
                conn.execute(
                    "INSERT INTO control (project_id, state, reason, previous_state, changed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        DEFAULT_PROJECT,
                        STOPPED,
                        previous["reason"],
                        previous["state"],
                        self.now(),
                    ),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ----------------------------------------------------------- projects

    def add_project(self, project: Project) -> None:
        """Register a project, or update one already registered.

        Persisting this is what makes a restart survivable: every field here
        used to be a CLI flag, so there was nothing to read back.
        """
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO projects (project_id, name, repo, work_dir, base_branch, "
                "checks, plan_path, roles, max_workers, max_attempts, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "name=excluded.name, repo=excluded.repo, work_dir=excluded.work_dir, "
                "base_branch=excluded.base_branch, checks=excluded.checks, "
                "plan_path=excluded.plan_path, roles=excluded.roles, "
                "max_workers=excluded.max_workers, max_attempts=excluded.max_attempts, "
                "updated_at=excluded.updated_at",
                (
                    project.project_id,
                    project.name,
                    project.repo,
                    project.work_dir,
                    project.base_branch,
                    json.dumps(project.checks),
                    project.plan_path,
                    json.dumps(project.roles) if project.roles else None,
                    project.max_workers,
                    project.max_attempts,
                    project.created_at or self.now(),
                    self.now(),
                ),
            )
            # A project starts stopped: registering one must not begin
            # spending money on it.
            conn.execute(
                "INSERT OR IGNORE INTO control (project_id, state, changed_at) VALUES (?, ?, ?)",
                (project.project_id, STOPPED, self.now()),
            )
        finally:
            conn.close()

    def projects(self) -> list[Project]:
        conn = self._connect()
        try:
            return [
                Project.from_row(r) for r in conn.execute("SELECT * FROM projects ORDER BY name")
            ]
        finally:
            conn.close()

    def get_project(self, project_id: str) -> Project | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            return Project.from_row(row) if row else None
        finally:
            conn.close()

    # ------------------------------------------------------------ loading

    def add(self, records: Iterable[WorkRecord], project_id: str = DEFAULT_PROJECT) -> int:
        """Add work to a project, leaving anything already present untouched.

        Re-adding is how a re-synced plan reaches the queue, so it must not
        reset progress: an item already claimed or done stays that way, and
        only its description is refreshed.

        The project is scoped explicitly rather than taken from the record so
        that loading a plan cannot silently scatter items across projects.
        """
        conn = self._connect()
        added = 0
        conn.execute(
            "INSERT OR IGNORE INTO projects (project_id, name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (project_id, project_id, self.now(), self.now()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO control (project_id, state, changed_at) VALUES (?, ?, ?)",
            (project_id, STOPPED, self.now()),
        )
        for record in records:
            existing = conn.execute(
                "SELECT state FROM work WHERE project_id = ? AND item_id = ?",
                (project_id, record.item_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO work (project_id, item_id, issue, title, brief, depends_on, "
                    "state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
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
                    "updated_at = ? WHERE project_id = ? AND item_id = ?",
                    (
                        record.title,
                        record.brief,
                        json.dumps(record.depends_on),
                        record.issue,
                        self.now(),
                        project_id,
                        record.item_id,
                    ),
                )
        conn.close()
        return added

    # ------------------------------------------------------------ control

    def control(self, project_id: str = DEFAULT_PROJECT) -> tuple[str, str | None]:
        """A project's control state and why it was set."""
        state, reason, _ = self.control_detail(project_id)
        return state, reason

    def control_detail(
        self, project_id: str = DEFAULT_PROJECT
    ) -> tuple[str, str | None, str | None]:
        """State, reason, and what it was doing before boot stopped it.

        The third value is what keeps "was running" distinguishable from "was
        drained because we were deploying" across a restart. Without it the
        operator's intent is the thing a restart destroys.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state, reason, previous_state FROM control WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                # An unregistered project is not running. Defaulting to
                # RUNNING here would mean a typo in a project id silently
                # granted claims.
                return (STOPPED, None, None)
            return (row["state"], row["reason"], row["previous_state"])
        finally:
            conn.close()

    def set_control(
        self,
        state: str,
        reason: str | None = None,
        project_id: str = DEFAULT_PROJECT,
    ) -> None:
        """Pause, drain, resume or stop one project.

        Takes effect at the next claim. Nothing in flight is interrupted:
        killing an agent mid-item destroys its context and leaves a
        half-finished worktree, which is worse than waiting.
        """
        if state not in CONTROL_STATES:
            raise ValueError(f"unknown control state {state!r}; expected {CONTROL_STATES}")
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO control (project_id, state, reason, changed_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET "
                "state=excluded.state, reason=excluded.reason, changed_at=excluded.changed_at",
                (project_id, state, reason, self.now()),
            )
        finally:
            conn.close()

    def stop_all_on_boot(self, reason: str = "process started") -> list[str]:
        """Stop every project, remembering what each was doing.

        Called once at startup. Nothing resumes on its own: an auto-resuming
        fleet turns a routine restart into unattended spend against a stack
        nobody has looked at yet, and a crash-looping deploy would restart the
        fleet on every loop.

        What each project WAS doing is preserved, so the operator who drained
        one deliberately does not come back to a screen that looks identical
        to the project that was running happily.
        """
        conn = self._connect()
        try:
            rows = conn.execute("SELECT project_id, state, reason FROM control").fetchall()
            stopped = []
            for row in rows:
                if row["state"] == STOPPED:
                    continue
                combined = (
                    f"{reason} (was {row['state']}: {row['reason']})"
                    if row["reason"]
                    else f"{reason} (was {row['state']})"
                )
                conn.execute(
                    "UPDATE control SET state = ?, reason = ?, previous_state = ?, "
                    "changed_at = ? WHERE project_id = ?",
                    (STOPPED, combined, row["state"], self.now(), row["project_id"]),
                )
                stopped.append(row["project_id"])
            return stopped
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

    def claim(
        self, owner: str | None = None, project_id: str = DEFAULT_PROJECT
    ) -> WorkRecord | None:
        """Take the next available item, or None.

        Available means: pending (or a lease that has expired), and every
        dependency done. The whole selection and claim happens inside one
        IMMEDIATE transaction, so two workers racing cannot both win — the
        loser sees the row already claimed and picks something else.
        """
        owner = owner or worker_identity()
        # Checked before anything is taken, so a pause stops the fleet at the
        # next item boundary rather than part-way through one.
        if self.control(project_id)[0] != RUNNING:
            return None
        now = self.now()
        conn = self._connect()
        try:
            limit = self.max_attempts_for(conn, project_id)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM work WHERE project_id = ? "
                "AND (state = ? OR (state = ? AND lease_until < ?)) "
                "ORDER BY attempts, item_id LIMIT ?",
                (project_id, PENDING, CLAIMED, now, CLAIM_SCAN_LIMIT),
            ).fetchall()
            for candidate in row:
                record = WorkRecord.from_row(candidate)
                if limit and record.attempts >= limit:
                    # Give up rather than recycle. An item that reliably kills
                    # its worker is never released, so its lease expires and it
                    # is re-claimed forever -- spending money every cycle while
                    # looking exactly like an item that is merely busy.
                    conn.execute(
                        "UPDATE work SET state = ?, owner = NULL, lease_until = 0, "
                        "last_error = ?, updated_at = ? WHERE project_id = ? AND item_id = ?",
                        (
                            EXHAUSTED,
                            f"gave up after {record.attempts} attempts",
                            now,
                            project_id,
                            record.item_id,
                        ),
                    )
                    continue
                if not self._dependencies_met(conn, record):
                    continue
                conn.execute(
                    "UPDATE work SET state = ?, owner = ?, lease_until = ?, "
                    "attempts = attempts + 1, updated_at = ? "
                    "WHERE project_id = ? AND item_id = ?",
                    (
                        CLAIMED,
                        owner,
                        now + self.lease_seconds,
                        now,
                        project_id,
                        record.item_id,
                    ),
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

    def max_attempts_for(self, conn: sqlite3.Connection, project_id: str) -> int:
        """The project's give-up threshold. 0 disables it.

        Read per claim rather than cached: raising it is how an operator
        rescues a batch of exhausted items, and that should take effect
        without a restart.
        """
        row = conn.execute(
            "SELECT max_attempts FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        return int(row["max_attempts"]) if row else DEFAULT_MAX_ATTEMPTS

    def _dependencies_met(self, conn: sqlite3.Connection, record: WorkRecord) -> bool:
        for dependency in record.depends_on:
            # Scoped to the item's own project: an id means one thing here and
            # something else there, and resolving across the boundary would
            # let a project unblock on another project's work.
            row = conn.execute(
                "SELECT state FROM work WHERE project_id = ? AND item_id = ?",
                (record.project_id, dependency),
            ).fetchone()
            # A dependency on something not in the queue is not a blocker:
            # plans routinely reference work tracked elsewhere, and refusing
            # to start would strand the item forever.
            if row is not None and row["state"] != DONE:
                return False
        return True

    def heartbeat(self, item_id: str, owner: str, project_id: str = DEFAULT_PROJECT) -> bool:
        """Extend the lease. Returns False if the claim was lost — which is
        the signal to stop working, because someone else now owns it."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE work SET lease_until = ?, updated_at = ? "
                "WHERE project_id = ? AND item_id = ? AND owner = ? AND state = ?",
                (
                    self.now() + self.lease_seconds,
                    self.now(),
                    project_id,
                    item_id,
                    owner,
                    CLAIMED,
                ),
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
        project_id: str = DEFAULT_PROJECT,
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
                "pr_url = COALESCE(?, pr_url), updated_at = ? "
                "WHERE project_id = ? AND item_id = ?"
            )
            params: list[Any] = [
                state,
                error,
                branch,
                pr_url,
                self.now(),
                project_id,
                item_id,
            ]
            if owner is not None:
                sql += " AND owner = ?"
                params.append(owner)
            cursor = conn.execute(sql, params)
            return cursor.rowcount > 0
        finally:
            conn.close()

    # --------------------------------------------------------- projections

    def get(self, item_id: str, project_id: str = DEFAULT_PROJECT) -> WorkRecord | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM work WHERE project_id = ? AND item_id = ?",
                (project_id, item_id),
            ).fetchone()
            return WorkRecord.from_row(row) if row else None
        finally:
            conn.close()

    def items(self, project_id: str | None = None) -> list[WorkRecord]:
        """Work items. Without a project, every project's -- the overview
        screen wants one call, not one per project."""
        conn = self._connect()
        try:
            if project_id is None:
                rows = conn.execute("SELECT * FROM work ORDER BY project_id, item_id")
            else:
                rows = conn.execute(
                    "SELECT * FROM work WHERE project_id = ? ORDER BY item_id", (project_id,)
                )
            return [WorkRecord.from_row(r) for r in rows]
        finally:
            conn.close()

    def checkpoint(self) -> None:
        """Fold the WAL back into the main database file.

        Called on clean shutdown. Nearly all of a WAL-mode database can live
        in the -wal sidecar -- this one was 4 KB against a 754 KB WAL in
        production -- so any backup or volume migration that copies only the
        .sqlite silently takes almost nothing.
        """
        conn = self._connect()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            log.warning("could not checkpoint %s: %s", self.path, exc)
        finally:
            conn.close()

    def counts(self, project_id: str | None = None) -> dict[str, int]:
        """Item count per state. Without a project, the cross-project rollup
        the overview screen needs."""
        conn = self._connect()
        try:
            if project_id is None:
                rows = conn.execute("SELECT state, COUNT(*) AS n FROM work GROUP BY state")
            else:
                rows = conn.execute(
                    "SELECT state, COUNT(*) AS n FROM work WHERE project_id = ? GROUP BY state",
                    (project_id,),
                )
            return {r["state"]: r["n"] for r in rows}
        finally:
            conn.close()

    # ------------------------------------------------- abandoned sessions

    def record_abandoned_session(
        self,
        session_id: str,
        item_id: str,
        *,
        reason: str | None = None,
        session_url: str | None = None,
    ) -> None:
        """Remember a session left alive after a timeout.

        Recording it is what makes the decision to keep it a decision. An
        unrecorded survivor is indistinguishable from a leak, and each one may
        still hold an agent spending tokens.
        """
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO abandoned_sessions "
                "(session_id, item_id, reason, session_url, abandoned_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, item_id, reason, session_url, self.now()),
            )
        finally:
            conn.close()

    def abandoned_sessions(self, older_than: float | None = None) -> list[dict[str, Any]]:
        """Sessions kept alive after a timeout, oldest first.

        `older_than` is an age in seconds, not a timestamp -- callers care
        that a session has been sitting for an hour, not what the clock said
        when it started.
        """
        conn = self._connect()
        try:
            sql = "SELECT * FROM abandoned_sessions"
            params: list[Any] = []
            if older_than is not None:
                sql += " WHERE abandoned_at <= ?"
                params.append(self.now() - older_than)
            sql += " ORDER BY abandoned_at"
            return [dict(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def forget_abandoned_session(self, session_id: str) -> None:
        """Drop the record once the session is actually gone."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM abandoned_sessions WHERE session_id = ?", (session_id,))
        finally:
            conn.close()

    def all(self) -> list[WorkRecord]:
        """Every item, across every project. Kept as the name the API uses."""
        return self.items()

    def stale(self, project_id: str | None = None) -> list[WorkRecord]:
        """Claims whose lease has expired: work that was being done by
        something that is no longer doing it."""
        conn = self._connect()
        try:
            sql = "SELECT * FROM work WHERE state = ? AND lease_until < ?"
            params: list[Any] = [CLAIMED, self.now()]
            if project_id is not None:
                sql += " AND project_id = ?"
                params.append(project_id)
            rows = conn.execute(sql, params)
            return [WorkRecord.from_row(r) for r in rows]
        finally:
            conn.close()
