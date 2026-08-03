from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from agent_harness.command_service import (
    ActionProposal,
    ActionRule,
    Approval,
    AuthorityProof,
    CommandPolicy,
    CommandResultDeliveryError,
    CommandService,
    CommandStatus,
    ExpectedTargetState,
    MutationOutcome,
    OutboundMessage,
    Risk,
    SQLiteCommandJournal,
)


class MemorySink:
    def __init__(self) -> None:
        self.messages: dict[str, OutboundMessage] = {}

    def append(self, message: OutboundMessage) -> None:
        self.messages.setdefault(message.idempotency_key, message)


class FailingSink(MemorySink):
    def __init__(self) -> None:
        super().__init__()
        self.available = False

    def append(self, message: OutboundMessage) -> None:
        if not self.available:
            raise OSError("coordination ledger unavailable")
        super().append(message)


class MemoryMutationPort:
    """A target implementation with the idempotency/CAS contract the service requires."""

    def __init__(self) -> None:
        self.states: dict[tuple[str, str], dict[str, object]] = {}
        self.outcomes: dict[tuple[str, str], MutationOutcome] = {}
        self.apply_calls = 0
        self.effects = 0
        self.authorities: list[AuthorityProof | None] = []

    def put(
        self,
        project_id: str,
        target: str,
        *,
        revision: int = 3,
        state: str = "claimed",
        owner: str | None = "worker-1",
        attempt: int = 2,
    ) -> None:
        self.states[(project_id, target)] = {
            "revision": revision,
            "state": state,
            "owner": owner,
            "attempt": attempt,
        }

    def apply(self, proposal: ActionProposal, authority: AuthorityProof | None) -> MutationOutcome:
        self.apply_calls += 1
        self.authorities.append(authority)
        key = (proposal.project_id, proposal.idempotency_key)
        if key in self.outcomes:
            return self.outcomes[key]

        state = self.states[(proposal.project_id, proposal.target)]
        expected = proposal.expected
        actual = (
            state["revision"],
            state["state"],
            state["owner"],
            state["attempt"],
        )
        wanted = (
            expected.graph_revision,
            expected.item_state,
            expected.owner,
            expected.attempt,
        )
        if actual != wanted:
            outcome = MutationOutcome.rejected(
                "precondition_failed", f"expected {wanted!r}, found {actual!r}"
            )
        elif proposal.payload.get("weaken_gate") is True:
            outcome = MutationOutcome.rejected("gate_policy_denied", "gates cannot be bypassed")
        else:
            self.effects += 1
            state["state"] = str(proposal.payload.get("new_state", "blocked"))
            outcome = MutationOutcome.accepted("state_changed", "target updated")
        self.outcomes[key] = outcome
        return outcome


class AuthorityValidator:
    def __init__(self, valid: bool = True) -> None:
        self.valid = valid

    def validate(self, proof: AuthorityProof, proposal: ActionProposal) -> bool:
        return (
            self.valid
            and proof.project_id == proposal.project_id
            and proof.holder_id == proposal.proposer_id
        )


def expected() -> ExpectedTargetState:
    return ExpectedTargetState(graph_revision=3, item_state="claimed", owner="worker-1", attempt=2)


def proposal(**changes: object) -> ActionProposal:
    values: dict[str, object] = {
        "proposal_id": "proposal-1",
        "project_id": "project-a",
        "room_id": "work:T1",
        "proposer_id": "human-1",
        "proposer_role": "human",
        "action_type": "block_work",
        "target": "T1",
        "expected": expected(),
        "reason": "dependency is unresolved",
        "evidence_message_ids": ("message-1",),
        "idempotency_key": "command-1",
        "risk": Risk.LOW,
        "approval_required": False,
        "expires_at": 2000.0,
        "payload": {"new_state": "blocked"},
    }
    values.update(changes)
    return ActionProposal(**values)  # type: ignore[arg-type]


def policy() -> CommandPolicy:
    return CommandPolicy(
        {
            "block_work": ActionRule(
                risk=Risk.LOW, allowed_roles=frozenset({"human", "oversight"})
            ),
            "release_session": ActionRule(
                risk=Risk.HIGH,
                approval_required=True,
                allowed_roles=frozenset({"human", "oversight"}),
            ),
        }
    )


def service(
    path: Path,
    port: MemoryMutationPort,
    sink: MemorySink,
    *,
    now: float = 1000.0,
    authority: AuthorityValidator | None = None,
) -> CommandService:
    return CommandService(
        journal=SQLiteCommandJournal(path),
        mutations=port,
        messages=sink,
        policy=policy(),
        authority_validator=authority,
        now=lambda: now,
    )


def test_command_applies_with_all_expected_state_preconditions(tmp_path: Path) -> None:
    port = MemoryMutationPort()
    port.put("project-a", "T1")
    sink = MemorySink()

    result = service(tmp_path / "commands.sqlite", port, sink).execute(proposal())

    assert result.status is CommandStatus.ACCEPTED
    assert result.code == "state_changed"
    assert port.effects == 1
    message = next(iter(sink.messages.values()))
    assert message.message_type == "command_accepted"
    assert message.project_id == "project-a"
    assert message.room_id == "work:T1"


def test_stale_expected_state_is_rejected_without_an_effect(tmp_path: Path) -> None:
    port = MemoryMutationPort()
    port.put("project-a", "T1", attempt=3)

    result = service(tmp_path / "commands.sqlite", port, MemorySink()).execute(proposal())

    assert result.status is CommandStatus.REJECTED
    assert result.code == "precondition_failed"
    assert port.effects == 0


def test_command_replay_survives_a_new_service_and_applies_once(tmp_path: Path) -> None:
    path = tmp_path / "commands.sqlite"
    port = MemoryMutationPort()
    port.put("project-a", "T1")
    sink = MemorySink()

    first = service(path, port, sink).execute(proposal())
    replay = service(path, port, sink).execute(proposal())

    assert first.status is CommandStatus.ACCEPTED
    assert replay.status is CommandStatus.ACCEPTED
    assert replay.replayed is True
    assert port.apply_calls == 1
    assert port.effects == 1
    assert len(sink.messages) == 1


def test_result_delivery_failure_is_recoverable_without_reapplying(tmp_path: Path) -> None:
    path = tmp_path / "commands.sqlite"
    port = MemoryMutationPort()
    port.put("project-a", "T1")
    sink = FailingSink()

    with pytest.raises(CommandResultDeliveryError) as caught:
        service(path, port, sink).execute(proposal())
    assert caught.value.result.status is CommandStatus.ACCEPTED
    assert port.effects == 1

    sink.available = True
    replay = service(path, port, sink).execute(proposal())
    assert replay.replayed is True
    assert port.apply_calls == 1
    assert len(sink.messages) == 1


def test_reusing_an_idempotency_key_for_different_content_is_rejected(tmp_path: Path) -> None:
    port = MemoryMutationPort()
    port.put("project-a", "T1")
    sink = MemorySink()
    commands = service(tmp_path / "commands.sqlite", port, sink)
    commands.execute(proposal())

    conflict = commands.execute(proposal(reason="a different operation"))

    assert conflict.status is CommandStatus.REJECTED
    assert conflict.code == "idempotency_conflict"
    assert port.effects == 1


def test_idempotency_is_scoped_by_project(tmp_path: Path) -> None:
    port = MemoryMutationPort()
    port.put("project-a", "T1")
    port.put("project-b", "T1")
    commands = service(tmp_path / "commands.sqlite", port, MemorySink())

    first = commands.execute(proposal())
    second = commands.execute(
        proposal(project_id="project-b", room_id="work:T1", proposal_id="proposal-b")
    )

    assert first.status is CommandStatus.ACCEPTED
    assert second.status is CommandStatus.ACCEPTED
    assert port.effects == 2


@pytest.mark.parametrize(
    ("candidate", "code"),
    [
        (proposal(expires_at=999.0), "expired"),
        (proposal(action_type="override_review"), "unknown_action"),
        (proposal(action_type="release_session", risk=Risk.LOW), "risk_understated"),
    ],
)
def test_expiry_and_policy_are_deterministic_rejections(
    tmp_path: Path, candidate: ActionProposal, code: str
) -> None:
    port = MemoryMutationPort()
    port.put("project-a", "T1")

    result = service(tmp_path / f"{code}.sqlite", port, MemorySink()).execute(candidate)

    assert result.status is CommandStatus.REJECTED
    assert result.code == code
    assert port.apply_calls == 0


def test_high_risk_action_requires_an_exact_unexpired_approval(tmp_path: Path) -> None:
    port = MemoryMutationPort()
    port.put("project-a", "T1")
    candidate = proposal(action_type="release_session", risk=Risk.HIGH, approval_required=True)
    commands = service(tmp_path / "commands.sqlite", port, MemorySink())

    no_approval = commands.execute(candidate)

    assert no_approval.code == "approval_required"
    assert port.apply_calls == 0


def test_valid_approval_is_bound_to_the_exact_proposal(tmp_path: Path) -> None:
    port = MemoryMutationPort()
    port.put("project-a", "T1")
    candidate = proposal(action_type="release_session", risk=Risk.HIGH, approval_required=True)
    approval = Approval(
        approval_id="approval-1",
        proposal_digest=candidate.digest,
        project_id="project-a",
        approved_by="operator-1",
        expires_at=1500.0,
    )

    result = service(tmp_path / "commands.sqlite", port, MemorySink()).execute(
        candidate, approval=approval
    )

    assert result.status is CommandStatus.ACCEPTED
    assert port.effects == 1


def test_gate_bypass_payload_is_still_rejected_by_the_mutation_boundary(tmp_path: Path) -> None:
    port = MemoryMutationPort()
    port.put("project-a", "T1")
    candidate = proposal(payload={"new_state": "done", "weaken_gate": True})

    result = service(tmp_path / "commands.sqlite", port, MemorySink()).execute(candidate)

    assert result.code == "gate_policy_denied"
    assert port.effects == 0


def test_oversight_command_requires_a_current_lease_proof(tmp_path: Path) -> None:
    port = MemoryMutationPort()
    port.put("project-a", "T1")
    candidate = proposal(proposer_id="oversight-1", proposer_role="oversight")

    missing = service(
        tmp_path / "missing.sqlite", port, MemorySink(), authority=AuthorityValidator()
    ).execute(candidate)
    assert missing.code == "authority_required"

    proof = AuthorityProof(
        project_id="project-a", holder_id="oversight-1", generation=4, valid_until=1200.0
    )
    accepted = service(
        tmp_path / "accepted.sqlite", port, MemorySink(), authority=AuthorityValidator()
    ).execute(candidate, authority=proof)
    assert accepted.status is CommandStatus.ACCEPTED
    assert port.authorities[-1] == proof


def test_structured_payload_must_be_json_serializable() -> None:
    with pytest.raises(ValueError, match="JSON serializable"):
        proposal(payload={"bad": object()})


def test_message_payload_is_read_only_to_consumers(tmp_path: Path) -> None:
    port = MemoryMutationPort()
    port.put("project-a", "T1")
    sink = MemorySink()
    service(tmp_path / "commands.sqlite", port, sink).execute(proposal())

    payload: Mapping[str, object] = next(iter(sink.messages.values())).payload
    with pytest.raises(TypeError):
        payload["code"] = "forged"  # type: ignore[index]
