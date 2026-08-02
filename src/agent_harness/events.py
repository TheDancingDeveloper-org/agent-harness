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

from . import providers

# Event kinds. Deliberately coarse: a kind names *what happened*, and the
# detail lives in `data`. Adding a field to `data` never needs a migration;
# adding a kind is a deliberate act.
MODEL_CALL = "model_call"  # one attempt against a model endpoint
PATCH_APPLY = "patch_apply"  # one git-apply of a candidate diff
LESSON = "lesson"  # one semantic outcome (review verdict, gap closed)
WORK = "work"  # one stage transition of a work item (claimed, applied, reviewed…)

KINDS = (MODEL_CALL, PATCH_APPLY, LESSON, WORK)

# Error classes. Defined once, in `providers`, because the classifier and
# the store must never disagree about what a class is called -- two
# definitions of one fact drift, and the drift shows up as a panel silently
# counting nothing.
RPM = providers.RPM
WINDOW_CAP = providers.WINDOW_CAP
TERMINAL_CAP = providers.TERMINAL_CAP
RATE_LIMIT_CLASSES = (RPM, WINDOW_CAP, TERMINAL_CAP)
RATE_LIMIT_MEANING = {k: providers.MEANING[k] for k in RATE_LIMIT_CLASSES}

# The class given to a rate limit recorded before anything classified it --
# by an older build, or by a tool that never read the provider's
# discriminator. It is a statement that the answer is unknowable, not a
# category of rate limit, so it is deliberately NOT in RATE_LIMIT_CLASSES
# and equally not "unknown": we know exactly what it is.
UNCLASSIFIED = "unclassified"

# Errors that are not rate limits. Kept explicit so a class this build has
# genuinely never seen is reported as unknown rather than quietly filed
# alongside them.
KNOWN_OTHER_CLASSES = (
    providers.TRANSIENT,
    providers.NON_RETRYABLE,
    providers.FATAL,
    "connection",
    "deadline",
    "empty_reply",
)


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
