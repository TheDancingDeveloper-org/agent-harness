"""Connecting a coordinator to a real queue, without handing it the keys.

`oversight.py` deliberately knows nothing about `WorkQueue`. It reads a
read-only view and emits typed proposals, and something else decides whether a
proposal becomes a state change. This module is that something else: the
narrowest possible adapter between the two, in one file, so the blast radius of
"the coordinator can now touch production state" is a single reviewable page.

Three things it does not do, each for a stated reason:

**It does not widen the view.** `QueueWorkView` exposes `item` and `items` and
nothing else. The coordinator cannot claim, release, requeue or write a
setting, because the object it holds has no such method — not because it is
asked politely not to.

**It does not invent actions.** Exactly two are implemented, `block_work` and
`retry_work`, both of which a human can already perform through the API and
both of which are reversible. An action type the policy does not name is
refused by the command service before this module is reached.

**It does not re-decide.** Every precondition is checked here again, against
the queue, at the moment of application — the proposal's own account of the
world is treated as a claim to verify, not as context to trust.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .command_service import ActionProposal, MutationOutcome
from .outcomes import BLOCKED
from .oversight import ItemView

log = logging.getLogger(__name__)

#: What a coordinator may ask for. Both are reversible by a human through the
#: existing API, which is the bar for anything on this list: an action nobody
#: can undo is not one to hand to a model.
BLOCK_WORK = "block_work"
RETRY_WORK = "retry_work"
#: Answer a question a worker asked and is held on. Reversible in the sense
#: that matters: it returns the item to its own worker, with its worktree and
#: context intact, and decides nothing about the work itself.
ANSWER_QUESTION = "answer_question"
SUPPORTED_ACTIONS = frozenset({BLOCK_WORK, RETRY_WORK, ANSWER_QUESTION})


class QueueWorkView:
    """The queue, readable and nothing else.

    Deliberately not a subclass of, or a wrapper delegating to, `WorkQueue`:
    an adapter that forwards unknown attributes would silently regain every
    mutating method the day someone adds one.
    """

    def __init__(self, queue: Any) -> None:
        self._queue = queue

    def item(self, project_id: str, item_id: str) -> ItemView | None:
        record = self._queue.get(item_id, project_id=project_id)
        return None if record is None else _view(record)

    def items(self, project_id: str) -> list[ItemView]:
        return [_view(r) for r in self._queue.items(project_id=project_id)]


def _view(record: Any) -> ItemView:
    return ItemView(
        item_id=record.item_id,
        state=record.state,
        owner=record.owner or "",
        attempts=record.attempts,
        graph_revision=getattr(record, "admitted_revision", 0),
        title=getattr(record, "title", ""),
    )


class QueueMutations:
    """Applies an accepted proposal to the queue, re-checking every claim.

    The command service has already validated the policy, the role, the risk
    and the authority. What it cannot know is whether the world still looks the
    way the coordinator believed it did when it reasoned — so that is checked
    here, against the queue, immediately before the change.
    """

    def __init__(self, queue: Any, *, now: Callable[[], float] | None = None) -> None:
        self._queue = queue
        self._now = now

    def _answer(self, proposal: ActionProposal, record: Any) -> MutationOutcome:
        """Answer the question this item is held on.

        **The resume token never leaves this module.** It is looked up here,
        from the open hold, rather than travelling to the coordinator and back
        — a token in a room is a token anything that can read the room may
        spend, and it exists precisely so that a reply arriving after a
        timeout cannot land on whatever the item is doing an hour later.

        Nothing interprets the answer. It is recorded verbatim and the item
        returns to its own worker: a model reading human feedback to decide
        what it meant would be a gate decided by a model, which `AGENTS.md`
        rejects.
        """
        from .holds import Answer

        text = _answer_of(proposal)
        if not text:
            return MutationOutcome.rejected(
                "empty_answer", "an answer with no content resolves nothing"
            )
        hold = self._queue.holds.current(proposal.project_id, record.item_id)
        if hold is None:
            return MutationOutcome.rejected(
                "no_question", f"{record.item_id} is not waiting on an answer"
            )
        self._queue.answer_hold(
            record.item_id,
            hold.resume_token,
            Answer(text=text, who=proposal.proposer_id),
            project_id=proposal.project_id,
        )
        log.info("oversight answered %s/%s", proposal.project_id, record.item_id)
        return MutationOutcome.accepted("answered", f"{record.item_id} may continue")

    def apply(self, proposal: ActionProposal, _authority: Any) -> MutationOutcome:
        if proposal.action_type not in SUPPORTED_ACTIONS:
            # Reachable only if a deployment's policy names an action this
            # bridge cannot perform. Refusing beats a partial application.
            return MutationOutcome.rejected(
                "unsupported_action",
                f"{proposal.action_type!r} is allowed by policy but not implemented here",
            )
        record = self._queue.get(proposal.target, project_id=proposal.project_id)
        if record is None:
            return MutationOutcome.rejected(
                "unknown_target", f"no item {proposal.target!r} in {proposal.project_id!r}"
            )

        stale = _stale(proposal, record)
        if stale:
            # The item moved between the coordinator reasoning and the queue
            # being asked. Applying anyway would land a decision about a
            # different world, which is the failure an overwatch layer is
            # most likely to cause and least likely to notice.
            return MutationOutcome.rejected("stale_proposal", stale)

        if proposal.action_type == ANSWER_QUESTION:
            return self._answer(proposal, record)

        if proposal.action_type == BLOCK_WORK:
            reason = proposal.reason or "blocked by oversight"
            self._queue.release(
                record.item_id, BLOCKED, error=reason, project_id=proposal.project_id
            )
            log.info("oversight blocked %s/%s: %s", proposal.project_id, record.item_id, reason)
            return MutationOutcome.accepted("blocked", f"{record.item_id} is blocked")

        self._queue.requeue(record.item_id, project_id=proposal.project_id)
        log.info("oversight requeued %s/%s", proposal.project_id, record.item_id)
        return MutationOutcome.accepted("requeued", f"{record.item_id} is pending again")


def _answer_of(proposal: ActionProposal) -> str:
    payload = dict(proposal.payload)
    return str(payload.get("answer") or payload.get("body") or "").strip()


def _stale(proposal: ActionProposal, record: Any) -> str:
    """Why this proposal no longer describes the item, if it does not."""
    expected = proposal.expected
    checks = (
        ("state", expected.item_state, record.state),
        ("attempt", expected.attempt, record.attempts),
        ("owner", expected.owner or "", record.owner or ""),
        ("graph revision", expected.graph_revision, getattr(record, "admitted_revision", 0)),
    )
    for name, was, now in checks:
        # An expectation of zero or empty means the proposer did not pin that
        # field, which is different from pinning it to a falsy value. Only a
        # stated expectation can be contradicted.
        if was and was != now:
            return f"{name} was {was!r} when this was reasoned and is {now!r} now"
    return ""


class LedgerSink:
    """Publishes a command decision into the ledger the agents read.

    The command service speaks `OutboundMessage`, which is deliberately
    ledger-neutral — it has no idea what a room is or who may see one. The
    ledger speaks `Submission`. Translating between them is exactly the kind
    of join that belongs in a bridge and nowhere else.

    A failed publish is raised, not swallowed: the service treats undelivered
    results as a failure on purpose, because a decision nobody can read is
    indistinguishable from a decision nobody made.
    """

    def __init__(self, ledger: Any) -> None:
        self._ledger = ledger

    def append(self, message: Any) -> None:
        from .coordination import Submission

        self._ledger.append(
            Submission(
                project_id=message.project_id,
                room_id=message.room_id,
                sender_id=message.sender_id,
                sender_role="system",
                message_type=message.message_type,
                body=str(dict(message.payload).get("detail", message.message_type)),
                idempotency_key=message.idempotency_key,
                correlation_id=message.correlation_id or None,
                causation_id=message.causation_id or None,
                payload=dict(message.payload),
            )
        )


def build_coordinator(
    queue: Any,
    project_id: str,
    *,
    db_path: str,
    model: Any | None,
    ledger: Any,
    now: Callable[[], float] | None = None,
) -> Any:
    """Assemble a coordinator over a real queue, with the default policy.

    The policy here is the conservative one: both actions are LOW risk and
    both may be proposed by oversight without a human, because both are
    reversible through the same API a person already uses. Anything else a
    deployment wants to allow is a deliberate act of configuration, not a
    default.
    """
    from .command_service import (
        ActionRule,
        CommandPolicy,
        CommandService,
        Risk,
        SQLiteCommandJournal,
    )
    from .coordination import MessageLedger  # noqa: F401  (documents the expected type)
    from .oversight import AuthorityStore, OversightActor

    authority = AuthorityStore(f"{db_path}.oversight")
    policy = CommandPolicy(
        {
            BLOCK_WORK: ActionRule(risk=Risk.LOW, allowed_roles=frozenset({"human", "oversight"})),
            RETRY_WORK: ActionRule(risk=Risk.LOW, allowed_roles=frozenset({"human", "oversight"})),
            ANSWER_QUESTION: ActionRule(
                risk=Risk.LOW, allowed_roles=frozenset({"human", "oversight"})
            ),
        }
    )
    commands = CommandService(
        journal=SQLiteCommandJournal(f"{db_path}.commands"),
        policy=policy,
        mutations=QueueMutations(queue, now=now),
        messages=LedgerSink(ledger),
        authority_validator=authority,
    )
    return OversightActor(
        project_id,
        ledger=ledger,
        authority=authority,
        commands=commands,
        work=QueueWorkView(queue),
        model=model,
    )


class RoomSweep:
    """Run a coordinator over every room in a project, keeping a cursor each.

    One room per item means a coordinator can no longer poll a single place.
    It also means it no longer has to read one item's traffic to reach
    another's, which is the reason the rooms exist.

    Cursors are held per room and in memory. That is deliberate for now: a
    restarted sweep re-reads a room from the beginning, and the ledger's
    idempotency makes acting on the same evidence twice the same command
    rather than a second one. A durable cursor is worth having and is not
    worth having *first*.
    """

    def __init__(self, actor: Any, ledger: Any, *, general: bool = True) -> None:
        self._actor = actor
        self._ledger = ledger
        self._general = general
        self._cursors: dict[str, int] = {}

    def run_once(self) -> list[Any]:
        """One pass over every room. Returns each room's report."""
        from .coordination import GENERAL_ROOM

        rooms = list(self._ledger.rooms(self._actor.project_id))
        if self._general and GENERAL_ROOM not in rooms:
            rooms.append(GENERAL_ROOM)
        reports = []
        for room in sorted(rooms):
            report = self._actor.run_once(room, after=self._cursors.get(room, 0))
            if not report.authoritative:
                # Another process holds the lease. Stop the sweep rather than
                # walking every remaining room to be told the same thing.
                reports.append(report)
                break
            self._cursors[room] = report.cursor
            reports.append(report)
        return reports
