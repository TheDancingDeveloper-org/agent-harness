"""Where events come from. Nothing here knows about any particular project.

The harness's own event stream is newline-delimited JSON, one object per
model-call attempt — the shape `ModelClient` emits. That is the only format
this module needs to understand.

Anything else is an **adapter**: a reader that translates some other tool's
log into `Event`s. Adapters are opt-in and live in `agent_harness.adapters`,
so a workload that does not use them never pays for them and the core never
grows a dependency on one project's file layout. See
`adapters/oxidex.py` for a worked example of adapting a legacy text log,
including how to represent information the old format simply did not record.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .events import MODEL_CALL, Event

#: A reader turns a file into events. Yielding None marks a record the reader
#: could not parse — counted, never silently dropped, because a log the
#: parser cannot read is a fact about the measurement.
Reader = Callable[[Path], Iterator["Event | None"]]


@dataclass(frozen=True)
class Source:
    """One file to ingest, and how to read it."""

    name: str
    path: Path
    reader: Reader


def iter_lines(path: Path) -> Iterator[str]:
    try:
        with path.open(errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    yield stripped
    except OSError:
        return


def read_harness_events(path: Path) -> Iterator[Event | None]:
    """The harness's own stream: one JSON object per attempt.

    Every field is optional except `ts`, so a stream written by a newer
    version — with fields this build has never heard of — still ingests. The
    unknown fields are preserved in `data` rather than discarded, because the
    alternative is losing information that the writer thought worth
    recording.
    """
    known = {
        "ts",
        "role",
        "model",
        "endpoint",
        "outcome",
        "error_class",
        "latency_s",
        "worker",
        "kind",
        "source",
    }
    for line in iter_lines(path):
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
            kind=record.get("kind") or MODEL_CALL,
            source=record.get("source") or path.name,
            worker=record.get("worker"),
            role=record.get("role"),
            model=record.get("model"),
            endpoint=record.get("endpoint"),
            outcome=record.get("outcome"),
            error_class=record.get("error_class"),
            latency_s=record.get("latency_s"),
            data={k: v for k, v in record.items() if k not in known},
        )


def harness_source(path: Path | str, name: str | None = None) -> Source:
    path = Path(path)
    return Source(name=name or path.name, path=path, reader=read_harness_events)
