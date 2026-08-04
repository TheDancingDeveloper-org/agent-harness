"""Where an attempt got to, durably, so a crash does not re-pay for it.

An attempt is currently monolithic. `Executor._execute` starts at the planner
call every time; if anything raises, the item is released and the next claim
begins again from `PLAN_PROMPT` — re-paying for the planner, re-selecting
context, and re-calling the implementer. The only thing that survived was the
branch and PR recovered through `_partial_for`, which is real and worth having,
but it is a recovered *artefact*, not a resumed *position*.

The repository already holds the correct principle — checkpoint before the
expensive gate — and applies it at exactly one point. This generalises one
checkpoint into a record of where an attempt got to.

## The three rules this module is built around

**Resumption is not replay.** LangGraph can replay a graph because it requires
its nodes to be deterministic. A model writing a diff is not deterministic and
never will be. Resuming here means *continue from the last durable artefact* —
the plan we already paid for, the diff we already have, the commit that already
exists — and never *run the log again and expect the same answers*.

**Recording a stage is not the same as being able to resume at it.** A stage is
recorded when it is reached; it is *resumable* only if its artefact survives the
worker. An uncommitted working tree does not survive a crash, so `applied` is
recorded and resumes by re-applying the diff it stored. The resumable positions
are stated in `RESUMES_AT` rather than implied, because an implied one is a
promise nothing keeps.

**Anything re-executed on resume must be idempotent.** Re-applying a stored diff
to a freshly cut branch is. Re-running a project's checks is. Pushing is not,
and neither is opening a pull request — so those sit *after* the durable
boundary that records them, and `sync` durability records the intent to do them
before they happen so a crash in the middle is visible rather than invisible.

## What this is not

**It is not a workflow engine.** The stage list below is the executor's own,
fixed, and known at compile time. There is no DSL, no user-defined graph, no
dynamic step registration, and adding one would be the failure mode §12 of the
original proposal names. This module records positions in a pipeline; it does
not let anyone define one.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------- the stages

#: The planner answered. Artefact: the plan and its targets.
PLANNED = "planned"
#: The implementer answered. Artefact: the diff, as text.
IMPLEMENTED = "implemented"
#: The diff applied to a branch. Artefact: the branch, its base, and the diff
#: that actually landed — which is not always the one the model wrote, because
#: the tolerance ladder rescues malformed patches.
APPLIED = "applied"
#: The project's checks passed. Artefact: nothing that makes resumption
#: cheaper; recorded so a report can say how far the attempt got.
CHECKED = "checked"
#: Committed to the branch, before the expensive gate. Artefact: the commit
#: sha, the branch, and the pull-request url if one was opened.
CHECKPOINTED = "checkpointed"
#: The reviewer answered. Artefact: the verdict and its text.
REVIEWED = "reviewed"

#: In order. Fixed, known here, and not extensible — see the module docstring.
STAGES = (PLANNED, IMPLEMENTED, APPLIED, CHECKED, CHECKPOINTED, REVIEWED)

#: What each recorded stage lets a resumed attempt *skip*, which is a smaller
#: set than what it records. `applied` and `checked` resume as `implemented`
#: does, because an uncommitted working tree is not durable: the stored diff is
#: re-applied to a freshly cut branch, which is idempotent and costs no model
#: call. `checkpointed` is the strong one — the commit is already in git.
RESUMES_AT: dict[str, str] = {
    PLANNED: PLANNED,
    IMPLEMENTED: IMPLEMENTED,
    APPLIED: IMPLEMENTED,
    CHECKED: IMPLEMENTED,
    CHECKPOINTED: CHECKPOINTED,
    # The verdict is durable too, and re-asking would be worse than wasteful:
    # a model is not deterministic, so a crash after review would become a way
    # to shop for a different answer.
    REVIEWED: REVIEWED,
}


# ----------------------------------------------------------- durability mode

#: Nothing is made durable until the attempt ends. Cheapest, and it means a
#: crash resumes from the planner exactly as it did before this module existed.
#: The deterministic demo runs here; a fleet should not.
EXIT = "exit"
#: One write per stage boundary. The default, and what "resumable" means.
BOUNDARY = "boundary"
#: Every boundary, **and** a record of the intent to perform each external
#: effect before it is performed. A crash between "about to push" and "pushed"
#: is then a fact rather than a gap.
SYNC = "sync"

MODES = (EXIT, BOUNDARY, SYNC)

DEFAULT_MODE = BOUNDARY


SCHEMA = """
-- One row per stage an attempt reached. Keyed by the attempt number, so the
-- history of an item that was tried three times is three sets of rows rather
-- than one set overwritten twice.
CREATE TABLE IF NOT EXISTS attempt_stages (
    project_id  TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    attempt     INTEGER NOT NULL,
    stage       TEXT NOT NULL,
    artefact    TEXT NOT NULL DEFAULT '{}',
    -- The graph revision the attempt was admitted at, carried on every row so
    -- a resumed attempt can say which graph it was briefed against without
    -- reading the work row, which may have moved.
    admitted_revision INTEGER NOT NULL DEFAULT 0,
    mode        TEXT NOT NULL DEFAULT 'boundary',
    -- Whether the attempt this row belongs to reached a DECISION. A sealed
    -- attempt is history and is never resumed: an item that was reviewed and
    -- rejected must be re-planned if it is retried, not resumed into its own
    -- rejection. Only an attempt nobody finished with -- a killed worker --
    -- stays resumable.
    sealed      INTEGER NOT NULL DEFAULT 0,
    recorded_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, item_id, attempt, stage)
);

-- What the worker was actually told. `WorkQueue.add` rewrites title, brief and
-- depends_on on live claimed rows, so a worker can be briefed from one
-- revision and judged against another. This pins the briefing.
CREATE TABLE IF NOT EXISTS attempt_brief (
    project_id  TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    attempt     INTEGER NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    brief       TEXT NOT NULL DEFAULT '',
    depends_on  TEXT NOT NULL DEFAULT '[]',
    admitted_revision INTEGER NOT NULL DEFAULT 0,
    started_at  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, item_id, attempt)
);

-- An external effect that was about to happen. Written only in `sync` mode,
-- and cleared when the effect completes: a row left behind is a crash caught
-- in the one window where an effect may have half-happened.
CREATE TABLE IF NOT EXISTS attempt_intents (
    project_id  TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    attempt     INTEGER NOT NULL,
    effect      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    opened_at   REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, item_id, attempt, effect)
);
"""


@dataclass(frozen=True)
class StageRecord:
    """One stage an attempt reached, and what it left behind."""

    stage: str
    artefact: dict[str, Any] = field(default_factory=dict)
    admitted_revision: int = 0
    mode: str = DEFAULT_MODE
    recorded_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "artefact": self.artefact,
            "admitted_revision": self.admitted_revision,
            "mode": self.mode,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class Brief:
    """What the worker was told, pinned at the revision it was told it."""

    title: str
    brief: str
    depends_on: list[str]
    admitted_revision: int
    started_at: float = 0.0


@dataclass
class Resume:
    """Where a re-claimed attempt should start, and what it already has.

    `at` is one of `STAGES` or empty. Empty means start at the planner, which
    is what every attempt did before this module existed and is still the
    correct answer for a first attempt.
    """

    attempt: int
    at: str = ""
    reached: tuple[str, ...] = ()
    artefacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    brief: Brief | None = None
    #: Effects that were begun and never confirmed. Only ever non-empty in
    #: `sync` mode; in the others a half-done push is simply invisible.
    open_intents: tuple[str, ...] = ()

    @property
    def resumable(self) -> bool:
        return bool(self.at)

    def artefact(self, stage: str) -> dict[str, Any]:
        return self.artefacts.get(stage, {})

    def skips(self, stage: str) -> bool:
        """Whether `stage` has already been paid for and must not be redone.

        Ordered by the fixed stage list, so "we got to `checkpointed`" answers
        yes for every stage before it without each caller re-deriving that.
        """
        if not self.at:
            return False
        return STAGES.index(stage) <= STAGES.index(self.at)


class AttemptLog:
    """The durable record of attempts, over the same database as the queue.

    Deliberately the same file. A resumable position that can disappear
    independently of the item it belongs to is a resumable position that will
    one day point at work the queue has forgotten.
    """

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        *,
        mode: str = DEFAULT_MODE,
        now: Callable[[], float] = time.time,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown durability mode {mode!r}; expected {MODES}")
        self._connect = connect
        self.mode = mode
        self.now = now
        #: Buffered stage records, for `exit` mode. Lost on a crash, which is
        #: exactly what that mode promises.
        self._pending: list[tuple[str, str, int, StageRecord]] = []

    # ------------------------------------------------------------- schema

    def migrate(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA)

    # ------------------------------------------------------------ writing

    def mode_for(self, configured: str | None) -> str:
        """The mode this attempt runs under. An unknown one is refused loudly.

        Refusing beats defaulting: a typo in a project's configuration that
        silently downgraded durability to `exit` would look exactly like a
        harness that had stopped resuming, and nobody would know which.
        """
        if not configured:
            return self.mode
        if configured not in MODES:
            raise ValueError(f"unknown durability mode {configured!r}; expected {MODES}")
        return configured

    def begin(
        self,
        project_id: str,
        item_id: str,
        attempt: int,
        *,
        title: str,
        brief: str,
        depends_on: list[str],
        admitted_revision: int,
    ) -> None:
        """Pin what the worker was told, before it is told anything else.

        Written in every mode, including `exit`. The brief is not a resumption
        artefact — it is the record of what was *asked for*, and losing it
        means an attempt can be judged against a question nobody put to it.
        `INSERT OR IGNORE`, so re-claiming an attempt keeps the original
        briefing rather than re-pinning the current row over it.
        """
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO attempt_brief "
                "(project_id, item_id, attempt, title, brief, depends_on, "
                "admitted_revision, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    item_id,
                    attempt,
                    title,
                    brief,
                    json.dumps(list(depends_on)),
                    admitted_revision,
                    self.now(),
                ),
            )
        finally:
            conn.close()

    def record(
        self,
        project_id: str,
        item_id: str,
        attempt: int,
        stage: str,
        artefact: Mapping[str, Any] | None = None,
        *,
        admitted_revision: int = 0,
        mode: str | None = None,
    ) -> StageRecord:
        """Say the attempt reached `stage`, with what it left behind."""
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}; expected {STAGES}")
        effective = self.mode_for(mode)
        entry = StageRecord(
            stage=stage,
            artefact=dict(artefact or {}),
            admitted_revision=admitted_revision,
            mode=effective,
            recorded_at=self.now(),
        )
        if effective == EXIT:
            # Buffered, not written. A crash loses it, which is the whole
            # meaning of the mode rather than an accident of it.
            self._pending.append((project_id, item_id, attempt, entry))
            return entry
        self._write(project_id, item_id, attempt, entry)
        return entry

    def _write(self, project_id: str, item_id: str, attempt: int, entry: StageRecord) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO attempt_stages "
                "(project_id, item_id, attempt, stage, artefact, admitted_revision, "
                "mode, sealed, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?) "
                "ON CONFLICT(project_id, item_id, attempt, stage) DO UPDATE SET "
                "artefact=excluded.artefact, admitted_revision=excluded.admitted_revision, "
                "mode=excluded.mode, recorded_at=excluded.recorded_at",
                (
                    project_id,
                    item_id,
                    attempt,
                    entry.stage,
                    json.dumps(entry.artefact),
                    entry.admitted_revision,
                    entry.mode,
                    entry.recorded_at,
                ),
            )
        finally:
            conn.close()

    def flush(self, mode: str | None = None) -> int:
        """Write anything `exit` mode buffered. Returns how many rows landed.

        Called when the attempt ends, however it ends. A failed attempt's
        record is as useful as a successful one's — more so, since it is the
        one somebody will read.
        """
        if self.mode_for(mode) != EXIT or not self._pending:
            self._pending.clear()
            return 0
        written = list(self._pending)
        self._pending.clear()
        for project_id, item_id, attempt, entry in written:
            self._write(project_id, item_id, attempt, entry)
        return len(written)

    def discard(self) -> None:
        """Throw away anything buffered. For a worker that lost its claim."""
        self._pending.clear()

    # ------------------------------------------------------- sync intents

    def opening(
        self,
        project_id: str,
        item_id: str,
        attempt: int,
        effect: str,
        detail: str = "",
        *,
        mode: str | None = None,
    ) -> bool:
        """About to do something external. Recorded only in `sync` mode.

        Returns whether anything was written, so a caller can say in its event
        stream which durability it actually got rather than which it asked for.
        """
        if self.mode_for(mode) != SYNC:
            return False
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO attempt_intents "
                "(project_id, item_id, attempt, effect, detail, opened_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(project_id, item_id, attempt, effect) DO UPDATE SET "
                "detail=excluded.detail, opened_at=excluded.opened_at",
                (project_id, item_id, attempt, effect, detail, self.now()),
            )
        finally:
            conn.close()
        return True

    def closed(
        self, project_id: str, item_id: str, attempt: int, effect: str, *, mode: str | None = None
    ) -> None:
        """It finished. The intent row goes away; a surviving one is a crash."""
        if self.mode_for(mode) != SYNC:
            return
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM attempt_intents WHERE project_id = ? AND item_id = ? "
                "AND attempt = ? AND effect = ?",
                (project_id, item_id, attempt, effect),
            )
        finally:
            conn.close()

    # ------------------------------------------------------------ reading

    def resume(self, project_id: str, item_id: str, attempt: int) -> Resume:
        """Where this attempt should start.

        An attempt with no recorded stages is not an error and not a failure —
        it is a first attempt, or one that ran under `exit` durability and
        crashed. Either way the answer is "start at the planner", which is
        what the executor did before any of this existed.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT stage, artefact, admitted_revision FROM attempt_stages "
                "WHERE project_id = ? AND item_id = ? AND attempt = ? AND sealed = 0",
                (project_id, item_id, attempt),
            ).fetchall()
            brief_row = conn.execute(
                "SELECT title, brief, depends_on, admitted_revision, started_at "
                "FROM attempt_brief WHERE project_id = ? AND item_id = ? AND attempt = ?",
                (project_id, item_id, attempt),
            ).fetchone()
            intents = conn.execute(
                "SELECT effect FROM attempt_intents "
                "WHERE project_id = ? AND item_id = ? AND attempt = ?",
                (project_id, item_id, attempt),
            ).fetchall()
        finally:
            conn.close()

        artefacts = {row["stage"]: json.loads(row["artefact"] or "{}") for row in rows}
        reached = tuple(stage for stage in STAGES if stage in artefacts)
        # The furthest stage reached, then what that lets us resume at — which
        # is not always the same thing.
        at = RESUMES_AT[reached[-1]] if reached else ""
        pinned = (
            Brief(
                title=brief_row["title"],
                brief=brief_row["brief"],
                depends_on=json.loads(brief_row["depends_on"] or "[]"),
                admitted_revision=brief_row["admitted_revision"],
                started_at=brief_row["started_at"],
            )
            if brief_row is not None
            else None
        )
        return Resume(
            attempt=attempt,
            at=at,
            reached=reached,
            artefacts=artefacts,
            brief=pinned,
            open_intents=tuple(sorted(row["effect"] for row in intents)),
        )

    def has_resumable_work(self, project_id: str, item_id: str, attempt: int) -> bool:
        """Whether re-claiming this item would continue rather than restart.

        Read by `WorkQueue.claim` to decide whether the re-claim consumes a new
        attempt. **D11 is resolved: a resumed attempt continues the existing
        one**, so an item whose last attempt left a durable position keeps its
        number, and `max_attempts` goes on bounding genuine failures rather
        than crashes.
        """
        if attempt <= 0:
            return False
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM attempt_stages WHERE project_id = ? AND item_id = ? "
                "AND attempt = ? AND sealed = 0 LIMIT 1",
                (project_id, item_id, attempt),
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def seal(self, project_id: str, item_id: str, attempt: int) -> None:
        """A decision was reached about this attempt, so it stops being resumable.

        The distinction this keeps is the one the whole module turns on. A
        **killed** worker never got to a decision, so its position is a place
        to continue from. A worker that reached a verdict — approved, rejected,
        escalated — *did* decide, and resuming into that decision would mean an
        operator's retry silently replaying the rejection it was retrying.

        The rows are kept rather than deleted. They are the history of what
        happened, and the cost-under-crashes accounting is answered by counting
        them.
        """
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE attempt_stages SET sealed = 1 "
                "WHERE project_id = ? AND item_id = ? AND attempt = ?",
                (project_id, item_id, attempt),
            )
            conn.execute(
                "DELETE FROM attempt_intents WHERE project_id = ? AND item_id = ? AND attempt = ?",
                (project_id, item_id, attempt),
            )
        finally:
            conn.close()

    def forget_item(self, project_id: str, item_id: str) -> None:
        """Everything about every attempt at this item, gone.

        For `requeue`: an operator saying "try this again" means from the
        start, against the current plan, with no memory of what a previous
        attempt decided. Keeping the history here would make a retry
        indistinguishable from a resume.
        """
        conn = self._connect()
        try:
            for table in ("attempt_stages", "attempt_brief", "attempt_intents"):
                conn.execute(
                    f"DELETE FROM {table} WHERE project_id = ? AND item_id = ?",  # noqa: S608
                    (project_id, item_id),
                )
        finally:
            conn.close()

    def abandon(self, project_id: str, item_id: str, attempt: int) -> None:
        """Forget this attempt's position entirely, so it restarts from the planner.

        Used when the graph moved under a live claim: the plan the attempt was
        briefed with is no longer the plan, so the diff it produced is an
        answer to a question nobody is asking any more. Resuming into it would
        be the silent-judgement-against-a-newer-brief failure §7.4 names.
        """
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM attempt_stages WHERE project_id = ? AND item_id = ? AND attempt = ?",
                (project_id, item_id, attempt),
            )
            conn.execute(
                "DELETE FROM attempt_intents WHERE project_id = ? AND item_id = ? AND attempt = ?",
                (project_id, item_id, attempt),
            )
        finally:
            conn.close()

    def history(self, project_id: str, item_id: str) -> list[tuple[int, StageRecord]]:
        """Every stage of every attempt at this item, oldest first."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT attempt, stage, artefact, admitted_revision, mode, recorded_at "
                "FROM attempt_stages WHERE project_id = ? AND item_id = ? "
                # `rowid` breaks the tie. Several stages of one attempt can
                # share a timestamp -- a fast run, or an injected clock -- and
                # "the order they happened in" is then insertion order, not
                # whatever the index happens to return.
                "ORDER BY attempt, recorded_at, rowid",
                (project_id, item_id),
            ).fetchall()
        finally:
            conn.close()
        return [
            (
                row["attempt"],
                StageRecord(
                    stage=row["stage"],
                    artefact=json.loads(row["artefact"] or "{}"),
                    admitted_revision=row["admitted_revision"],
                    mode=row["mode"],
                    recorded_at=row["recorded_at"],
                ),
            )
            for row in rows
        ]
