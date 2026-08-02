"""Ingest sources into the event store. Never writes to the sources.

Generic: it takes `Source`s and does not care where they came from or what
tool wrote them. Format knowledge lives in the reader on each source, and
project knowledge lives only in adapters.

Idempotence comes from each event's content-derived `dedupe_key`, not from
remembering how far into a file we got. That choice is deliberate — an
offset does not survive rotation, truncation, or an out-of-order backfill,
whereas re-reading the same content twice always collapses onto the same
row. Re-running a full ingest is therefore a no-op, which is what makes it
safe on a timer, safe after a crash, and safe to run twice by mistake.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .events import Event
from .sources import Source
from .store import EventStore


@dataclass
class IngestReport:
    """What a run actually did.

    `skipped` is never silent: a record the parser could not read is a fact
    about the measurement, and a report that hides it invites confident
    conclusions drawn from half the data.
    """

    inserted: int = 0
    seen: int = 0
    skipped: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    skipped_by_source: dict[str, int] = field(default_factory=dict)

    def __str__(self) -> str:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(self.by_source.items()))
        summary = (
            f"ingested {self.inserted} new of {self.seen} records ({self.skipped} unparseable)"
        )
        return f"{summary} [{parts}]" if parts else summary


def ingest(store: EventStore, sources: Iterable[Source], batch: int = 2000) -> IngestReport:
    """Read every source into the store. Safe to re-run.

    A source whose file is missing is skipped rather than raising: on a host
    that has not produced that log yet, absence is a normal state, not a
    failure.
    """
    report = IngestReport()
    for source in sources:
        if not source.path.exists():
            continue
        pending: list[Event] = []
        inserted = 0
        skipped = 0
        for event in source.reader(source.path):
            report.seen += 1
            if event is None:
                skipped += 1
                continue
            pending.append(event)
            if len(pending) >= batch:
                inserted += store.append(pending)
                pending.clear()
        inserted += store.append(pending)
        report.inserted += inserted
        report.skipped += skipped
        report.by_source[source.name] = inserted
        if skipped:
            report.skipped_by_source[source.name] = skipped
    return report
