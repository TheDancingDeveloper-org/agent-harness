"""The permanent coordination ledger.

Retention is the whole point, so most of these tests are about what the
ledger refuses to do rather than what it does. A message store that can be
edited is a chat log; one that cannot is a record.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

import pytest

from agent_harness import coordination
from agent_harness.coordination import (
    GENERAL_ROOM,
    IdempotencyConflict,
    LedgerUnavailable,
    MessageLedger,
    SecretDetected,
    Submission,
    UnknownMessageType,
    item_room,
)

LEDGER_SOURCE = Path(coordination.__file__)


def sub(
    *,
    project_id: str = "p1",
    room_id: str = GENERAL_ROOM,
    sender_id: str = "worker-1",
    message_type: str = "observation",
    body: str = "hello",
    key: str = "k1",
    **extra: object,
) -> Submission:
    return Submission(
        project_id=project_id,
        room_id=room_id,
        sender_id=sender_id,
        message_type=message_type,
        body=body,
        idempotency_key=str(extra.pop("idempotency_key", key)),
        **extra,  # type: ignore[arg-type]
    )


@pytest.fixture
def ledger(tmp_path: Path) -> MessageLedger:
    return MessageLedger(tmp_path / "coordination.sqlite", now=lambda: 1000.0)


# ------------------------------------------------------------- acceptance


def test_an_accepted_message_survives_process_and_database_restart(tmp_path: Path) -> None:
    path = tmp_path / "coordination.sqlite"
    first = MessageLedger(path)
    accepted = first.append(sub(body="the schema task is absent"))
    first.close()

    reopened = MessageLedger(path)
    read = reopened.read("p1", GENERAL_ROOM)
    assert [m.message_id for m in read] == [accepted.message_id]
    assert read[0].body == "the schema task is absent"
    assert read[0].digest == accepted.digest


def test_a_room_sequence_is_monotonic_and_starts_at_one(ledger: MessageLedger) -> None:
    first = ledger.append(sub(key="a", body="one"))
    second = ledger.append(sub(key="b", body="two"))
    other_room = ledger.append(sub(key="c", room_id=item_room("T1"), body="three"))

    assert (first.sequence, second.sequence) == (1, 2)
    # Sequence is per room, so a busy general room does not push item rooms
    # into arbitrary numbers nobody can reason about.
    assert other_room.sequence == 1


def test_reading_after_a_cursor_returns_only_newer_messages(ledger: MessageLedger) -> None:
    ledger.append(sub(key="a", body="one"))
    second = ledger.append(sub(key="b", body="two"))
    ledger.append(sub(key="c", body="three"))

    later = ledger.read("p1", GENERAL_ROOM, after=second.sequence)
    assert [m.body for m in later] == ["three"]


def test_an_unknown_message_type_is_refused(ledger: MessageLedger) -> None:
    with pytest.raises(UnknownMessageType):
        ledger.append(sub(message_type="freeform_vibes"))
    assert ledger.read("p1", GENERAL_ROOM) == []


def test_the_envelope_carries_the_work_it_is_about(ledger: MessageLedger) -> None:
    message = ledger.append(
        sub(
            room_id=item_room("T7"),
            message_type="dependency_found",
            body="T7 needs the schema task, which is not in the queue",
            item_id="T7",
            attempt=2,
            session_id="s-9",
            recipients=("oversight",),
            payload={"missing": "SCHEMA-1"},
        )
    )
    assert message.item_id == "T7"
    assert message.attempt == 2
    assert message.session_id == "s-9"
    assert message.recipients == ("oversight",)
    assert message.payload == {"missing": "SCHEMA-1"}
    assert message.schema_version == coordination.SCHEMA_VERSION


# ------------------------------------------------------------ idempotency


def test_replaying_a_submission_returns_the_original_record(ledger: MessageLedger) -> None:
    first = ledger.append(sub(key="same", body="one"))
    replayed = ledger.append(sub(key="same", body="one"))

    assert replayed.message_id == first.message_id
    assert replayed.sequence == first.sequence
    assert len(ledger.read("p1", GENERAL_ROOM)) == 1


def test_a_reused_key_with_different_content_is_refused_not_merged(
    ledger: MessageLedger,
) -> None:
    """Silently returning the first record would hide the second message."""
    ledger.append(sub(key="same", body="one"))
    with pytest.raises(IdempotencyConflict):
        ledger.append(sub(key="same", body="something else entirely"))
    assert [m.body for m in ledger.read("p1", GENERAL_ROOM)] == ["one"]


def test_an_idempotency_key_is_scoped_to_its_project(ledger: MessageLedger) -> None:
    ledger.append(sub(project_id="p1", key="same", body="one"))
    other = ledger.append(sub(project_id="p2", key="same", body="two"))
    assert other.body == "two"


# --------------------------------------------------------------- isolation


def test_two_projects_with_the_same_room_id_cannot_see_each_other(
    ledger: MessageLedger,
) -> None:
    room = item_room("T1")
    ledger.append(sub(project_id="p1", room_id=room, key="a", body="mine"))
    ledger.append(sub(project_id="p2", room_id=room, key="b", body="theirs"))

    assert [m.body for m in ledger.read("p1", room)] == ["mine"]
    assert [m.body for m in ledger.read("p2", room)] == ["theirs"]
    # Both are the first message in their own room; neither numbering leaked.
    assert ledger.read("p1", room)[0].sequence == 1
    assert ledger.read("p2", room)[0].sequence == 1


def test_a_message_cannot_be_fetched_from_another_project(ledger: MessageLedger) -> None:
    mine = ledger.append(sub(project_id="p1", body="mine"))
    assert ledger.get("p2", mine.message_id) is None
    assert ledger.get("p1", mine.message_id) is not None


def test_rooms_are_listed_per_project(ledger: MessageLedger) -> None:
    ledger.append(sub(project_id="p1", key="a", room_id=GENERAL_ROOM))
    ledger.append(sub(project_id="p1", key="b", room_id=item_room("T1")))
    ledger.append(sub(project_id="p2", key="c", room_id=item_room("T2")))

    assert ledger.rooms("p1") == [GENERAL_ROOM, item_room("T1")]
    assert ledger.rooms("p2") == [item_room("T2")]


# ------------------------------------------------------------- concurrency


def test_two_concurrent_senders_receive_distinct_ordered_records(tmp_path: Path) -> None:
    ledger = MessageLedger(tmp_path / "coordination.sqlite")
    senders = 8
    start = threading.Barrier(senders)
    accepted: list[int] = []
    lock = threading.Lock()

    def send(index: int) -> None:
        start.wait()
        message = ledger.append(sub(key=f"k{index}", body=f"body {index}"))
        with lock:
            accepted.append(message.sequence)

    threads = [threading.Thread(target=send, args=(i,)) for i in range(senders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(accepted) == list(range(1, senders + 1)), "sequences collided or skipped"
    stored = ledger.read("p1", GENERAL_ROOM)
    assert [m.sequence for m in stored] == list(range(1, senders + 1))
    assert ledger.verify("p1", GENERAL_ROOM) is True


# -------------------------------------------------------------- durability


def test_an_unavailable_ledger_does_not_acknowledge(tmp_path: Path) -> None:
    """No false success. A caller that gets a Message has a durable record."""
    path = tmp_path / "coordination.sqlite"
    broken = _BreakableConnections(path)
    ledger = MessageLedger(path, connect=broken)

    broken.fail = True
    with pytest.raises(LedgerUnavailable):
        ledger.append(sub(body="this must not be acknowledged"))

    broken.fail = False
    assert ledger.read("p1", GENERAL_ROOM) == []


def test_a_failed_append_does_not_consume_a_sequence_number(tmp_path: Path) -> None:
    path = tmp_path / "coordination.sqlite"
    broken = _BreakableConnections(path)
    ledger = MessageLedger(path, connect=broken)

    ledger.append(sub(key="a", body="one"))
    broken.fail = True
    with pytest.raises(LedgerUnavailable):
        ledger.append(sub(key="b", body="two"))
    broken.fail = False

    assert ledger.append(sub(key="b", body="two")).sequence == 2


class _FailingWrites:
    """A connection whose durable write fails on demand.

    Injected rather than simulated with file permissions, because a test that
    depends on not being run as root proves nothing when it is skipped. The
    rollback is deliberately still allowed through: a storage error that also
    broke rollback would be a different failure, and this test is about the
    one that matters -- the insert did not land, so nothing may be returned.
    """

    def __init__(self, conn: sqlite3.Connection, owner: _BreakableConnections) -> None:
        self._conn = conn
        self._owner = owner

    def execute(self, sql: str, *args: object) -> object:
        if self._owner.fail and sql.lstrip().upper().startswith("INSERT"):
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *args)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


class _BreakableConnections:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fail = False

    def __call__(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return _FailingWrites(conn, self)  # type: ignore[return-value]


# ------------------------------------------------------- backup / restore


def test_a_restored_backup_holds_every_message_and_still_verifies(tmp_path: Path) -> None:
    """Retention without a tested restore is a claim, not a property."""
    path = tmp_path / "coordination.sqlite"
    ledger = MessageLedger(path)
    for index in range(5):
        ledger.append(sub(key=f"k{index}", body=f"body {index}"))
    ledger.append(sub(key="item", room_id=item_room("T1"), body="about T1"))

    copy = ledger.backup(tmp_path / "backups" / "coordination.sqlite")
    ledger.close()

    # Restoring is an ordinary file copy back into place.
    restored_path = tmp_path / "restored.sqlite"
    restored_path.write_bytes(copy.read_bytes())
    restored = MessageLedger(restored_path)

    assert [m.body for m in restored.read("p1", GENERAL_ROOM)] == [f"body {i}" for i in range(5)]
    assert [m.body for m in restored.read("p1", item_room("T1"))] == ["about T1"]
    assert restored.verify("p1", GENERAL_ROOM) is True
    assert restored.verify("p1", item_room("T1")) is True


def test_a_backup_taken_while_senders_are_writing_is_consistent(tmp_path: Path) -> None:
    """A live WAL database copied with `cp` opens and is wrong. This must not."""
    path = tmp_path / "coordination.sqlite"
    ledger = MessageLedger(path)
    stop = threading.Event()

    def write() -> None:
        index = 0
        while not stop.is_set():
            ledger.append(sub(key=f"k{index}", body=f"body {index}"))
            index += 1

    writer = threading.Thread(target=write)
    writer.start()
    try:
        copy = ledger.backup(tmp_path / "mid-flight.sqlite")
    finally:
        stop.set()
        writer.join()

    restored = MessageLedger(copy)
    assert restored.verify("p1", GENERAL_ROOM) is True, "the backup caught a torn write"
    stored = restored.read("p1", GENERAL_ROOM, limit=10_000)
    assert [m.sequence for m in stored] == list(range(1, len(stored) + 1))


def test_a_backup_refuses_to_overwrite_the_live_ledger(tmp_path: Path) -> None:
    path = tmp_path / "coordination.sqlite"
    ledger = MessageLedger(path)
    with pytest.raises(ValueError, match="backup"):
        ledger.backup(path)


# ----------------------------------------------------------- immutability


def test_the_ledger_has_no_write_path_but_append() -> None:
    """The retention invariant, enforced against the source.

    A future change that needs to edit a message has to delete this test
    first, which is the point: permanence is a decision, not an accident.
    """
    source = LEDGER_SOURCE.read_text()
    literals = re.findall(r'"""(?:.|\n)*?"""|\'[^\']*\'|"(?:[^"\\]|\\.)*"', source)
    sql = " ".join(s for s in literals if not s.startswith('"""'))
    for forbidden in (
        "UPDATE messages",
        "DELETE FROM messages",
        "DROP TABLE messages",
        "UPDATE access_restrictions",
        "DELETE FROM access_restrictions",
    ):
        assert forbidden not in sql, f"{forbidden} appears in a SQL literal in coordination.py"
    assert "INSERT INTO messages" in sql


def test_a_correction_preserves_the_original(ledger: MessageLedger) -> None:
    original = ledger.append(sub(key="a", body="the schema task is T4"))
    correction = ledger.append(
        sub(
            key="b",
            message_type="correction",
            body="it is T5, not T4",
            reply_to=original.message_id,
        )
    )

    stored = ledger.read("p1", GENERAL_ROOM)
    assert [m.body for m in stored] == ["the schema task is T4", "it is T5, not T4"]
    assert correction.reply_to == original.message_id
    assert stored[0].digest == original.digest
    assert ledger.corrections("p1", original.message_id) == [correction.message_id]


def test_an_access_restriction_hides_the_body_without_changing_the_record(
    ledger: MessageLedger,
) -> None:
    message = ledger.append(sub(body="an internal hostname nobody else should read"))

    ledger.restrict(
        "p1",
        message.message_id,
        audience=("operator",),
        reason="contains deployment detail",
        restricted_by="human-1",
    )

    ordinary = ledger.read("p1", GENERAL_ROOM)[0]
    assert ordinary.restricted is True
    assert "hostname" not in ordinary.body
    # The stored record is untouched: same digest, and the chain still verifies.
    assert ordinary.digest == message.digest
    assert ledger.verify("p1", GENERAL_ROOM) is True

    privileged = ledger.read("p1", GENERAL_ROOM, audience="operator")[0]
    assert privileged.body == "an internal hostname nobody else should read"
    assert privileged.restricted is True


def test_restricting_an_absent_message_is_refused(ledger: MessageLedger) -> None:
    with pytest.raises(KeyError):
        ledger.restrict(
            "p1", "no-such-message", audience=("operator",), reason="x", restricted_by="human-1"
        )


def test_the_hash_chain_detects_an_edited_row(tmp_path: Path) -> None:
    """Tamper evidence: editing the file behind the ledger's back shows up."""
    path = tmp_path / "coordination.sqlite"
    ledger = MessageLedger(path)
    ledger.append(sub(key="a", body="one"))
    ledger.append(sub(key="b", body="two"))
    assert ledger.verify("p1", GENERAL_ROOM) is True
    ledger.close()

    conn = sqlite3.connect(path)
    conn.execute("UPDATE messages SET body = 'not what was said' WHERE sequence = 1")
    conn.commit()
    conn.close()

    assert MessageLedger(path).verify("p1", GENERAL_ROOM) is False


# --------------------------------------------------------- secret handling


def test_a_detected_secret_is_refused_before_acceptance(ledger: MessageLedger) -> None:
    """Permanent retention means a posted credential cannot be unposted."""
    with pytest.raises(SecretDetected) as caught:
        ledger.append(sub(body="use AKIAIOSFODNN7EXAMPLE to reach the bucket"))

    # The report names the kind, never the value -- an exception message is
    # itself something that gets logged.
    assert "AKIAIOSFODNN7EXAMPLE" not in str(caught.value)
    assert ledger.read("p1", GENERAL_ROOM) == []


def test_a_secret_in_the_payload_is_refused_too(ledger: MessageLedger) -> None:
    with pytest.raises(SecretDetected):
        ledger.append(sub(body="here", payload={"env": "-----BEGIN RSA PRIVATE KEY-----"}))


def test_the_scanner_is_replaceable(tmp_path: Path) -> None:
    """The core cannot know every deployment's secret shapes, so it does not
    pretend to: the default is best-effort and the scanner is injected."""

    class RefuseEverything:
        def find(self, text: str) -> list[str]:
            return ["everything"] if text else []

    ledger = MessageLedger(tmp_path / "coordination.sqlite", scanner=RefuseEverything())
    with pytest.raises(SecretDetected):
        ledger.append(sub(body="entirely innocent"))


def test_an_attachment_is_recorded_by_digest_not_by_value(ledger: MessageLedger) -> None:
    message = ledger.append(
        sub(
            body="the failing log",
            attachments=(
                coordination.Attachment(
                    digest="sha256:abc123",
                    size=4096,
                    media_type="text/plain",
                    location="s3://artifacts/abc123",
                ),
            ),
        )
    )
    assert message.attachments[0].digest == "sha256:abc123"
    assert ledger.read("p1", GENERAL_ROOM)[0].attachments[0].location == "s3://artifacts/abc123"


# ------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "field",
    ["project_id", "room_id", "sender_id", "idempotency_key"],
)
def test_an_empty_required_field_is_refused(ledger: MessageLedger, field: str) -> None:
    with pytest.raises(ValueError, match=field):
        ledger.append(sub(**{field: "   "}))


def test_a_reply_must_name_a_message_in_the_same_project(ledger: MessageLedger) -> None:
    mine = ledger.append(sub(project_id="p1", body="mine"))
    with pytest.raises(ValueError, match="reply_to"):
        ledger.append(sub(project_id="p2", key="b", reply_to=mine.message_id))
