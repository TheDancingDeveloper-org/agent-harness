"""Tests for the oxidex adapter, and for the generic ingest driving it.

Two properties carry the weight: a full re-ingest must produce an identical
store, and a rate limit from the pre-classification manifest must never
acquire a class it never had.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_harness.adapters.oxidex import (
    SOURCE_CALLS,
    SOURCE_REQUESTS,
    UNCLASSIFIED,
    parse_local_timestamp,
    sources_for,
)
from agent_harness.ingest import ingest
from agent_harness.store import EventStore

CALLS = [
    {
        "ts": 1785931200.0,
        "role": "fixer",
        "model": "gpt-5.6-sol",
        "endpoint": "https://gw",
        "outcome": "error",
        "error_class": "rpm",
        "attempt": 1,
        "latency_s": 0.3,
        "worker": "jpeg",
    },
    {
        "ts": 1785931201.0,
        "role": "reviewer",
        "model": "glm5.2-fast",
        "endpoint": "https://gw",
        "outcome": "error",
        "error_class": "terminal_cap",
        "attempt": 0,
        "latency_s": 0.2,
        "worker": "tiff",
    },
    {
        "ts": 1785931202.0,
        "role": "fixer",
        "model": "gpt-5.6-sol",
        "endpoint": "https://gw",
        "outcome": "ok",
        "error_class": None,
        "attempt": 0,
        "latency_s": 12.0,
        "worker": "jpeg",
    },
]

MANIFEST = """\
2026-07-25T13:04:11 phase=fixer worker=jpeg tier=T1 provider=clawbay model=gpt-5.6-sol \
prompt_chars=8123 elapsed=12.4s reply_chars=900 OK
2026-07-25T13:04:12 phase=fixer worker=jpeg tier=T1 provider=clawbay model=gpt-5.6-sol \
prompt_chars=8123 elapsed=0.3s ERROR=HTTP Error 429: Too Many Requests
2026-07-25T13:04:13 phase=reviewer worker=tiff tier=T1 provider=clawbay model=glm5.2-fast \
prompt_chars=100 elapsed=0.2s ERROR=HTTP Error 500: Internal Server Error
2026-07-25T13:04:14 phase=fixer worker=jpeg tier=T1 provider=clawbay model=gpt-5.6-sol \
RETRY model call retry 1/1000 after HTTPError(), waiting 4s
not a manifest line at all
"""

DIFFS = """\
2026-07-25T13:05:00 worker=jpeg model=gpt-5.6-sol tag=Exif:Make applied=true tolerance=0
2026-07-25T13:05:10 worker=jpeg model=gpt-5.6-sol tag=Exif:Model applied=false tolerance=3
"""

LESSONS = [
    {
        "ts": 1785931300.0,
        "worker": "jpeg",
        "model": "glm5.2-fast",
        "outcome": "review_rejected",
        "tag": "Exif:Make",
        "reason": "fabricated offset",
    },
    {
        "ts": 1785931301.0,
        "worker": "jpeg",
        "model": "glm5.2-fast",
        "outcome": "gap_closed",
        "tag": "Exif:Model",
    },
]


def write_logs(root: Path) -> Path:
    logs = root / "logs"
    (logs / "model-fix-requests").mkdir(parents=True)
    (logs / "model-fix-diffs").mkdir(parents=True)
    (logs / "model-calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in CALLS))
    (logs / "model-fix-requests" / "manifest.log").write_text(MANIFEST)
    (logs / "model-fix-diffs" / "manifest.log").write_text(DIFFS)
    (logs / "lessons.jsonl").write_text("".join(json.dumps(r) + "\n" for r in LESSONS))
    return logs


def test_ingests_every_source(tmp_path: Path) -> None:
    logs = write_logs(tmp_path)
    store = EventStore(tmp_path / "t.sqlite")
    report = ingest(store, sources_for(logs))
    assert report.inserted == 3 + 4 + 2 + 2
    assert store.count() == report.inserted


def test_a_second_ingest_inserts_nothing(tmp_path: Path) -> None:
    """T22's acceptance: re-running produces no duplicates."""
    logs = write_logs(tmp_path)
    store = EventStore(tmp_path / "t.sqlite")
    first = ingest(store, sources_for(logs))
    second = ingest(store, sources_for(logs))
    assert first.inserted > 0
    assert second.inserted == 0
    assert store.count() == first.inserted


def test_a_replay_into_a_fresh_store_is_identical(tmp_path: Path) -> None:
    logs = write_logs(tmp_path)
    a = EventStore(tmp_path / "a.sqlite")
    b = EventStore(tmp_path / "b.sqlite")
    ingest(a, sources_for(logs))
    ingest(b, sources_for(logs))
    ingest(b, sources_for(logs))  # and again, for good measure
    keys_a = [e["dedupe_key"] for e in a.iter_all()]
    keys_b = [e["dedupe_key"] for e in b.iter_all()]
    assert keys_a == keys_b


def test_historical_429s_are_unclassified_never_guessed(tmp_path: Path) -> None:
    """The single most important property of the historical import.

    Assigning these a class would fabricate the exact number P1 exists to
    establish for the first time.
    """
    logs = write_logs(tmp_path)
    store = EventStore(tmp_path / "t.sqlite")
    ingest(store, sources_for(logs))
    rows = [e for e in store.iter_all() if e["source"] == SOURCE_REQUESTS]
    classes = {r["error_class"] for r in rows}
    assert UNCLASSIFIED in classes
    assert "rpm" not in classes
    assert "window_cap" not in classes
    assert "terminal_cap" not in classes


def test_classified_and_historical_429s_are_distinguishable(tmp_path: Path) -> None:
    logs = write_logs(tmp_path)
    store = EventStore(tmp_path / "t.sqlite")
    ingest(store, sources_for(logs))
    counts = store.rate_limits_by_class()
    assert counts["rpm"] == 1  # from model-calls.jsonl
    assert counts["terminal_cap"] == 1  # from model-calls.jsonl
    assert counts[UNCLASSIFIED] == 1  # from the historical manifest


def test_a_non_429_error_keeps_its_status_class(tmp_path: Path) -> None:
    logs = write_logs(tmp_path)
    store = EventStore(tmp_path / "t.sqlite")
    ingest(store, sources_for(logs))
    assert store.rate_limits_by_class()["http_500"] == 1


def test_retry_lines_are_their_own_outcome(tmp_path: Path) -> None:
    # Folding RETRY into success or terminal failure skews the rate; the
    # existing oxidex tooling counts them separately and so does this.
    logs = write_logs(tmp_path)
    store = EventStore(tmp_path / "t.sqlite")
    ingest(store, sources_for(logs))
    assert store.outcome_counts("model_call")["retry"] == 1


def test_unparseable_lines_are_counted_not_hidden(tmp_path: Path) -> None:
    logs = write_logs(tmp_path)
    store = EventStore(tmp_path / "t.sqlite")
    report = ingest(store, sources_for(logs))
    assert report.skipped == 1
    assert "not a manifest line" not in str(report)


def test_missing_sources_are_not_an_error(tmp_path: Path) -> None:
    """A fresh host has no history; a fleet not yet upgraded has no
    model-calls.jsonl. Both are normal."""
    logs = tmp_path / "logs"
    logs.mkdir()
    store = EventStore(tmp_path / "t.sqlite")
    report = ingest(store, sources_for(logs))
    assert report.inserted == 0
    assert store.count() == 0


def test_only_the_new_stream_is_present_when_the_worker_is_upgraded(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "model-calls.jsonl").write_text(json.dumps(CALLS[0]) + "\n")
    store = EventStore(tmp_path / "t.sqlite")
    report = ingest(store, sources_for(logs))
    assert report.by_source == {SOURCE_CALLS: 1}


def test_patch_apply_outcomes(tmp_path: Path) -> None:
    logs = write_logs(tmp_path)
    store = EventStore(tmp_path / "t.sqlite")
    ingest(store, sources_for(logs))
    assert store.outcome_counts("patch_apply") == {"applied": 1, "rejected": 1}


def test_lessons_carry_the_verdict(tmp_path: Path) -> None:
    logs = write_logs(tmp_path)
    store = EventStore(tmp_path / "t.sqlite")
    ingest(store, sources_for(logs))
    outcomes = store.outcome_counts("lesson")
    assert outcomes["review_rejected"] == 1
    assert outcomes["gap_closed"] == 1


def test_manifest_timestamps_are_parsed_as_local_time(tmp_path: Path) -> None:
    # The manifests are stamped with time.strftime -- local, no zone.
    # Parsing as UTC would shift every historical row by the host offset and
    # silently misalign the baseline comparison.
    import datetime

    parsed = parse_local_timestamp("2026-07-25T13:04:11")
    assert parsed is not None
    assert datetime.datetime.fromtimestamp(parsed).hour == 13


def test_a_malformed_timestamp_is_rejected_not_defaulted() -> None:
    assert parse_local_timestamp("yesterday") is None
