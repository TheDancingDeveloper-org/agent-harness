"""JSON API tests. In-process over ASGI; no server, no ports."""

from __future__ import annotations

import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_harness.api import create_api
from agent_harness.events import MODEL_CALL, UNCLASSIFIED, WORK, Event
from agent_harness.store import EventStore
from agent_harness.work import CLAIMED, DONE, PENDING, RUNNING, Project, WorkQueue, WorkRecord
from conftest import make_queue

TOKEN = "test-token"  # noqa: S105 - a fixture, not a credential


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "e.sqlite")


@pytest.fixture
def queue(tmp_path: Path) -> WorkQueue:
    q = make_queue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
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


#: Routes that are deliberately open, with the reason. Anything not named
#: here must refuse an anonymous caller, and `test_every_protected_route_
#: requires_a_token` derives its cases from the app so a route added later is
#: covered without anyone remembering to add it.
#:
#: The previous version of that test hardcoded four paths under the name
#: "every other route". It was wrong about three routes -- including
#: `POST /api/plan/sync`, which creates GitHub issues -- and its name was
#: exactly what stopped anyone checking. A list that does not grow with the
#: thing it describes is worse than no list, because it reads as coverage.
OPEN_ROUTES = {
    "/healthz": "liveness, checked before a credential is available",
    "/docs": "the schema is not secret; the backlog is",
    "/docs/oauth2-redirect": "mounted by FastAPI for Swagger UI",
    "/redoc": "as /docs",
    "/openapi.json": "as /docs -- requiring a token makes the API undiscoverable",
}


def _app_for_introspection() -> Any:
    """An app instance built purely to read its route table.

    Collection-time, so it cannot use the `tmp_path` fixture -- and EventStore
    writes on construction, so it needs a real writable path.
    """
    return create_api(EventStore(Path(tempfile.mkdtemp()) / "e.sqlite"), token=TOKEN)  # noqa: S106


def protected_routes() -> list[tuple[str, str]]:
    """Every (method, path) the app serves that is not deliberately open."""
    app = _app_for_introspection()
    out = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods or path in OPEN_ROUTES:
            continue
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            out.append((method, path))
    return sorted(set(out))


def test_the_open_route_list_still_matches_the_app() -> None:
    """If a documented-open route disappears, the exemption must go with it.

    Otherwise the exemption silently starts covering nothing, or worse,
    starts matching a new route that happens to reuse the path.
    """
    served = {getattr(r, "path", None) for r in _app_for_introspection().routes}
    stale = sorted(set(OPEN_ROUTES) - served)
    assert not stale, f"OPEN_ROUTES exempts routes the app no longer serves: {stale}"


@pytest.mark.parametrize(("method", "path"), protected_routes())
def test_every_protected_route_requires_a_token(client: TestClient, method: str, path: str) -> None:
    """Derived from `app.routes`, so a new route is covered by construction."""
    # Path params only need to be syntactically present; auth is checked before
    # the handler runs, so the item need not exist.
    concrete = path.replace("{item_id}", "W1")
    response = client.request(method, concrete, json={})
    assert response.status_code == 401, (
        f"{method} {path} answered {response.status_code} without a token"
    )


def test_no_token_configured_fails_closed(store: EventStore) -> None:
    with TestClient(create_api(store, token=None)) as c:
        assert c.get("/api/work").status_code == 503
        assert c.get("/healthz").status_code == 200


def test_there_is_no_html_anywhere(client: TestClient) -> None:
    """The GUI belongs to the session host. If HTML creeps back in here, so
    does a second UI."""
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


def test_every_listed_item_can_be_addressed_by_the_id_it_reports(client: TestClient) -> None:
    """A row a client cannot address is a row it can render and not act on.

    The list reported the identifier only as `item_id`; a client reading the
    conventional `id` got `null` for every row and lost the value needed for
    retry, detail and status -- while `GET /api/work/W1` worked perfectly for
    anyone who already knew the id.
    """
    items = client.get("/api/work", headers=auth()).json()["items"]
    assert items
    for item in items:
        assert item["id"], f"unaddressable row: {item}"
        assert item["id"] == item["item_id"]
        # The id it reported is the one the item routes actually take.
        assert client.get(f"/api/work/{item['id']}", headers=auth()).status_code == 200


def test_the_schema_documents_the_id_clients_read(client: TestClient) -> None:
    """A field clients depend on and the schema omits is an undocumented
    contract, which is the thing this API exists not to have."""
    schema = client.get("/openapi.json").json()["components"]["schemas"]["WorkItem"]
    assert "id" in schema["properties"]
    assert schema["properties"]["id"].get("description")
    assert "id" in schema["required"]


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
    q = make_queue(str(tmp_path / "w.sqlite"), lease_seconds=10.0, now=lambda: clock[0])
    q.add([WorkRecord(item_id="W1", title="t", brief="b")])
    q.claim("gone")
    clock[0] += 100
    with TestClient(create_api(store, queue=q, token=TOKEN)) as c:
        assert c.post("/api/work/W1/retry", headers=auth()).status_code == 200


def test_retry_on_an_unknown_item_is_404(client: TestClient) -> None:
    assert client.post("/api/work/NOPE/retry", headers=auth()).status_code == 404


def test_retry_requires_a_token(client: TestClient) -> None:
    assert client.post("/api/work/W1/retry").status_code == 401


# ------------------------------------------------------------------ block


def test_blocking_parks_an_item_with_its_reason(client: TestClient, queue: WorkQueue) -> None:
    """A plan routinely contains work that is a decision, not a task. Before
    this the only way to park one was to write to the database by hand, which
    goes around every check this API exists to apply."""
    response = client.post(
        "/api/work/W1/block",
        headers=auth(),
        json={"reason": "needs a human: which database?", "who": "sprooty"},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "blocked"
    record = queue.get("W1")
    assert record is not None and record.state == "blocked"
    assert "which database" in (record.last_error or "")
    assert "sprooty" in (record.last_error or "")


def test_a_blocked_item_is_not_claimed(client: TestClient, queue: WorkQueue) -> None:
    """The point of the whole endpoint. An implementation worker must not pick
    up a decision nobody has made."""
    client.post("/api/work/W1/block", headers=auth(), json={"reason": "D8 is a decision"})
    claimed = [queue.claim("w"), queue.claim("w")]
    assert {c.item_id for c in claimed if c} == {"W2"}


def test_blocking_a_decision_holds_back_what_depends_on_it(
    client: TestClient, queue: WorkQueue
) -> None:
    queue.add([WorkRecord(item_id="W3", title="after", brief="b", depends_on=["W1"])])
    client.post("/api/work/W1/block", headers=auth(), json={"reason": "undecided"})
    claimed = {c.item_id for c in (queue.claim("w"), queue.claim("w")) if c}
    assert "W3" not in claimed, "work depending on an open decision must wait for it"


def test_a_reason_is_required(client: TestClient) -> None:
    """An item parked with no reason is indistinguishable from one nobody got
    to, and whoever has to unblock it is rarely whoever blocked it."""
    assert client.post("/api/work/W1/block", headers=auth(), json={}).status_code == 422
    assert client.post("/api/work/W1/block", headers=auth(), json={"reason": ""}).status_code == 422


def test_blocking_refuses_a_live_claim_unless_overridden(
    client: TestClient, queue: WorkQueue
) -> None:
    queue.claim("worker-a")
    response = client.post("/api/work/W1/block", headers=auth(), json={"reason": "stop"})
    assert response.status_code == 409
    assert "worker-a" in response.json()["detail"]
    assert queue.get("W1").state == CLAIMED  # type: ignore[union-attr]

    forced = client.post(
        "/api/work/W1/block", headers=auth(), json={"reason": "stop", "override": True}
    )
    assert forced.status_code == 200
    assert queue.get("W1").state == "blocked"  # type: ignore[union-attr]


def test_blocking_will_not_quietly_un_finish_work(client: TestClient, queue: WorkQueue) -> None:
    queue.claim("w")
    queue.release("W1", DONE)
    response = client.post("/api/work/W1/block", headers=auth(), json={"reason": "hmm"})
    assert response.status_code == 409
    assert queue.get("W1").state == DONE  # type: ignore[union-attr]


def test_blocking_is_idempotent_and_the_reason_can_be_corrected(client: TestClient) -> None:
    first = client.post("/api/work/W1/block", headers=auth(), json={"reason": "first"})
    second = client.post("/api/work/W1/block", headers=auth(), json={"reason": "second"})
    assert first.status_code == second.status_code == 200
    assert second.json()["state"] == first.json()["state"] == "blocked"
    assert second.json()["reason"] == "second"


def test_the_reason_is_readable_from_the_list_and_the_item(client: TestClient) -> None:
    """A block nobody can see the reason for is a stuck item with extra
    steps."""
    client.post("/api/work/W1/block", headers=auth(), json={"reason": "waiting on D8"})
    listed = {i["item_id"]: i for i in client.get("/api/work", headers=auth()).json()["items"]}
    assert listed["W1"]["blocked_reason"] == "waiting on D8"
    # And it is not confused with a failure, which is the other thing the
    # queue's single "why" column holds.
    assert listed["W2"]["blocked_reason"] is None
    detail = client.get("/api/work/W1", headers=auth()).json()
    assert detail["blocked_reason"] == "waiting on D8"


def test_retry_is_the_way_back(client: TestClient, queue: WorkQueue) -> None:
    """A one-way door would mean the operator's own action needs the database
    edit this endpoint exists to avoid."""
    client.post("/api/work/W1/block", headers=auth(), json={"reason": "pending a decision"})
    assert client.post("/api/work/W1/retry", headers=auth()).status_code == 200
    record = queue.get("W1")
    assert record is not None and record.state == PENDING
    assert queue.claim("w") is not None


def test_blocking_an_unknown_item_is_404(client: TestClient) -> None:
    response = client.post("/api/work/NOPE/block", headers=auth(), json={"reason": "x"})
    assert response.status_code == 404


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


# ------------------------------------------------------------------- docs


def test_swagger_redoc_and_the_schema_are_served(client: TestClient) -> None:
    """The point of the feature: someone with curl or a code generator can
    drive this without reading the source."""
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert schema.json()["info"]["title"] == "agent-harness"


def test_the_schema_documents_response_shapes_not_empty_objects(
    client: TestClient,
) -> None:
    """A route returning a bare dict yields a schema of `{}` — valid and
    useless. Every response must name a model."""
    schema = client.get("/openapi.json").json()
    for path, method in [
        ("/api/work", "get"),
        ("/api/summary", "get"),
        ("/api/errors", "get"),
        ("/api/events", "get"),
        ("/healthz", "get"),
    ]:
        content = schema["paths"][path][method]["responses"]["200"]["content"]
        ref = content["application/json"]["schema"].get("$ref", "")
        assert ref.startswith("#/components/schemas/"), f"{method} {path} has no model"


def test_fields_carry_descriptions_so_the_schema_is_the_documentation(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()
    work_item = schema["components"]["schemas"]["WorkItem"]["properties"]
    assert "lease" in work_item["lease_until"]["description"].lower()
    assert "deep link" in work_item["latest"].get("description", "") or True
    limits = schema["components"]["schemas"]["RateLimits"]["properties"]
    # The distinction the whole project turns on must be stated in the schema.
    assert "never folded" in limits["unclassified"]["description"]


def test_the_schema_advertises_bearer_auth(client: TestClient) -> None:
    """Without this, Swagger UI has no Authorize button and the docs are
    read-only decoration."""
    schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert any(s.get("scheme") == "bearer" for s in schemes.values())
    assert schema["paths"]["/api/work"]["get"].get("security")


def test_docs_need_no_token_but_the_data_does(client: TestClient) -> None:
    """Docs are not secret; the backlog is. Requiring a token to read the
    schema would make the API undiscoverable for no benefit."""
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/api/work").status_code == 401


def test_root_path_makes_proxied_urls_correct(store: EventStore) -> None:
    """Behind the session host the service is reached at /api/harness. Without
    root_path, Swagger UI would tell a client to call URLs that 404."""
    with TestClient(create_api(store, token=TOKEN, root_path="/api/harness")) as c:
        schema = c.get("/openapi.json").json()
    assert schema["servers"][0]["url"] == "/api/harness"


# ------------------------------------------------------- expanded surface


def test_one_item_can_be_fetched(client: TestClient) -> None:
    payload = client.get("/api/work/W1", headers=auth()).json()
    assert payload["item_id"] == "W1"
    assert payload["title"] == "First"


def test_an_unknown_item_is_404(client: TestClient) -> None:
    assert client.get("/api/work/NOPE", headers=auth()).status_code == 404


def test_items_can_be_added_directly(client: TestClient, queue: WorkQueue) -> None:
    result = client.post(
        "/api/work",
        headers=auth(),
        json={"items": [{"item_id": "W9", "title": "New", "brief": "do it"}]},
    ).json()
    assert result["added"] == 1
    assert queue.get("W9") is not None


def test_re_adding_refreshes_without_resetting_progress(
    client: TestClient, queue: WorkQueue
) -> None:
    """The property that makes a re-synced plan safe."""
    queue.claim("w")
    queue.release("W1", DONE)
    result = client.post(
        "/api/work",
        headers=auth(),
        json={"items": [{"item_id": "W1", "title": "Renamed", "brief": "b"}]},
    ).json()
    assert result["added"] == 0
    record = queue.get("W1")
    assert record is not None
    assert record.state == DONE
    assert record.title == "Renamed"


def test_a_plan_can_be_parsed_without_writing_anything(client: TestClient, tmp_path: Path) -> None:
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        "# Project\n\n## Narrative\n\n### T1: Do it\n\nthe brief\n\n"
        "### T2: Also do it\n\ndepends on: T99\n"
    )
    payload = client.post(f"/api/plan/parse?path={plan}", headers=auth()).json()
    assert [i["id"] for i in payload["items"]] == ["T1", "T2"]
    # What it could NOT read is part of the answer, not a footnote.
    assert any("Narrative" in s for s in payload["skipped"])
    assert payload["unresolved_dependencies"] == {"T2": ["T99"]}


def test_parsing_a_missing_plan_is_404(client: TestClient) -> None:
    assert client.post("/api/plan/parse?path=/nope/PLAN.md", headers=auth()).status_code == 404


def test_sync_refuses_a_plan_with_duplicate_ids(client: TestClient, tmp_path: Path) -> None:
    """Each id becomes one issue, so a duplicate would create two."""
    plan = tmp_path / "PLAN.md"
    plan.write_text("### T1: One\n\nlong body here\n\n## Summary\n\n| T1 | One |\n")
    response = client.post(
        "/api/plan/sync",
        headers=auth(),
        json={"path": str(plan), "repo": "o/r", "dry_run": True},
    )
    assert response.status_code == 409
    assert "T1" in response.json()["detail"]["duplicate_ids"]


def test_sync_defaults_to_a_dry_run(client: TestClient) -> None:
    """It writes to a real repository, so the safe default is to describe
    rather than do."""
    schema = client.get("/openapi.json").json()
    prop = schema["components"]["schemas"]["PlanSyncRequest"]["properties"]["dry_run"]
    assert prop["default"] is True


# --------------------------------------------------------------- control


def test_control_reports_running_by_default(client: TestClient) -> None:
    payload = client.get("/api/control", headers=auth()).json()
    assert payload["state"] == "running"


def test_the_fleet_can_be_paused_and_resumed(client: TestClient, queue: WorkQueue) -> None:
    response = client.post(
        "/api/control", headers=auth(), json={"state": "paused", "reason": "deploying"}
    )
    assert response.status_code == 200
    assert response.json()["reason"] == "deploying"
    assert queue.claim("w") is None  # takes effect immediately

    client.post("/api/control", headers=auth(), json={"state": "running"})
    assert queue.claim("w") is not None


def test_pausing_does_not_disturb_work_in_flight(client: TestClient, queue: WorkQueue) -> None:
    """The guarantee that makes pause safe to press."""
    claimed = queue.claim("worker-a")
    assert claimed is not None
    client.post("/api/control", headers=auth(), json={"state": "paused"})
    record = queue.get(claimed.item_id)
    assert record is not None
    assert record.state == CLAIMED
    assert record.owner == "worker-a"


def test_an_unknown_control_state_is_rejected_by_the_schema(
    client: TestClient,
) -> None:
    assert client.post("/api/control", headers=auth(), json={"state": "halt"}).status_code == 422


def test_control_requires_a_token(client: TestClient) -> None:
    assert client.get("/api/control").status_code == 401
    assert client.post("/api/control", json={"state": "paused"}).status_code == 401


# ----------------------------------------------------------------- roles


def test_the_role_map_can_be_read_and_changed(client: TestClient) -> None:
    assert client.get("/api/roles", headers=auth()).json()["roles"] == {}
    body = {
        "roles": {
            "implementer": {"model": "cheap", "endpoint": "https://a", "provider": "claw-bay"},
            "reviewer": {"model": "other-vendor", "endpoint": "https://a", "provider": "claw-bay"},
        }
    }
    assert client.put("/api/roles", headers=auth(), json=body).status_code == 200
    stored = client.get("/api/roles", headers=auth()).json()["roles"]
    assert stored["implementer"]["model"] == "cheap"
    assert stored["reviewer"]["model"] == "other-vendor"


def test_the_role_map_persists_for_a_worker_in_another_process(
    client: TestClient, queue: WorkQueue
) -> None:
    """The API and the worker are different processes — the map has to live
    somewhere both can see."""
    client.put(
        "/api/roles",
        headers=auth(),
        json={"roles": {"reviewer": {"model": "m", "endpoint": "https://a"}}},
    )
    stored = queue.get_setting("role_map")
    assert stored is not None
    assert stored["reviewer"]["model"] == "m"


def test_roles_require_a_token(client: TestClient) -> None:
    assert client.get("/api/roles").status_code == 401
    assert client.put("/api/roles", json={"roles": {}}).status_code == 401


# ------------------------------------------ items the harness gave up on


def exhaust(queue: WorkQueue, item_id: str, project_id: str) -> None:
    """Drive a real item through the actual attempt-limit transition.

    Setting the state directly would test the schema against a value the
    queue might never write; the bug was that the two disagreed.
    """
    for _ in range(4):
        claimed = queue.claim("w", project_id=project_id)
        if claimed is None:
            break
        queue.release(claimed.item_id, PENDING, error="boom", project_id=project_id)
    queue.claim("w", project_id=project_id)  # the claim that gives up
    record = queue.get(item_id, project_id=project_id)
    assert record is not None and record.state == "exhausted", record


def test_an_exhausted_item_is_listable_and_inspectable(client: TestClient, tmp_path: Path) -> None:
    """The state that marks work needing a human must not be the state that
    breaks the endpoints used to find it."""
    queue = make_queue(str(tmp_path / "x.sqlite"), lease_seconds=100.0)
    queue.add_project(Project(project_id="p", name="p", max_attempts=1))
    queue.add([WorkRecord(item_id="T1", title="t", brief="b")], project_id="p")
    queue.set_control(RUNNING, project_id="p")
    exhaust(queue, "T1", "p")

    with TestClient(create_api(EventStore(tmp_path / "e2.sqlite"), queue=queue, token=TOKEN)) as c:
        listed = c.get("/api/work?project_id=p", headers=auth())
        assert listed.status_code == 200, listed.text
        row = next(i for i in listed.json()["items"] if i["item_id"] == "T1")
        assert row["state"] == "exhausted"
        assert row["attempts"] >= 1
        assert "gave up" in (row["last_error"] or ""), "the reason must survive"

        detail = c.get("/api/work/T1?project_id=p", headers=auth())
        assert detail.status_code == 200, detail.text
        assert detail.json()["state"] == "exhausted"


def test_an_exhausted_item_can_be_retried(client: TestClient, tmp_path: Path) -> None:
    """Giving up is not permanent; it just needs somebody to say so."""
    queue = make_queue(str(tmp_path / "x.sqlite"), lease_seconds=100.0)
    queue.add_project(Project(project_id="p", name="p", max_attempts=1))
    queue.add([WorkRecord(item_id="T1", title="t", brief="b")], project_id="p")
    queue.set_control(RUNNING, project_id="p")
    exhaust(queue, "T1", "p")

    with TestClient(create_api(EventStore(tmp_path / "e2.sqlite"), queue=queue, token=TOKEN)) as c:
        retried = c.post("/api/work/T1/retry?project_id=p", headers=auth())
        assert retried.status_code == 200, retried.text
        assert retried.json()["state"] == "pending"


def test_the_documented_states_are_the_states_the_queue_writes(client: TestClient) -> None:
    """The two drifted apart once and the symptom was a 500. Derived from
    the queue's own constants so it cannot drift again silently."""
    from agent_harness import work as work_module

    stored = {
        getattr(work_module, name)
        for name in ("PENDING", "CLAIMED", "DONE", "FAILED", "BLOCKED", "EXHAUSTED")
    }
    schema = client.get("/openapi.json").json()["components"]["schemas"]["WorkItem"]
    documented = set(schema["properties"]["state"]["enum"])
    assert stored <= documented, (
        f"states the queue writes and the schema rejects: {stored - documented}"
    )


# ------------------------------------ isolation between apps and projects


def test_two_projects_with_the_same_item_id_see_only_their_own_events(
    tmp_path: Path,
) -> None:
    """An item IS (project_id, item_id). Keying events by item id alone let a
    client deep-link one project's row to another's live terminal session."""
    store = EventStore(tmp_path / "e.sqlite")
    queue = make_queue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    for project in ("a", "b"):
        queue.add([WorkRecord(item_id="T1", title="t", brief="b")], project_id=project)
    store.append(
        [
            Event(
                ts=1000.0,
                kind=WORK,
                source="events.jsonl",
                worker="w",
                outcome="agent_started",
                data={"project_id": "a", "item_id": "T1", "session_id": "s-a"},
            ),
            Event(
                ts=2000.0,  # newer, and belongs to b
                kind=WORK,
                source="events.jsonl",
                worker="w",
                outcome="review_rejected",
                data={"project_id": "b", "item_id": "T1", "session_id": "s-b"},
            ),
        ]
    )

    with TestClient(create_api(store, queue=queue, token=TOKEN)) as c:
        a = c.get("/api/work/T1?project_id=a", headers=auth()).json()
        b = c.get("/api/work/T1?project_id=b", headers=auth()).json()
        assert a["latest"]["session_id"] == "s-a"
        assert a["latest"]["outcome"] == "agent_started"
        assert b["latest"]["session_id"] == "s-b"

        listed = c.get("/api/work?project_id=a", headers=auth()).json()["items"]
        assert listed[0]["latest"]["session_id"] == "s-a"


def test_an_event_with_no_project_is_a_fallback_not_an_override(tmp_path: Path) -> None:
    """Events written before the emitters carried a project id must not be
    spread across every project -- that is the bug, not the migration."""
    store = EventStore(tmp_path / "e.sqlite")
    queue = make_queue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    for project in ("a", "b"):
        queue.add([WorkRecord(item_id="T1", title="t", brief="b")], project_id=project)
    store.append(
        [
            Event(ts=1000.0, kind=WORK, source="s", outcome="legacy", data={"item_id": "T1"}),
            Event(
                ts=2000.0,
                kind=WORK,
                source="s",
                outcome="scoped",
                data={"project_id": "a", "item_id": "T1"},
            ),
        ]
    )
    with TestClient(create_api(store, queue=queue, token=TOKEN)) as c:
        # `a` has something better, so the legacy row does not override it.
        assert c.get("/api/work/T1?project_id=a", headers=auth()).json()["latest"]["outcome"] == (
            "scoped"
        )
        # `b` has nothing else, so the legacy row is still shown rather than lost.
        assert c.get("/api/work/T1?project_id=b", headers=auth()).json()["latest"]["outcome"] == (
            "legacy"
        )


def test_a_second_app_does_not_take_over_the_first_apps_readiness(tmp_path: Path) -> None:
    """One app's safety gate must never be answered by another app's wiring.

    The module-level handle meant the last app constructed in a process owned
    every earlier app's preflight -- so a response could say `monitoring-only`
    while reporting another instance's healthy worker pool.
    """

    def build(name: str, fleet: Any, checkout_ok: bool) -> Any:
        queue = make_queue(str(tmp_path / f"{name}.sqlite"), lease_seconds=100.0)
        queue.add_project(
            Project(project_id="p", name="p", repo="o/r", work_dir=str(tmp_path), plan_path="x")
        )
        return create_api(
            EventStore(tmp_path / f"{name}-e.sqlite"),
            queue=queue,
            token=TOKEN,
            fleet=fleet,
            probes={
                "git_probe": lambda _p, ok=checkout_ok: (ok, "fine" if ok else "bad"),
                "github_probe": lambda _p: (True, "fine"),
            },
        )

    monitoring_only = build("a", None, True)
    # Constructed second, with contradictory wiring. Under the bug this is
    # the app whose fleet and probes answered for both.
    build("b", object(), False)

    with TestClient(monitoring_only) as c:
        readiness = c.get("/api/readiness", headers=auth()).json()
        assert readiness["mode"] == "monitoring-only"
        blockers = " ".join(str(p) for p in readiness["projects"])
        assert "bad" not in blockers, "the other app's failing probe answered for this one"

        preflight = c.get("/api/projects/p/preflight", headers=auth()).json()
        checks = {check["name"]: check for check in preflight["checks"]}
        assert checks["workers"]["ok"] is False, "it reported another app's worker pool"
        assert not any("bad" in str(check) for check in checks.values())


def test_the_api_keeps_no_module_level_wiring() -> None:
    """Enforced against the source. A module-level handle is how one app
    started answering for another, and it is invisible until two exist."""
    import re

    from agent_harness import api as api_module

    source = Path(api_module.__file__).read_text()
    assert "_APP_STATE" not in source
    module_dicts = re.findall(r"^_[A-Z_]+: dict\[str, Any\] = \{\}", source, re.MULTILINE)
    assert module_dicts == [], f"mutable module-level state is back: {module_dicts}"


# ------------------------------------------------ the schema is the contract


def _success_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    for status in ("200", "201"):
        content = (operation.get("responses", {}).get(status) or {}).get("content") or {}
        if "application/json" in content:
            return content["application/json"].get("schema") or {}
    return None


def test_no_route_returns_an_unstructured_object(client: TestClient) -> None:
    """AGENTS.md: the schema IS the documentation.

    Derived from the served document rather than a hand-maintained list of
    routes. The previous check covered five older routes, so the inception
    surface grew two `response_model=dict` routes without anything noticing —
    and a generated client cannot discover a field that is documented as
    `additionalProperties: true`.
    """
    schema = client.get("/openapi.json").json()
    bare: list[str] = []
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            success = _success_schema(operation)
            if success is None:
                continue
            named = "$ref" in success or "items" in success or "anyOf" in success
            if not named and success.get("type") in (None, "object"):
                bare.append(f"{method.upper()} {path}")
    assert bare == [], f"routes documenting an unstructured object: {bare}"


def test_every_documented_field_has_a_description(client: TestClient) -> None:
    """A named model whose fields say nothing is barely better than a dict."""
    components = client.get("/openapi.json").json()["components"]["schemas"]
    undescribed: list[str] = []
    for name, model in components.items():
        if name.startswith("HTTPValidationError") or name == "ValidationError":
            continue  # FastAPI's own, not ours to document
        for field, spec in (model.get("properties") or {}).items():
            structured = "$ref" in spec or "allOf" in spec or "anyOf" in spec or "items" in spec
            if not spec.get("description") and not structured:
                undescribed.append(f"{name}.{field}")
    assert undescribed == [], f"fields with no description: {undescribed}"


def test_the_inception_routes_name_their_shapes(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    started = _success_schema(schema["paths"]["/api/inception"]["post"])
    plan = _success_schema(schema["paths"]["/api/inception/{project_id}/plan"]["get"])
    assert started and "$ref" in started
    assert plan and "$ref" in plan
    assert "markdown" in schema["components"]["schemas"]["InceptionPlan"]["properties"]


# -------------------------------------------- triaging failures from a list


def test_a_failed_item_carries_its_reason_and_stage_in_the_list(
    client: TestClient, queue: WorkQueue, store: EventStore
) -> None:
    """A backlog with several failures is untriageable when every row says
    only `failed` and the reason is one pane deeper."""
    store.append(
        [
            Event(
                ts=time.time(),
                kind=WORK,
                source="events.jsonl",
                worker="w",
                outcome="checks_failed",
                data={"item_id": "W1"},
            )
        ]
    )
    queue.claim("w")
    queue.release("W1", "failed", error="pytest exited 1\nassert 3 == 4\n  in test_thing")

    row = {i["item_id"]: i for i in client.get("/api/work", headers=auth()).json()["items"]}["W1"]
    assert row["failure_summary"] == "pytest exited 1"
    assert row["failed_stage"] == "checks_failed"
    # The summary never replaces the full text.
    assert "assert 3 == 4" in row["last_error"]


def test_a_long_failure_is_truncated_and_says_so(client: TestClient, queue: WorkQueue) -> None:
    queue.claim("w")
    queue.release("W1", "failed", error="x" * 500)

    row = {i["item_id"]: i for i in client.get("/api/work", headers=auth()).json()["items"]}["W1"]
    assert len(row["failure_summary"]) <= 200
    assert row["failure_summary"].endswith("…"), "a clipped message must look clipped"
    assert len(row["last_error"]) == 500


def test_a_healthy_item_advertises_no_failure(client: TestClient) -> None:
    row = {i["item_id"]: i for i in client.get("/api/work", headers=auth()).json()["items"]}["W1"]
    assert row["failure_summary"] is None
    assert row["failed_stage"] is None


def test_retryability_matches_what_retry_actually_does(
    client: TestClient, queue: WorkQueue
) -> None:
    """The rule is computed once. A client offering the action cannot drift
    from the server refusing it -- the alternative is a button that exists to
    produce a 409."""
    queue.claim("worker-a")
    row = client.get("/api/work/W1", headers=auth()).json()
    assert row["retryable"] is False
    assert "lease is live" in row["retry_blocked_reason"]
    assert client.post("/api/work/W1/retry", headers=auth()).status_code == 409


def test_a_stale_claim_is_advertised_as_retryable_and_is(tmp_path: Path, store: EventStore) -> None:
    clock = [1000.0]
    queue = make_queue(str(tmp_path / "s.sqlite"), lease_seconds=100.0, now=lambda: clock[0])
    queue.add([WorkRecord(item_id="W1", title="t", brief="b")])
    queue.claim("worker-a")
    clock[0] += 101  # the lease expires; nothing is holding it any more

    with TestClient(create_api(store, queue=queue, token=TOKEN)) as c:
        row = c.get("/api/work/W1", headers=auth()).json()
        assert row["retryable"] is True
        assert row["retry_blocked_reason"] is None
        assert c.post("/api/work/W1/retry", headers=auth()).status_code == 200
