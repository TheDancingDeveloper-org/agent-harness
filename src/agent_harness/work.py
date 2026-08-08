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

import contextlib
import json
import logging
import os
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from .attempts import DEFAULT_MODE as DEFAULT_DURABILITY
from .attempts import AttemptLog
from .graph import (
    LOCAL_WORK,
    WORK_DECLARATION,
    DependencyGraph,
    DependencySpec,
    Readiness,
    ReadinessReason,
    Resolver,
    parse_dependencies,
)
from .holds import CANCELLED as HOLD_CANCELLED
from .holds import DEFAULT_MAX_HOLD_SECONDS, Answer, Hold, HoldError, Holds

# Re-exported deliberately, in the `X as X` form mypy reads as explicit. The
# queue's states are defined in `outcomes.py` because both of that module's
# vocabularies refer to them and this module refers to those vocabularies --
# but `from .work import DONE` is what every caller and every test has always
# written, and there is no reason to make a hundred of them say otherwise.
from .outcomes import BLOCKED as BLOCKED
from .outcomes import CLAIMED as CLAIMED
from .outcomes import DONE as DONE
from .outcomes import ESCALATED as ESCALATED_DISPOSITION
from .outcomes import EXHAUSTED as EXHAUSTED
from .outcomes import FAILED as FAILED
from .outcomes import HELD as HELD
from .outcomes import HOLD_EXPIRED as HOLD_EXPIRED
from .outcomes import PENDING as PENDING

# Lease length. Long enough that an agent thinking hard about a hard problem
# is not evicted; short enough that a crashed worker's item is picked up in
# the same session rather than the next day.
log = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 900.0

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

#: How many candidate rows a single claim reads **at a time**. The whole
#: eligible backlog used to be loaded into memory inside the write transaction
#: on every claim, which holds the write lock for longer the larger the
#: backlog gets. Ordering is by attempts then id, so the most deserving rows
#: are in the first page.
#:
#: A page, not the whole scan. This was once the scan, and a project whose
#: first page was entirely dependency-blocked was handed nothing at all while
#: ready work sat one row past the limit -- a permanently stalled fleet with a
#: full queue. `claim` now walks the pages until it finds something or runs
#: out; the limit still bounds how much is in memory at once.
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
    -- Stage K: `check command -> argv believed to clear it`. Recorded when a
    -- check fails; run only when `apply_fixes` is on.
    fixes       TEXT NOT NULL DEFAULT '{}',
    -- #155. Whether a declared fix may be run and its check re-run. Zero by
    -- default, so an upgrade changes nothing until an operator says so.
    apply_fixes INTEGER NOT NULL DEFAULT 0,
    -- Stage H: how often an attempt at this project is made durable.
    durability  TEXT NOT NULL DEFAULT '',
    -- Stage L. Per-item ceilings for this project. Zero is unlimited, which
    -- is the default, which is why an upgrade changes nothing. Whether that
    -- is the right default for an unattended run is D14, and it is open.
    max_item_seconds   REAL NOT NULL DEFAULT 0,
    max_item_spend_usd REAL NOT NULL DEFAULT 0,
    -- Stage J. How long a question may go unanswered before the item is
    -- returned to the queue with the question preserved. Not decoration: D12
    -- keeps the claim during a hold, so this is what stops one unanswered
    -- question from consuming a worker indefinitely.
    max_hold_seconds REAL NOT NULL DEFAULT 21600,
    plan_path   TEXT,
    plan_branch TEXT,
    roles       TEXT,
    max_workers INTEGER NOT NULL DEFAULT 1,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    min_free_disk_gb REAL NOT NULL DEFAULT 0,
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
    -- What this item produces: 'code' (a diff) or 'findings' (an answer,
    -- possibly with nothing to commit). Declared by the plan, never by the
    -- agent. Defaults to 'code', which is what every row written before
    -- this column existed meant.
    deliverable TEXT NOT NULL DEFAULT 'code',
    -- The graph revision this claim was admitted at. Admission and the check
    -- before the expensive gate have to be talking about the same graph; this
    -- is how the second one can tell that the first one saw a different one.
    admitted_revision INTEGER NOT NULL DEFAULT 0,
    -- WHY the item is in the state it is in. `state` says where it ended up;
    -- these say what put it there. `failed` covers a reviewer's rejection and
    -- a crashed worker alike, and those want different responses from a
    -- human, so the difference is stored rather than left in a log line.
    -- Empty on any row written before this column existed, and on any row
    -- nothing has finished with yet.
    disposition TEXT NOT NULL DEFAULT '',
    reason_kind TEXT NOT NULL DEFAULT '',
    -- Stage L. Per-item ceilings; zero means "take the project's", which is
    -- itself zero by default, which is unlimited. Three levels, one spelling.
    budget_seconds    REAL NOT NULL DEFAULT 0,
    budget_spend_usd  REAL NOT NULL DEFAULT 0,
    -- What this item has cost, accumulated across attempts, and how many of
    -- its calls nobody could price. While `unpriced_calls` is non-zero,
    -- `spend_usd` is a LOWER BOUND and the spend ceiling is unenforceable --
    -- unknown cost is never zero cost.
    spend_usd     REAL NOT NULL DEFAULT 0,
    unpriced_calls INTEGER NOT NULL DEFAULT 0,
    -- When work on this item first began. Not the current attempt's start:
    -- the wall-clock ceiling bounds the ITEM, and an item that crashes in a
    -- loop would otherwise reset its own clock on every re-claim.
    first_started_at REAL NOT NULL DEFAULT 0,
    -- Stage J. When a hold on this item gives up and returns it. Zero when
    -- the item is not held. Denormalised from `holds` so the reaper's sweep
    -- and the queue's own view of an item cannot disagree.
    held_until  REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, item_id)
);
CREATE INDEX IF NOT EXISTS work_state ON work (project_id, state, lease_until);

-- The mutable current plan projection. The event stream records transitions;
-- this table answers which exact local ref is current after a restart.
CREATE TABLE IF NOT EXISTS plans (
    project_id  TEXT PRIMARY KEY,
    branch      TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    target_sha  TEXT NOT NULL,
    head_sha    TEXT NOT NULL,
    plan_digest TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0
);

-- A short-lived, per-project lease for the serialized plan integration path.
-- It is separate from work claims: a promotion lease protects the expensive
-- read/gate/publish sequence across serving processes, while the append-only
-- promotion journal remains the recovery record if a process dies.
CREATE TABLE IF NOT EXISTS plan_promotion_leases (
    project_id  TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    lease_until REAL NOT NULL,
    updated_at  REAL NOT NULL DEFAULT 0
);

-- Append-only promotion history. A conflict or failed integration gate is a
-- fact worth retaining, even though it does not advance the plan head.
CREATE TABLE IF NOT EXISTS plan_promotions (
    promotion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    item_branch TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    item_sha TEXT,
    old_head_sha TEXT NOT NULL,
    new_head_sha TEXT,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);

-- Durable journal for rebuilding a plan branch after its target branch moves.
CREATE TABLE IF NOT EXISTS plan_refreshes (
    refresh_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    old_target_sha TEXT NOT NULL,
    new_target_sha TEXT NOT NULL,
    old_head_sha TEXT NOT NULL,
    new_head_sha TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);

-- Immutable identity journal for remote review webhooks or polling records.
-- The source adapter owns interpretation; this table only prevents a replay
-- from creating a second correction item.
CREATE TABLE IF NOT EXISTS remote_review_events (
    source TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    disposition TEXT NOT NULL,
    status TEXT NOT NULL,
    correction_item_id TEXT,
    received_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (source, remote_id)
);

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
    project_id  TEXT,
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


class LeaseHeartbeat:
    """Keep one claim alive for as long as its worker is working on it.

    Renewing only at stage boundaries made the lease a bound on the longest
    *stage*, not on how long a worker may be absent -- and the two stages that
    matter, an agent thinking and a full check suite, are both routinely
    longer than the lease. A single 915s agent run was enough: the lease
    lapsed 15s before the agent returned, another worker's claim scan retired
    the item underneath it, and the 15 minutes of work, the reason it ended
    and its worktree were all lost, because every later write was owner-scoped
    to an owner that no longer held the row.

    So the heartbeat runs on its own thread for the whole attempt. It is a
    daemon: a worker that dies takes its heartbeat with it, which is exactly
    what makes the lease expire and the item recoverable. The point is to make
    "slow" and "dead" distinguishable, and that only works if the stamping
    stops when the process does.
    """

    def __init__(
        self,
        queue: WorkQueue,
        item_id: str,
        owner: str,
        *,
        project_id: str = DEFAULT_PROJECT,
        interval: float | None = None,
    ) -> None:
        self.queue = queue
        self.item_id = item_id
        self.owner = owner
        self.project_id = project_id
        # A third of the lease, so two consecutive failed beats still leave
        # room for a third before the claim actually lapses. The floor only
        # bites for a lease short enough that the interval would busy-loop,
        # and it tracks the lease rather than a fixed second so a deployment
        # (or a test) with a short lease still beats often enough to keep it.
        self.interval = interval if interval is not None else max(0.05, queue.lease_seconds / 3.0)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        """Whether a beat was refused, i.e. the claim is already gone."""
        return self._lost.is_set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"harness-lease-{self.item_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    def __enter__(self) -> LeaseHeartbeat:
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                held = self.queue.heartbeat(self.item_id, self.owner, project_id=self.project_id)
            except Exception:  # noqa: BLE001 - a database blip is not a lost claim
                # Deliberately not treated as a loss. Giving up the item on a
                # transient sqlite error would throw away live work for a
                # condition the next beat will very likely recover from.
                log.warning("lease: could not beat for %s", self.item_id, exc_info=True)
                continue
            if not held:
                self._lost.set()
                return


#: States a rewritten brief rescues an item from. `done` is absent on purpose —
#: editing the description of finished work does not un-finish it — and so are
#: `claimed` and `pending`, which are not stuck.
STALLED = frozenset({FAILED, BLOCKED})


def revives(state: str, was: str | None, now: str | None) -> bool:
    """Does this refresh un-stick a stalled item?

    Only a changed **brief** does. Re-syncing an unchanged plan must leave
    every state exactly where it was, and fixing a typo in a title or adding a
    label is not an answer to whatever stopped the item.

    A changed brief is different in the one way that matters: the attempt that
    failed was made against wording that no longer exists. Leaving it `failed`
    records a verdict on a question nobody is asking any more — and it is
    silent, so a person who rewrites an item in response to an agent refusing
    it as impossible (#174) watches the next run say "nothing to do" (#178).
    """
    return state in STALLED and (was or "").strip() != (now or "").strip()


def _process_alive(pid: int) -> bool:
    """Whether a pid on this host is still running.

    Signal 0 performs the permission and existence checks without delivering
    anything. A pid we are not allowed to signal still exists, which is why
    `PermissionError` is a yes.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Anything unexpected is treated as alive: releasing a claim from a
        # live worker is far worse than leaving one for its lease to expire.
        return True
    return True


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
    #: `check command -> argv believed to clear it`. Declared, because only
    #: the person who wrote the check knows that `ruff format` fixes what
    #: `ruff format --check` reports. Always recorded when the check fails;
    #: run only if `apply_fixes` is on.
    fixes: dict[str, list[str]] = field(default_factory=dict)
    #: Whether a declared fix may be **run**, with its check then re-run and
    #: the re-run taken as the verdict (#155). Off by default: turning it on
    #: is the operator asserting that every command they declared a fix for is
    #: mechanically fixable — true of a formatter, false of a test suite, and
    #: not something the harness can check. See `executor.Checks`.
    apply_fixes: bool = False
    #: How often this project's attempts are made durable: `exit`, `boundary`
    #: or `sync`. Empty takes the deployment's default. See `attempts.py`.
    durability: str = ""
    #: Per-item ceilings for this project. Zero is unlimited, and unlimited is
    #: the default so an existing deployment upgrades unchanged. D14 asks
    #: whether that should stay true for an unattended run; it is open.
    max_item_seconds: float = 0.0
    max_item_spend_usd: float = 0.0
    #: How long a question on this project's items may go unanswered. Not
    #: unlimited by default, unlike the budgets: a hold keeps the claim, so an
    #: unbounded default would let one unanswered question tie up a worker for
    #: ever, and "unlimited" is not a safe reading of "nobody said".
    max_hold_seconds: float = DEFAULT_MAX_HOLD_SECONDS
    plan_path: str | None = None
    plan_branch: str | None = None
    roles: dict[str, Any] | None = None
    max_workers: int = 1
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    min_free_disk_gb: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Project:
        data = dict(row)
        data["checks"] = json.loads(data.get("checks") or "[]")
        data["fixes"] = json.loads(data.get("fixes") or "{}")
        data["apply_fixes"] = bool(data.get("apply_fixes"))
        data["durability"] = data.get("durability") or ""
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
    #: What this item produces: `code` (a diff) or `findings` (an answer).
    #: The plan declares it; the agent never chooses it, or the first hard
    #: test failure becomes an essay about why the test was wrong (#182).
    deliverable: str = "code"
    pr_url: str | None = None
    updated_at: float = 0.0
    project_id: str = DEFAULT_PROJECT
    #: Graph revision at the moment this item was claimed. 0 for anything
    #: never claimed under a Stage G build.
    admitted_revision: int = 0
    #: Why the item is in `state`, per `outcomes.py`. Empty means nobody has
    #: finished with it yet — which is a different thing from any of the five
    #: dispositions, and is why the empty string is not one of them.
    disposition: str = ""
    reason_kind: str = ""
    #: Per-item ceilings. Zero means "take the project's".
    budget_seconds: float = 0.0
    budget_spend_usd: float = 0.0
    #: What this item has cost across every attempt, and how many calls nobody
    #: could price. A non-zero `unpriced_calls` makes `spend_usd` a lower
    #: bound and the spend ceiling unenforceable.
    spend_usd: float = 0.0
    unpriced_calls: int = 0
    #: When work on this item first began, across attempts.
    first_started_at: float = 0.0
    #: When this hold gives up and returns the item. Zero when not held.
    held_until: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> WorkRecord:
        data = dict(row)
        data["depends_on"] = json.loads(data.get("depends_on") or "[]")
        return cls(**data)

    def dependency_specs(self, provenance: str = WORK_DECLARATION) -> list[DependencySpec]:
        """This item's declared edges, typed.

        `depends_on` stays a list of strings on the wire and in the row --
        that is what a plan writes and what every existing client sends --
        but each string is a token with a grammar, so `T1`,
        `external:tracker:TICKET-9` and `decision:D9` are three different
        kinds of edge rather than three strings that look alike.
        """
        return parse_dependencies(self.depends_on, provenance=provenance)


class WorkQueue:
    """Claimable work, backed by the same SQLite file as the event store."""

    def __init__(
        self,
        path: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: Any = time.time,
        *,
        resolvers: Mapping[str, Resolver] | None = None,
        durability: str = DEFAULT_DURABILITY,
        on_hold: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.path = path
        self.lease_seconds = lease_seconds
        self.now = now
        #: The typed dependency graph over the same file. Constructed with
        #: this queue's connection factory so admission can evaluate the graph
        #: inside the very transaction that hands out the work.
        self.graph = DependencyGraph(self._connect, now=now, resolvers=resolvers)
        #: Where each attempt got to. Same file for the same reason: a
        #: resumable position that can disappear independently of the item it
        #: belongs to will one day point at work the queue has forgotten.
        self.attempts_log = AttemptLog(self._connect, mode=durability, now=now)
        #: Items waiting on a person. Same file for the same reason: a
        #: question that can vanish independently of the item it is about is a
        #: question somebody answers into nothing.
        #: `on_hold` is told once, when a question is opened, so a held item
        #: is not something only a poll can discover (#188). Delivery cannot
        #: reach the item: see `holds.deliver`.
        self.holds = Holds(self._connect, now=now, on_hold=on_hold)
        self._migrate()
        # `try/finally`, not `with`: a sqlite3 connection's context manager
        # manages a TRANSACTION and does not close anything. Under
        # `isolation_level=None` there is no transaction for it to manage, so
        # `with` here left the connection open for the cyclic collector to
        # find -- and this object holds a reference cycle (its graph, attempt
        # log and holds all carry its bound `_connect`), so "eventually" meant
        # a gc pass, not a scope exit. #206.
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            self.graph.create_schema(conn)
            self.attempts_log.migrate(conn)
            self.holds.migrate(conn)
        finally:
            conn.close()

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
                    plan_branch TEXT,
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
        "projects": {
            "max_attempts": "INTEGER NOT NULL DEFAULT 5",
            "min_free_disk_gb": "REAL NOT NULL DEFAULT 0",
            # Stage K. Additive; an older build ignores it and a project that
            # declares no fix reads as `{}`.
            "fixes": "TEXT NOT NULL DEFAULT '{}'",
            # #155. Additive on the same terms: an older build ignores it, and
            # a database that predates it reads as off, which is the default.
            "apply_fixes": "INTEGER NOT NULL DEFAULT 0",
            # Stage H, additive on the same terms.
            "durability": "TEXT NOT NULL DEFAULT ''",
            "max_item_seconds": "REAL NOT NULL DEFAULT 0",
            "max_item_spend_usd": "REAL NOT NULL DEFAULT 0",
            "max_hold_seconds": "REAL NOT NULL DEFAULT 21600",
            "plan_branch": "TEXT",
        },
        # Stage G. Additive, so a rollback to an older build still reads every
        # column it knows and simply ignores this one. The migration plan is
        # docs/MIGRATION-graph.md.
        "work": {
            "admitted_revision": "INTEGER NOT NULL DEFAULT 0",
            # Stage K. Additive for the same reason: an older build reads
            # every column it knows and ignores these two, and an upgraded
            # database with no disposition recorded yet reports an empty
            # string -- which is "nobody has finished with this", not a
            # sixth disposition.
            "disposition": "TEXT NOT NULL DEFAULT ''",
            "reason_kind": "TEXT NOT NULL DEFAULT ''",
            # Stage L. All zero-defaulted, so an existing database upgrades
            # to "unlimited, nothing spent yet" -- no behaviour change.
            "budget_seconds": "REAL NOT NULL DEFAULT 0",
            "budget_spend_usd": "REAL NOT NULL DEFAULT 0",
            "spend_usd": "REAL NOT NULL DEFAULT 0",
            "unpriced_calls": "INTEGER NOT NULL DEFAULT 0",
            "first_started_at": "REAL NOT NULL DEFAULT 0",
            # Stage J, additive: an existing row reads as "not held".
            "held_until": "REAL NOT NULL DEFAULT 0",
            # #182, additive: every existing item produces a diff, which is
            # what it always did.
            "deliverable": "TEXT NOT NULL DEFAULT 'code'",
        },
        "abandoned_sessions": {
            "project_id": "TEXT",
        },
        "plan_promotions": {
            "item_sha": "TEXT",
        },
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
                "checks, fixes, apply_fixes, durability, max_item_seconds, max_item_spend_usd, "
                "max_hold_seconds, plan_path, plan_branch, roles, max_workers, max_attempts, "
                "min_free_disk_gb, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "name=excluded.name, repo=excluded.repo, work_dir=excluded.work_dir, "
                "base_branch=excluded.base_branch, checks=excluded.checks, "
                "fixes=excluded.fixes, apply_fixes=excluded.apply_fixes, "
                "durability=excluded.durability, "
                "max_item_seconds=excluded.max_item_seconds, "
                "max_item_spend_usd=excluded.max_item_spend_usd, "
                "max_hold_seconds=excluded.max_hold_seconds, "
                "plan_path=excluded.plan_path, roles=excluded.roles, "
                "plan_branch=excluded.plan_branch, "
                "max_workers=excluded.max_workers, max_attempts=excluded.max_attempts, "
                "min_free_disk_gb=excluded.min_free_disk_gb, "
                "updated_at=excluded.updated_at",
                (
                    project.project_id,
                    project.name,
                    project.repo,
                    project.work_dir,
                    project.base_branch,
                    json.dumps(project.checks),
                    json.dumps(project.fixes),
                    int(project.apply_fixes),
                    project.durability,
                    project.max_item_seconds,
                    project.max_item_spend_usd,
                    project.max_hold_seconds,
                    project.plan_path,
                    project.plan_branch,
                    json.dumps(project.roles) if project.roles else None,
                    project.max_workers,
                    project.max_attempts,
                    project.min_free_disk_gb,
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

    def update_project(self, project: Project, *, expected_updated_at: float) -> bool:
        """Replace an existing project only if it is still the reviewed version.

        Browser review is a judgement about exact values. A compare followed
        by an unconditional update leaves a race for another process between
        those operations, so the version predicate belongs in the UPDATE.
        """
        conn = self._connect()
        try:
            changed = conn.execute(
                "UPDATE projects SET name = ?, repo = ?, work_dir = ?, base_branch = ?, "
                "checks = ?, fixes = ?, durability = ?, max_item_seconds = ?, "
                "max_item_spend_usd = ?, max_hold_seconds = ?, plan_path = ?, "
                "plan_branch = ?, roles = ?, "
                "max_workers = ?, max_attempts = ?, min_free_disk_gb = ?, updated_at = ? "
                "WHERE project_id = ? AND updated_at = ?",
                (
                    project.name,
                    project.repo,
                    project.work_dir,
                    project.base_branch,
                    json.dumps(project.checks),
                    json.dumps(project.fixes),
                    project.durability,
                    project.max_item_seconds,
                    project.max_item_spend_usd,
                    project.max_hold_seconds,
                    project.plan_path,
                    project.plan_branch,
                    json.dumps(project.roles) if project.roles else None,
                    project.max_workers,
                    project.max_attempts,
                    project.min_free_disk_gb,
                    self.now(),
                    project.project_id,
                    expected_updated_at,
                ),
            ).rowcount
            return changed == 1
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

    # ------------------------------------------------------ plan integration

    def plan(self, project_id: str) -> sqlite3.Row | None:
        conn = self._connect()
        try:
            return cast(
                sqlite3.Row | None,
                conn.execute(
                    "SELECT project_id, branch, target_branch, target_sha, head_sha, plan_digest "
                    "FROM plans WHERE project_id = ?",
                    (project_id,),
                ).fetchone(),
            )
        finally:
            conn.close()

    def create_plan(
        self,
        project_id: str,
        *,
        branch: str,
        target_branch: str,
        target_sha: str,
        head_sha: str,
        plan_digest: str,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO plans (project_id, branch, target_branch, target_sha, head_sha, "
                "plan_digest, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    branch,
                    target_branch,
                    target_sha,
                    head_sha,
                    plan_digest,
                    self.now(),
                    self.now(),
                ),
            )
        finally:
            conn.close()

    def acquire_plan_promotion_lease(
        self, project_id: str, owner: str, lease_seconds: float
    ) -> tuple[bool, float]:
        """Acquire a cross-process lease for serialized plan integration.

        The compare-and-replace is one immediate SQLite transaction. A live
        owner cannot be displaced; an expired owner can be taken over after a
        crash without an operator clearing state.
        """
        if lease_seconds <= 0:
            raise ValueError("plan promotion lease must be positive")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = self.now()
            row = conn.execute(
                "SELECT owner, lease_until FROM plan_promotion_leases WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is not None and row["owner"] != owner and float(row["lease_until"]) > now:
                conn.rollback()
                return False, float(row["lease_until"])
            lease_until = now + lease_seconds
            conn.execute(
                "INSERT INTO plan_promotion_leases (project_id, owner, lease_until, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET owner = excluded.owner, "
                "lease_until = excluded.lease_until, updated_at = excluded.updated_at",
                (project_id, owner, lease_until, now),
            )
            conn.commit()
            return True, lease_until
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            raise
        finally:
            conn.close()

    def renew_plan_promotion_lease(self, project_id: str, owner: str, lease_seconds: float) -> bool:
        """Extend a still-owned promotion lease; false means it was lost."""
        if lease_seconds <= 0:
            raise ValueError("plan promotion lease must be positive")
        conn = self._connect()
        try:
            now = self.now()
            changed = conn.execute(
                "UPDATE plan_promotion_leases SET lease_until = ?, updated_at = ? "
                "WHERE project_id = ? AND owner = ? AND lease_until > ?",
                (now + lease_seconds, now, project_id, owner, now),
            ).rowcount
            return changed == 1
        finally:
            conn.close()

    def release_plan_promotion_lease(self, project_id: str, owner: str) -> bool:
        """Release only this coordinator's promotion lease."""
        conn = self._connect()
        try:
            changed = conn.execute(
                "DELETE FROM plan_promotion_leases WHERE project_id = ? AND owner = ?",
                (project_id, owner),
            ).rowcount
            return changed == 1
        finally:
            conn.close()

    def advance_plan(self, project_id: str, head_sha: str) -> None:
        conn = self._connect()
        try:
            changed = conn.execute(
                "UPDATE plans SET head_sha = ?, updated_at = ? WHERE project_id = ?",
                (head_sha, self.now(), project_id),
            ).rowcount
            if changed != 1:
                raise KeyError(f"no plan for project {project_id!r}")
        finally:
            conn.close()

    def complete_promotion(
        self,
        project_id: str,
        item_id: str,
        item_branch: str,
        base_sha: str,
        old_head_sha: str,
        new_head_sha: str,
        promotion_id: int | None = None,
        detail: str = "",
    ) -> None:
        """Commit the plan head and its successful promotion fact together.

        The git ref is advanced before this method is called, but the two
        durable SQLite projections must still be one fact.  Keeping the
        ``plans`` update and the successful ``plan_promotions`` row in
        separate transactions creates a restart window in which a dependent
        item can see the new head but no promoted prerequisite.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE plans SET head_sha = ?, updated_at = ? "
                "WHERE project_id = ? AND head_sha = ?",
                (new_head_sha, self.now(), project_id, old_head_sha),
            ).rowcount
            if changed != 1:
                conn.execute("ROLLBACK")
                raise KeyError(
                    f"plan {project_id!r} no longer has head {old_head_sha!r}; "
                    "promotion must be replayed from its current head"
                )
            if promotion_id is None:
                changed = conn.execute(
                    "UPDATE plan_promotions SET status = 'promoted', detail = ? "
                    "WHERE project_id = ? AND item_id = ? AND item_branch = ? "
                    "AND base_sha = ? AND old_head_sha = ? AND new_head_sha = ? "
                    "AND status = 'applying'",
                    (
                        detail,
                        project_id,
                        item_id,
                        item_branch,
                        base_sha,
                        old_head_sha,
                        new_head_sha,
                    ),
                ).rowcount
            else:
                changed = conn.execute(
                    "UPDATE plan_promotions SET status = 'promoted', detail = ? "
                    "WHERE promotion_id = ? AND project_id = ? AND status = 'applying'",
                    (detail, promotion_id, project_id),
                ).rowcount
            if changed != 1:
                conn.execute("ROLLBACK")
                raise KeyError("no matching in-progress promotion to complete")
            conn.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise

    def begin_promotion(
        self,
        project_id: str,
        item_id: str,
        item_branch: str,
        base_sha: str,
        old_head_sha: str,
        new_head_sha: str,
        item_sha: str | None = None,
    ) -> int:
        """Durably record the Git ref update that is about to happen."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT head_sha FROM plans WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None or row["head_sha"] != old_head_sha:
                conn.execute("ROLLBACK")
                raise KeyError("plan head changed before promotion began")
            cursor = conn.execute(
                "INSERT INTO plan_promotions (project_id, item_id, item_branch, base_sha, "
                "item_sha, old_head_sha, new_head_sha, status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'applying', '', ?)",
                (
                    project_id,
                    item_id,
                    item_branch,
                    base_sha,
                    item_sha,
                    old_head_sha,
                    new_head_sha,
                    self.now(),
                ),
            )
            conn.execute("COMMIT")
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return the promotion id")
            return cursor.lastrowid
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise

    def in_progress_promotions(self, project_id: str) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return cast(
                list[sqlite3.Row],
                conn.execute(
                    "SELECT * FROM plan_promotions WHERE project_id = ? "
                    "AND status = 'applying' ORDER BY promotion_id",
                    (project_id,),
                ).fetchall(),
            )
        finally:
            conn.close()

    def promotion(self, promotion_id: int) -> sqlite3.Row | None:
        """Return one durable promotion fact by its immutable identity."""
        conn = self._connect()
        try:
            return cast(
                sqlite3.Row | None,
                conn.execute(
                    "SELECT * FROM plan_promotions WHERE promotion_id = ?",
                    (promotion_id,),
                ).fetchone(),
            )
        finally:
            conn.close()

    def finish_promotion(self, promotion_id: int, status: str, detail: str) -> None:
        """Close an interrupted promotion without claiming success."""
        conn = self._connect()
        try:
            changed = conn.execute(
                "UPDATE plan_promotions SET status = ?, detail = ? "
                "WHERE promotion_id = ? AND status = 'applying'",
                (status, detail, promotion_id),
            ).rowcount
            if changed != 1:
                raise KeyError(f"no in-progress promotion {promotion_id}")
        finally:
            conn.close()

    def record_promotion(
        self,
        project_id: str,
        item_id: str,
        item_branch: str,
        base_sha: str,
        old_head_sha: str,
        new_head_sha: str | None,
        status: str,
        detail: str = "",
        item_sha: str | None = None,
    ) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO plan_promotions (project_id, item_id, item_branch, base_sha, "
                "item_sha, old_head_sha, new_head_sha, status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    item_id,
                    item_branch,
                    base_sha,
                    item_sha,
                    old_head_sha,
                    new_head_sha,
                    status,
                    detail,
                    self.now(),
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return the promotion id")
            return cursor.lastrowid
        finally:
            conn.close()

    def successful_promotions(self, project_id: str) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return cast(
                list[sqlite3.Row],
                conn.execute(
                    "SELECT * FROM plan_promotions WHERE project_id = ? "
                    "AND status = 'promoted' ORDER BY promotion_id",
                    (project_id,),
                ).fetchall(),
            )
        finally:
            conn.close()

    def begin_refresh(
        self,
        project_id: str,
        old_target_sha: str,
        new_target_sha: str,
        old_head_sha: str,
        new_head_sha: str,
    ) -> int:
        """Journal a plan rebuild before changing its Git ref."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT target_sha, head_sha FROM plans WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if (
                row is None
                or row["target_sha"] != old_target_sha
                or row["head_sha"] != old_head_sha
            ):
                conn.execute("ROLLBACK")
                raise KeyError("plan changed before refresh began")
            cursor = conn.execute(
                "INSERT INTO plan_refreshes (project_id, old_target_sha, new_target_sha, "
                "old_head_sha, new_head_sha, status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'applying', '', ?)",
                (
                    project_id,
                    old_target_sha,
                    new_target_sha,
                    old_head_sha,
                    new_head_sha,
                    self.now(),
                ),
            )
            conn.execute("COMMIT")
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return the refresh id")
            return cursor.lastrowid
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise

    def set_refresh_head(self, refresh_id: int, new_head_sha: str) -> None:
        """Durably set the ref value before the Git ref update is attempted."""
        conn = self._connect()
        try:
            changed = conn.execute(
                "UPDATE plan_refreshes SET new_head_sha = ? "
                "WHERE refresh_id = ? AND status = 'applying'",
                (new_head_sha, refresh_id),
            ).rowcount
            if changed != 1:
                raise KeyError(f"no in-progress refresh {refresh_id}")
        finally:
            conn.close()

    def complete_refresh(
        self,
        refresh_id: int,
        project_id: str,
        old_target_sha: str,
        new_target_sha: str,
        old_head_sha: str,
        new_head_sha: str,
        detail: str = "",
    ) -> None:
        """Commit a rebuilt Git ref and plan projection together."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE plans SET target_sha = ?, head_sha = ?, updated_at = ? "
                "WHERE project_id = ? AND target_sha = ? AND head_sha = ?",
                (
                    new_target_sha,
                    new_head_sha,
                    self.now(),
                    project_id,
                    old_target_sha,
                    old_head_sha,
                ),
            ).rowcount
            if changed != 1:
                conn.execute("ROLLBACK")
                raise KeyError("plan changed before refresh completed")
            changed = conn.execute(
                "UPDATE plan_refreshes SET status = 'refreshed', detail = ? "
                "WHERE refresh_id = ? AND project_id = ? AND status = 'applying' "
                "AND old_target_sha = ? AND new_target_sha = ? AND old_head_sha = ? "
                "AND new_head_sha = ?",
                (
                    detail,
                    refresh_id,
                    project_id,
                    old_target_sha,
                    new_target_sha,
                    old_head_sha,
                    new_head_sha,
                ),
            ).rowcount
            if changed != 1:
                conn.execute("ROLLBACK")
                raise KeyError("no matching in-progress refresh to complete")
            conn.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise

    def in_progress_refreshes(self, project_id: str) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return cast(
                list[sqlite3.Row],
                conn.execute(
                    "SELECT * FROM plan_refreshes WHERE project_id = ? "
                    "AND status = 'applying' ORDER BY refresh_id",
                    (project_id,),
                ).fetchall(),
            )
        finally:
            conn.close()

    def finish_refresh(self, refresh_id: int, status: str, detail: str) -> None:
        conn = self._connect()
        try:
            changed = conn.execute(
                "UPDATE plan_refreshes SET status = ?, detail = ? "
                "WHERE refresh_id = ? AND status = 'applying'",
                (status, detail, refresh_id),
            ).rowcount
            if changed != 1:
                raise KeyError(f"no in-progress refresh {refresh_id}")
        finally:
            conn.close()

    def latest_promotion(self, project_id: str, item_id: str) -> sqlite3.Row | None:
        conn = self._connect()
        try:
            return cast(
                sqlite3.Row | None,
                conn.execute(
                    "SELECT * FROM plan_promotions WHERE project_id = ? AND item_id = ? "
                    "AND status = 'promoted' ORDER BY promotion_id DESC LIMIT 1",
                    (project_id, item_id),
                ).fetchone(),
            )
        finally:
            conn.close()

    # ------------------------------------------------------------ loading

    def add(
        self,
        records: Iterable[WorkRecord],
        project_id: str = DEFAULT_PROJECT,
        *,
        provenance: str = WORK_DECLARATION,
    ) -> int:
        """Add work to a project, leaving anything already present untouched.

        Re-adding is how a re-synced plan reaches the queue, so it must not
        reset progress: an item already claimed or done stays that way, and
        only its description is refreshed.

        The project is scoped explicitly rather than taken from the record so
        that loading a plan cannot silently scatter items across projects.

        Each record's `depends_on` is also written into the dependency graph.
        That write is idempotent: re-ingesting an unchanged plan leaves the
        graph revision exactly where it was, so a routine re-sync cannot
        invalidate the claims that are running.
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
                "SELECT state, brief FROM work WHERE project_id = ? AND item_id = ?",
                (project_id, record.item_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO work (project_id, item_id, issue, title, brief, depends_on, "
                    "state, updated_at, deliverable) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        record.item_id,
                        record.issue,
                        record.title,
                        record.brief,
                        json.dumps(record.depends_on),
                        record.state,
                        self.now(),
                        record.deliverable,
                    ),
                )
                added += 1
            else:
                conn.execute(
                    "UPDATE work SET title = ?, brief = ?, depends_on = ?, issue = ?, "
                    "updated_at = ?, deliverable = ? WHERE project_id = ? AND item_id = ?",
                    (
                        record.title,
                        record.brief,
                        json.dumps(record.depends_on),
                        record.issue,
                        self.now(),
                        record.deliverable,
                        project_id,
                        record.item_id,
                    ),
                )
                if revives(existing["state"], existing["brief"], record.brief):
                    # The verdict that stopped this item was reached against
                    # wording that no longer exists, so it is no longer a
                    # verdict on anything. Clearing `last_error` with it: the
                    # next attempt is told what the previous one was refused
                    # for, and that refusal is about a question nobody is
                    # asking any more.
                    conn.execute(
                        "UPDATE work SET state = ?, last_error = NULL "
                        "WHERE project_id = ? AND item_id = ?",
                        (PENDING, project_id, record.item_id),
                    )
                    log.info(
                        "%s/%s: brief rewritten, returning it to pending from %s",
                        project_id,
                        record.item_id,
                        existing["state"],
                    )
            self.graph.set_edges(
                project_id,
                record.item_id,
                record.dependency_specs(provenance),
                conn=conn,
            )
        conn.close()
        return added

    def accept_remote_review(
        self,
        *,
        source: str,
        remote_id: str,
        project_id: str,
        item_id: str,
        disposition: str,
        status: str,
        correction_item_id: str | None,
        title: str,
        brief: str,
        depends_on: list[str],
        state: str,
        last_error: str | None,
        received_at: float | None = None,
    ) -> tuple[bool, sqlite3.Row | None]:
        """Record one remote review and its correction projection atomically."""
        if disposition not in {"actionable", "ambiguous", "already_resolved"}:
            raise ValueError(f"unknown remote review disposition {disposition!r}")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "INSERT OR IGNORE INTO remote_review_events "
                "(source, remote_id, project_id, item_id, disposition, status, "
                "correction_item_id, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source,
                    remote_id,
                    project_id,
                    item_id,
                    disposition,
                    status,
                    correction_item_id,
                    received_at if received_at is not None else self.now(),
                ),
            )
            if cursor.rowcount == 0:
                row = conn.execute(
                    "SELECT status, correction_item_id FROM remote_review_events "
                    "WHERE source = ? AND remote_id = ?",
                    (source, remote_id),
                ).fetchone()
                conn.execute("COMMIT")
                return False, row
            conn.execute(
                "INSERT OR IGNORE INTO projects (project_id, name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (project_id, project_id, self.now(), self.now()),
            )
            conn.execute(
                "INSERT OR IGNORE INTO control (project_id, state, changed_at) VALUES (?, ?, ?)",
                (project_id, STOPPED, self.now()),
            )
            if correction_item_id is not None:
                conn.execute(
                    "INSERT INTO work (project_id, item_id, title, brief, depends_on, state, "
                    "last_error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        correction_item_id,
                        title,
                        brief,
                        json.dumps(depends_on),
                        state,
                        last_error,
                        self.now(),
                    ),
                )
                self.graph.set_edges(
                    project_id,
                    correction_item_id,
                    WorkRecord(
                        correction_item_id,
                        title,
                        brief=brief,
                        depends_on=depends_on,
                    ).dependency_specs(),
                    conn=conn,
                )
            conn.execute("COMMIT")
            return True, None
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

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

    def compare_and_set_setting(self, key: str, expected: Any | None, value: Any) -> bool:
        """Replace a setting only when its complete stored value is unchanged.

        Browser reviews use this instead of comparing a timestamp and then
        writing in a second transaction.  The immediate transaction makes
        the comparison and replacement one operation, so an API or CLI edit
        cannot be overwritten between those two steps.  ``None`` means the
        setting was absent when reviewed.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            current = json.loads(row["value"]) if row else None
            if current != expected:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, json.dumps(value), self.now()),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------- claims

    def claim(
        self, owner: str | None = None, project_id: str = DEFAULT_PROJECT
    ) -> WorkRecord | None:
        """Take the next available item, or None.

        Available means: pending (or a lease that has expired), and every
        **required** dependency edge explicitly satisfied. The whole selection
        and claim happens inside one IMMEDIATE transaction, so two workers
        racing cannot both win — the loser sees the row already claimed and
        picks something else.

        The graph revision the decision was made at is written onto the row.
        Admission and the check before the expensive gate must be able to say
        they looked at the same graph; without a recorded revision the second
        check can only ever answer "not now", never "and the graph moved".
        """
        owner = owner or worker_identity()
        # Checked before anything is taken, so a pause stops the fleet at the
        # next item boundary rather than part-way through one.
        if self.control(project_id)[0] != RUNNING:
            return None
        # Questions nobody answered in time are returned before anything else
        # is considered. Done here, before the transaction opens, because it
        # needs its own connection and because a claim scan is the moment the
        # queue's view of what is available has to be true. Nothing schedules
        # this separately: a sweep that only ran under a cron would leave a
        # held item stuck for as long as the cron was broken.
        self.expire_holds()
        now = self.now()
        conn = self._connect()
        try:
            limit = self.max_attempts_for(conn, project_id)
            conn.execute("BEGIN IMMEDIATE")
            # Computed once for the whole scan rather than per candidate: a
            # cycle is a property of the project's graph, not of one row.
            cycles = self.graph.cycles(project_id, conn=conn)
            revision = self.graph.revision(project_id, conn=conn)
            # Paged, and the paging is the point. A single `LIMIT` over the
            # eligible rows makes "the queue has ready work" and "the first
            # page has ready work" the same question, and they are not: a
            # project whose first page is entirely dependency-blocked used to
            # be handed nothing at all, for ever, while a ready item sat one
            # row past the limit. That is a stalled fleet with a full queue,
            # and nothing in the queue says why.
            #
            # Keyset, not OFFSET: rows are retired to `exhausted` inside this
            # very loop, so a positional window would skip whatever slid up
            # into the gap.
            cursor_attempts, cursor_item = -1, ""
            while True:
                page = conn.execute(
                    "SELECT * FROM work WHERE project_id = ? "
                    "AND (state = ? OR (state = ? AND lease_until < ?)) "
                    "AND (attempts > ? OR (attempts = ? AND item_id > ?)) "
                    "ORDER BY attempts, item_id LIMIT ?",
                    (
                        project_id,
                        PENDING,
                        CLAIMED,
                        now,
                        cursor_attempts,
                        cursor_attempts,
                        cursor_item,
                        CLAIM_SCAN_LIMIT,
                    ),
                ).fetchall()
                if not page:
                    break
                cursor_attempts = page[-1]["attempts"]
                cursor_item = page[-1]["item_id"]
                for candidate in page:
                    record = WorkRecord.from_row(candidate)
                    if limit and record.attempts >= limit:
                        # Give up rather than recycle. An item that reliably
                        # kills its worker is never released, so its lease
                        # expires and it is re-claimed forever -- spending
                        # money every cycle while looking exactly like an item
                        # that is merely busy.
                        conn.execute(
                            "UPDATE work SET state = ?, owner = NULL, lease_until = 0, "
                            "last_error = ?, updated_at = ? WHERE project_id = ? AND item_id = ?",
                            (
                                EXHAUSTED,
                                (
                                    f"gave up after {record.attempts} attempts"
                                    + (f": {record.last_error}" if record.last_error else "")
                                ),
                                now,
                                project_id,
                                record.item_id,
                            ),
                        )
                        continue
                    admission = self.graph.readiness(
                        project_id, record.item_id, conn=conn, cycles=cycles
                    )
                    if not admission.ready:
                        continue
                    # A plan's graph can say a prerequisite is done before its
                    # promotion fact is durable.  Do not release a dependent
                    # into a checkout that cannot contain that prerequisite.
                    if self._plan_promotion_reasons(conn, record):
                        continue
                    # D11, resolved 2026-08-04: **a resumed attempt continues
                    # the existing one.** A crash is not a failure of the work,
                    # so re-claiming an item that left a durable position keeps
                    # its attempt number, and `max_attempts` goes on bounding
                    # genuine failures rather than crashes. The consequence is
                    # named rather than hidden: an item that crashes in a loop
                    # is then bounded by a wall-clock or spend budget and by
                    # nothing else.
                    resuming = self.attempts_log.has_resumable_work(
                        project_id, record.item_id, record.attempts
                    )
                    conn.execute(
                        "UPDATE work SET state = ?, owner = ?, lease_until = ?, "
                        "attempts = CASE WHEN ? THEN attempts ELSE attempts + 1 END, "
                        "admitted_revision = ?, "
                        # Stamped once and never again. The wall-clock ceiling
                        # bounds the ITEM, so an item that crashes in a loop
                        # must not reset its own clock on every re-claim --
                        # which is exactly the failure D11's ruling left for
                        # this to catch.
                        "first_started_at = CASE WHEN first_started_at > 0 "
                        "THEN first_started_at ELSE ? END, "
                        "updated_at = ? "
                        "WHERE project_id = ? AND item_id = ?",
                        (
                            CLAIMED,
                            owner,
                            now + self.lease_seconds,
                            resuming,
                            revision,
                            now,
                            now,
                            project_id,
                            record.item_id,
                        ),
                    )
                    conn.execute("COMMIT")
                    record.state = CLAIMED
                    record.owner = owner
                    record.lease_until = now + self.lease_seconds
                    if not resuming:
                        record.attempts += 1
                    record.admitted_revision = revision
                    record.first_started_at = record.first_started_at or now
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

    def readiness(self, item_id: str, *, project_id: str = DEFAULT_PROJECT) -> Readiness:
        """Whether an item may be admitted, and — when it may not — why.

        The **same** call `claim` uses. Two implementations of "is it ready"
        is two answers, and the one that disagreed would be the one that let
        ineligible work reach a durable gate.
        """
        state = self.graph.readiness(project_id, item_id)
        if not state.ready:
            return state
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM work WHERE project_id = ? AND item_id = ?",
                (project_id, item_id),
            ).fetchone()
            if row is None:
                return state
            reasons = self._plan_promotion_reasons(conn, WorkRecord.from_row(row))
            if not reasons:
                return state
            return Readiness(
                project_id,
                item_id,
                False,
                state.revision,
                reasons=(*state.reasons, *reasons),
                advisory=state.advisory,
                overridden=state.overridden,
                override_reason=state.override_reason,
            )
        finally:
            conn.close()

    def _plan_promotion_reasons(
        self, conn: sqlite3.Connection, record: WorkRecord
    ) -> tuple[ReadinessReason, ...]:
        """Require local prerequisites to be promoted before plan admission.

        The dependency graph answers whether prerequisite work is complete;
        this projection answers whether that completed work is present on the
        plan's durable integration branch.  It runs inside ``claim``'s write
        transaction, so a dependent cannot slip in between those two facts.
        """
        plan_configured = conn.execute(
            "SELECT 1 FROM projects WHERE project_id = ? "
            "AND plan_path IS NOT NULL AND TRIM(plan_path) <> '' "
            "AND plan_branch IS NOT NULL AND TRIM(plan_branch) <> ''",
            (record.project_id,),
        ).fetchone()
        plan_initialized = conn.execute(
            "SELECT 1 FROM plans WHERE project_id = ?", (record.project_id,)
        ).fetchone()
        if plan_configured is None and plan_initialized is None:
            return ()
        reasons: list[ReadinessReason] = []
        for dependency in record.dependency_specs():
            if not dependency.required or dependency.target_kind != LOCAL_WORK:
                continue
            if plan_initialized is None:
                reasons.append(
                    ReadinessReason(
                        kind="plan_promotion",
                        explanation=(
                            f"local work target {dependency.target_id!r} cannot be released "
                            "until the configured plan branch is initialized"
                        ),
                        target_kind=dependency.target_kind,
                        target_id=dependency.target_id,
                        state="blocked",
                        evidence="durable plan identity is absent",
                    )
                )
                continue
            promoted = conn.execute(
                "SELECT 1 FROM plan_promotions WHERE project_id = ? AND item_id = ? "
                "AND status = 'promoted' LIMIT 1",
                (record.project_id, dependency.target_id),
            ).fetchone()
            if promoted is not None:
                continue
            reasons.append(
                ReadinessReason(
                    kind="plan_promotion",
                    explanation=(
                        f"local work target {dependency.target_id!r} is complete but "
                        "has not been promoted to the local plan branch"
                    ),
                    target_kind=dependency.target_kind,
                    target_id=dependency.target_id,
                    state="blocked",
                    evidence="promotion record is absent",
                )
            )
        return tuple(reasons)

    def unmet_dependencies(self, item_id: str, *, project_id: str = DEFAULT_PROJECT) -> list[str]:
        """Required targets of an item that are not satisfied, right now.

        `claim` checks this once, at the moment of claiming. The graph can be
        corrected while an item is in flight -- that is what correcting a plan
        looks like -- and an item that is no longer eligible must not go on to
        pass a durable gate on the strength of a check made minutes earlier.

        A thin projection of `readiness` kept for callers that only want the
        identities. Anything reporting to a human should use `readiness`
        instead: a list of ids cannot say whether a target is missing, merely
        unfinished, or part of a cycle, and those need different actions.
        """
        state = self.readiness(item_id, project_id=project_id)
        if state.ready:
            return []
        return [
            reason.target_id or reason.evidence or reason.kind
            for reason in state.reasons
            if reason.required
        ]

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
        consume_attempt: bool = True,
        disposition: str = "",
        reason_kind: str = "",
        project_id: str = DEFAULT_PROJECT,
    ) -> bool:
        """Finish with an item. `state` is done, failed, blocked or pending
        (pending puts it back for another attempt).

        `disposition` and `reason_kind` are the Stage K taxonomy from
        `outcomes.py`: `state` is where the item ended up, these are why. Both
        default to empty and empty is written through, so a caller that does
        not know the taxonomy clears a stale reason rather than leaving the
        previous attempt's explanation attached to this one's result.

        Returns True if the item was actually updated.

        **A worker must pass its `owner`.** A worker that stalled past its
        lease is not dead, only slow: it will surface eventually and report a
        result for an item that now belongs to someone else. Accepting that
        late report marks the item finished from work the new owner never did,
        and leaves the new owner running with nothing left to release. The
        guard makes the late report a no-op, which is what it is.

        ``consume_attempt=False`` returns work that could not be attempted at
        all, notably when every route is out of spend budget. A cap is an
        endpoint condition, not a failed item attempt; counting it would
        retire sound work merely because it was repeatedly claimed while no
        provider could serve it.

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
                "attempts = CASE WHEN ? THEN attempts ELSE MAX(0, attempts - 1) END, "
                "last_error = ?, branch = COALESCE(?, branch), "
                "pr_url = COALESCE(?, pr_url), disposition = ?, reason_kind = ?, "
                "updated_at = ? "
                "WHERE project_id = ? AND item_id = ?"
            )
            params: list[Any] = [
                state,
                consume_attempt,
                error,
                branch,
                pr_url,
                disposition,
                reason_kind,
                self.now(),
                project_id,
                item_id,
            ]
            if owner is not None:
                sql += " AND owner = ?"
                params.append(owner)
            cursor = conn.execute(sql, params)
            applied = cursor.rowcount > 0
            if not applied and owner is not None:
                # Say it out loud. A discarded result used to be indis-
                # tinguishable from a successful release, so an item that ended
                # with no recorded outcome looked like a harness that simply
                # forgot to write one.
                log.warning(
                    "release: %s/%s is no longer owned by %s, so its %r result was discarded",
                    project_id,
                    item_id,
                    owner,
                    state,
                )
        finally:
            conn.close()
        if applied:
            self._cancel_open_hold(item_id, project_id, "released")
        return applied

    def _cancel_open_hold(self, item_id: str, project_id: str, what: str) -> None:
        """Close any question left open on an item that has moved on.

        A hold is a *suspended attempt*. Once the item has been released,
        retried or requeued, the attempt that asked is over — but the question
        used to stay `open` for ever, and `holds.CANCELLED` was a state
        nothing in the codebase ever wrote. Three things followed: the
        operator's inbox showed a question answering could no longer affect;
        the item's **new** owner was refused a question of its own with
        "already has an unanswered question"; and the abandoned hold went on
        to expire and be recorded as "nobody answered", which is not what
        happened.

        `expired` and `cancelled` are deliberately different facts, and this
        writes the one that is true.
        """
        closed = self.holds.close(project_id, item_id, HOLD_CANCELLED)
        if closed:
            log.info(
                "%s/%s: %s while a question was open, so the question was cancelled",
                project_id,
                item_id,
                what,
            )

    def hold(
        self,
        item_id: str,
        *,
        question: str,
        owner: str | None = None,
        reason: str = "",
        who_may_answer: str = "anyone",
        session_id: str | None = None,
        session_url: str | None = None,
        max_seconds: float | None = None,
        project_id: str = DEFAULT_PROJECT,
    ) -> Hold:
        """Suspend this item on a question, keeping its claim.

        **D12: the claim is kept and the lease is suspended.** The owner stays
        on the row so answering hands the item back to the worker that asked,
        with its worktree and its context; `lease_until` goes to zero because
        a lease exists to distinguish slow from dead and a held item is
        neither. `claim` never selects a `held` row, so nothing can take the
        item while a person is thinking about it.

        Refused for an item nobody is working on. A hold is a *suspended
        attempt*; holding an unclaimed item would be an operator parking it,
        which is what `blocked` already is.
        """
        record = self.get(item_id, project_id=project_id)
        if record is None:
            raise HoldError(f"no item {item_id!r} in project {project_id!r}")
        # Checked first, so an item that is already held says so specifically
        # rather than reporting the state that being held put it in.
        if self.holds.current(project_id, item_id) is not None:
            raise HoldError(f"{item_id} already has an unanswered question")
        if record.state != CLAIMED:
            raise HoldError(
                f"{item_id} is {record.state!r}, not claimed; a hold suspends an attempt, "
                "and an item nobody is working on is parked with `block` instead"
            )
        if owner is not None and record.owner != owner:
            raise HoldError(f"{item_id} is not owned by {owner!r}, so that worker cannot hold it")

        project = self.get_project(project_id)
        limit = (
            max_seconds
            if max_seconds is not None
            else float(getattr(project, "max_hold_seconds", DEFAULT_MAX_HOLD_SECONDS) or 0.0)
        )
        hold = self.holds.open(
            project_id,
            item_id,
            question=question,
            attempt=record.attempts,
            reason=reason,
            who_may_answer=who_may_answer,
            owner=record.owner,
            session_id=session_id,
            session_url=session_url,
            max_seconds=limit,
        )
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE work SET state = ?, lease_until = 0, held_until = ?, updated_at = ? "
                "WHERE project_id = ? AND item_id = ?",
                (HELD, hold.expires_at, self.now(), project_id, item_id),
            )
        finally:
            conn.close()
        return hold

    def hold_pending(
        self,
        item_id: str,
        *,
        question: str,
        reason: str = "",
        project_id: str = DEFAULT_PROJECT,
        max_seconds: float | None = None,
    ) -> Hold:
        """Open a human hold before a worker exists.

        Remote review can be ambiguous before correction work is claimable.
        This narrow path is distinct from ``hold``: it accepts only a pending
        or blocked correction item, gives it no worker owner, and answering
        returns it to ``pending`` so a later worker can claim it.
        """
        record = self.get(item_id, project_id=project_id)
        if record is None:
            raise HoldError(f"no item {item_id!r} in project {project_id!r}")
        if record.state not in {PENDING, BLOCKED}:
            raise HoldError(f"{item_id} is {record.state!r}, not a waiting correction item")
        project = self.get_project(project_id)
        limit = (
            max_seconds
            if max_seconds is not None
            else float(getattr(project, "max_hold_seconds", DEFAULT_MAX_HOLD_SECONDS) or 0.0)
        )
        hold = self.holds.open(
            project_id,
            item_id,
            question=question,
            reason=reason,
            max_seconds=limit,
        )
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE work SET state = ?, owner = NULL, lease_until = 0, held_until = ?, "
                "updated_at = ? WHERE project_id = ? AND item_id = ? AND state IN (?, ?)",
                (HELD, hold.expires_at, self.now(), project_id, item_id, PENDING, BLOCKED),
            )
        finally:
            conn.close()
        return hold

    def answer_hold(
        self,
        item_id: str,
        token: str,
        answer: Answer,
        *,
        project_id: str = DEFAULT_PROJECT,
    ) -> Hold:
        """Answer the open question and hand the item back to its worker.

        Returns to `claimed`, with a fresh lease for the owner that asked, so
        the worktree and context the hold preserved are the ones that continue.
        If that worker is dead the lease expires exactly as it always did and
        another worker re-claims — and, since Stage H, continues the attempt
        rather than restarting it.

        Nothing is injected into a session. The answer is recorded and the
        worker reads it; `COORDINATION-PLANE.md` §5.1 rules on why, and the
        reason is that the process may be at a shell and an answer becomes a
        command.
        """
        hold = self.holds.answer(project_id, item_id, token, answer)
        now = self.now()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE work SET state = ?, lease_until = ?, held_until = 0, updated_at = ? "
                "WHERE project_id = ? AND item_id = ? AND state = ?",
                (
                    CLAIMED if hold.owner else PENDING,
                    now + self.lease_seconds if hold.owner else 0,
                    now,
                    project_id,
                    item_id,
                    HELD,
                ),
            )
        finally:
            conn.close()
        return hold

    def expire_holds(self) -> list[Hold]:
        """Return every item whose question has gone unanswered too long.

        **To `blocked`, never to `ready`.** A hold that times out has not been
        approved and must not be treated as though it had; the item goes to a
        state a person has to act on, with the question preserved in the
        blocked reason so it is not lost with the hold.

        Returns the holds it closed, so a caller can say what happened rather
        than reporting a count.
        """
        expired: list[Hold] = []
        for hold in self.holds.due():
            self.holds.close(hold.project_id, hold.item_id, "expired")
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE work SET state = ?, owner = NULL, lease_until = 0, held_until = 0, "
                    "last_error = ?, disposition = ?, reason_kind = ?, updated_at = ? "
                    "WHERE project_id = ? AND item_id = ? AND state = ?",
                    (
                        BLOCKED,
                        (
                            f"nobody answered within the hold window: {hold.question}"
                            + (f" ({hold.reason})" if hold.reason else "")
                        ),
                        ESCALATED_DISPOSITION,
                        HOLD_EXPIRED,
                        self.now(),
                        hold.project_id,
                        hold.item_id,
                        HELD,
                    ),
                )
            finally:
                conn.close()
            expired.append(hold)
        return expired

    def add_spend(
        self,
        item_id: str,
        usd: float,
        unpriced: int = 0,
        *,
        project_id: str = DEFAULT_PROJECT,
    ) -> None:
        """Fold one attempt's spend into the item's running total.

        Accumulated across attempts on purpose: a per-item ceiling that reset
        on every re-claim would bound one attempt, and the thing worth bounding
        is the item. Added rather than replaced, so a resumed attempt's cost
        lands on top of what the crashed one already spent rather than
        pretending that money was never spent.
        """
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE work SET spend_usd = spend_usd + ?, "
                "unpriced_calls = unpriced_calls + ?, updated_at = ? "
                "WHERE project_id = ? AND item_id = ?",
                (usd, unpriced, self.now(), project_id, item_id),
            )
        finally:
            conn.close()

    def set_item_budget(
        self,
        item_id: str,
        *,
        seconds: float | None = None,
        spend_usd: float | None = None,
        project_id: str = DEFAULT_PROJECT,
    ) -> bool:
        """Override this item's ceilings. Zero means "take the project's"."""
        sets: list[str] = []
        params: list[Any] = []
        if seconds is not None:
            sets.append("budget_seconds = ?")
            params.append(float(seconds))
        if spend_usd is not None:
            sets.append("budget_spend_usd = ?")
            params.append(float(spend_usd))
        if not sets:
            return False
        sets.append("updated_at = ?")
        params.extend([self.now(), project_id, item_id])
        conn = self._connect()
        try:
            cursor = conn.execute(
                f"UPDATE work SET {', '.join(sets)} "  # noqa: S608 - fixed column names
                "WHERE project_id = ? AND item_id = ?",
                params,
            )
            return cursor.rowcount > 0
        finally:
            conn.close()

    def requeue(self, item_id: str, *, project_id: str = DEFAULT_PROJECT) -> bool:
        """Put an item back for another attempt, and mean it.

        The attempt counter is reset, because an item at its ceiling is
        retired by the very next claim scan: a "retry" that left the count
        alone put the item back to `pending` and watched it return to
        `exhausted` before any worker saw it, while reporting success.

        `last_error` is kept. It is the only record of why the item failed,
        it is what the retirement message appends to itself, and the operator
        retrying an item is usually the person who needs to read it. Clearing
        it turned a diagnosable failure into `gave up after N attempts` with
        no cause.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE work SET state = ?, owner = NULL, lease_until = 0, attempts = 0, "
                "updated_at = ? WHERE project_id = ? AND item_id = ?",
                (PENDING, self.now(), project_id, item_id),
            )
            requeued = cursor.rowcount > 0
            if requeued:
                # And every durable position with it. "Retry this" means from
                # the start, against the current plan -- not resumed into the
                # verdict the operator is retrying. Keeping the position here
                # would make a retry indistinguishable from a resume, and the
                # rejected item would be re-rejected without a model call.
                self.attempts_log.forget_item(project_id, item_id)
        finally:
            conn.close()
        if requeued:
            self._cancel_open_hold(item_id, project_id, "requeued")
        return requeued

    def reclaim_dead_workers(self, *, project_id: str | None = None) -> list[str]:
        """Release claims held by a process on this host that no longer exists.

        A lease expiring is how a *crashed* worker's item comes back, and it
        is deliberately slow so that a merely slow worker is not evicted. But
        after a pool restart the old worker is provably gone -- its pid is not
        running -- and waiting out its lease leaves an item stuck alongside
        newly dispatched work, with no session and nothing saying why.

        Only claims owned by *this host* are touched: a pid on another machine
        says nothing about whether that process is alive.
        """
        here = socket.gethostname()
        released: list[str] = []
        for record in self.claimed(project_id=project_id):
            owner = record.owner or ""
            host, _, pid = owner.rpartition(":")
            if host != here or not pid.isdigit():
                continue
            if _process_alive(int(pid)):
                continue
            self.release(
                record.item_id,
                PENDING,
                error=f"worker {owner} is gone; the claim was released rather than waited out",
                project_id=record.project_id,
            )
            released.append(record.item_id)
            log.info("reclaimed %s from dead worker %s", record.item_id, owner)
        return released

    def claimed(self, project_id: str | None = None) -> list[WorkRecord]:
        """Every item currently held by a worker, expired lease or not."""
        conn = self._connect()
        try:
            sql = "SELECT * FROM work WHERE state = ?"
            params: list[Any] = [CLAIMED]
            if project_id is not None:
                sql += " AND project_id = ?"
                params.append(project_id)
            return [WorkRecord.from_row(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def record_pr_url(
        self, item_id: str, pr_url: str, *, project_id: str = DEFAULT_PROJECT
    ) -> bool:
        """Attach a pull request to an item that has none.

        Fills a hole and nothing more: `pr_url IS NULL` is part of the match,
        so recovering a URL that was dropped can never overwrite one a worker
        recorded correctly. Deliberately not `release` -- the item's state is
        whatever it already is, and this is a fact about it rather than an
        outcome for it.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE work SET pr_url = ?, updated_at = ? "
                "WHERE project_id = ? AND item_id = ? AND pr_url IS NULL",
                (pr_url, self.now(), project_id, item_id),
            )
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
        project_id: str | None = None,
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
                "(session_id, project_id, item_id, reason, session_url, abandoned_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, project_id, item_id, reason, session_url, self.now()),
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
