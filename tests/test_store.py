"""Store tests.

The load-bearing one is `test_the_store_has_no_write_path_but_append`: risk
R3 in the plan is the dashboard becoming a second source of truth and
drifting, and the defence is that there is no code that could mutate a row.
That is checked against the source, not merely intended.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_harness.events import MODEL_CALL, PATCH_APPLY, Event
from agent_harness.store import EventStore

STORE_SOURCE = Path(__file__).resolve().parents[1] / "src" / "agent_harness" / "store.py"


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "t.sqlite")


def call(ts: float = 1000.0, **kw: object) -> Event:
    base: dict[str, object] = {
        "ts": ts,
        "kind": MODEL_CALL,
        "source": "model-calls.jsonl",
        "worker": "jpeg",
        "role": "fixer",
        "model": "gpt-5.6-sol",
        "endpoint": "https://gw",
        "outcome": "ok",
        "error_class": None,
        "latency_s": 1.5,
        "data": {},
    }
    base.update(kw)
    return Event(**base)  # type: ignore[arg-type]


def test_append_and_count(store: EventStore) -> None:
    assert store.append([call(), call(ts=1001.0)]) == 2
    assert store.count() == 2


def test_appending_the_same_event_twice_is_a_no_op(store: EventStore) -> None:
    """This is what makes the ingester replayable without file offsets."""
    events = [call(), call(ts=1001.0)]
    assert store.append(events) == 2
    assert store.append(events) == 0
    assert store.count() == 2


def test_two_genuinely_different_events_in_the_same_second_both_land(
    store: EventStore,
) -> None:
    # A dedupe key that collapsed these would silently undercount a busy
    # fleet, which is the failure mode most likely to look plausible.
    assert (
        store.append(
            [
                call(worker="jpeg", data={"attempt": 0}),
                call(worker="jpeg", data={"attempt": 1}),
            ]
        )
        == 2
    )


def test_rate_limits_by_class(store: EventStore) -> None:
    store.append(
        [
            call(outcome="error", error_class="rpm"),
            call(ts=1001.0, outcome="error", error_class="rpm"),
            call(ts=1002.0, outcome="error", error_class="terminal_cap"),
            call(ts=1003.0, outcome="error", error_class="connection"),
        ]
    )
    counts = store.rate_limits_by_class()
    assert counts["rpm"] == 2
    assert counts["terminal_cap"] == 1
    # Present but not a rate limit; the caller decides, the store does not
    # silently filter.
    assert counts["connection"] == 1


def test_since_filters_the_window(store: EventStore) -> None:
    store.append(
        [
            call(ts=1000.0, outcome="error", error_class="rpm"),
            call(ts=5000.0, outcome="error", error_class="rpm"),
        ]
    )
    assert store.rate_limits_by_class(since=2000.0) == {"rpm": 1}


def test_group_counts_rejects_an_unknown_column(store: EventStore) -> None:
    # Refusing beats interpolating an arbitrary string into SQL.
    with pytest.raises(ValueError):
        store.group_counts("worker; DROP TABLE events")


def test_group_counts_restricts_to_rate_limits_by_default(store: EventStore) -> None:
    store.append(
        [
            call(worker="a", outcome="error", error_class="rpm"),
            call(ts=1001.0, worker="b", outcome="ok"),
        ]
    )
    keys = {row["key"] for row in store.group_counts("worker")}
    assert keys == {"a"}
    keys_all = {row["key"] for row in store.group_counts("worker", rate_limits_only=False)}
    assert keys_all == {"a", "b"}


def test_since_id_is_ordered_and_resumable(store: EventStore) -> None:
    store.append([call(ts=1000.0 + i) for i in range(5)])
    first = store.since_id(0, limit=2)
    assert [r["id"] for r in first] == [1, 2]
    rest = store.since_id(first[-1]["id"])
    assert [r["id"] for r in rest] == [3, 4, 5]


def test_workers_projection(store: EventStore) -> None:
    store.append(
        [
            call(worker="jpeg", outcome="ok"),
            call(ts=1001.0, worker="jpeg", outcome="error", error_class="rpm"),
            call(ts=1002.0, worker="tiff", outcome="ok"),
        ]
    )
    rows = {r["worker"]: r for r in store.workers()}
    assert rows["jpeg"]["calls"] == 2
    assert rows["jpeg"]["errors"] == 1
    assert rows["jpeg"]["last_seen"] == 1001.0


def test_data_round_trips_as_json(store: EventStore) -> None:
    store.append([call(kind=PATCH_APPLY, outcome="applied", data={"tag": "Exif:Make"})])
    assert store.recent(PATCH_APPLY)[0]["data"] == {"tag": "Exif:Make"}


def test_reopening_the_same_file_keeps_the_events(tmp_path: Path) -> None:
    path = tmp_path / "t.sqlite"
    EventStore(path).append([call()])
    assert EventStore(path).count() == 1


def test_a_schema_from_another_version_is_refused_not_migrated(tmp_path: Path) -> None:
    # Silently reshaping the source of truth is exactly the drift R3 warns
    # about. Refusing forces a deliberate migration.
    path = tmp_path / "t.sqlite"
    store = EventStore(path)
    conn = store._connect()
    conn.execute("UPDATE schema_version SET version = 999")
    store.close()
    with pytest.raises(RuntimeError, match="schema v999"):
        EventStore(path)


def test_the_store_has_no_write_path_but_append() -> None:
    """R3, enforced against the source rather than by intention.

    If a future change needs to mutate an event, it must delete this test
    first — which is the point. That is a decision someone should have to
    make deliberately.
    """
    source = STORE_SOURCE.read_text()
    # Strip comments and docstrings' prose mentions of the words.
    statements = re.findall(r'"""(?:.|\n)*?"""|\'[^\']*\'|"[^"]*"', source)
    sql = " ".join(s for s in statements if not s.startswith('"""'))
    for forbidden in ("UPDATE events", "DELETE FROM events", "DROP TABLE events"):
        assert forbidden not in sql, f"{forbidden} appears in a SQL literal in store.py"
    # The one legitimate UPDATE is on schema_version, and only in a test.
    assert "INSERT OR IGNORE INTO events" in sql
