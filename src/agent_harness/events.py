"""The event schema. One append-only record per thing that happened.

Every view in this service is a projection over these rows (plan §3.5).
Nothing writes back. If a panel needs a number the events cannot produce,
the fix is a new event, never a mutable table beside them.

The `dedupe_key` is what makes the ingester replayable (T22): it is derived
from the source record itself, so re-reading the same log line twice yields
the same key and the second insert is dropped by a unique index rather than
by the ingester remembering how far it got. A file offset would not survive
log rotation, truncation, or an out-of-order backfill; a content-derived key
does.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# Event kinds. Deliberately coarse: a kind names *what happened*, and the
# detail lives in `data`. Adding a field to `data` never needs a migration;
# adding a kind is a deliberate act.
MODEL_CALL = "model_call"  # one attempt against a model endpoint
PATCH_APPLY = "patch_apply"  # one git-apply of a candidate diff
LESSON = "lesson"  # one semantic outcome (review verdict, gap closed)

KINDS = (MODEL_CALL, PATCH_APPLY, LESSON)

# Error classes the P1 worker emits. Mirrored rather than imported -- this
# service must be able to ingest a log written by a worker newer than
# itself, so an unrecognised class is stored verbatim and surfaced, never
# coerced into a known one or dropped.
RPM = "rpm"
WINDOW_CAP = "window_cap"
TERMINAL_CAP = "terminal_cap"
RATE_LIMIT_CLASSES = (RPM, WINDOW_CAP, TERMINAL_CAP)

RATE_LIMIT_MEANING = {
    RPM: "going too fast — retried locally with jitter",
    WINDOW_CAP: "5h cost budget spent — not retried, endpoint parked",
    TERMINAL_CAP: "weekly budget spent or key rejected — not retried, endpoint parked",
}

# The class given to a 429 from a log written before the harness read the
# gateway's discriminator. It is a statement that the answer is unknowable,
# not a category of rate limit -- so it is deliberately NOT in
# RATE_LIMIT_CLASSES, and equally not "unknown": we know exactly what it is.
# It lives here rather than in ingest.py so that every consumer classifying
# a class agrees about it without importing the ingester.
UNCLASSIFIED = "unclassified"

# Classes that are errors but not rate limits. Kept explicit so a class this
# build has genuinely never seen is reported as unknown rather than quietly
# filed alongside them.
KNOWN_OTHER_CLASSES = ("connection", "deadline", "empty_reply")


def is_rate_limit(error_class: str | None) -> bool:
    return error_class in RATE_LIMIT_CLASSES


def is_known_class(error_class: str | None) -> bool:
    if error_class is None:
        return True
    return (
        error_class in RATE_LIMIT_CLASSES
        or error_class in KNOWN_OTHER_CLASSES
        or error_class == UNCLASSIFIED
        or error_class.startswith("http_")
    )


@dataclass(frozen=True)
class Event:
    """One thing that happened, as stored.

    `ts` is a unix timestamp. `source` names the log it came from, so a
    panel can say "this row is from the pre-classification manifest, which
    could not tell you the class" instead of showing a silent gap.
    """

    ts: float
    kind: str
    source: str
    worker: str | None = None
    role: str | None = None
    model: str | None = None
    endpoint: str | None = None
    outcome: str | None = None
    error_class: str | None = None
    latency_s: float | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown event kind {self.kind!r}; expected one of {KINDS}")

    @property
    def dedupe_key(self) -> str:
        """Stable identity of the underlying source record.

        Derived from content, not position, so a replay of the same history
        collapses onto the same rows no matter how the file was read.
        `data` is included because two attempts can otherwise be
        indistinguishable -- same worker, same model, same second.
        """
        payload = json.dumps(
            [
                round(self.ts, 3),
                self.kind,
                self.source,
                self.worker,
                self.role,
                self.model,
                self.endpoint,
                self.outcome,
                self.error_class,
                self.data,
            ],
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()
