"""An item waiting on a person, durably, and answerable from anywhere.

`waiting_for_input` was a **projection over the event stream**: the API scanned
recent events and reported which items had most recently emitted one. The row
stayed `claimed`, the heartbeat kept stamping, and the lease kept renewing — so
a lease whose entire purpose is to distinguish *slow* from *dead* was being used
to hold open a human's inbox. Nothing bounded it, nothing survived the worker
dying, and the answer could only come from the process that happened to be
attached.

Issue #103 is that hole seen from outside: a silent-but-active session is
indistinguishable from a hang. A held item now says which it is.

## What a hold is

A durable state on the work item, carrying the question, who may answer it, and
a resume token. **D12, resolved 2026-08-04: a hold suspends the lease and keeps
the claim.** The worktree and the agent's context survive, so answering resumes
where the item stopped — the reasoning `work.py` already gives about pause
semantics, that stopping mid-item destroys context and leaves a half-finished
worktree, applies unchanged. The item is not eligible for another worker while
held.

The cost of that ruling is named rather than hidden: a worker slot is tied up
for the whole hold. **The maximum hold duration is therefore not decoration** —
it is what stops one unanswered question from consuming a worker indefinitely.

## Four rules

**No model may interpret the answer into a routing decision.** An answer is
structured data, or it is a message recorded for a person to read. It is never a
prompt that decides what the approval meant. CrewAI routes on an LLM's reading
of human feedback; under `AGENTS.md` that is a gate decided by a model, and it
is rejected.

**No text is injected into a live PTY.** `COORDINATION-PLANE.md` §5.1 rules on
this and the reason is exact: the process may be at a shell, and an answer
becomes a command. Nothing here writes to a session.

**Being held is not approval.** A hold weakens no gate, and a hold that times
out returns the item to `blocked` — never to `ready`, and never past a gate it
had not passed.

**This is not the coordination plane.** The item-level hold only. The message
ledger, rooms and the oversight actor remain proposed and unimplemented, and
nothing here is a step towards implementing them by accident.

## Saying that a question exists

A hold was durable and silent (#188). Every route to one was a *pull* — list
the inbox, read the item — so an item could sit unanswered overnight while
every dashboard read healthy, which is the failure `/api/audit/health` exists
to defend against elsewhere: a system that looks identical whether or not it is
doing the thing it is for.

Opening a hold therefore emits a **notice** through one injected callable,
`on_hold`, exactly as `ModelClient` takes `on_event`. Core does not know what
the other end is: a session host, a webhook, a file, or nothing at all. It is
delivery of a question, **not a notification system** — there is no retry, no
queue and no provider in here.

Two rules the notice keeps:

- **It cannot reach the item.** A hook that raises, or that is broken in any
  other way, is logged and dropped. Nothing it does can fail, stall or
  un-hold the item it concerns — the rule `audit` already follows for
  telemetry, for the same reason: the fleet must not depend on it.
- **It carries no resume token.** The notice says where the answer goes, not
  what spends it. `executor.py` already keeps the token out of the message
  ledger — "a token in a room is a token anything that can read the room may
  spend" — and a notice is read by strictly more things than a room is.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

log = logging.getLogger(__name__)

#: How long a hold lasts before it gives up and returns the item, when nothing
#: says otherwise. Six hours: long enough to survive a night for someone in
#: another timezone, short enough that a forgotten question does not hold a
#: worker for a week. Configurable per project and per hold.
DEFAULT_MAX_HOLD_SECONDS = 6 * 60 * 60

#: A hold nobody has answered.
OPEN = "open"
#: Answered, and the item released back to its worker.
ANSWERED = "answered"
#: The maximum duration passed. The item went to `blocked` with the question
#: preserved; the hold is kept as the record of what was asked and never
#: answered.
EXPIRED = "expired"
#: The item was blocked, retried or requeued out from under the hold. Recorded
#: rather than deleted: "nobody answered" and "somebody cancelled it" are
#: different facts about the same unanswered question.
CANCELLED = "cancelled"

HOLD_STATES = (OPEN, ANSWERED, EXPIRED, CANCELLED)

#: Who may answer, when the asker does not care. Recorded explicitly rather
#: than left null, so "anyone" is a decision somebody made.
ANYONE = "anyone"


SCHEMA = """
-- One row per question. Keyed by (project, item, attempt, asked_at) rather
-- than by item, because an attempt may legitimately ask more than one thing
-- and the history of what was asked is worth as much as the current question.
CREATE TABLE IF NOT EXISTS holds (
    project_id   TEXT NOT NULL,
    item_id      TEXT NOT NULL,
    attempt      INTEGER NOT NULL DEFAULT 0,
    asked_at     REAL NOT NULL DEFAULT 0,
    state        TEXT NOT NULL DEFAULT 'open',
    question     TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    -- Who may answer. Recorded and reported; NOT enforced as authentication,
    -- because this service has one bearer token and pretending otherwise
    -- would be a security claim it cannot keep.
    who_may_answer TEXT NOT NULL DEFAULT 'anyone',
    -- The claim that is suspended, kept so answering can hand the item back
    -- to the worker that asked rather than to whoever happens to be free.
    owner        TEXT,
    session_id   TEXT,
    session_url  TEXT,
    -- Opaque, single-use, and the only thing that authorises an answer to
    -- THIS question. A stale answer arriving after a timeout must not land on
    -- whatever the item is doing an hour later.
    resume_token TEXT NOT NULL,
    expires_at   REAL NOT NULL DEFAULT 0,
    answered_at  REAL NOT NULL DEFAULT 0,
    answered_by  TEXT NOT NULL DEFAULT '',
    -- Structured data, or a message for a person to read. Never fed to a
    -- model to work out what the human meant.
    answer       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, item_id, attempt, asked_at)
);
CREATE INDEX IF NOT EXISTS holds_open ON holds (project_id, state, expires_at);
"""


class HoldError(RuntimeError):
    """An answer that cannot be accepted, with a reason a human can act on."""


@dataclass(frozen=True)
class Hold:
    """One question, and everything needed to answer it from anywhere."""

    project_id: str
    item_id: str
    attempt: int
    asked_at: float
    question: str
    resume_token: str
    state: str = OPEN
    reason: str = ""
    who_may_answer: str = ANYONE
    owner: str | None = None
    session_id: str | None = None
    session_url: str | None = None
    expires_at: float = 0.0
    answered_at: float = 0.0
    answered_by: str = ""
    answer: str = ""

    def age(self, now: float) -> float:
        """How long this has been waiting. The number #103 is really about:
        a silent session and a hung one look identical until somebody can say
        how long the silence has lasted."""
        return max(0.0, now - self.asked_at)

    def remaining(self, now: float) -> float:
        return max(0.0, self.expires_at - now) if self.expires_at else float("inf")

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        at = now if now is not None else time.time()
        return {
            "project_id": self.project_id,
            "item_id": self.item_id,
            "attempt": self.attempt,
            "state": self.state,
            "question": self.question,
            "reason": self.reason,
            "who_may_answer": self.who_may_answer,
            "asked_at": self.asked_at,
            "age_seconds": round(self.age(at), 1),
            "expires_at": self.expires_at or None,
            "session_id": self.session_id,
            "session_url": self.session_url,
            "answered_at": self.answered_at or None,
            "answered_by": self.answered_by or None,
            "answer": self.answer or None,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Hold:
        return cls(**dict(row))


#: What a notice says happened. One outcome rather than a new event kind: a
#: kind is a deliberate act (`events.py`), and this is a stage of a work item
#: like every other one the stream already carries.
HOLD_OPENED = "hold_opened"


def answer_path(hold: Hold) -> str:
    """Where the answer to this question goes, relative to the API root.

    Relative on purpose. The consumer knows the base URL it is already
    talking to; the harness does not know what it is reached as from
    outside, and a URL it guessed would be a URL nobody can call.
    """
    return (
        f"/api/work/{quote(hold.item_id, safe='')}/answer"
        f"?project_id={quote(hold.project_id, safe='')}"
    )


def hold_notice(hold: Hold) -> dict[str, Any]:
    """One opened hold, as the event a consumer is told about.

    Flat, and shaped like the rest of the stream (`kind`, `outcome`, `ts`,
    then detail), so the same callable that already writes events can take it
    without translation.

    **No resume token.** Answering is an action through the API, which looks
    the token up itself; a token in a notice is a token anything that can read
    the notice may spend.
    """
    return {
        "ts": hold.asked_at,
        "kind": "work",
        "outcome": HOLD_OPENED,
        "worker": hold.owner,
        "project_id": hold.project_id,
        "item_id": hold.item_id,
        "attempt": hold.attempt,
        "question": hold.question,
        "reason": hold.reason,
        "who_may_answer": hold.who_may_answer,
        "asked_at": hold.asked_at,
        "expires_at": hold.expires_at or None,
        "session_id": hold.session_id,
        "session_url": hold.session_url,
        # Enough to build the URL that answers it, without pretending to know
        # what this service is reached as.
        "answer_path": answer_path(hold),
        # The line a person reads. Built here so every consumer says the same
        # thing rather than each inventing its own wording.
        "detail": f"{hold.item_id} is waiting on a person: {hold.question}",
    }


def deliver(hook: Callable[[dict[str, Any]], None] | None, notice: dict[str, Any]) -> bool:
    """Hand a notice to a hook, and let nothing it does escape.

    The one rule that matters: **a failed notification is dropped, never
    raised.** A hold exists because an item stopped to ask a person something,
    and a broken webhook must not turn that into a failed item.
    """
    if hook is None:
        return False
    try:
        hook(notice)
    except Exception:  # noqa: BLE001 - a notice must never reach the item
        log.warning(
            "hold: could not deliver the notice for %s/%s; the hold is unaffected",
            notice.get("project_id"),
            notice.get("item_id"),
            exc_info=True,
        )
        return False
    return True


def fanout(
    *hooks: Callable[[dict[str, Any]], None] | None,
) -> Callable[[dict[str, Any]], None] | None:
    """One hook over several consumers, each isolated from the others.

    A deployment usually has two: the event stream it already writes, and
    whatever the operator configured. One of them failing must not stop the
    other, so each goes through `deliver`.
    """
    live = [hook for hook in hooks if hook is not None]
    if not live:
        return None
    if len(live) == 1:
        only = live[0]

        def one(notice: dict[str, Any]) -> None:
            deliver(only, notice)

        return one

    def all_of_them(notice: dict[str, Any]) -> None:
        for hook in live:
            deliver(hook, notice)

    return all_of_them


def webhook_hook(
    url: str,
    *,
    timeout: float = 5.0,
    send: Callable[[str, bytes, float], None] | None = None,
) -> Callable[[dict[str, Any]], None] | None:
    """POST each notice, as JSON, to one URL the operator names.

    A URL is the whole configuration. This service does not learn what is on
    the other end of it, and adding a chat product is that product's own
    receiver rather than a branch in here.

    Bounded by a timeout because "never stalls the item" includes a consumer
    that accepts the connection and then thinks about it forever. `send` is
    injected so a test proves the shape without a socket.
    """
    if not url:
        return None
    post = send if send is not None else _post_json

    def hook(notice: dict[str, Any]) -> None:
        post(url, json.dumps(notice).encode(), timeout)

    return hook


def _post_json(url: str, body: bytes, timeout: float) -> None:
    request = urllib.request.Request(  # noqa: S310 - the URL is the operator's own config
        url, data=body, headers={"content-type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout):  # noqa: S310
        return


@dataclass
class Answer:
    """What a person said. Structured data, or a message. Never a prompt.

    `data` is for a caller that has something machine-readable to say — a
    choice from a list, a boolean, an identifier. `text` is for a caller that
    has a sentence. Both are recorded verbatim and **neither is ever shown to
    a model to work out what the human meant.**
    """

    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    who: str = ""

    def stored(self) -> str:
        return json.dumps({"text": self.text, "data": self.data}, sort_keys=True)


class Holds:
    """The durable holds, over the same database as the queue."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        *,
        now: Callable[[], float] = time.time,
        on_hold: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._connect = connect
        self.now = now
        #: Told, once, that a question now exists. Injected exactly like
        #: `ModelClient.on_event`, assignable after construction because a
        #: deployment builds its event sink after it opens the queue. `None`
        #: is a supported configuration: the pull routes still work.
        self.on_hold: Callable[[dict[str, Any]], None] | None = on_hold

    def migrate(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA)

    # ------------------------------------------------------------- asking

    def open(
        self,
        project_id: str,
        item_id: str,
        *,
        question: str,
        attempt: int = 0,
        reason: str = "",
        who_may_answer: str = ANYONE,
        owner: str | None = None,
        session_id: str | None = None,
        session_url: str | None = None,
        max_seconds: float = DEFAULT_MAX_HOLD_SECONDS,
    ) -> Hold:
        """Record a question, and return the token that answers it.

        A question is required and refused if empty. A hold with no question is
        indistinguishable from a hang, which is the entire thing this exists to
        fix.
        """
        if not question.strip():
            raise HoldError(
                "a hold needs a question: one with no question is indistinguishable "
                "from the hang it exists to be distinguished from"
            )
        now = self.now()
        hold = Hold(
            project_id=project_id,
            item_id=item_id,
            attempt=attempt,
            asked_at=now,
            question=question,
            # Opaque and single-use. It authorises an answer to *this*
            # question, so a reply arriving after a timeout cannot land on
            # whatever the item is doing an hour later.
            resume_token=secrets.token_urlsafe(24),
            reason=reason,
            who_may_answer=who_may_answer or ANYONE,
            owner=owner,
            session_id=session_id,
            session_url=session_url,
            expires_at=(now + max_seconds) if max_seconds > 0 else 0.0,
        )
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO holds (project_id, item_id, attempt, asked_at, state, question, "
                "reason, who_may_answer, owner, session_id, session_url, resume_token, "
                "expires_at, answered_at, answered_by, answer) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', '')",
                (
                    hold.project_id,
                    hold.item_id,
                    hold.attempt,
                    hold.asked_at,
                    OPEN,
                    hold.question,
                    hold.reason,
                    hold.who_may_answer,
                    hold.owner,
                    hold.session_id,
                    hold.session_url,
                    hold.resume_token,
                    hold.expires_at,
                ),
            )
        finally:
            conn.close()
        # After the row is durable, and outside its connection: the notice is
        # a statement that a question exists, so it must not be able to be
        # made about one that was never recorded — and a slow consumer must
        # not hold a write connection open while it thinks.
        deliver(self.on_hold, hold_notice(hold))
        return hold

    # ------------------------------------------------------------ reading

    def current(self, project_id: str, item_id: str) -> Hold | None:
        """The open question on this item, if there is one."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM holds WHERE project_id = ? AND item_id = ? AND state = ? "
                "ORDER BY asked_at DESC LIMIT 1",
                (project_id, item_id, OPEN),
            ).fetchone()
        finally:
            conn.close()
        return Hold.from_row(row) if row is not None else None

    def history(self, project_id: str, item_id: str) -> list[Hold]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM holds WHERE project_id = ? AND item_id = ? ORDER BY asked_at",
                (project_id, item_id),
            ).fetchall()
        finally:
            conn.close()
        return [Hold.from_row(row) for row in rows]

    def open_holds(self, project_id: str | None = None) -> list[Hold]:
        """Every unanswered question, oldest first — which is the order a
        person should work through them in."""
        conn = self._connect()
        try:
            if project_id is None:
                rows = conn.execute(
                    "SELECT * FROM holds WHERE state = ? ORDER BY asked_at", (OPEN,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM holds WHERE state = ? AND project_id = ? ORDER BY asked_at",
                    (OPEN, project_id),
                ).fetchall()
        finally:
            conn.close()
        return [Hold.from_row(row) for row in rows]

    # ------------------------------------------------------------ closing

    def answer(self, project_id: str, item_id: str, token: str, answer: Answer) -> Hold:
        """Accept an answer to the open question, from anywhere.

        The token is the whole authorisation, and it is checked against the
        *open* hold only. Answering an expired or already-answered question
        raises rather than silently doing nothing: a person who typed an
        answer deserves to be told it arrived too late.
        """
        hold = self.current(project_id, item_id)
        if hold is None:
            raise HoldError(f"{item_id} has no open question")
        if not secrets.compare_digest(hold.resume_token, token):
            raise HoldError("that resume token does not answer this question")
        now = self.now()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE holds SET state = ?, answered_at = ?, answered_by = ?, answer = ? "
                "WHERE project_id = ? AND item_id = ? AND attempt = ? AND asked_at = ?",
                (
                    ANSWERED,
                    now,
                    answer.who,
                    answer.stored(),
                    project_id,
                    item_id,
                    hold.attempt,
                    hold.asked_at,
                ),
            )
        finally:
            conn.close()
        return Hold(
            **{
                **hold.__dict__,
                "state": ANSWERED,
                "answered_at": now,
                "answered_by": answer.who,
                "answer": answer.stored(),
            }
        )

    def close(self, project_id: str, item_id: str, state: str) -> int:
        """Close any open question on this item, without answering it.

        `expired` and `cancelled` are different facts and both are kept: the
        question that timed out and the question somebody overrode are not the
        same thing to whoever reads this later.
        """
        if state not in (EXPIRED, CANCELLED):
            raise ValueError(f"a hold can only be closed as expired or cancelled, not {state!r}")
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE holds SET state = ? WHERE project_id = ? AND item_id = ? AND state = ?",
                (state, project_id, item_id, OPEN),
            )
            return int(cursor.rowcount)
        finally:
            conn.close()

    def due(self, now: float | None = None) -> list[Hold]:
        """Open holds whose maximum duration has passed."""
        at = self.now() if now is None else now
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM holds WHERE state = ? AND expires_at > 0 AND expires_at <= ? "
                "ORDER BY expires_at",
                (OPEN, at),
            ).fetchall()
        finally:
            conn.close()
        return [Hold.from_row(row) for row in rows]
