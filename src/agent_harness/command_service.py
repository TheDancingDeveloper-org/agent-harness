"""Deterministic, project-scoped authority boundary for coordination actions.

Conversation and model output are not state.  This module turns a typed
proposal into an accepted or rejected command result without giving the
proposer access to mutable storage or external-system credentials.

The mutation port is deliberately narrow.  Its ``apply`` operation MUST
atomically validate the proposal's expected revision/state/owner/attempt,
enforce the target's dependency and gate policy, apply at most one effect,
and persist the project-scoped idempotency key with its outcome.  Keeping
that compare-and-set beside the authoritative state is the only honest way
to handle a crash between the state change and this service's journal write.
The journal makes the command decision durable; the message sink publishes
that decision to the permanent coordination ledger.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Protocol


class Risk(IntEnum):
    """Minimum review level attached to an action."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3


class CommandStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IN_PROGRESS = "in_progress"


def _require_text(value: str, name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must not be empty")


def _json_copy(value: object, name: str) -> object:
    try:
        return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON serializable") from exc


def _readonly_mapping(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    copied = _json_copy(dict(value), name)
    assert isinstance(copied, dict)
    return MappingProxyType(copied)


@dataclass(frozen=True)
class ExpectedTargetState:
    """Every mutable coordinate that a proposal was reasoned against."""

    graph_revision: int
    item_state: str
    owner: str | None
    attempt: int

    def __post_init__(self) -> None:
        if self.graph_revision < 0:
            raise ValueError("graph_revision must be non-negative")
        _require_text(self.item_state, "item_state")
        if self.attempt < 0:
            raise ValueError("attempt must be non-negative")


@dataclass(frozen=True)
class ActionProposal:
    """An untrusted request to the deterministic command boundary."""

    proposal_id: str
    project_id: str
    room_id: str
    proposer_id: str
    proposer_role: str
    action_type: str
    target: str
    expected: ExpectedTargetState
    reason: str
    evidence_message_ids: tuple[str, ...]
    idempotency_key: str
    risk: Risk
    approval_required: bool
    expires_at: float
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "proposal_id",
            "project_id",
            "room_id",
            "proposer_id",
            "proposer_role",
            "action_type",
            "target",
            "reason",
            "idempotency_key",
        ):
            _require_text(str(getattr(self, name)), name)
        if self.expires_at <= 0:
            raise ValueError("expires_at must be positive")
        if any(not message_id.strip() for message_id in self.evidence_message_ids):
            raise ValueError("evidence_message_ids must not contain empty IDs")
        object.__setattr__(self, "evidence_message_ids", tuple(self.evidence_message_ids))
        object.__setattr__(self, "payload", _readonly_mapping(self.payload, "payload"))

    def as_dict(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "project_id": self.project_id,
            "room_id": self.room_id,
            "proposer_id": self.proposer_id,
            "proposer_role": self.proposer_role,
            "action_type": self.action_type,
            "target": self.target,
            "expected": {
                "graph_revision": self.expected.graph_revision,
                "item_state": self.expected.item_state,
                "owner": self.expected.owner,
                "attempt": self.expected.attempt,
            },
            "reason": self.reason,
            "evidence_message_ids": list(self.evidence_message_ids),
            "idempotency_key": self.idempotency_key,
            "risk": self.risk.name.lower(),
            "approval_required": self.approval_required,
            "expires_at": self.expires_at,
            "payload": dict(self.payload),
        }

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Approval:
    """Approval bound to one exact proposal, project and validity window."""

    approval_id: str
    proposal_digest: str
    project_id: str
    approved_by: str
    expires_at: float

    def __post_init__(self) -> None:
        for name in ("approval_id", "proposal_digest", "project_id", "approved_by"):
            _require_text(str(getattr(self, name)), name)


@dataclass(frozen=True)
class AuthorityProof:
    """Fencing proof supplied for an oversight-originated proposal."""

    project_id: str
    holder_id: str
    generation: int
    valid_until: float


@dataclass(frozen=True)
class ActionRule:
    """Project policy for one action type; gates are enforced again by the owner."""

    risk: Risk
    allowed_roles: frozenset[str]
    approval_required: bool = False


@dataclass(frozen=True)
class CommandResult:
    project_id: str
    room_id: str
    proposal_id: str
    idempotency_key: str
    status: CommandStatus
    code: str
    detail: str
    decided_at: float
    replayed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "room_id": self.room_id,
            "proposal_id": self.proposal_id,
            "idempotency_key": self.idempotency_key,
            "status": self.status.value,
            "code": self.code,
            "detail": self.detail,
            "decided_at": self.decided_at,
        }


@dataclass(frozen=True)
class MutationOutcome:
    """Outcome durably associated with the key by the authoritative owner."""

    status: CommandStatus
    code: str
    detail: str

    @classmethod
    def accepted(cls, code: str, detail: str) -> MutationOutcome:
        return cls(CommandStatus.ACCEPTED, code, detail)

    @classmethod
    def rejected(cls, code: str, detail: str) -> MutationOutcome:
        return cls(CommandStatus.REJECTED, code, detail)

    def __post_init__(self) -> None:
        if self.status not in (CommandStatus.ACCEPTED, CommandStatus.REJECTED):
            raise ValueError("a mutation outcome must be final")


@dataclass(frozen=True)
class OutboundMessage:
    """A ledger-neutral append request for a command decision."""

    project_id: str
    room_id: str
    sender_id: str
    message_type: str
    correlation_id: str
    causation_id: str
    idempotency_key: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _readonly_mapping(self.payload, "message payload"))


class MutationPort(Protocol):
    """Authoritative mutation boundary; implementations own atomic CAS/idempotency."""

    def apply(
        self, proposal: ActionProposal, authority: AuthorityProof | None
    ) -> MutationOutcome: ...


class MessageSink(Protocol):
    """Append-only sink; ``idempotency_key`` MUST make duplicate appends harmless."""

    def append(self, message: OutboundMessage) -> None: ...


class AuthorityValidator(Protocol):
    def validate(self, proof: AuthorityProof, proposal: ActionProposal) -> bool: ...


class CommandPolicy:
    """Closed action allow-list.  An absent action is never implicitly allowed."""

    def __init__(self, rules: Mapping[str, ActionRule]) -> None:
        self._rules = dict(rules)

    def validate(self, proposal: ActionProposal) -> MutationOutcome | None:
        rule = self._rules.get(proposal.action_type)
        if rule is None:
            return MutationOutcome.rejected("unknown_action", "action is not allowed by policy")
        if proposal.proposer_role not in rule.allowed_roles:
            return MutationOutcome.rejected(
                "role_denied", "proposer role is not allowed to request this action"
            )
        if proposal.risk < rule.risk:
            return MutationOutcome.rejected(
                "risk_understated", "proposal risk is lower than the configured action risk"
            )
        return None

    def requires_approval(self, proposal: ActionProposal) -> bool:
        rule = self._rules.get(proposal.action_type)
        return proposal.approval_required or (rule is not None and rule.approval_required)


@dataclass(frozen=True)
class _Reservation:
    acquired: bool
    result: CommandResult | None = None
    conflict: bool = False


class SQLiteCommandJournal:
    """Durable command decisions, distinct from mutable work and telemetry stores."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS command_journal (
        project_id       TEXT NOT NULL,
        idempotency_key  TEXT NOT NULL,
        proposal_digest  TEXT NOT NULL,
        proposal_id      TEXT NOT NULL,
        room_id          TEXT NOT NULL,
        status           TEXT,
        code             TEXT,
        detail           TEXT,
        decided_at       REAL,
        processor_token  TEXT,
        processor_until  REAL NOT NULL DEFAULT 0,
        delivered        INTEGER NOT NULL DEFAULT 0,
        created_at       REAL NOT NULL,
        PRIMARY KEY (project_id, idempotency_key)
    );
    """

    def __init__(self, path: str | object) -> None:
        self.path = str(path)
        with self._connect() as conn:
            conn.executescript(self._SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _result(row: sqlite3.Row) -> CommandResult | None:
        if row["status"] is None:
            return None
        return CommandResult(
            project_id=str(row["project_id"]),
            room_id=str(row["room_id"]),
            proposal_id=str(row["proposal_id"]),
            idempotency_key=str(row["idempotency_key"]),
            status=CommandStatus(str(row["status"])),
            code=str(row["code"]),
            detail=str(row["detail"]),
            decided_at=float(row["decided_at"]),
        )

    def reserve(
        self,
        proposal: ActionProposal,
        *,
        token: str,
        now: float,
        processing_seconds: float,
    ) -> _Reservation:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM command_journal
                   WHERE project_id = ? AND idempotency_key = ?""",
                (proposal.project_id, proposal.idempotency_key),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO command_journal
                       (project_id, idempotency_key, proposal_digest, proposal_id, room_id,
                        processor_token, processor_until, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        proposal.project_id,
                        proposal.idempotency_key,
                        proposal.digest,
                        proposal.proposal_id,
                        proposal.room_id,
                        token,
                        now + processing_seconds,
                        now,
                    ),
                )
                return _Reservation(acquired=True)
            if row["proposal_digest"] != proposal.digest:
                return _Reservation(acquired=False, conflict=True)
            result = self._result(row)
            if result is not None:
                return _Reservation(acquired=False, result=result)
            if float(row["processor_until"]) > now and row["processor_token"] != token:
                return _Reservation(acquired=False)
            conn.execute(
                """UPDATE command_journal
                   SET processor_token = ?, processor_until = ?
                   WHERE project_id = ? AND idempotency_key = ? AND status IS NULL""",
                (token, now + processing_seconds, proposal.project_id, proposal.idempotency_key),
            )
            return _Reservation(acquired=True)

    def finish(self, result: CommandResult, *, token: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE command_journal
                   SET status = ?, code = ?, detail = ?, decided_at = ?,
                       processor_token = NULL, processor_until = 0
                   WHERE project_id = ? AND idempotency_key = ?
                     AND processor_token = ? AND status IS NULL""",
                (
                    result.status.value,
                    result.code,
                    result.detail,
                    result.decided_at,
                    result.project_id,
                    result.idempotency_key,
                    token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("command journal processing lease was lost")

    def delivered(self, project_id: str, idempotency_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT delivered FROM command_journal
                   WHERE project_id = ? AND idempotency_key = ?""",
                (project_id, idempotency_key),
            ).fetchone()
        return bool(row and row["delivered"])

    def mark_delivered(self, project_id: str, idempotency_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE command_journal SET delivered = 1
                   WHERE project_id = ? AND idempotency_key = ? AND status IS NOT NULL""",
                (project_id, idempotency_key),
            )


class CommandResultDeliveryError(RuntimeError):
    """The decision is durable, but its ledger result could not be appended."""

    def __init__(self, result: CommandResult) -> None:
        super().__init__(f"command {result.idempotency_key!r} decided but result delivery failed")
        self.result = result


class CommandService:
    """Validate, apply, journal and publish one action proposal."""

    def __init__(
        self,
        *,
        journal: SQLiteCommandJournal,
        mutations: MutationPort,
        messages: MessageSink,
        policy: CommandPolicy,
        authority_validator: AuthorityValidator | None = None,
        now: Callable[[], float] = time.time,
        processing_seconds: float = 30.0,
    ) -> None:
        if processing_seconds <= 0:
            raise ValueError("processing_seconds must be positive")
        self.journal = journal
        self.mutations = mutations
        self.messages = messages
        self.policy = policy
        self.authority_validator = authority_validator
        self.now = now
        self.processing_seconds = processing_seconds

    def execute(
        self,
        proposal: ActionProposal,
        *,
        approval: Approval | None = None,
        authority: AuthorityProof | None = None,
    ) -> CommandResult:
        decided_at = self.now()
        token = uuid.uuid4().hex
        reservation = self.journal.reserve(
            proposal,
            token=token,
            now=decided_at,
            processing_seconds=self.processing_seconds,
        )
        if reservation.conflict:
            result = self._result(
                proposal,
                CommandStatus.REJECTED,
                "idempotency_conflict",
                "idempotency key was already used for different proposal content",
                decided_at,
            )
            self._deliver(result, proposal, persist_delivery=False)
            return result
        if reservation.result is not None:
            result = replace(reservation.result, replayed=True)
            self._deliver(result, proposal, persist_delivery=True)
            return result
        if not reservation.acquired:
            return self._result(
                proposal,
                CommandStatus.IN_PROGRESS,
                "command_in_progress",
                "another process is applying this command",
                decided_at,
            )

        outcome = self._validate(proposal, approval, authority, decided_at)
        if outcome is None:
            outcome = self.mutations.apply(proposal, authority)
        result = self._result(proposal, outcome.status, outcome.code, outcome.detail, decided_at)
        self.journal.finish(result, token=token)
        self._deliver(result, proposal, persist_delivery=True)
        return result

    def _validate(
        self,
        proposal: ActionProposal,
        approval: Approval | None,
        authority: AuthorityProof | None,
        now: float,
    ) -> MutationOutcome | None:
        if proposal.expires_at <= now:
            return MutationOutcome.rejected("expired", "proposal has expired")
        denied = self.policy.validate(proposal)
        if denied is not None:
            return denied
        if proposal.proposer_role == "oversight":
            if authority is None or self.authority_validator is None:
                return MutationOutcome.rejected(
                    "authority_required", "oversight action requires a current lease proof"
                )
            if authority.valid_until <= now or not self.authority_validator.validate(
                authority, proposal
            ):
                return MutationOutcome.rejected(
                    "authority_invalid", "oversight lease proof is stale or does not match"
                )
        if self.policy.requires_approval(proposal):
            if approval is None:
                return MutationOutcome.rejected(
                    "approval_required", "action requires operator approval"
                )
            if (
                approval.project_id != proposal.project_id
                or approval.proposal_digest != proposal.digest
                or approval.expires_at <= now
            ):
                return MutationOutcome.rejected(
                    "approval_invalid", "approval is stale or does not match this proposal"
                )
        return None

    @staticmethod
    def _result(
        proposal: ActionProposal,
        status: CommandStatus,
        code: str,
        detail: str,
        decided_at: float,
    ) -> CommandResult:
        return CommandResult(
            project_id=proposal.project_id,
            room_id=proposal.room_id,
            proposal_id=proposal.proposal_id,
            idempotency_key=proposal.idempotency_key,
            status=status,
            code=code,
            detail=detail,
            decided_at=decided_at,
        )

    def _deliver(
        self,
        result: CommandResult,
        proposal: ActionProposal,
        *,
        persist_delivery: bool,
    ) -> None:
        if persist_delivery and self.journal.delivered(result.project_id, result.idempotency_key):
            return
        suffix = "accepted" if result.status is CommandStatus.ACCEPTED else "rejected"
        key = f"command-result:{result.project_id}:{result.idempotency_key}:{proposal.digest}"
        message = OutboundMessage(
            project_id=result.project_id,
            room_id=result.room_id,
            sender_id="command-service",
            message_type=f"command_{suffix}",
            correlation_id=proposal.proposal_id,
            causation_id=proposal.proposal_id,
            idempotency_key=key,
            payload={
                **result.as_dict(),
                "evidence_message_ids": list(proposal.evidence_message_ids),
            },
        )
        try:
            self.messages.append(message)
        except Exception as exc:
            raise CommandResultDeliveryError(result) from exc
        if persist_delivery:
            self.journal.mark_delivered(result.project_id, result.idempotency_key)
