"""JSON API tests. In-process over ASGI; no server, no ports."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.api import create_api
from agent_harness.events import MODEL_CALL, UNCLASSIFIED, WORK, Event
from agent_harness.store import EventStore
from agent_harness.work import CLAIMED, DONE, PENDING, WorkQueue, WorkRecord

TOKEN = "test-token"  # noqa: S105 - a fixture, not a credential


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "e.sqlite")


@pytest.fixture
def queue(tmp_path: Path) -> WorkQueue:
    q = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    q.add(
        [
            WorkRecord(item_id="W1", title="First", brief="do the first thing", issue=1),
            WorkRecord(item_id="W2", title="Second", brief="do the second thing"),
        ]
    )
    return q


@pytest.fixture
def client(store: EventStore, queue: WorkQueue) -> Iterator[TestClient]:
    with TestClient(create_api(store, queue=queue, token=TOKEN)) as c:
        yield c


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


# ------------------------------------------------------------------- auth


def test_healthz_is_open(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("path", ["/api/work", "/api/errors", "/api/events", "/api/summary"])
def test_every_other_route_requires_a_token(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


def test_no_token_configured_fails_closed(store: EventStore) -> None:
    with TestClient(create_api(store, token=None)) as c:
        assert c.get("/api/work").status_code == 503
        assert c.get("/healthz").status_code == 200


def test_there_is_no_html_anywhere(client: TestClient) -> None:
    """The GUI is MyDevEnv2's. If HTML creeps back in, so does a second UI."""
    response = client.get("/api/work", headers=auth())
    assert response.headers["content-type"].startswith("application/json")
    assert client.get("/").status_code == 404


# ------------------------------------------------------------------- work


def test_work_returns_items_counts_and_stale_in_one_call(client: TestClient) -> None:
    """One request, not three: a phone on a flaky connection should not need
    a successful fan-out to show anything."""
    payload = client.get("/api/work", headers=auth()).json()
    assert payload["configured"] is True
    assert {i["item_id"] for i in payload["items"]} == {"W1", "W2"}
    assert payload["counts"] == {PENDING: 2}
    assert payload["stale"] == []


def test_work_carries_the_session_deep_link(client: TestClient, store: EventStore) -> None:
    """The whole point of the Work tab: click an item, land in the terminal
    that is doing it."""
    store.append(
        [
            Event(
                ts=time.time(),
                kind=WORK,
                source="events.jsonl",
                worker="w",
                outcome="agent_started",
                data={
                    "item_id": "W1",
                    "session_id": "s-9",
                    "session_url": "https://dev.example/t/s-9",
                },
            )
        ]
    )
    items = {i["item_id"]: i for i in client.get("/api/work", headers=auth()).json()["items"]}
    assert items["W1"]["latest"]["session_url"] == "https://dev.example/t/s-9"
    assert items["W2"]["latest"] is None


def test_work_says_so_when_no_queue_is_attached(store: EventStore) -> None:
    with TestClient(create_api(store, queue=None, token=TOKEN)) as c:
        payload = c.get("/api/work", headers=auth()).json()
    assert payload["configured"] is False
    assert "no work queue" in payload["reason"]


# ------------------------------------------------------------------ retry


def test_retry_puts_a_failed_item_back(client: TestClient, queue: WorkQueue) -> None:
    queue.claim("w")
    queue.release("W1", DONE)
    assert client.post("/api/work/W1/retry", headers=auth()).status_code == 200
    assert queue.get("W1").state == PENDING  # type: ignore[union-attr]


def test_retry_refuses_an_item_with_a_live_claim(client: TestClient, queue: WorkQueue) -> None:
    """Yanking an item out from under a live agent gives two workers on one
    item, which is worse than one stuck item."""
    queue.claim("worker-a")
    response = client.post("/api/work/W1/retry", headers=auth())
    assert response.status_code == 409
    assert "worker-a" in response.json()["detail"]
    assert queue.get("W1").state == CLAIMED  # type: ignore[union-attr]


def test_retry_allows_an_item_whose_lease_expired(tmp_path: Path, store: EventStore) -> None:
    """A stale claim means the worker is gone; it needs no ceremony."""
    clock = [1000.0]
    q = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=10.0, now=lambda: clock[0])
    q.add([WorkRecord(item_id="W1", title="t", brief="b")])
    q.claim("gone")
    clock[0] += 100
    with TestClient(create_api(store, queue=q, token=TOKEN)) as c:
        assert c.post("/api/work/W1/retry", headers=auth()).status_code == 200


def test_retry_on_an_unknown_item_is_404(client: TestClient) -> None:
    assert client.post("/api/work/NOPE/retry", headers=auth()).status_code == 404


def test_retry_requires_a_token(client: TestClient) -> None:
    assert client.post("/api/work/W1/retry").status_code == 401


# ----------------------------------------------------------------- errors


def test_errors_splits_by_class_and_keeps_unclassified_separate(
    client: TestClient, store: EventStore
) -> None:
    now = time.time()
    store.append(
        [
            Event(ts=now, kind=MODEL_CALL, source="s", outcome="error", error_class="rpm"),
            Event(
                ts=now - 1, kind=MODEL_CALL, source="s", outcome="error", error_class="terminal_cap"
            ),
            Event(
                ts=now - 2, kind=MODEL_CALL, source="old", outcome="error", error_class=UNCLASSIFIED
            ),
        ]
    )
    payload = client.get("/api/errors?window=24h", headers=auth()).json()
    assert payload["classified"]["rpm"] == 1
    assert payload["classified"]["terminal_cap"] == 1
    assert payload["unclassified"] == 1
    assert payload["total"] == 2  # unclassified is NOT folded in
    assert "not retried" in payload["meaning"]["terminal_cap"]


def test_an_unknown_window_is_refused(client: TestClient) -> None:
    assert client.get("/api/errors?window=fortnight", headers=auth()).status_code == 400


# ----------------------------------------------------------------- events


def test_events_page_forward_by_row_id(client: TestClient, store: EventStore) -> None:
    """Cursor is the row id, not a timestamp: two events in one millisecond
    must still have a total order or a poll silently drops one."""
    now = time.time()
    store.append(
        [
            Event(ts=now, kind=WORK, source="s", outcome="a", data={"n": 1}),
            Event(ts=now, kind=WORK, source="s", outcome="b", data={"n": 2}),
        ]
    )
    first = client.get("/api/events?since_id=0&limit=1", headers=auth()).json()
    assert len(first["events"]) == 1
    second = client.get(f"/api/events?since_id={first['cursor']}", headers=auth()).json()
    assert [e["outcome"] for e in second["events"]] == ["b"]


def test_an_empty_poll_keeps_the_cursor(client: TestClient) -> None:
    payload = client.get("/api/events?since_id=99", headers=auth()).json()
    assert payload["events"] == []
    assert payload["cursor"] == 99


# ---------------------------------------------------------------- summary


def test_summary_is_enough_for_a_tab_badge(client: TestClient, queue: WorkQueue) -> None:
    queue.claim("w")
    payload = client.get("/api/summary", headers=auth()).json()
    assert payload["running"] == 1
    assert payload["pending"] == 1


def test_summary_surfaces_an_agent_waiting_on_a_human(
    client: TestClient, store: EventStore
) -> None:
    """The one thing that genuinely needs a person, so it gets its own field
    rather than being buried in a count."""
    store.append(
        [
            Event(
                ts=time.time(),
                kind=WORK,
                source="s",
                worker="w",
                outcome="waiting_for_input",
                data={"item_id": "W1", "session_url": "https://dev.example/t/s-1"},
            )
        ]
    )
    payload = client.get("/api/summary", headers=auth()).json()
    assert payload["waiting_for_input"][0]["item_id"] == "W1"
    assert payload["waiting_for_input"][0]["session_url"].endswith("/t/s-1")
