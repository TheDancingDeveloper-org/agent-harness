"""One coordinator per project, with bounded authority and a real lease.

The oversight actor reads what agents said, looks at authoritative state,
and proposes what should happen next. It is a *participant* in the control
plane, not a safety mechanism: claims, dependency admission, ownership,
checks and review stay deterministic whether it is right, wrong or entirely
absent.

Three constraints shape every line here, and each is a failure this exists
to make impossible rather than a preference.

**It cannot be two.** Authority is a lease taken by compare-and-set, with a
fencing generation. A restart, a duplicate deployment or a network partition
can all produce a second process that believes it is the coordinator; only
one holds the lease, and the command service refuses proposals carrying a
stale generation. The lease is per project, so a deposed coordinator for one
project cannot stall any other.

**It cannot reach anything.** No queue handle, no ledger write path beyond
appending messages, no GitHub credential, no database. It reads a read-only
view and it emits typed proposals. Everything else happens because a
deterministic command service accepted one -- and that service re-validates
every precondition regardless of who proposed it. The model's worst possible
output is a bad suggestion.

**Its absence is safe.** If the model is unavailable, the route is
misconfigured or the actor crashes mid-cycle, unresolved work stays blocked
and everything whose deterministic gates are satisfied keeps running. A
coordinator that could pause the fleet by failing would be a worse liability
than the problems it exists to resolve.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .command_service import (
    ActionProposal,
    AuthorityProof,
    CommandResult,
    ExpectedTargetState,
    Risk,
)
from .coordination import GENERAL_ROOM, Message, MessageLedger, Submission

log = logging.getLogger(__name__)

#: How long a coordinator holds authority before it must renew. Short enough
#: that a dead one is replaced within a cycle or two, long enough that a slow
#: model call does not lose the lease mid-decision.
DEFAULT_LEASE_SECONDS = 120.0

#: Messages the actor answers to. Anything else is conversation it may read
#: for context but does not act on -- an actor that reacts to every message
#: is one that reacts to its own.
TRIGGER_TYPES = frozenset({"dependency_found", "dependency_unresolved", "question", "observation"})

#: What the model is allowed to come back with. A closed set, validated
#: before anything reaches the command service, because "the model emitted
#: an action name nobody implemented" must be a rejection and not a crash.
ANSWER = "answer"
PROPOSE = "propose"
ESCALATE = "escalate"
WAIT = "wait"
DECISIONS = frozenset({ANSWER, PROPOSE, ESCALATE, WAIT})


class OversightUnavailable(RuntimeError):
    """The coordinator could not run. Nothing is unblocked as a result."""


@dataclass(frozen=True)
class Authority:
    """Proof that this process is the current coordinator for one project."""

    project_id: str
    holder_id: str
    generation: int
    valid_until: float

    def as_proof(self) -> AuthorityProof:
        return AuthorityProof(
            project_id=self.project_id,
            holder_id=self.holder_id,
            generation=self.generation,
            valid_until=self.valid_until,
        )


class AuthorityStore:
    """Per-project coordinator leases, taken by compare-and-set.

    A row per project, holding the current holder and a generation that only
    ever increases. Deliberately not a module-level variable or a process
    singleton: two API instances in one deployment would each have their own,
    and both would be certain they were the only one.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS oversight_authority (
        project_id  TEXT PRIMARY KEY,
        holder_id   TEXT NOT NULL,
        generation  INTEGER NOT NULL,
        valid_until REAL NOT NULL
    );
    """

    def __init__(self, path: str | object, *, now: Callable[[], float] = time.time) -> None:
        self.path = str(path)
        self.now = now
        with self._connect() as conn:
            conn.executescript(self._SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def acquire(
        self, project_id: str, holder_id: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS
    ) -> Authority | None:
        """Take or renew authority. None means somebody else holds it.

        The whole read-decide-write is one IMMEDIATE transaction, because two
        processes starting simultaneously is the exact case this is for: with
        a read outside the transaction they both see it free and both take it.
        """

        now = self.now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT holder_id, generation, valid_until FROM oversight_authority "
                "WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                generation = 1
            elif row["holder_id"] == holder_id:
                # A renewal, not a takeover. The generation stays put so
                # proposals already in flight from this holder do not become
                # stale because it renewed while they were being applied.
                generation = int(row["generation"])
            elif float(row["valid_until"]) > now:
                conn.execute("ROLLBACK")
                return None
            else:
                # The previous holder's lease expired. Bumping the generation
                # is what makes its in-flight proposals stop being accepted:
                # it may be alive and slow rather than dead, and a coordinator
                # that lost authority must not keep acting on it.
                generation = int(row["generation"]) + 1
            valid_until = now + lease_seconds
            conn.execute(
                "INSERT INTO oversight_authority (project_id, holder_id, generation, valid_until) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET "
                "holder_id = excluded.holder_id, generation = excluded.generation, "
                "valid_until = excluded.valid_until",
                (project_id, holder_id, generation, valid_until),
            )
            conn.execute("COMMIT")
        return Authority(project_id, holder_id, generation, valid_until)

    def release(self, project_id: str, holder_id: str) -> bool:
        """Give up authority early, so a clean shutdown is not a dead wait."""

        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE oversight_authority SET valid_until = 0 "
                "WHERE project_id = ? AND holder_id = ?",
                (project_id, holder_id),
            )
        return cursor.rowcount > 0

    def current(self, project_id: str) -> Authority | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT holder_id, generation, valid_until FROM oversight_authority "
                "WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None or float(row["valid_until"]) <= self.now():
            return None
        return Authority(
            project_id, str(row["holder_id"]), int(row["generation"]), float(row["valid_until"])
        )

    def validate(self, proof: AuthorityProof, _proposal: Any = None) -> bool:
        """The command service's fencing check.

        A proposal is honoured only if its holder still holds the lease at
        the generation it was reasoned under. This is what stops a coordinator
        that was slow, partitioned or replaced from landing decisions it made
        while it was still authoritative.
        """

        held = self.current(proof.project_id)
        return (
            held is not None
            and held.holder_id == proof.holder_id
            and held.generation == proof.generation
        )


# ------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class ItemView:
    """Read-only projection of one work item. No handle, no methods."""

    item_id: str
    state: str
    owner: str | None
    attempts: int
    graph_revision: int = 0
    title: str = ""
    unresolved: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "state": self.state,
            "owner": self.owner,
            "attempts": self.attempts,
            "graph_revision": self.graph_revision,
            "unresolved_dependencies": list(self.unresolved),
        }


class WorkView(Protocol):
    """Everything the coordinator may know about work. Reads only.

    A Protocol rather than the queue itself, and deliberately so: passing the
    queue would give a model's decision loop a `release` and an `add` one
    attribute lookup away from being called.
    """

    def item(self, project_id: str, item_id: str) -> ItemView | None: ...

    def items(self, project_id: str) -> Sequence[ItemView]: ...


@dataclass(frozen=True)
class Decision:
    """What the model came back with, after validation."""

    kind: str
    reason: str
    body: str = ""
    action_type: str = ""
    target: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    risk: Risk = Risk.MEDIUM
    approval_required: bool = True


@dataclass
class CycleReport:
    """What one pass did. Returned rather than logged, so a caller can act."""

    project_id: str
    authoritative: bool
    triggers: int = 0
    answered: int = 0
    proposed: int = 0
    escalated: int = 0
    waited: int = 0
    rejected: int = 0
    #: Triggers that were the same failure again, and cost no model call.
    repeats: int = 0
    errors: list[str] = field(default_factory=list)
    cursor: int = 0

    @property
    def acted(self) -> int:
        return self.answered + self.proposed + self.escalated


SYSTEM_PROMPT = """\
You coordinate one project's agents. You are not in charge of correctness
gates and you cannot override them; proposing something the rules forbid
simply gets rejected and wastes a cycle.

Answer with a single JSON object and nothing else:

  {"kind": "answer",   "reason": "...", "body": "what to tell them"}
  {"kind": "propose",  "reason": "...", "action_type": "...", "target": "...",
   "payload": {...}, "risk": "low|medium|high", "approval_required": true}
  {"kind": "escalate", "reason": "...", "body": "what a human must decide"}
  {"kind": "wait",     "reason": "..."}

Rules you must follow, because a proposal breaking one is rejected:

- Never claim a dependency is complete without evidence in what you were
  given. "It is probably tracked elsewhere" is not evidence.
- Never propose overriding a check, a reviewer verdict or a cost cap.
- Never propose taking, giving or fabricating a claim on an item.
- If you cannot tell whether a reference is a typo, an external dependency
  or work nobody wrote, `escalate` and say exactly what you could not
  determine. Guessing wrongly costs an agent a whole attempt.
- `wait` is a correct answer when nothing you were given is actionable.
"""


class OversightActor:
    """Reads a project's rooms, decides, and proposes. Nothing else."""

    def __init__(
        self,
        project_id: str,
        *,
        ledger: MessageLedger,
        authority: AuthorityStore,
        commands: Any,
        work: WorkView,
        model: Any | None = None,
        role: str = "oversight",
        holder_id: str | None = None,
        now: Callable[[], float] = time.time,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        proposal_ttl_seconds: float = 300.0,
        repeat_threshold: int = 3,
    ) -> None:
        self.project_id = project_id
        self.ledger = ledger
        self.authority = authority
        self.commands = commands
        self.work = work
        self.model = model
        self.role = role
        self.holder_id = holder_id or f"oversight-{uuid.uuid4().hex[:12]}"
        self.now = now
        self.lease_seconds = lease_seconds
        self.proposal_ttl_seconds = proposal_ttl_seconds
        #: How many times one failure must recur before it is worth a fresh
        #: opinion. Measured: shown three identical observations a real model
        #: answered three times, in near-identical words, having no notion it
        #: had seen any of them before. Repetition is the signal §6 is written
        #: for; paying for it three times is not how to notice it.
        self.repeat_threshold = max(1, repeat_threshold)
        self._held: Authority | None = None
        #: signature -> how many times seen. In memory, like the sweep's
        #: cursors: a restarted coordinator re-reads and re-decides, which is
        #: wasteful and safe, and a durable version of both belongs together.
        self._seen: dict[str, int] = {}

    # ------------------------------------------------------------ the cycle

    def run_once(self, room_id: str = GENERAL_ROOM, *, after: int = 0) -> CycleReport:
        """One pass over a room. Safe to call when it holds no authority.

        A non-authoritative pass reads nothing and proposes nothing, and says
        so in the report rather than raising: a standby process polling every
        few seconds is the normal way a second instance waits to take over,
        not an error condition.
        """

        held = self.authority.acquire(
            self.project_id, self.holder_id, lease_seconds=self.lease_seconds
        )
        self._held = held
        if held is None:
            return CycleReport(self.project_id, authoritative=False, cursor=after)

        report = CycleReport(self.project_id, authoritative=True, cursor=after)
        messages = self.ledger.read(self.project_id, room_id, after=after, audience=self.role)
        for message in messages:
            report.cursor = message.sequence
            if message.sender_role == self.role or message.message_type not in TRIGGER_TYPES:
                # Its own traffic is not a trigger. Without this a single
                # observation becomes a conversation the coordinator holds
                # with itself, at model prices.
                continue
            report.triggers += 1
            repeat = self._repeat_count(message)
            if 1 < repeat < self.repeat_threshold:
                # The same failure *again*, and not yet often enough to be a
                # pattern worth a fresh opinion. Counted, not paid for.
                #
                # `1 <` is load-bearing and was wrong first: a first sighting
                # also "has a count", and skipping it made the coordinator
                # blind to every new failure — strictly worse than having no
                # deduplication at all. A test caught it; the ordering of the
                # comparison is the whole feature.
                report.repeats += 1
                continue
            try:
                self._handle(message, room_id, held, report, repeat=repeat)
            except OversightUnavailable as exc:
                # The model is down or the route is wrong. Stop this cycle and
                # leave everything exactly as it was: unresolved work stays
                # blocked, and nothing else in the fleet is touched.
                report.errors.append(str(exc))
                log.warning("oversight for %s stopping this cycle: %s", self.project_id, exc)
                break
            except Exception as exc:  # noqa: BLE001 - one message must not kill the loop
                report.errors.append(f"{message.message_id}: {exc}")
                log.warning("oversight could not handle %s: %s", message.message_id, exc)
        return report

    def release(self) -> None:
        """Give up authority. A clean shutdown should not cost a lease."""

        self.authority.release(self.project_id, self.holder_id)
        self._held = None

    # ----------------------------------------------------------- one message

    def _repeat_count(self, message: Message) -> int:
        """How many times this exact failure has now been seen, or 0.

        Zero means "no signature, or the first sighting" — both of which are
        handled normally. A worker that reports no signature is never
        deduplicated, because an unknown failure must not be mistaken for a
        familiar one.
        """
        signature = str(message.payload.get("signature") or "")
        if not signature:
            return 0
        self._seen[signature] = self._seen.get(signature, 0) + 1
        return self._seen[signature]

    def _handle(
        self,
        message: Message,
        room_id: str,
        held: Authority,
        report: CycleReport,
        *,
        repeat: int = 0,
    ) -> None:
        decision = self._decide(message, repeat=repeat)
        if decision.kind == WAIT:
            report.waited += 1
            return
        if decision.kind == ANSWER:
            self._say(
                room_id,
                message,
                "answer",
                decision.body or decision.reason,
                {"reason": decision.reason},
            )
            report.answered += 1
            return
        if decision.kind == ESCALATE:
            # Not a refusal to work: an escalation IS the work when the
            # ambiguity is one only a person can resolve. It is recorded so
            # the question does not evaporate with this process.
            self._say(
                room_id,
                message,
                "decision",
                decision.body or decision.reason,
                {"escalated": True, "reason": decision.reason},
                recipients=("operator",),
            )
            report.escalated += 1
            return

        result = self._propose(message, room_id, held, decision)
        if result is None:
            report.rejected += 1
        elif str(getattr(result, "status", "")).endswith("accepted"):
            report.proposed += 1
        else:
            report.rejected += 1

    def _propose(
        self, message: Message, room_id: str, held: Authority, decision: Decision
    ) -> CommandResult | None:
        item = self.work.item(self.project_id, message.item_id or decision.target)
        if item is None:
            # Every precondition a proposal is checked against comes from a
            # real item. Without one there is nothing to compare-and-set on,
            # and a proposal that cannot be stale is a proposal that cannot
            # be safely retried.
            self._say(
                room_id,
                message,
                "system_notice",
                f"cannot propose {decision.action_type!r}: {decision.target!r} is not an "
                f"item in this project",
                {"rejected": "unknown_target"},
            )
            return None

        proposal = ActionProposal(
            proposal_id=uuid.uuid4().hex,
            project_id=self.project_id,
            room_id=room_id,
            proposer_id=self.holder_id,
            proposer_role="oversight",
            action_type=decision.action_type,
            target=item.item_id,
            # The state the decision was reasoned against. If any of it has
            # moved by the time the command service looks, the proposal is
            # stale and is rejected rather than applied to a different world.
            expected=ExpectedTargetState(
                graph_revision=item.graph_revision,
                item_state=item.state,
                owner=item.owner,
                attempt=item.attempts,
            ),
            reason=decision.reason,
            evidence_message_ids=(message.message_id,),
            # Derived from the evidence, so replaying the same conclusion
            # about the same message cannot apply the action twice.
            idempotency_key=(
                f"oversight:{self.project_id}:{decision.action_type}:"
                f"{item.item_id}:{message.message_id}"
            ),
            risk=decision.risk,
            approval_required=decision.approval_required,
            expires_at=self.now() + self.proposal_ttl_seconds,
            payload=dict(decision.payload),
        )
        return self.commands.execute(proposal, authority=held.as_proof())  # type: ignore[no-any-return]

    # ------------------------------------------------------------- the model

    def _decide(self, message: Message, *, repeat: int = 0) -> Decision:
        if self.model is None:
            # No route configured. Escalating beats guessing and beats
            # silence: the finding reaches a person, and no gate moved.
            return Decision(
                ESCALATE,
                reason="no oversight model is configured, so this was not assessed",
                body=f"Needs a human: {message.body[:500]}",
            )
        prompt = json.dumps(
            {
                "message": {
                    "type": message.message_type,
                    "body": message.body,
                    "from": message.sender_id,
                    "item_id": message.item_id,
                    "payload": dict(message.payload),
                },
                "work": [i.as_dict() for i in self.work.items(self.project_id)][:50],
                # Stated rather than left to be inferred from the room. "This
                # is the third identical failure" is a different question from
                # "this failed", and the answer to it is the whole reason a
                # coordinator is cheaper than a person.
                **({"identical_occurrences": repeat} if repeat > 1 else {}),
            },
            default=str,
        )
        try:
            response = self.model.call(
                self.role,
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
        except Exception as exc:  # noqa: BLE001 - every model failure is the same here
            raise OversightUnavailable(f"oversight model call failed: {exc}") from exc
        return _parse_decision(_text_of(response))

    # ------------------------------------------------------------- the room

    def _say(
        self,
        room_id: str,
        about: Message,
        message_type: str,
        body: str,
        payload: Mapping[str, Any],
        recipients: Sequence[str] = (),
    ) -> None:
        self.ledger.append(
            Submission(
                project_id=self.project_id,
                room_id=room_id,
                sender_id=self.holder_id,
                sender_role=self.role,
                message_type=message_type,
                body=body,
                # Derived from what it is answering, so a re-run of the same
                # cycle appends nothing new. The ledger has no delete, which
                # makes a duplicate permanent.
                idempotency_key=f"oversight:{message_type}:{about.message_id}",
                recipients=tuple(recipients),
                payload=dict(payload),
                item_id=about.item_id,
                reply_to=about.message_id,
                correlation_id=about.correlation_id or about.message_id,
                causation_id=about.message_id,
            )
        )


def _text_of(response: Any) -> str:
    """Pull assistant text out of a provider response, or give up clearly."""

    body: Any = getattr(response, "body", response)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            # Not JSON at all. Handed back as-is so `_parse_decision` can
            # report what was actually said rather than an empty string.
            return str(body)
    if isinstance(body, Mapping):
        choices = body.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            return str(message.get("content", ""))
    return str(body)


def _parse_decision(text: str) -> Decision:
    """Turn model output into a validated decision, or into a `wait`.

    Unparseable output is not an emergency and must not be one: it means the
    coordinator said nothing this cycle, which is exactly the state the
    system is designed to be safe in. Raising here would let a malformed
    reply take a project down.
    """

    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if "```" in raw[3:] else raw.strip("`")
        raw = raw.removeprefix("json").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return Decision(WAIT, reason=f"oversight reply was not JSON: {text[:200]}")
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return Decision(WAIT, reason=f"oversight reply was not valid JSON: {text[:200]}")
    if not isinstance(data, dict):
        return Decision(WAIT, reason="oversight reply was not an object")

    kind = str(data.get("kind", "")).lower()
    if kind not in DECISIONS:
        return Decision(WAIT, reason=f"oversight proposed unknown decision {kind!r}")
    reason = str(data.get("reason", "")).strip() or "no reason given"
    if kind == PROPOSE and not str(data.get("action_type", "")).strip():
        return Decision(WAIT, reason="oversight proposed an action with no type")

    risk_name = str(data.get("risk", "medium")).upper()
    try:
        risk = Risk[risk_name]
    except KeyError:
        # An unrecognised risk becomes the highest, never the lowest. Getting
        # this backwards would let a typo downgrade a gate.
        risk = Risk.HIGH
    payload = data.get("payload")
    return Decision(
        kind=kind,
        reason=reason,
        body=str(data.get("body", "")),
        action_type=str(data.get("action_type", "")),
        target=str(data.get("target", "")),
        payload=payload if isinstance(payload, dict) else {},
        risk=risk,
        # Absent means required. A model that forgets the field must not
        # thereby grant itself unattended authority.
        approval_required=bool(data.get("approval_required", True)),
    )
