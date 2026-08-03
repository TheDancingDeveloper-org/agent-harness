"""The oversight actor.

Most of these are about what it cannot do. A coordinator that helps when it
is right is worth little if it can also break things when it is wrong, and
"wrong" is the normal operating condition for a model.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_harness.command_service import (
    ActionRule,
    CommandPolicy,
    CommandService,
    CommandStatus,
    MutationOutcome,
    Risk,
    SQLiteCommandJournal,
)
from agent_harness.coordination import GENERAL_ROOM, MessageLedger, Submission
from agent_harness.oversight import (
    AuthorityStore,
    ItemView,
    OversightActor,
    _parse_decision,
)


class FakeModel:
    """Returns scripted replies, and records what it was asked."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def call(self, role: str, messages: list[dict[str, Any]], **_: Any) -> Any:
        self.prompts.append(messages[-1]["content"])
        reply = self.replies.pop(0) if self.replies else '{"kind": "wait", "reason": "nothing"}'
        if isinstance(reply, Exception):  # pragma: no cover - raised below
            raise reply
        return type("R", (), {"body": {"choices": [{"message": {"content": reply}}]}})()


class ExplodingModel:
    def call(self, *_: Any, **__: Any) -> Any:
        raise RuntimeError("the route is misconfigured")


class FakeWork:
    """A read-only view. Deliberately has no mutating method at all."""

    def __init__(self, items: dict[str, ItemView] | None = None) -> None:
        self._items = items or {}

    def item(self, _project_id: str, item_id: str) -> ItemView | None:
        return self._items.get(item_id)

    def items(self, _project_id: str) -> list[ItemView]:
        return list(self._items.values())


class RecordingMutations:
    def __init__(self) -> None:
        self.applied: list[Any] = []

    def apply(self, proposal: Any, _authority: Any) -> MutationOutcome:
        self.applied.append(proposal)
        return MutationOutcome.accepted("applied", "done")


@pytest.fixture
def ledger(tmp_path: Path) -> MessageLedger:
    return MessageLedger(tmp_path / "coordination.sqlite")


@pytest.fixture
def authority(tmp_path: Path) -> AuthorityStore:
    return AuthorityStore(tmp_path / "authority.sqlite")


@pytest.fixture
def mutations() -> RecordingMutations:
    return RecordingMutations()


@pytest.fixture
def commands(
    tmp_path: Path, ledger: MessageLedger, authority: AuthorityStore, mutations: RecordingMutations
) -> CommandService:
    class Sink:
        def append(self, _message: Any) -> None:
            return None

    return CommandService(
        journal=SQLiteCommandJournal(tmp_path / "commands.sqlite"),
        mutations=mutations,
        messages=Sink(),
        policy=CommandPolicy(
            {
                "correct_dependency": ActionRule(
                    risk=Risk.MEDIUM, allowed_roles=frozenset({"oversight"})
                )
            }
        ),
        authority_validator=authority,
    )


def actor(
    ledger: MessageLedger,
    authority: AuthorityStore,
    commands: CommandService,
    *,
    model: Any = None,
    work: FakeWork | None = None,
    holder_id: str = "coordinator-1",
) -> OversightActor:
    return OversightActor(
        "alpha",
        ledger=ledger,
        authority=authority,
        commands=commands,
        work=work or FakeWork({"T7": ItemView("T7", "pending", None, 0, graph_revision=3)}),
        model=model,
        holder_id=holder_id,
    )


def report_dependency(ledger: MessageLedger, key: str = "k1", item_id: str = "T7") -> Any:
    return ledger.append(
        Submission(
            project_id="alpha",
            room_id=GENERAL_ROOM,
            sender_id="worker-1",
            message_type="dependency_found",
            body="T7 needs the schema task, which is not in the queue",
            idempotency_key=key,
            item_id=item_id,
        )
    )


# ---------------------------------------------------------------- authority


def test_only_one_process_can_hold_authority(authority: AuthorityStore) -> None:
    """A restart, a duplicate deploy or a partition all produce a second
    process that believes it is the coordinator. Only one may be."""
    first = authority.acquire("alpha", "coordinator-1")
    assert first is not None
    assert authority.acquire("alpha", "coordinator-2") is None
    # Renewal by the holder is not a takeover, and does not bump the fence.
    renewed = authority.acquire("alpha", "coordinator-1")
    assert renewed is not None and renewed.generation == first.generation


def test_a_concurrent_start_produces_exactly_one_holder(tmp_path: Path) -> None:
    """The case the transaction is for: with the read outside it, both
    processes see the lease free and both take it."""
    store = AuthorityStore(tmp_path / "authority.sqlite")
    contenders = 8
    start = threading.Barrier(contenders)
    won: list[Any] = []
    lock = threading.Lock()

    def contend(index: int) -> None:
        start.wait()
        held = store.acquire("alpha", f"coordinator-{index}")
        if held is not None:
            with lock:
                won.append(held)

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(contenders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(won) == 1, f"{len(won)} processes each believed they were the coordinator"


def test_an_expired_lease_is_taken_over_with_a_new_generation(tmp_path: Path) -> None:
    clock = [1000.0]
    store = AuthorityStore(tmp_path / "authority.sqlite", now=lambda: clock[0])
    first = store.acquire("alpha", "coordinator-1", lease_seconds=100)
    assert first is not None

    clock[0] += 101
    second = store.acquire("alpha", "coordinator-2", lease_seconds=100)
    assert second is not None
    # The fence moves, so the old holder -- which may be slow rather than
    # dead -- cannot land decisions it made while it was authoritative.
    assert second.generation == first.generation + 1
    assert store.validate(first.as_proof()) is False
    assert store.validate(second.as_proof()) is True


def test_authority_is_per_project(authority: AuthorityStore) -> None:
    """A coordinator losing its route must not stall an unrelated project."""
    assert authority.acquire("alpha", "coordinator-1") is not None
    assert authority.acquire("beta", "coordinator-2") is not None
    assert authority.acquire("alpha", "coordinator-2") is None


def test_releasing_lets_the_next_process_take_over(authority: AuthorityStore) -> None:
    authority.acquire("alpha", "coordinator-1")
    assert authority.release("alpha", "coordinator-1") is True
    assert authority.acquire("alpha", "coordinator-2") is not None


def test_a_non_authoritative_pass_reads_nothing_and_says_so(
    ledger: MessageLedger, authority: AuthorityStore, commands: CommandService
) -> None:
    """A standby polling to take over is normal operation, not an error."""
    authority.acquire("alpha", "somebody-else")
    report_dependency(ledger)
    model = FakeModel()

    result = actor(ledger, authority, commands, model=model).run_once()
    assert result.authoritative is False
    assert result.triggers == 0
    assert model.prompts == [], "a standby must not spend money on model calls"


# ------------------------------------------------------------------ acting


def test_a_reported_dependency_gets_an_answer_in_the_room(
    ledger: MessageLedger, authority: AuthorityStore, commands: CommandService
) -> None:
    reported = report_dependency(ledger)
    model = FakeModel(
        json.dumps({"kind": "answer", "reason": "it is tracked in T4", "body": "See T4."})
    )
    result = actor(ledger, authority, commands, model=model).run_once()

    assert result.answered == 1
    replies = [m for m in ledger.read("alpha", GENERAL_ROOM) if m.sender_role == "oversight"]
    assert [m.body for m in replies] == ["See T4."]
    assert replies[0].reply_to == reported.message_id


def test_an_accepted_proposal_carries_the_state_it_was_reasoned_against(
    ledger: MessageLedger,
    authority: AuthorityStore,
    commands: CommandService,
    mutations: RecordingMutations,
) -> None:
    """A proposal that cannot be stale is a proposal that cannot be safely
    retried, so every mutable coordinate travels with it."""
    report_dependency(ledger)
    model = FakeModel(
        json.dumps(
            {
                "kind": "propose",
                "reason": "the plan says SCHEMA-1, the queue says T4",
                "action_type": "correct_dependency",
                "target": "T7",
                "approval_required": False,
                "risk": "medium",
            }
        )
    )
    result = actor(ledger, authority, commands, model=model).run_once()

    assert result.proposed == 1, result.errors
    assert len(mutations.applied) == 1
    proposal = mutations.applied[0]
    assert proposal.expected.graph_revision == 3
    assert proposal.expected.item_state == "pending"
    assert proposal.proposer_role == "oversight"
    assert proposal.evidence_message_ids, "a proposal must name what it is based on"


def test_the_same_finding_twice_proposes_once(
    ledger: MessageLedger,
    authority: AuthorityStore,
    commands: CommandService,
    mutations: RecordingMutations,
) -> None:
    """Cycles overlap and processes restart. Replay must not act twice."""
    report_dependency(ledger)
    decision = json.dumps(
        {
            "kind": "propose",
            "reason": "same conclusion",
            "action_type": "correct_dependency",
            "target": "T7",
            "approval_required": False,
        }
    )
    model = FakeModel(decision, decision)
    subject = actor(ledger, authority, commands, model=model)
    subject.run_once()
    subject.run_once()  # deliberately from the start, as a crashed cycle would

    assert len(mutations.applied) == 1, "the same evidence applied the action twice"


def test_an_escalation_is_recorded_rather_than_evaporating(
    ledger: MessageLedger, authority: AuthorityStore, commands: CommandService
) -> None:
    report_dependency(ledger)
    model = FakeModel(
        json.dumps(
            {
                "kind": "escalate",
                "reason": "cannot tell a typo from an external reference",
                "body": "Is SCHEMA-1 a typo for T4, or work nobody wrote?",
            }
        )
    )
    result = actor(ledger, authority, commands, model=model).run_once()

    assert result.escalated == 1
    escalations = [m for m in ledger.read("alpha", GENERAL_ROOM) if m.message_type == "decision"]
    assert escalations and "operator" in escalations[0].recipients
    # It survives this process: the question is in the permanent record.
    assert "SCHEMA-1" in escalations[0].body


def test_it_does_not_answer_its_own_messages(
    ledger: MessageLedger, authority: AuthorityStore, commands: CommandService
) -> None:
    """Otherwise one observation becomes a conversation it holds with
    itself, at model prices."""
    report_dependency(ledger)
    answer = json.dumps({"kind": "answer", "reason": "r", "body": "an answer"})
    model = FakeModel(answer, answer)
    subject = actor(ledger, authority, commands, model=model)

    first = subject.run_once()
    assert first.triggers == 1
    # Its own reply is now the newest message in the room. Resuming from the
    # cursor, there is nothing to react to.
    assert subject.run_once(after=first.cursor).triggers == 0

    # And even re-reading the whole room, the reply is not a trigger: only
    # the worker's original message is, so exactly one more model call.
    assert subject.run_once().triggers == 1
    assert len(model.prompts) == 2


def test_a_proposal_about_an_unknown_item_is_refused_not_invented(
    ledger: MessageLedger,
    authority: AuthorityStore,
    commands: CommandService,
    mutations: RecordingMutations,
) -> None:
    report_dependency(ledger, item_id="NOT-AN-ITEM")
    model = FakeModel(
        json.dumps(
            {
                "kind": "propose",
                "reason": "r",
                "action_type": "correct_dependency",
                "target": "NOT-AN-ITEM",
            }
        )
    )
    result = actor(ledger, authority, commands, model=model).run_once()

    assert result.rejected == 1
    assert mutations.applied == []
    notices = [m for m in ledger.read("alpha", GENERAL_ROOM) if m.message_type == "system_notice"]
    assert notices and "not an" in notices[0].body


# ------------------------------------------------------------- failing safe


def test_an_unavailable_model_stops_the_cycle_and_changes_nothing(
    ledger: MessageLedger,
    authority: AuthorityStore,
    commands: CommandService,
    mutations: RecordingMutations,
) -> None:
    """Oversight failure must not weaken a gate or unblock anything."""
    report_dependency(ledger)
    result = actor(ledger, authority, commands, model=ExplodingModel()).run_once()

    assert result.errors and "misconfigured" in result.errors[0]
    assert result.acted == 0
    assert mutations.applied == []


def test_no_model_configured_escalates_rather_than_guessing(
    ledger: MessageLedger, authority: AuthorityStore, commands: CommandService
) -> None:
    report_dependency(ledger)
    result = actor(ledger, authority, commands, model=None).run_once()
    assert result.escalated == 1
    assert result.proposed == 0


def test_a_stale_generation_cannot_land_a_proposal(
    tmp_path: Path,
    ledger: MessageLedger,
    commands: CommandService,
    mutations: RecordingMutations,
) -> None:
    """The fencing property. A coordinator that was replaced while thinking
    must not apply the decision it reached when it was authoritative."""
    clock = [1000.0]
    store = AuthorityStore(tmp_path / "authority.sqlite", now=lambda: clock[0])
    commands.authority_validator = store
    report_dependency(ledger)

    deposed = OversightActor(
        "alpha",
        ledger=ledger,
        authority=store,
        commands=commands,
        work=FakeWork({"T7": ItemView("T7", "pending", None, 0)}),
        model=FakeModel(
            json.dumps(
                {
                    "kind": "propose",
                    "reason": "r",
                    "action_type": "correct_dependency",
                    "target": "T7",
                    "approval_required": False,
                }
            )
        ),
        holder_id="coordinator-1",
        now=lambda: clock[0],
        lease_seconds=100,
    )
    held = store.acquire("alpha", "coordinator-1", lease_seconds=100)
    assert held is not None

    # It is replaced while it is thinking, and only then submits.
    clock[0] += 101
    store.acquire("alpha", "coordinator-2", lease_seconds=100)
    clock[0] -= 101  # its own lease read still succeeds; the fence is what stops it

    deposed.run_once()
    assert mutations.applied == [], "a deposed coordinator landed a decision"


def test_the_work_view_has_no_way_to_mutate_anything() -> None:
    """Enforced against the type, not by intention: passing the queue would
    put `release` and `add` one attribute lookup from a model's decision."""
    from agent_harness.oversight import WorkView

    allowed = {name for name in dir(WorkView) if not name.startswith("_")}
    assert allowed == {"item", "items"}, f"the read-only view grew a method: {allowed}"


def test_it_holds_no_queue_ledger_write_or_github_handle() -> None:
    """The model's worst possible output must be a bad suggestion."""
    import inspect

    from agent_harness import oversight

    source = inspect.getsource(oversight)
    for forbidden in ("import github", "from .github", "from .work import", "WorkQueue"):
        assert forbidden not in source, f"oversight reaches {forbidden}"


# ------------------------------------------------------ reading the model


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        "{",
        "[]",
        '{"kind": "delete_everything", "reason": "r"}',
        '{"kind": "propose", "reason": "r"}',  # no action type
    ],
)
def test_unusable_model_output_becomes_a_wait(reply: str) -> None:
    """A malformed reply means the coordinator said nothing this cycle,
    which is the state the whole system is designed to be safe in. Raising
    would let one bad reply take a project down."""
    assert _parse_decision(reply).kind == "wait"


def test_a_fenced_json_reply_is_read() -> None:
    decision = _parse_decision('```json\n{"kind": "answer", "reason": "r", "body": "b"}\n```')
    assert decision.kind == "answer"
    assert decision.body == "b"


def test_an_unrecognised_risk_becomes_the_highest_not_the_lowest() -> None:
    """Getting this backwards would let a typo downgrade a gate."""
    decision = _parse_decision(
        '{"kind": "propose", "reason": "r", "action_type": "x", "risk": "trivial"}'
    )
    assert decision.risk is Risk.HIGH


def test_a_forgotten_approval_field_means_approval_is_required() -> None:
    """A model that omits the field must not thereby grant itself
    unattended authority."""
    decision = _parse_decision('{"kind": "propose", "reason": "r", "action_type": "x"}')
    assert decision.approval_required is True


def test_the_command_service_still_refuses_an_unknown_action(
    ledger: MessageLedger,
    authority: AuthorityStore,
    commands: CommandService,
    mutations: RecordingMutations,
) -> None:
    """The policy allow-list is the gate, not the prompt. A model asking for
    something nobody configured is rejected, not implemented."""
    report_dependency(ledger)
    model = FakeModel(
        json.dumps(
            {
                "kind": "propose",
                "reason": "r",
                "action_type": "override_the_reviewer",
                "target": "T7",
                "approval_required": False,
            }
        )
    )
    result = actor(ledger, authority, commands, model=model).run_once()

    assert result.rejected == 1
    assert mutations.applied == []


def test_an_action_needing_approval_is_not_applied_without_one(
    ledger: MessageLedger,
    authority: AuthorityStore,
    commands: CommandService,
    mutations: RecordingMutations,
) -> None:
    report_dependency(ledger)
    model = FakeModel(
        json.dumps(
            {
                "kind": "propose",
                "reason": "r",
                "action_type": "correct_dependency",
                "target": "T7",
                "approval_required": True,
            }
        )
    )
    result = actor(ledger, authority, commands, model=model).run_once()

    assert result.rejected == 1
    assert mutations.applied == [], "an unapproved action was applied"


def test_a_cycle_reports_where_it_got_to(
    ledger: MessageLedger, authority: AuthorityStore, commands: CommandService
) -> None:
    """The cursor is how a caller resumes without re-reading the room."""
    report_dependency(ledger, key="a")
    report_dependency(ledger, key="b")
    model = FakeModel(
        json.dumps({"kind": "wait", "reason": "r"}), json.dumps({"kind": "wait", "reason": "r"})
    )
    result = actor(ledger, authority, commands, model=model).run_once()
    assert result.cursor == 2
    assert result.waited == 2

    assert CommandStatus.ACCEPTED  # the enum is part of the contract these rely on
