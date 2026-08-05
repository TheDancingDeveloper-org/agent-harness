"""#186 — a credential must not reach a store that cannot unwrite it.

Both stores are append-only by design and the audit store outlives the
rollup, so a credential written to either can be rotated and never removed.
That makes the filter's *placement* the whole property: it has to sit at the
one boundary before the first write, where a write path added later cannot
route around it.

The tests below are therefore in two halves. The behavioural ones show that
the paths which exist today are covered. The structural ones fail if a new
write path is added that bypasses the redactor — which is the check the issue
actually asked for, because the behavioural half can only ever cover the
callers someone remembered to write a test for.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agent_harness.audit import AuditStore
from agent_harness.events import MODEL_CALL, WORK, Event
from agent_harness.ingest import ingest
from agent_harness.redaction import MARK, REDACTED_FLAG, REDACTION_FAILED, Redactor
from agent_harness.sources import Source
from agent_harness.store import EventStore

SRC = Path(__file__).resolve().parents[1] / "src" / "agent_harness"

SECRET = "sekrit-value-1234567890"


def redactor() -> Redactor:
    return Redactor([SECRET])


def event(**kw: Any) -> Event:
    fields: dict[str, Any] = {
        "ts": 1000.0,
        "kind": MODEL_CALL,
        "source": "test",
        "worker": "w1",
    }
    fields.update(kw)
    return Event(**fields)


# --------------------------------------------------------- the paths that exist


def test_a_direct_store_write_is_redacted(tmp_path: Path) -> None:
    """The executors write here directly, without going through ingest."""
    store = EventStore(tmp_path / "e.sqlite", redact=redactor())
    store.append([event(data={"agent_output": f"export KEY={SECRET}"})])

    row = store.recent()[0]
    assert SECRET not in json.dumps(row)
    assert MARK in row["data"]["agent_output"]


def test_check_output_nested_in_a_payload_is_redacted(tmp_path: Path) -> None:
    """A payload is arbitrary JSON, and the credential-carrying cases arrive
    nested at least as often as they arrive at the top level."""
    store = EventStore(tmp_path / "e.sqlite", redact=redactor())
    store.append(
        [
            event(
                kind=WORK,
                data={"checks": [{"cmd": "pytest", "stderr": f"used {SECRET} and failed"}]},
            )
        ]
    )

    assert SECRET not in json.dumps(store.recent()[0])


def test_a_provider_error_envelope_is_redacted_by_shape(tmp_path: Path) -> None:
    """Nothing told this deployment about the header the provider quoted."""
    store = EventStore(tmp_path / "e.sqlite", redact=Redactor([]))
    store.append([event(data={"error": "rejected Authorization: Bearer abcdef1234567890"})])

    assert "abcdef1234567890" not in json.dumps(store.recent()[0])


def test_a_key_in_an_endpoint_url_is_redacted(tmp_path: Path) -> None:
    """`endpoint` is a URL, and a URL can carry a key in a query string."""
    store = EventStore(tmp_path / "e.sqlite", redact=redactor())
    store.append([event(endpoint=f"https://host/v1?api_key={SECRET}")])

    assert SECRET not in json.dumps(store.recent()[0])


def test_ingest_is_redacted(tmp_path: Path) -> None:
    """The other tool's log is the path the issue names first."""
    log = tmp_path / "events.jsonl"
    log.write_text(json.dumps({"secret": f"token={SECRET}"}) + "\n")

    def reader(path: Path) -> Any:
        for line in path.read_text().splitlines():
            yield event(data=json.loads(line))

    store = EventStore(tmp_path / "e.sqlite", redact=redactor())
    report = ingest(store, [Source(name="s", path=log, reader=reader)])

    assert report.inserted == 1
    assert SECRET not in json.dumps(store.recent()[0])


def test_the_audit_store_is_redacted(tmp_path: Path) -> None:
    """The second database, and the one that outlives the rollup."""
    audit = AuditStore(tmp_path / "a.sqlite", redact=redactor())
    audit.append([event(data={"answer": SECRET, "project_id": "p", "run_id": "r", "seq": 1})])

    rows = audit.recent(limit=10)
    assert rows
    assert SECRET not in json.dumps(rows)


def test_adopting_legacy_history_is_redacted(tmp_path: Path) -> None:
    """History written before this filter existed is the history most likely
    to be carrying a credential, and adopting it is a second write path."""
    legacy = EventStore(tmp_path / "legacy.sqlite", redact=Redactor([]))
    legacy.append([event(data={"answer": SECRET, "project_id": "p"})])
    legacy.close()

    audit = AuditStore(tmp_path / "a.sqlite", redact=redactor())
    assert audit.adopt(tmp_path / "legacy.sqlite") == 1
    assert SECRET not in json.dumps(audit.recent(limit=10))


# ------------------------------------------------------------------ the record


def test_a_redaction_is_recorded_not_silent(tmp_path: Path) -> None:
    """The audit must say *something was removed here*. A record that quietly
    differs from what the agent said is worse than one that says it is
    incomplete, because nothing distinguishes it from the truth."""
    store = EventStore(tmp_path / "e.sqlite", redact=redactor())
    store.append([event(data={"answer": f"key {SECRET}"}), event(ts=1001.0, data={"answer": "hi"})])

    rows = {r["ts"]: r["data"] for r in store.recent()}
    assert rows[1000.0][REDACTED_FLAG] is True
    assert MARK in rows[1000.0]["answer"]
    assert REDACTED_FLAG not in rows[1001.0]


def test_redaction_never_drops_an_event_when_it_fails(tmp_path: Path) -> None:
    """Best-effort, like everything else on the observation path.

    A bug in the filter must not make an event that happened look like a call
    that never did. The payload is dropped and marked — the store cannot
    unwrite what it takes, so on a failure detail is the thing to lose — but
    the row, and every column a panel counts, still lands.
    """

    def explode(text: str | None) -> str | None:
        raise RuntimeError("boom")

    store = EventStore(tmp_path / "e.sqlite", redact=explode)
    assert store.append([event(outcome="error", error_class="rpm", data={"answer": SECRET})]) == 1

    row = store.recent()[0]
    assert row["outcome"] == "error"
    assert row["error_class"] == "rpm"
    assert row["data"] == {REDACTED_FLAG: True, REDACTION_FAILED: True}
    assert SECRET not in json.dumps(row)


def test_a_replay_still_collapses_onto_the_same_row(tmp_path: Path) -> None:
    """Redaction is deterministic, so the content-derived dedupe key is too.
    If it were not, re-ingesting a log would double every redacted row."""
    store = EventStore(tmp_path / "e.sqlite", redact=redactor())
    events = [event(data={"answer": SECRET})]
    assert store.append(events) == 1
    assert store.append(events) == 0


# ------------------------------------------------- the bypass-detecting checks


def _module_paths() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _writes_events(source: str) -> bool:
    lowered = source.lower()
    return "insert" in lowered and "into events" in lowered


def test_only_the_two_stores_write_events_at_all() -> None:
    """A third writer is a third boundary, and one of them will be forgotten.

    This covers the blind spot the issue names: an adapter, or anything else,
    reaching the events table without going through a store.
    """
    writers = {
        p.relative_to(SRC).as_posix() for p in _module_paths() if _writes_events(p.read_text())
    }
    assert writers == {"store.py", "audit.py"}, (
        f"a module outside the two stores writes events: {sorted(writers)}. "
        "Every write must go through a redacting append, because the stores "
        "are append-only and a credential that lands cannot be removed."
    )


def test_every_insert_into_events_redacts_first() -> None:
    """The check the issue asked for: a new write path that bypasses the
    filter fails here rather than in an incident.

    Structural on purpose. A behavioural test can only cover the callers
    someone remembered; this covers the ones nobody has written yet, by
    asserting that no function can reach the events table without naming the
    redactor on the way.
    """
    offenders: list[str] = []
    for path in _module_paths():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            body = ast.unparse(node)
            if not _writes_events(body):
                continue
            if "redact" not in body:
                offenders.append(f"{path.relative_to(SRC).as_posix()}:{node.name}")
    assert not offenders, (
        f"these write to the events table without redacting: {offenders}. "
        "Redaction belongs at the write boundary; there is no way to remove a "
        "credential from an append-only store afterwards."
    )


def test_a_store_defaults_to_redacting_when_nobody_passes_one(tmp_path: Path) -> None:
    """Opt-in redaction is redaction that a caller added later will not have.

    Both stores are constructed in several places, and none of them should
    have to know this exists for it to happen.
    """
    payload = [event(data={"answer": "Authorization: Bearer abcdef1234567890"})]

    events = EventStore(tmp_path / "d1.sqlite")
    events.append(payload)
    assert "abcdef1234567890" not in json.dumps(events.recent())

    audit = AuditStore(tmp_path / "d2.sqlite")
    audit.append(payload)
    assert "abcdef1234567890" not in json.dumps(audit.recent(limit=10))


@pytest.mark.parametrize("module", ["store.py", "audit.py"])
def test_a_store_has_exactly_one_public_way_in(module: str) -> None:
    """One door, so there is one place to put the lock.

    `append` is it. A second public writer is how the chokepoint stops being
    one — and `adopt`, which is the exception, copies rows through the same
    filter.
    """
    tree = ast.parse((SRC / module).read_text())
    writers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and _writes_events(ast.unparse(node))
        and not node.name.startswith("_")
    }
    assert writers <= {"append", "adopt"}, (
        f"{module} grew a public write path: {sorted(writers)}. Every one of them "
        "must redact, and each one is a place a future change can forget to."
    )


def test_the_legacy_row_copy_cannot_regress_to_a_verbatim_blob(tmp_path: Path) -> None:
    """`adopt` used to write the source row's `data` column through unread.

    Worth its own test because it is the one write path where the payload is
    already a serialised blob, and passing a blob along is the natural thing
    to write.
    """
    legacy = tmp_path / "legacy.sqlite"
    store = EventStore(legacy, redact=Redactor([]))
    store.append([event(data={"answer": f"pass {SECRET}"})])
    store.close()

    audit = AuditStore(tmp_path / "a.sqlite", redact=redactor())
    audit.adopt(legacy)

    conn = sqlite3.connect(tmp_path / "a.sqlite")
    blobs = [r[0] for r in conn.execute("SELECT data FROM events")]
    conn.close()
    assert blobs and all(SECRET not in b for b in blobs)
