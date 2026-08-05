"""In-process journeys for the self-contained, read-only browser slice."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from agent_harness.api import create_api
from agent_harness.events import WORK, Event
from agent_harness.store import EventStore
from agent_harness.work import Project, WorkQueue, WorkRecord

TOKEN = "browser-test-token"  # noqa: S105 - fixture value


def make_client(tmp_path: Path, *, token: str | None = TOKEN) -> TestClient:
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P"))
    queue.add(
        [WorkRecord(item_id="T1", title="First item", brief="A safe read path")],
        project_id="p",
    )
    return TestClient(create_api(store, queue=queue, token=token))


def make_rooted_client(tmp_path: Path) -> TestClient:
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P"))
    return TestClient(create_api(store, queue=queue, token=TOKEN, root_path="/harness"))


def login(client: TestClient) -> str:
    response = client.post("/login", data={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/projects")
    cookie = response.cookies.get("harness_session")
    assert cookie
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    return cookie


def test_root_and_pages_fail_closed_until_login(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        assert client.get("/projects").status_code == 401
        assert client.get("/work").status_code == 401
        assert client.get("/api/work").status_code == 401


def test_login_cookie_is_opaque_and_pages_are_read_only(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        session = login(client)
        assert TOKEN not in session
        projects = client.get("/projects")
        assert projects.status_code == 200
        assert "Project P" in projects.text
        assert "read-only" in projects.text.lower()
        assert client.get("/work?project_id=p").status_code == 200
        assert "First item" in client.get("/work?project_id=p").text
        assert client.post("/api/projects/p/start").status_code == 401


def test_ui_security_headers_and_packaged_assets(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/login")
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert "X-Content-Type-Options" in response.headers
        css = client.get("/assets/app.css")
        js = client.get("/assets/htmx.min.js")
        assert css.status_code == 200 and "--accent" in css.text
        assert js.status_code == 200 and "cdn" not in js.text.lower()
        assert "immutable" in css.headers["cache-control"]


def test_login_refuses_without_configured_token(tmp_path: Path) -> None:
    with make_client(tmp_path, token=None) as client:
        assert client.get("/login").status_code == 503
        assert client.post("/login", data={"token": TOKEN}).status_code == 503
        assert client.get("/api/work").status_code == 503


def test_logout_requires_csrf_and_revokes_session(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        login(client)
        assert client.post("/logout", follow_redirects=False).status_code == 403
        csrf = (
            client.get("/projects").text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        )
        response = client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert client.get("/projects").status_code == 401


def test_item_detail_renders_durable_evidence_without_xss(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        store = client.app.state.store  # type: ignore[attr-defined]
        store.append(
            [
                Event(
                    ts=time.time(),
                    kind=WORK,
                    source="fixture",
                    outcome="agent_started",
                    data={
                        "project_id": "p",
                        "item_id": "T1",
                        "detail": "<script>alert(1)</script>",
                    },
                )
            ]
        )
        login(client)
        response = client.get("/work/T1?project_id=p")
        assert response.status_code == 200
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
        assert "<script>alert(1)</script>" not in response.text
        evidence = client.get(
            "/api/work/T1/evidence?project_id=p",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert evidence.status_code == 200
        assert evidence.json()["item_id"] == "T1"


def test_event_stream_accepts_cursor_and_requires_auth(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert client.get("/api/events/stream?since_id=0").status_code == 401
        login(client)
        response = client.get("/api/events/stream?since_id=bad")
        assert response.status_code == 400


def test_ui_named_urls_honor_root_path(tmp_path: Path) -> None:
    with make_rooted_client(tmp_path) as client:
        response = client.get("/login")
        assert response.status_code == 200
        assert "/harness/login" in response.text
        assert "/harness/assets/app.css" in response.text
