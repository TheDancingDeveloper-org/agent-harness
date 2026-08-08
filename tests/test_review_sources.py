from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_harness.api import create_api
from agent_harness.review_events import RemoteReviewEvent
from agent_harness.review_sources import ReviewBatch, ReviewPoller
from agent_harness.store import EventStore
from agent_harness.work import PENDING, Project, WorkQueue, WorkRecord


def queue_for(tmp_path: Path) -> WorkQueue:
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project("p", "project"))
    queue.add([WorkRecord("T1", "original")], project_id="p")
    return queue


def review(remote_id: str) -> RemoteReviewEvent:
    return RemoteReviewEvent(
        source="configured-source",
        remote_id=remote_id,
        project_id="p",
        item_id="T1",
        disposition="actionable",
        summary="Please update the implementation.",
    )


class Source:
    name = "configured-source"
    api_version = 1

    def __init__(self, batches: list[ReviewBatch]) -> None:
        self.batches = batches
        self.cursors: list[str | None] = []

    def poll(self, cursor: str | None) -> ReviewBatch:
        self.cursors.append(cursor)
        return self.batches.pop(0)


def test_poller_advances_cursor_only_after_the_batch_is_durable(tmp_path: Path) -> None:
    queue = queue_for(tmp_path)
    source = Source([ReviewBatch((review("r1"),), next_cursor="cursor-1")])
    poller = ReviewPoller(queue, source)

    result = poller.poll_once()

    assert result.fetched == 1
    assert result.accepted == 1
    assert result.duplicates == 0
    assert poller.cursor == "cursor-1"
    correction = queue.get(result.results[0].correction_item_id or "", project_id="p")
    assert correction is not None and correction.state == PENDING


def test_poller_replay_is_safe_and_uses_the_saved_cursor(tmp_path: Path) -> None:
    queue = queue_for(tmp_path)
    source = Source(
        [
            ReviewBatch((review("r1"),), next_cursor="cursor-1"),
            ReviewBatch((review("r1"),), next_cursor="cursor-2"),
        ]
    )
    poller = ReviewPoller(queue, source)

    first = poller.poll_once()
    second = poller.poll_once()

    assert first.accepted == 1
    assert second.accepted == 0
    assert second.duplicates == 1
    assert source.cursors == [None, "cursor-1"]
    assert poller.cursor == "cursor-2"
    assert len(queue.items(project_id="p")) == 2


def test_poller_leaves_cursor_unchanged_when_processing_fails(tmp_path: Path) -> None:
    queue = queue_for(tmp_path)
    source = Source([ReviewBatch((review("r1"),), next_cursor="cursor-1")])

    class FailingProcessor:
        def process(self, event: RemoteReviewEvent) -> Any:
            raise RuntimeError("normalization sink unavailable")

    poller = ReviewPoller(queue, source, processor=FailingProcessor())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="sink unavailable"):
        poller.poll_once()
    assert poller.cursor is None


def test_api_poll_is_typed_and_requires_a_configured_source(tmp_path: Path) -> None:
    queue = queue_for(tmp_path)
    source = Source([ReviewBatch((review("api-r1"),), next_cursor="api-cursor")])
    poller = ReviewPoller(queue, source)
    client = TestClient(
        create_api(
            EventStore(tmp_path / "events.sqlite"),
            queue=queue,
            review_poller=poller,
            token="token",
        )
    )

    response = client.post("/api/review-poll", headers={"Authorization": "Bearer token"})

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert response.json()["cursor"] == "api-cursor"
