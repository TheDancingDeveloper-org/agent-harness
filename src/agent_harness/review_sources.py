"""Metadata-resolved sources for normalized remote review events.

The harness owns review state and correction semantics. A source adapter owns
how an external system authenticates, polls or receives a webhook, and how it
turns that system's record into :class:`RemoteReviewEvent`. Core only sees
the normalized batch and a durable cursor.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Protocol

from .review_events import RemoteReviewEvent, ReviewEventProcessor, ReviewIntakeResult
from .work import WorkQueue

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "agent_harness.review_sources"
API_VERSION = 1
CURSOR_PREFIX = "review-source-cursor:"


@dataclass(frozen=True)
class ReviewBatch:
    """One source response and the cursor that follows it."""

    events: tuple[RemoteReviewEvent, ...] = ()
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if self.next_cursor is not None and not self.next_cursor.strip():
            raise ValueError("a review batch cursor must be non-empty or None")


class ReviewSource(Protocol):
    """A configured, authenticated source of normalized review events."""

    name: str
    api_version: int

    def poll(self, cursor: str | None, /) -> ReviewBatch: ...


def _declared_targets() -> dict[str, str]:
    """Read source metadata without importing any source adapter."""
    try:
        return {point.name: point.value for point in entry_points(group=ENTRY_POINT_GROUP)}
    except Exception:  # noqa: BLE001 - broken metadata is a named readiness failure
        log.warning("could not read %s entry points", ENTRY_POINT_GROUP, exc_info=True)
        return {}


def names() -> list[str]:
    """Return installed source names without loading their implementations."""
    return sorted(_declared_targets())


def resolve(name: str, config: Mapping[str, Any] | None = None) -> ReviewSource:
    """Load one named source and validate its contract before use.

    A declared target may be a source instance or a factory accepting the
    supplied configuration. Configuration is intentionally opaque to core;
    the selected adapter decides which values it needs, including credentials.
    """
    target = _declared_targets().get(name)
    if target is None:
        raise LookupError(
            f"unknown review source {name!r}; installed names: "
            f"{', '.join(names()) or 'none'} ({ENTRY_POINT_GROUP})"
        )
    module_name, _, attribute = target.partition(":")
    try:
        found: Any = getattr(importlib.import_module(module_name), attribute)
        if callable(found) and not callable(getattr(found, "poll", None)):
            found = found(dict(config or {}))
    except Exception as exc:  # noqa: BLE001 - named source must fail before polling
        raise RuntimeError(f"review source {name!r} could not load from {target!r}: {exc}") from exc
    if (
        not callable(getattr(found, "poll", None))
        or getattr(found, "api_version", None) != API_VERSION
    ):
        raise RuntimeError(
            f"review source {name!r} does not implement review-source contract {API_VERSION}"
        )
    return found  # type: ignore[no-any-return]


@dataclass(frozen=True)
class ReviewPollResult:
    """What one poll did, including the cursor now durable in the queue."""

    fetched: int
    accepted: int
    duplicates: int
    cursor: str | None
    results: tuple[ReviewIntakeResult, ...]


class ReviewPoller:
    """Poll one source with crash-safe, duplicate-safe cursor advancement."""

    def __init__(
        self,
        queue: WorkQueue,
        source: ReviewSource,
        *,
        processor: ReviewEventProcessor | None = None,
        on_event: Any | None = None,
    ) -> None:
        if getattr(source, "api_version", None) != API_VERSION:
            raise ValueError(f"review source must implement contract {API_VERSION}")
        if not str(getattr(source, "name", "")).strip():
            raise ValueError("review source needs a non-empty name")
        self.queue = queue
        self.source = source
        self.processor = processor or ReviewEventProcessor(queue, on_event=on_event)
        self.cursor_key = f"{CURSOR_PREFIX}{source.name}"

    @property
    def cursor(self) -> str | None:
        value = self.queue.get_setting(self.cursor_key)
        return str(value) if value else None

    def poll_once(self) -> ReviewPollResult:
        """Process a complete batch before advancing its source cursor.

        If processing raises, the cursor is unchanged. Replaying already
        processed rows is safe because the queue's immutable source identity
        journal makes the processor idempotent.
        """
        batch = self.source.poll(self.cursor)
        if not isinstance(batch, ReviewBatch):
            raise TypeError("review source poll() must return ReviewBatch")
        results = tuple(self.processor.process(event) for event in batch.events)
        if batch.next_cursor is not None:
            self.queue.set_setting(self.cursor_key, batch.next_cursor)
        return ReviewPollResult(
            fetched=len(batch.events),
            accepted=sum(result.accepted for result in results),
            duplicates=sum(result.duplicate for result in results),
            cursor=batch.next_cursor,
            results=results,
        )
