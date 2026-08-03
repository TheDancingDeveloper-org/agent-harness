"""The permanent, project-scoped coordination ledger.

Agents, humans and the oversight actor need somewhere to say things to each
other. That store is deliberately *not* the work queue and *not* the audit
telemetry, because both of those are allowed to forget:

- ``harness.sqlite`` holds mutable operational state that is rebuildable;
- ``audit.sqlite`` holds telemetry with rollup and thinning semantics;
- ``coordination.sqlite`` -- this module -- holds messages, forever.

**Once a message is accepted it is never edited, deleted, compacted, rolled
up or replaced.** There is no update path and no delete path in this module,
and a test greps the source to keep it that way. Everything that looks like
a mutation is another append: a correction references the message it
corrects, an access restriction limits who may read a body without touching
the body, and a receipt is its own record.

Three consequences fall out of that, and each is a design decision rather
than an oversight:

**Acceptance must mean durability.** ``append`` returns a ``Message`` only
after the row exists. If the database cannot be written, it raises
``LedgerUnavailable``; it never degrades to a best-effort acknowledgement,
because a sender that believes it reported a blocking dependency and did not
is worse than one that knows it failed.

**Secrets cannot be un-posted.** Submission is scanned before acceptance and
refused if it looks like a credential. The default scanner is best-effort
and generic; a deployment that knows its own secret shapes injects its own.

**Order has to be real.** Each room carries its own gapless sequence
assigned inside the write transaction, so two concurrent senders get two
distinct ordered records rather than a guess based on wall-clock time. Each
message also chains the previous message's digest, so a row edited behind
the ledger's back stops verifying.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

#: Every project has one general room; every work item gets its own.
GENERAL_ROOM = "general"


def item_room(item_id: str) -> str:
    """The room for one work item. A convention, not a constraint."""

    return f"item:{item_id}"


#: The closed set of things a message can be. A closed set is the point: a
#: reader deciding what to do with a message must not have to parse prose,
#: and a new kind of traffic should be a deliberate addition here.
MESSAGE_TYPES = frozenset(
    {
        "observation",
        "question",
        "answer",
        "dependency_found",
        "dependency_unresolved",
        "action_proposal",
        "decision",
        "command_accepted",
        "command_rejected",
        "delivery_receipt",
        "system_notice",
        "correction",
    }
)

#: What an ordinary reader sees in place of a restricted body. The record
#: itself is unchanged -- this is applied on the way out.
REDACTED = "[restricted]"


class LedgerError(RuntimeError):
    """Base class, so a caller can catch every ledger refusal at once."""


class LedgerUnavailable(LedgerError):
    """The durable record could not be written. Nothing was accepted."""


class IdempotencyConflict(LedgerError):
    """The key was already used, for different content.

    Returning the earlier record would silently swallow the new message, and
    appending a second one would make the key meaningless. Neither is safe,
    so the sender is told.
    """


class UnknownMessageType(LedgerError):
    """The type is not in ``MESSAGE_TYPES``."""


class SecretDetected(LedgerError):
    """Submission looks like it contains a credential, so it was refused.

    The exception names the *kinds* matched and never the matched text: an
    exception message is itself something that ends up in a log.
    """

    def __init__(self, kinds: Sequence[str]) -> None:
        listed = ", ".join(sorted(set(kinds)))
        super().__init__(
            f"submission refused before acceptance: it looks like it contains {listed}. "
            "The ledger is permanent, so a posted credential cannot be removed later."
        )
        self.kinds = tuple(sorted(set(kinds)))


# ----------------------------------------------------------------- envelope


@dataclass(frozen=True)
class Attachment:
    """An attachment lives in content-addressed storage, not in the ledger.

    Bodies are retained forever; blobs should not be. The message keeps
    enough to find and verify the blob, and nothing else.
    """

    digest: str
    size: int
    media_type: str
    location: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "size": self.size,
            "media_type": self.media_type,
            "location": self.location,
        }


@dataclass(frozen=True)
class Submission:
    """What a sender offers. The ledger assigns everything else."""

    project_id: str
    room_id: str
    sender_id: str
    message_type: str
    body: str
    idempotency_key: str
    sender_role: str = "agent"
    recipients: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)
    item_id: str | None = None
    attempt: int | None = None
    session_id: str | None = None
    reply_to: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    attachments: tuple[Attachment, ...] = ()

    def validate(self) -> None:
        for name in ("project_id", "room_id", "sender_id", "sender_role", "idempotency_key"):
            value = getattr(self, name)
            if not str(value).strip():
                raise ValueError(f"{name} must not be empty")
        if self.message_type not in MESSAGE_TYPES:
            raise UnknownMessageType(
                f"{self.message_type!r} is not a known message type; "
                f"known types are {', '.join(sorted(MESSAGE_TYPES))}"
            )
        if self.attempt is not None and self.attempt < 0:
            raise ValueError("attempt must not be negative")
        try:
            json.dumps(dict(self.payload), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must be JSON serializable") from exc

    def scannable(self) -> str:
        """Everything a secret could hide in, as one string for the scanner."""

        return "\n".join([self.body, json.dumps(dict(self.payload), sort_keys=True, default=str)])


@dataclass(frozen=True)
class Message:
    """An accepted record. Immutable, and never returned before it is durable."""

    message_id: str
    project_id: str
    room_id: str
    sequence: int
    sender_id: str
    sender_role: str
    recipients: tuple[str, ...]
    message_type: str
    body: str
    payload: Mapping[str, Any]
    item_id: str | None
    attempt: int | None
    session_id: str | None
    reply_to: str | None
    correlation_id: str | None
    causation_id: str | None
    attachments: tuple[Attachment, ...]
    created_at: float
    idempotency_key: str
    schema_version: int
    previous_digest: str | None
    digest: str
    #: Set on the way out when an access restriction covers this message.
    #: It describes the *view*, not the stored row, which is why it takes no
    #: part in the digest.
    restricted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "project_id": self.project_id,
            "room_id": self.room_id,
            "sequence": self.sequence,
            "sender_id": self.sender_id,
            "sender_role": self.sender_role,
            "recipients": list(self.recipients),
            "message_type": self.message_type,
            "body": self.body,
            "payload": dict(self.payload),
            "item_id": self.item_id,
            "attempt": self.attempt,
            "session_id": self.session_id,
            "reply_to": self.reply_to,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "attachments": [a.as_dict() for a in self.attachments],
            "created_at": self.created_at,
            "idempotency_key": self.idempotency_key,
            "schema_version": self.schema_version,
            "previous_digest": self.previous_digest,
            "digest": self.digest,
            "restricted": self.restricted,
        }


@dataclass(frozen=True)
class AccessRestriction:
    """An append-only limit on who may read one message's body."""

    restriction_id: str
    project_id: str
    message_id: str
    audience: tuple[str, ...]
    reason: str
    restricted_by: str
    created_at: float


# ------------------------------------------------------------------ secrets


class SecretScanner:
    """Pre-acceptance credential detection.

    Deliberately generic and deliberately modest: the core cannot know one
    deployment's secret shapes, and pretending otherwise would put a specific
    workload's conventions into generic code. Injected, so a deployment that
    does know can supply a real scanner.
    """

    #: Kind name -> pattern. Kinds are what an exception is allowed to name.
    PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("a private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
        ("an AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
        (
            "a bearer or basic authorization header",
            re.compile(r"(?i)\bauthorization:\s*(bearer|basic)\s+\S+"),
        ),
        ("a URL with embedded credentials", re.compile(r"://[^\s/:@]+:[^\s/@]+@")),
        (
            "an assigned secret, key, token or password",
            re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*\S{12,}"),
        ),
    )

    def find(self, text: str) -> list[str]:
        return [kind for kind, pattern in self.PATTERNS if pattern.search(text)]


# ------------------------------------------------------------------- ledger


SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS messages (
    message_id      TEXT    PRIMARY KEY,
    project_id      TEXT    NOT NULL,
    room_id         TEXT    NOT NULL,
    sequence        INTEGER NOT NULL,
    sender_id       TEXT    NOT NULL,
    sender_role     TEXT    NOT NULL,
    recipients      TEXT    NOT NULL DEFAULT '[]',
    message_type    TEXT    NOT NULL,
    body            TEXT    NOT NULL,
    payload         TEXT    NOT NULL DEFAULT '{}',
    item_id         TEXT,
    attempt         INTEGER,
    session_id      TEXT,
    reply_to        TEXT,
    correlation_id  TEXT,
    causation_id    TEXT,
    attachments     TEXT    NOT NULL DEFAULT '[]',
    created_at      REAL    NOT NULL,
    idempotency_key TEXT    NOT NULL,
    schema_version  INTEGER NOT NULL,
    previous_digest TEXT,
    digest          TEXT    NOT NULL,
    content_digest  TEXT    NOT NULL,
    UNIQUE (project_id, room_id, sequence),
    UNIQUE (project_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS messages_room ON messages (project_id, room_id, sequence);
CREATE INDEX IF NOT EXISTS messages_reply ON messages (project_id, reply_to);
CREATE INDEX IF NOT EXISTS messages_item ON messages (project_id, item_id);

CREATE TABLE IF NOT EXISTS access_restrictions (
    restriction_id TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    audience       TEXT NOT NULL,
    reason         TEXT NOT NULL,
    restricted_by  TEXT NOT NULL,
    created_at     REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS restrictions_message
    ON access_restrictions (project_id, message_id);
"""


def _digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class MessageLedger:
    """Append-only message storage. Safe to share between threads."""

    def __init__(
        self,
        path: Path | str,
        *,
        now: Callable[[], float] = time.time,
        scanner: Any | None = None,
        connect: Callable[[], sqlite3.Connection] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.now = now
        self.scanner = SecretScanner() if scanner is None else scanner
        # Injected so a test can prove the "no false acknowledgement" path
        # without depending on filesystem permissions, which are not the same
        # thing and are skipped when the suite runs as root.
        self._factory = connect or self._default_connect
        self._local = threading.local()
        conn = self._connect()
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT version FROM ledger_schema_version").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO ledger_schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
            )
        elif row[0] != SCHEMA_VERSION:
            raise LedgerError(
                f"coordination ledger at {self.path} is schema v{row[0]}, this build "
                f"expects v{SCHEMA_VERSION}. Migrating a permanent record is a "
                f"deliberate act, never an automatic one."
            )

    def _default_connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._factory()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------ backup/restore

    def backup(self, destination: Path | str) -> Path:
        """Take a consistent copy, without stopping the ledger.

        Permanent retention is a claim about the *file*, not only about this
        module's refusal to issue an UPDATE: a record nobody can restore was
        not really retained. This is SQLite's online backup rather than a
        filesystem copy, because copying a live WAL database out from under
        its writers produces a file that opens and is wrong -- the worst
        possible outcome for a backup.

        The destination must not be the live ledger. Restoring is then an
        ordinary file copy back into place, which is deliberately something
        an operator does rather than something this process can do to itself.
        """

        target = Path(destination)
        if target.resolve() == self.path.resolve():
            raise ValueError("a backup must not overwrite the ledger it is a backup of")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(target) as copy:
                self._connect().backup(copy)
        except sqlite3.Error as exc:
            raise LedgerUnavailable(f"backup of {self.path} to {target}: {exc}") from exc
        return target

    # -------------------------------------------------------------- write

    def append(self, submission: Submission) -> Message:
        """Accept one message, or raise. There is no third outcome.

        Validation, secret scanning and reply resolution all happen before
        the transaction, so a refusal costs nothing and cannot leave a
        half-written room.
        """

        submission.validate()
        found = self.scanner.find(submission.scannable())
        if found:
            raise SecretDetected(found)

        content_digest = _digest(
            {
                "room_id": submission.room_id,
                "sender_id": submission.sender_id,
                "sender_role": submission.sender_role,
                "recipients": list(submission.recipients),
                "message_type": submission.message_type,
                "body": submission.body,
                "payload": dict(submission.payload),
                "item_id": submission.item_id,
                "attempt": submission.attempt,
                "session_id": submission.session_id,
                "reply_to": submission.reply_to,
                "correlation_id": submission.correlation_id,
                "causation_id": submission.causation_id,
                "attachments": [a.as_dict() for a in submission.attachments],
            }
        )

        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise LedgerUnavailable(f"coordination ledger at {self.path}: {exc}") from exc

        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise LedgerUnavailable(f"coordination ledger at {self.path}: {exc}") from exc

        try:
            existing = conn.execute(
                "SELECT * FROM messages WHERE project_id = ? AND idempotency_key = ?",
                (submission.project_id, submission.idempotency_key),
            ).fetchone()
            if existing is not None:
                conn.execute("ROLLBACK")
                if existing["content_digest"] != content_digest:
                    raise IdempotencyConflict(
                        f"idempotency key {submission.idempotency_key!r} was already used "
                        f"in project {submission.project_id!r} for different content"
                    )
                return self._row_to_message(existing)

            if submission.reply_to is not None:
                target = conn.execute(
                    "SELECT message_id FROM messages WHERE project_id = ? AND message_id = ?",
                    (submission.project_id, submission.reply_to),
                ).fetchone()
                if target is None:
                    conn.execute("ROLLBACK")
                    raise ValueError(
                        f"reply_to {submission.reply_to!r} is not a message in project "
                        f"{submission.project_id!r}"
                    )

            previous = conn.execute(
                "SELECT sequence, digest FROM messages WHERE project_id = ? AND room_id = ? "
                "ORDER BY sequence DESC LIMIT 1",
                (submission.project_id, submission.room_id),
            ).fetchone()
            sequence = (previous["sequence"] + 1) if previous else 1
            previous_digest = previous["digest"] if previous else None

            message = Message(
                message_id=uuid.uuid4().hex,
                project_id=submission.project_id,
                room_id=submission.room_id,
                sequence=sequence,
                sender_id=submission.sender_id,
                sender_role=submission.sender_role,
                recipients=tuple(submission.recipients),
                message_type=submission.message_type,
                body=submission.body,
                payload=dict(submission.payload),
                item_id=submission.item_id,
                attempt=submission.attempt,
                session_id=submission.session_id,
                reply_to=submission.reply_to,
                correlation_id=submission.correlation_id,
                causation_id=submission.causation_id,
                attachments=tuple(submission.attachments),
                created_at=self.now(),
                idempotency_key=submission.idempotency_key,
                schema_version=SCHEMA_VERSION,
                previous_digest=previous_digest,
                digest="",
            )
            message = _with_digest(message)

            conn.execute(
                "INSERT INTO messages (message_id, project_id, room_id, sequence, sender_id, "
                "sender_role, recipients, message_type, body, payload, item_id, attempt, "
                "session_id, reply_to, correlation_id, causation_id, attachments, created_at, "
                "idempotency_key, schema_version, previous_digest, digest, content_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.message_id,
                    message.project_id,
                    message.room_id,
                    message.sequence,
                    message.sender_id,
                    message.sender_role,
                    json.dumps(list(message.recipients)),
                    message.message_type,
                    message.body,
                    json.dumps(dict(message.payload), default=str),
                    message.item_id,
                    message.attempt,
                    message.session_id,
                    message.reply_to,
                    message.correlation_id,
                    message.causation_id,
                    json.dumps([a.as_dict() for a in message.attachments]),
                    message.created_at,
                    message.idempotency_key,
                    message.schema_version,
                    message.previous_digest,
                    message.digest,
                    content_digest,
                ),
            )
            conn.execute("COMMIT")
        except (IdempotencyConflict, ValueError):
            raise
        except sqlite3.Error as exc:
            _rollback(conn)
            raise LedgerUnavailable(f"coordination ledger at {self.path}: {exc}") from exc
        except BaseException:
            _rollback(conn)
            raise
        return message

    def restrict(
        self,
        project_id: str,
        message_id: str,
        *,
        audience: Sequence[str],
        reason: str,
        restricted_by: str,
    ) -> AccessRestriction:
        """Limit who may read one message's body, without rewriting it.

        This is the only supported answer to "that should not have been
        posted": the record stays, its digest still verifies, and the
        restriction is itself an auditable append.
        """

        if not audience:
            raise ValueError("audience must name at least one role that may still read it")
        if not reason.strip():
            raise ValueError("reason must not be empty")
        conn = self._connect()
        target = conn.execute(
            "SELECT message_id FROM messages WHERE project_id = ? AND message_id = ?",
            (project_id, message_id),
        ).fetchone()
        if target is None:
            raise KeyError(f"{message_id!r} is not a message in project {project_id!r}")
        restriction = AccessRestriction(
            restriction_id=uuid.uuid4().hex,
            project_id=project_id,
            message_id=message_id,
            audience=tuple(audience),
            reason=reason,
            restricted_by=restricted_by,
            created_at=self.now(),
        )
        try:
            conn.execute(
                "INSERT INTO access_restrictions (restriction_id, project_id, message_id, "
                "audience, reason, restricted_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    restriction.restriction_id,
                    restriction.project_id,
                    restriction.message_id,
                    json.dumps(list(restriction.audience)),
                    restriction.reason,
                    restriction.restricted_by,
                    restriction.created_at,
                ),
            )
        except sqlite3.Error as exc:
            raise LedgerUnavailable(f"coordination ledger at {self.path}: {exc}") from exc
        return restriction

    # --------------------------------------------------------------- read

    def read(
        self,
        project_id: str,
        room_id: str,
        *,
        after: int = 0,
        limit: int = 200,
        audience: str | None = None,
    ) -> list[Message]:
        """Messages in one room after a cursor, oldest first.

        ``after`` is a sequence number rather than a timestamp because two
        messages in the same millisecond still have an order, and a reader
        resuming from a cursor must not skip one of them.
        """

        rows = (
            self._connect()
            .execute(
                "SELECT * FROM messages WHERE project_id = ? AND room_id = ? AND sequence > ? "
                "ORDER BY sequence LIMIT ?",
                (project_id, room_id, after, limit),
            )
            .fetchall()
        )
        return self._apply_restrictions(project_id, rows, audience)

    def get(
        self, project_id: str, message_id: str, *, audience: str | None = None
    ) -> Message | None:
        row = (
            self._connect()
            .execute(
                "SELECT * FROM messages WHERE project_id = ? AND message_id = ?",
                (project_id, message_id),
            )
            .fetchone()
        )
        if row is None:
            return None
        return self._apply_restrictions(project_id, [row], audience)[0]

    def rooms(self, project_id: str) -> list[str]:
        rows = (
            self._connect()
            .execute(
                "SELECT DISTINCT room_id FROM messages WHERE project_id = ? ORDER BY room_id",
                (project_id,),
            )
            .fetchall()
        )
        return [str(row["room_id"]) for row in rows]

    def corrections(self, project_id: str, message_id: str) -> list[str]:
        """IDs of the corrections filed against one message, oldest first."""

        rows = (
            self._connect()
            .execute(
                "SELECT message_id FROM messages WHERE project_id = ? AND reply_to = ? "
                "AND message_type = 'correction' ORDER BY created_at, sequence",
                (project_id, message_id),
            )
            .fetchall()
        )
        return [str(row["message_id"]) for row in rows]

    def restrictions(self, project_id: str, message_id: str) -> list[AccessRestriction]:
        rows = (
            self._connect()
            .execute(
                "SELECT * FROM access_restrictions WHERE project_id = ? AND message_id = ? "
                "ORDER BY created_at",
                (project_id, message_id),
            )
            .fetchall()
        )
        return [
            AccessRestriction(
                restriction_id=str(row["restriction_id"]),
                project_id=str(row["project_id"]),
                message_id=str(row["message_id"]),
                audience=tuple(json.loads(row["audience"])),
                reason=str(row["reason"]),
                restricted_by=str(row["restricted_by"]),
                created_at=float(row["created_at"]),
            )
            for row in rows
        ]

    def verify(self, project_id: str, room_id: str) -> bool:
        """Recompute the room's hash chain. False means a row was edited.

        Tamper *evidence*, not tamper proofing: nothing stops someone with
        the file from rewriting it, but they cannot do so unnoticed.
        """

        rows = (
            self._connect()
            .execute(
                "SELECT * FROM messages WHERE project_id = ? AND room_id = ? ORDER BY sequence",
                (project_id, room_id),
            )
            .fetchall()
        )
        previous: str | None = None
        expected = 1
        for row in rows:
            if row["sequence"] != expected or row["previous_digest"] != previous:
                return False
            message = self._row_to_message(row)
            if _with_digest(message).digest != row["digest"]:
                return False
            previous = str(row["digest"])
            expected += 1
        return True

    # ------------------------------------------------------------ helpers

    def _apply_restrictions(
        self, project_id: str, rows: Iterable[sqlite3.Row], audience: str | None
    ) -> list[Message]:
        messages = [self._row_to_message(row) for row in rows]
        if not messages:
            return []
        placeholders = ",".join("?" for _ in messages)
        restricted = (
            self._connect()
            .execute(
                f"SELECT message_id, audience FROM access_restrictions "  # noqa: S608 - ids are bound
                f"WHERE project_id = ? AND message_id IN ({placeholders})",
                (project_id, *[m.message_id for m in messages]),
            )
            .fetchall()
        )
        allowed: dict[str, set[str]] = {}
        for row in restricted:
            allowed.setdefault(str(row["message_id"]), set()).update(json.loads(row["audience"]))
        if not allowed:
            return messages
        out = []
        for message in messages:
            permitted = allowed.get(message.message_id)
            if permitted is None:
                out.append(message)
            elif audience is not None and audience in permitted:
                out.append(_replace_restricted(message, restricted=True))
            else:
                out.append(_redact(message))
        return out

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
        return Message(
            message_id=str(row["message_id"]),
            project_id=str(row["project_id"]),
            room_id=str(row["room_id"]),
            sequence=int(row["sequence"]),
            sender_id=str(row["sender_id"]),
            sender_role=str(row["sender_role"]),
            recipients=tuple(json.loads(row["recipients"])),
            message_type=str(row["message_type"]),
            body=str(row["body"]),
            payload=json.loads(row["payload"]),
            item_id=row["item_id"],
            attempt=row["attempt"],
            session_id=row["session_id"],
            reply_to=row["reply_to"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            attachments=tuple(Attachment(**a) for a in json.loads(row["attachments"])),
            created_at=float(row["created_at"]),
            idempotency_key=str(row["idempotency_key"]),
            schema_version=int(row["schema_version"]),
            previous_digest=row["previous_digest"],
            digest=str(row["digest"]),
        )


def _rollback(conn: sqlite3.Connection) -> None:
    with contextlib.suppress(sqlite3.Error):  # already out of the transaction
        conn.execute("ROLLBACK")


def _with_digest(message: Message) -> Message:
    """Seal an envelope. ``restricted`` is a view flag and is excluded."""

    envelope = message.as_dict()
    envelope.pop("digest")
    envelope.pop("restricted")
    return _replace(message, digest=_digest(envelope))


def _replace(message: Message, **changes: Any) -> Message:
    fields = message.as_dict()
    fields["recipients"] = tuple(message.recipients)
    fields["attachments"] = tuple(message.attachments)
    fields["payload"] = dict(message.payload)
    fields.update(changes)
    return Message(**fields)


def _replace_restricted(message: Message, *, restricted: bool) -> Message:
    return _replace(message, restricted=restricted)


def _redact(message: Message) -> Message:
    """The view an ordinary reader gets. The stored row is untouched."""

    return _replace(message, body=REDACTED, payload={}, attachments=(), restricted=True)
