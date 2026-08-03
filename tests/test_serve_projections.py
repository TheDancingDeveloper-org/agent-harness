"""What the API can actually see in a supervised deployment.

Two projections read stores the fleet does not write to under `serve`, and
both reported nothing forever as a result: an item's `latest` event, and the
base-branch check result. Neither failure was visible from a test that
populated the store it read from by hand — which is exactly what every test
did.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_harness.api import create_api
from agent_harness.audit import AuditStore
from agent_harness.events import WORK, Event
from agent_harness.store import EventStore
from agent_harness.work import Project, WorkQueue, WorkRecord


def hdr() -> dict[str, str]:
    return {"Authorization": "Bearer tok"}


@pytest.fixture
def parts(tmp_path: Path) -> tuple[EventStore, WorkQueue, AuditStore]:
    store = EventStore(str(tmp_path / "events.sqlite"))
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.add_project(Project(project_id="default", name="D", repo="o/r"))
    queue.add([WorkRecord(item_id="T27", title="do T27", brief="b")])
    return store, queue, AuditStore(tmp_path / "audit.sqlite")


def test_an_items_latest_event_comes_from_the_audit_store(
    parts: tuple[EventStore, WorkQueue, AuditStore],
) -> None:
    """The regression. Under `serve` nothing ingests `events.jsonl` into the
    `EventStore`, so reading only that reported `latest: null` for every item
    no matter how much work ran."""
    store, queue, audit = parts
    audit.append(
        [
            Event(
                ts=1000.0,
                kind=WORK,
                source="serve",
                outcome="agent_started",
                data={"item_id": "T27", "project_id": "default", "session_id": "sess-1"},
            ),
            Event(
                ts=2000.0,
                kind=WORK,
                source="serve",
                outcome="checks_failed",
                data={"item_id": "T27", "project_id": "default", "detail": "cargo clippy"},
            ),
        ]
    )
    # The ingest store is empty, as it is in every supervised deployment.
    assert store.recent(kind="work", limit=10) == []

    with TestClient(create_api(store, queue=queue, token="tok", audit=audit)) as c:  # noqa: S106
        item = c.get("/api/work/T27", headers=hdr()).json()

    assert item["latest"] is not None
    assert item["latest"]["outcome"] == "checks_failed"
    assert item["latest"]["detail"] == "cargo clippy"


def test_a_long_running_item_is_not_buried_by_other_items_events(
    parts: tuple[EventStore, WorkQueue, AuditStore],
) -> None:
    """Why this groups rather than scanning the last N events.

    A scan drops any item whose most recent activity is older than the window
    — which is precisely the long-running item someone is trying to read the
    status of, because it is the one that has been quiet the longest.
    """
    store, queue, audit = parts
    queue.add([WorkRecord(item_id=f"N{n}", title="noise", brief="b") for n in range(60)])
    audit.append(
        [
            Event(
                ts=1000.0,
                kind=WORK,
                source="serve",
                outcome="agent_started",
                data={"item_id": "T27", "project_id": "default"},
            )
        ]
    )
    audit.append(
        [
            Event(
                ts=1001.0 + n,
                kind=WORK,
                source="serve",
                outcome="done",
                data={"item_id": f"N{n}", "project_id": "default"},
            )
            for n in range(60)
        ]
    )

    with TestClient(create_api(store, queue=queue, token="tok", audit=audit)) as c:  # noqa: S106
        item = c.get("/api/work/T27", headers=hdr()).json()

    assert item["latest"] is not None
    assert item["latest"]["outcome"] == "agent_started"


def slow_probe(started: list[float], seconds: float = 2.0) -> Any:
    """Stands in for a real base build: slow, and it records that it ran."""

    def probe() -> tuple[bool, str]:
        started.append(time.monotonic())
        time.sleep(seconds)
        return (True, "8 check(s) pass on main")

    return probe


def test_base_checks_start_without_blocking_the_request(
    parts: tuple[EventStore, WorkQueue, AuditStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same mistake: a request held open for a whole
    build dies at the first proxy timeout while the build carries on."""
    import agent_harness.preflight as preflight_mod

    store, queue, audit = parts
    started: list[float] = []
    monkeypatch.setattr(
        preflight_mod, "clean_checks_probe", lambda project, timeout=900.0: slow_probe(started)
    )
    app = create_api(store, queue=queue, token="tok", audit=audit)  # noqa: S106
    checks: Any = app.state.base_checks

    with TestClient(app) as c:
        began = time.monotonic()
        run = checks.start(queue.get_project("default"))
        assert time.monotonic() - began < 0.5, "starting a run must not wait for it"
        assert run.state == "running"

        # Preflight reports the run rather than launching another.
        body = c.get("/api/projects/default/preflight?check_base=true", headers=hdr()).json()
        base = [ch for ch in body["checks"] if ch["name"] == "base checks"]
        assert base and base[0]["ok"] is False
        assert "still running" in base[0]["detail"]
        assert len(started) == 1, "polling preflight must not start a second build"


def test_a_second_start_joins_the_run_already_in_flight(
    parts: tuple[EventStore, WorkQueue, AuditStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrying a timed-out request used to start a second concurrent build."""
    import agent_harness.preflight as preflight_mod

    store, queue, audit = parts
    started: list[float] = []
    monkeypatch.setattr(
        preflight_mod, "clean_checks_probe", lambda project, timeout=900.0: slow_probe(started)
    )
    app = create_api(store, queue=queue, token="tok", audit=audit)  # noqa: S106
    checks: Any = app.state.base_checks
    project = queue.get_project("default")

    first = checks.start(project)
    second = checks.start(project)

    assert second is first
    assert len(started) == 1


def test_a_finished_run_is_reported_to_preflight(
    parts: tuple[EventStore, WorkQueue, AuditStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the point of remembering it: preflight can answer honestly about a
    check list without rebuilding the world on every poll."""
    import agent_harness.preflight as preflight_mod

    store, queue, audit = parts
    monkeypatch.setattr(
        preflight_mod,
        "clean_checks_probe",
        lambda project, timeout=900.0: lambda: (False, "`cargo clippy` failed on base branch"),
    )
    app = create_api(store, queue=queue, token="tok", audit=audit)  # noqa: S106
    checks: Any = app.state.base_checks
    checks.start(queue.get_project("default"))

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and checks.status("default").state == "running":
        time.sleep(0.01)

    with TestClient(app) as c:
        body = c.get("/api/projects/default/preflight?check_base=true", headers=hdr()).json()
        status = c.get("/api/projects/default/preflight/base", headers=hdr()).json()

    base = [ch for ch in body["checks"] if ch["name"] == "base checks"]
    assert base and base[0]["ok"] is False
    assert "cargo clippy" in base[0]["detail"]
    assert status["state"] == "failed"
    assert status["ok"] is False


def test_a_project_nobody_has_asked_about_says_so(
    parts: tuple[EventStore, WorkQueue, AuditStore],
) -> None:
    """`not_run` is a real answer, not a 404: it is what a fresh process knows."""
    store, queue, audit = parts
    with TestClient(create_api(store, queue=queue, token="tok", audit=audit)) as c:  # noqa: S106
        status = c.get("/api/projects/default/preflight/base", headers=hdr()).json()
    assert status["state"] == "not_run"
    assert status["ok"] is None
