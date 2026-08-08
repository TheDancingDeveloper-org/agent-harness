"""Generic remote-review event intake.

This module is deliberately upstream-neutral.  A remote adapter translates its
own webhook or polling record into :class:`RemoteReviewEvent`; the harness
only accepts the immutable identity and an explicit disposition.  It never
asks a model to decide whether a human comment is actionable.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .work import WorkQueue

ReviewDisposition = Literal["actionable", "ambiguous", "already_resolved"]


@dataclass(frozen=True)
class RemoteReviewEvent:
    """A normalized, deduplicable remote review observation."""

    source: str
    remote_id: str
    project_id: str
    item_id: str
    disposition: ReviewDisposition
    summary: str
    pr_url: str | None = None
    received_at: float | None = None

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.remote_id.strip():
            raise ValueError("remote review events need a source and immutable remote_id")
        if not self.project_id.strip() or not self.item_id.strip():
            raise ValueError("remote review events need a project_id and item_id")
        if not self.summary.strip():
            raise ValueError("remote review events need a bounded summary")


@dataclass(frozen=True)
class ReviewIntakeResult:
    """What intake did, including whether the source record was a replay."""

    accepted: bool
    duplicate: bool
    status: str
    correction_item_id: str | None = None
    detail: str = ""


class ReviewEventProcessor:
    """Persist and project normalized review events into one project queue."""

    def __init__(
        self,
        queue: WorkQueue,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.queue = queue
        self.on_event = on_event

    def process(self, event: RemoteReviewEvent) -> ReviewIntakeResult:
        original = self.queue.get(event.item_id, project_id=event.project_id)
        if original is None:
            raise ValueError(
                f"remote review names no existing item {event.project_id!r}/{event.item_id!r}"
            )
        correction_id = self._correction_id(event)
        if event.disposition == "actionable":
            status = "queued"
            title = f"Address remote review for {event.item_id}"
            brief = (
                f"Address this externally reported review feedback for item {event.item_id}.\n\n"
                f"Feedback summary: {event.summary}"
            )
            state = "pending"
            last_error = None
        elif event.disposition == "ambiguous":
            # Do not hand an ambiguous human comment to an agent.  It is a
            # durable exception for a person to resolve; the eventual hold or
            # correction action is explicit and cannot be inferred here.
            status = "needs_human"
            title = f"Resolve ambiguous remote review for {event.item_id}"
            brief = event.summary
            state = "blocked"
            last_error = "ambiguous remote review requires a human decision"
        else:
            status = "already_resolved"
            title = f"Remote review already resolved for {event.item_id}"
            brief = event.summary
            state = "done"
            last_error = None

        accepted, previous = self.queue.accept_remote_review(
            source=event.source,
            remote_id=event.remote_id,
            project_id=event.project_id,
            item_id=event.item_id,
            disposition=event.disposition,
            status=status,
            correction_item_id=correction_id if event.disposition != "already_resolved" else None,
            title=title,
            brief=brief,
            depends_on=[event.item_id] if event.disposition != "already_resolved" else [],
            state=state,
            last_error=last_error,
            received_at=event.received_at,
        )
        if not accepted:
            if previous is None:  # pragma: no cover - INSERT OR IGNORE guarantees a row
                raise RuntimeError("remote review duplicate had no durable row")
            result = ReviewIntakeResult(
                accepted=False,
                duplicate=True,
                status=str(previous["status"]),
                correction_item_id=previous["correction_item_id"],
                detail="remote review event was already recorded",
            )
        else:
            if event.disposition == "ambiguous" and correction_id is not None:
                self.queue.hold_pending(
                    correction_id,
                    question="How should this remote review feedback be handled?",
                    reason=event.summary,
                    project_id=event.project_id,
                )
            detail = (
                "queued correction work"
                if status == "queued"
                else "held for explicit human resolution"
                if status == "needs_human"
                else "recorded without creating work"
            )
            result = ReviewIntakeResult(
                accepted=True,
                duplicate=False,
                status=status,
                correction_item_id=(
                    correction_id if event.disposition != "already_resolved" else None
                ),
                detail=detail,
            )
        self._emit(event, result)
        return result

    @staticmethod
    def _correction_id(event: RemoteReviewEvent) -> str:
        import hashlib

        digest = hashlib.sha256(f"{event.source}\0{event.remote_id}".encode()).hexdigest()[:24]
        return f"review-{digest}"

    def _emit(self, event: RemoteReviewEvent, result: ReviewIntakeResult) -> None:
        if self.on_event is None:
            return
        with contextlib.suppress(Exception):
            self.on_event(
                {
                    "ts": event.received_at,
                    "kind": "work",
                    "outcome": "remote_review_received",
                    "project_id": event.project_id,
                    "item_id": event.item_id,
                    "source": event.source,
                    "remote_id": event.remote_id,
                    "disposition": event.disposition,
                    "status": result.status,
                    "duplicate": result.duplicate,
                    "correction_item_id": result.correction_item_id,
                    "detail": result.detail,
                }
            )
