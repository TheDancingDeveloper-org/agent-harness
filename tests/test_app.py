"""App tests, in-process over ASGI — no server, no ports (§6.1)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness.app import BASELINE_429_TOTAL, create_app
from agent_harness.events import MODEL_CALL, Event
from agent_harness.ingest import UNCLASSIFIED
from agent_harness.store import EventStore

TOKEN = "test-token"  # noqa: S105 - a fixture, not a credential


def call(ts: float, **kw: object) -> Event:
    base: dict[str, object] = {
        "ts": ts,
        "kind": MODEL_CALL,
        "source": "model-calls.jsonl",
        "worker": "jpeg",
        "role": "fixer",
        "model": "m",
        "endpoint": "https://gw",
        "outcome": "ok",
        "error_class": None,
        "latency_s": 1.0,
        "data": {},
    }
    base.update(kw)
    return Event(**base)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    import time

    now = time.time()
    store = EventStore(tmp_path / "t.sqlite")
    store.append(
        [
            call(now - 60, outcome="error", error_class="rpm"),
            call(now - 61, outcome="error", error_class="rpm"),
            call(now - 62, outcome="error", error_class="terminal_cap"),
            call(
                now - 63,
                outcome="error",
                error_class=UNCLASSIFIED,
                source="model-fix-requests/manifest.log",
            ),
            call(now - 64, outcome="ok"),
        ]
    )
    return store


@pytest.fixture
def client(store: EventStore) -> Iterator[TestClient]:
    # A short stream lifetime so the SSE tests terminate. Reconnect is
    # gapless via Last-Event-ID, which is exactly what production relies on.
    app = create_app(store, token=TOKEN, stream_max_seconds=2.0, stream_poll_seconds=0.05)
    with TestClient(app) as c:
        yield c


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def flat(text: str) -> str:
    """Collapse whitespace so an assertion on prose is not hostage to where
    the template happens to wrap a line."""
    return " ".join(text.split())


# ------------------------------------------------------------------ T20


def test_health_is_open_and_reports_the_store(client: httpx.Client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["events"] == 5


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/panel/errors",
        "/panel/fleet",
        "/panel/pipeline",
        "/panel/quota",
        "/panel/verdicts",
        "/api/errors",
    ],
)
def test_every_other_route_requires_a_token(client: httpx.Client, path: str) -> None:
    assert client.get(path).status_code == 401


def test_a_wrong_token_is_rejected(client: httpx.Client) -> None:
    assert client.get("/", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_a_service_with_no_token_configured_fails_closed(store: EventStore) -> None:
    """Coming up open because auth was left unset is not an acceptable
    default for something on a network."""
    with TestClient(create_app(store, token=None)) as c:
        assert c.get("/").status_code == 503
        assert c.get("/health").status_code == 200


def test_authenticated_requests_succeed(client: httpx.Client) -> None:
    for path in (
        "/",
        "/panel/errors",
        "/panel/fleet",
        "/panel/pipeline",
        "/panel/quota",
        "/panel/verdicts",
    ):
        response = client.get(path, headers=auth())
        assert response.status_code == 200, path
        assert "agent-harness" in response.text


# ------------------------------------------------------------------ T23


def test_the_errors_panel_breaks_429s_out_by_class(client: httpx.Client) -> None:
    body = client.get("/panel/errors?window=24h", headers=auth()).text
    assert "rpm" in body
    assert "terminal_cap" in body
    assert "window_cap" in body


def test_the_errors_panel_reports_the_baseline(client: httpx.Client) -> None:
    body = client.get("/panel/errors", headers=auth()).text
    assert str(BASELINE_429_TOTAL) in body


def test_the_errors_panel_refuses_to_imply_a_per_class_delta(
    client: httpx.Client,
) -> None:
    """The panel's second job, and the one easiest to lose in a redesign."""
    body = flat(client.get("/panel/errors", headers=auth()).text)
    assert "Do not report a per-class delta" in body
    assert "unclassified" in body


def test_historical_rate_limits_are_shown_as_unrecoverable_not_missing(
    client: httpx.Client,
) -> None:
    body = flat(client.get("/panel/errors", headers=auth()).text)
    assert "does not exist and cannot be recovered" in body


def test_api_errors_flags_the_baseline_as_unclassified(client: httpx.Client) -> None:
    payload = client.get("/api/errors?window=24h", headers=auth()).json()
    assert payload["classified"]["rpm"] == 2
    assert payload["classified"]["terminal_cap"] == 1
    assert payload["unclassified"] == 1
    # A consumer must not be able to mistake the baseline for a breakdown.
    assert payload["baseline"]["classified"] is False


def test_an_unknown_window_is_refused(client: httpx.Client) -> None:
    assert client.get("/?window=fortnight", headers=auth()).status_code == 400


def test_an_empty_store_renders_rather_than_erroring(tmp_path: Path) -> None:
    with TestClient(create_app(EventStore(tmp_path / "empty.sqlite"), token=TOKEN)) as c:
        response = c.get("/", headers=auth())
        assert response.status_code == 200
        assert "No rate limits in this window" in response.text


# ------------------------------------------------------------------ T28


def test_the_stream_requires_a_token(client: httpx.Client) -> None:
    assert client.get("/events/stream").status_code == 401


def test_the_stream_replays_from_last_event_id(client: httpx.Client) -> None:
    """Gapless resume is the whole point of Last-Event-ID; the cursor is the
    row id and not a timestamp so that two events in one millisecond still
    have an order."""
    with client.stream(
        "GET", "/events/stream", headers={**auth(), "Last-Event-ID": "0"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = list(response.iter_lines())
    ids = [line for line in lines if line.startswith("id: ")]
    assert ids == [f"id: {n}" for n in range(1, 6)]


def test_the_stream_without_a_cursor_starts_from_now(client: httpx.Client) -> None:
    # A fresh tab must not be flooded with the entire backlog.
    with client.stream("GET", "/events/stream", headers=auth()) as response:
        lines = list(response.iter_lines())
    assert not [line for line in lines if line.startswith("id: ")]


def test_the_stream_is_recycled_rather_than_held_open_forever(
    client: httpx.Client,
) -> None:
    """It must end on its own: a stream that never closes cannot be drained
    on shutdown and pins a thread per idle tab."""
    with client.stream("GET", "/events/stream", headers=auth()) as response:
        lines = list(response.iter_lines())
    assert any(line.startswith(":") for line in lines)  # keepalives, then EOF


def test_the_stream_tells_the_client_how_long_to_wait_before_reconnecting(
    client: httpx.Client,
) -> None:
    with client.stream("GET", "/events/stream", headers=auth()) as response:
        first = next(response.iter_lines())
        response.close()
    assert first.startswith("retry:")


def test_unclassified_is_not_reported_as_an_unrecognised_class(
    client: httpx.Client,
) -> None:
    """It is the opposite of unrecognised: we know exactly what it is, and
    saying otherwise sends someone hunting a version mismatch that isn't
    there."""
    body = flat(client.get("/panel/errors?window=24h", headers=auth()).text)
    assert "unclassified" in body
    assert "does not recognise" not in body


def test_times_are_rendered_for_humans_not_as_epochs(client: httpx.Client) -> None:
    from agent_harness.app import _ago, _when

    assert _when(0) == "—"
    assert _when(1785931200.0).startswith("20")
    assert _ago(1000.0, now=1030.0) == "30s ago"
    assert _ago(1000.0, now=1000.0 + 600) == "10m ago"
    assert _ago(1000.0, now=1000.0 + 7200) == "2h ago"
    assert _ago(1000.0, now=1000.0 + 3 * 86400) == "3d ago"


def test_the_quota_panel_says_why_spend_is_absent_not_just_that_it_is(
    client: httpx.Client,
) -> None:
    """An empty box reads as zero spend. The panel has to distinguish
    'not built yet' from 'the endpoint the plan assumed does not exist'."""
    body = flat(client.get("/panel/quota", headers=auth()).text)
    assert "not merely unimplemented" in body
    assert "404" in body
    assert "after the fact" in body
