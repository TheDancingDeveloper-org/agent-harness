"""Read the harness's logs into the event store. Never writes to them.

Four sources, three of which predate the P1 classification work and one of
which does not:

  model-calls.jsonl                    P1's structured stream. Has error_class.
  model-fix-requests/manifest.log      the historical record. Does NOT.
  model-fix-diffs/manifest.log         patch-apply outcomes.
  lessons.jsonl                        semantic outcomes (verdicts, gaps).

The distinction matters more than any parsing detail. The 27,662 rate-limit
errors in the historical manifest cannot be broken down by class, because
nothing recorded the discriminator at the time and no amount of re-parsing
recovers it. They are ingested as `error_class='unclassified'` rather than
guessed at or dropped: a viewer must be able to see that the gap exists.
Inventing a class for them would be inventing the very number P1 exists to
produce.

Idempotence (T22) comes from the events' content-derived `dedupe_key`, not
from remembering file offsets. Re-running a full ingest over the same files
inserts nothing the second time, and a truncated or rotated log cannot cause
either duplication or loss.
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .events import LESSON, MODEL_CALL, PATCH_APPLY, UNCLASSIFIED, Event
from .store import EventStore

__all__ = ["UNCLASSIFIED", "IngestReport", "ingest", "parse_local_timestamp"]

SOURCE_CALLS = "model-calls.jsonl"
SOURCE_REQUESTS = "model-fix-requests/manifest.log"
SOURCE_DIFFS = "model-fix-diffs/manifest.log"
SOURCE_LESSONS = "lessons.jsonl"

# `2026-07-25T13:04:11 phase=fixer worker=jpeg tier=T1 provider=clawbay
#  model=gpt-5.6-sol prompt_chars=8123 elapsed=12.4s reply_chars=900 OK`
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
_FIELD_RE = re.compile(r"(\w+)=(\S+)")


@dataclass
class IngestReport:
    """What an ingest run actually did. `skipped` is never silent: a line
    the parser could not read is a fact about the measurement."""

    inserted: int = 0
    seen: int = 0
    skipped: int = 0
    by_source: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if self.by_source is None:
            self.by_source = {}

    def __str__(self) -> str:
        parts = ", ".join(f"{k}={v}" for k, v in sorted((self.by_source or {}).items()))
        return (
            f"ingested {self.inserted} new of {self.seen} records "
            f"({self.skipped} unparseable) [{parts}]"
        )


def parse_local_timestamp(text: str) -> float | None:
    """The manifests are stamped with `time.strftime` -- local time, no
    zone. Parsing them as UTC would shift every historical row by the
    host's offset and silently misalign the baseline comparison."""
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").timestamp()
    except ValueError:
        return None


def _fields(line: str) -> dict[str, str]:
    return dict(_FIELD_RE.findall(line))


def read_model_calls(path: Path) -> Iterator[Event | None]:
    """P1's structured stream. One record per attempt, already classified."""
    for line in _lines(path):
        try:
            record = json.loads(line)
        except ValueError:
            yield None
            continue
        if not isinstance(record, dict) or not isinstance(record.get("ts"), (int, float)):
            yield None
            continue
        yield Event(
            ts=float(record["ts"]),
            kind=MODEL_CALL,
            source=SOURCE_CALLS,
            worker=record.get("worker"),
            role=record.get("role"),
            model=record.get("model"),
            endpoint=record.get("endpoint"),
            outcome=record.get("outcome"),
            error_class=record.get("error_class"),
            latency_s=record.get("latency_s"),
            data={k: v for k, v in record.items() if k in ("attempt", "detail")},
        )


def read_request_manifest(path: Path) -> Iterator[Event | None]:
    """The historical manifest. Every 429 here is `unclassified` -- see the
    module docstring. RETRY lines are ingested as their own outcome so they
    are never folded into either success or terminal failure."""
    for line in _lines(path):
        stamp = _TS_RE.match(line)
        if not stamp:
            yield None
            continue
        ts = parse_local_timestamp(stamp.group(1))
        if ts is None:
            yield None
            continue
        fields = _fields(line)
        if " RETRY " in line:
            outcome, error_class = "retry", None
        elif "ERROR=" in line:
            outcome = "error"
            error_class = UNCLASSIFIED if "429" in line else _status_class(line)
        else:
            outcome, error_class = "ok", None
        elapsed = fields.get("elapsed", "").rstrip("s")
        yield Event(
            ts=ts,
            kind=MODEL_CALL,
            source=SOURCE_REQUESTS,
            worker=fields.get("worker"),
            role=fields.get("phase"),
            model=fields.get("model"),
            endpoint=fields.get("provider"),
            outcome=outcome,
            error_class=error_class,
            latency_s=_maybe_float(elapsed),
            data={k: v for k, v in fields.items() if k in ("tier", "prompt_chars", "reply_chars")},
        )


def read_diff_manifest(path: Path) -> Iterator[Event | None]:
    """Patch-apply outcomes -- the §2.5 apply-rate term."""
    for line in _lines(path):
        stamp = _TS_RE.match(line)
        if not stamp:
            yield None
            continue
        ts = parse_local_timestamp(stamp.group(1))
        if ts is None:
            yield None
            continue
        fields = _fields(line)
        applied = fields.get("applied", "").lower()
        if applied in ("true", "1", "yes"):
            outcome = "applied"
        elif applied in ("false", "0", "no"):
            outcome = "rejected"
        else:
            outcome = "unknown"
        yield Event(
            ts=ts,
            kind=PATCH_APPLY,
            source=SOURCE_DIFFS,
            worker=fields.get("worker"),
            model=fields.get("model"),
            outcome=outcome,
            data={k: v for k, v in fields.items() if k in ("tag", "tolerance", "diff")},
        )


def read_lessons(path: Path) -> Iterator[Event | None]:
    """Semantic outcomes: review verdicts, closed gaps. The `review_rejected`
    rate the plan's D9 A/B needs comes from here."""
    for line in _lines(path):
        try:
            record = json.loads(line)
        except ValueError:
            yield None
            continue
        if not isinstance(record, dict):
            yield None
            continue
        ts = record.get("ts") or record.get("timestamp")
        if isinstance(ts, str):
            ts = parse_local_timestamp(ts[:19])
        if not isinstance(ts, (int, float)):
            yield None
            continue
        yield Event(
            ts=float(ts),
            kind=LESSON,
            source=SOURCE_LESSONS,
            worker=record.get("worker"),
            model=record.get("model"),
            outcome=record.get("outcome") or record.get("result"),
            data={k: v for k, v in record.items() if k in ("tag", "reason", "verdict", "format")},
        )


READERS = {
    SOURCE_CALLS: read_model_calls,
    SOURCE_REQUESTS: read_request_manifest,
    SOURCE_DIFFS: read_diff_manifest,
    SOURCE_LESSONS: read_lessons,
}


def ingest(store: EventStore, logs_dir: Path, batch: int = 2000) -> IngestReport:
    """Ingest every known source under `logs_dir`. Idempotent by
    construction -- running it twice over unchanged files inserts nothing.

    A source that does not exist is not an error: a fresh host has no
    history, and a fleet that has not been upgraded yet has no
    model-calls.jsonl. Both are normal and neither should fail the run.
    """
    report = IngestReport()
    for name, reader in READERS.items():
        path = logs_dir / name
        if not path.exists():
            continue
        pending: list[Event] = []
        inserted = 0
        for event in reader(path):
            report.seen += 1
            if event is None:
                report.skipped += 1
                continue
            pending.append(event)
            if len(pending) >= batch:
                inserted += store.append(pending)
                pending.clear()
        inserted += store.append(pending)
        report.inserted += inserted
        assert report.by_source is not None
        report.by_source[name] = inserted
    return report


def _lines(path: Path) -> Iterator[str]:
    try:
        with path.open(errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield line
    except OSError:
        return


def _maybe_float(text: Any) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _status_class(line: str) -> str | None:
    match = re.search(r"\b(4\d\d|5\d\d)\b", line)
    return f"http_{match.group(1)}" if match else None
