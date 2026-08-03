"""Execution readiness, separately from service health.

`/healthz` answers whether the service is up. A monitoring-only deployment is
perfectly healthy and cannot run a single item, so a healthy service reads as
an executable fleet -- and until this existed, the only way to find out
otherwise was to attempt a **state-changing** start and read the 409.

Everything here is read-only by construction: the assertions check that
nothing was created, claimed or mutated, because a readiness check with side
effects is not a readiness check.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_harness.api import ROLE_MAP_KEY, create_api
from agent_harness.preflight import session_host_probe
from agent_harness.session_host import IDLE, Session, SessionHostError
from agent_harness.store import EventStore
from agent_harness.work import Project, WorkQueue, WorkRecord

TOKEN = "tok"  # noqa: S105 - a fixture, not a credential

REVIEWER = {"model": "claude-sonnet-4-6", "endpoint": "https://e", "provider": "generic"}


def hdr() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class ReadableHost:
    """A session host that answers a read. Nothing else is needed to prove
    both that it is reachable and that the token is accepted."""

    def __init__(self, sessions: int = 2) -> None:
        self.sessions = sessions
        self.reads = 0
        self.created: list[str] = []

    def list_sessions(self) -> list[Session]:
        self.reads += 1
        return [Session(id=f"s{n}", name="s", activity=IDLE) for n in range(self.sessions)]

    def create_session(self, *args: Any, **kwargs: Any) -> Session:  # pragma: no cover
        self.created.append("boom")
        raise AssertionError("readiness must never create a session")


class RefusingHost:
    def list_sessions(self) -> list[Session]:
        raise SessionHostError("GET /api/sessions -> 401: unauthorized")


class OneWorker:
    def running(self) -> dict[str, int]:
        return {"p": 1}

    def failures(self, project_id: str | None = None) -> list[Any]:
        return []


def ok_git(path: str) -> tuple[bool, str]:
    return (True, path)


def ok_gh(repo: str) -> tuple[bool, str]:
    return (True, f"push access to {repo}")


#: Injected, so no test shells out to `gh` or reads a real checkout. Preflight
#: takes every probe for exactly this reason; the API layer now passes them
#: through instead of hardcoding a subprocess behind an HTTP route.
OFFLINE = {"git_probe": ok_git, "github_probe": ok_gh}


def project(**kw: Any) -> Project:
    kw.setdefault("project_id", "p")
    kw.setdefault("name", "P")
    kw.setdefault("repo", "o/r")
    kw.setdefault("work_dir", "/work/p")
    kw.setdefault("checks", ["pytest -q"])
    return Project(**kw)


def build(
    tmp_path: Path,
    *,
    fleet: Any = None,
    host: Any = None,
    reviewer: dict[str, str] | None = REVIEWER,
    projects: tuple[Project, ...] = (),
    probes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    for p in projects or (project(),):
        queue.add_project(p)
    if reviewer:
        queue.set_setting(ROLE_MAP_KEY, {"reviewer": reviewer})
    store = EventStore(tmp_path / "e.sqlite")
    with TestClient(
        create_api(
            store,
            queue=queue,
            token=TOKEN,
            fleet=fleet,
            session_host=host,
            probes=OFFLINE if probes is None else probes,
        )
    ) as client:
        holder: Any = client
        holder.queue = queue
        yield client


@pytest.fixture
def monitoring(tmp_path: Path) -> Iterator[Any]:
    yield from build(tmp_path)


@pytest.fixture
def supervised(tmp_path: Path) -> Iterator[Any]:
    yield from build(tmp_path, fleet=OneWorker(), host=ReadableHost())


# ------------------------------------------------------------------- modes


def test_monitoring_only_says_so_instead_of_looking_executable(monitoring: Any) -> None:
    """The whole point. `/healthz` is `ok: true` here, and nothing can run."""
    assert monitoring.get("/healthz").json()["ok"] is True

    body = monitoring.get("/api/readiness", headers=hdr()).json()

    assert body["mode"] == "monitoring-only"
    assert body["ready_to_start"] is False
    assert body["workers"]["configured"] is False
    assert "monitoring-only" in body["workers"]["detail"]
    assert body["session_host"]["configured"] is False


def test_a_supervised_deployment_reports_its_capabilities(supervised: Any) -> None:
    body = supervised.get("/api/readiness", headers=hdr()).json()

    assert body["mode"] == "supervised"
    assert body["ready_to_start"] is True
    assert body["projects"][0]["blockers"] == []
    assert body["workers"]["ok"] is True and "1 worker" in body["workers"]["detail"]
    assert body["session_host"]["ok"] is True
    assert "2 live session" in body["session_host"]["detail"]
    assert body["reviewer"]["ok"] is True
    assert "claude-sonnet-4-6" in body["reviewer"]["detail"]


# ------------------------------------------------------- blocking reasons


def test_every_blocking_reason_is_named_not_just_the_first(monitoring: Any) -> None:
    """An operator fixing one thing at a time, being told about the next one
    only after a redeploy, is why this returns the whole list."""
    body = monitoring.get("/api/readiness", headers=hdr()).json()
    project_state = body["projects"][0]

    assert project_state["ready_to_start"] is False
    names = {c["name"] for c in project_state["blockers"]}
    assert "workers" in names
    assert all(c["detail"] for c in project_state["blockers"])


def test_an_unreachable_session_host_blocks_and_says_which(tmp_path: Path) -> None:
    """Configured-but-refusing and not-configured are different problems with
    different fixes, and one boolean cannot tell them apart."""
    client = next(build(tmp_path, fleet=OneWorker(), host=RefusingHost()))

    body = client.get("/api/readiness", headers=hdr()).json()

    assert body["session_host"]["configured"] is True
    assert body["session_host"]["ok"] is False
    assert "401" in body["session_host"]["detail"]
    assert any(c["name"] == "session host" for c in body["projects"][0]["blockers"])


def test_a_missing_reviewer_is_reported_before_any_money_is_spent(tmp_path: Path) -> None:
    client = next(build(tmp_path, fleet=OneWorker(), host=ReadableHost(), reviewer=None))

    body = client.get("/api/readiness", headers=hdr()).json()

    assert body["reviewer"]["ok"] is False
    assert "fails closed" in body["reviewer"]["detail"]
    assert any(c["name"] == "reviewer" for c in body["projects"][0]["blockers"])


def test_warnings_are_kept_apart_from_blockers(tmp_path: Path) -> None:
    """A project with no verification commands can still finish an item. It
    should not be refused, and it should not be silent either."""
    client = next(
        build(tmp_path, fleet=OneWorker(), host=ReadableHost(), projects=(project(checks=[]),))
    )

    state = client.get("/api/readiness", headers=hdr()).json()["projects"][0]

    assert "checks" in {c["name"] for c in state["warnings"]}
    assert "checks" not in {c["name"] for c in state["blockers"]}


# --------------------------------------------------------------- read-only


def test_readiness_creates_no_session_claims_nothing_and_changes_no_state(
    supervised: Any,
) -> None:
    supervised.queue.add([WorkRecord(item_id="T1", title="t", brief="b")], project_id="p")
    before = supervised.queue.control(project_id="p")

    supervised.get("/api/readiness", headers=hdr())

    assert supervised.queue.control(project_id="p") == before
    assert supervised.queue.get("T1", project_id="p").state == "pending"
    assert supervised.queue.counts(project_id="p") == {"pending": 1}


def test_the_session_host_is_asked_once_however_many_projects(tmp_path: Path) -> None:
    """Readiness must not get more expensive the more projects you have."""
    host = ReadableHost()
    client = next(
        build(
            tmp_path,
            fleet=OneWorker(),
            host=host,
            projects=(project(), project(project_id="q", name="Q"), project(project_id="r")),
        )
    )

    body = client.get("/api/readiness", headers=hdr()).json()

    assert len(body["projects"]) == 3
    assert host.reads == 1


def test_one_project_can_be_asked_about_on_its_own(supervised: Any) -> None:
    supervised.queue.add_project(project(project_id="q", name="Q"))

    body = supervised.get("/api/readiness?project_id=q", headers=hdr()).json()

    assert [p["project_id"] for p in body["projects"]] == ["q"]
    assert supervised.get("/api/readiness?project_id=nope", headers=hdr()).status_code == 404


def test_readiness_needs_a_token(monitoring: Any) -> None:
    assert monitoring.get("/api/readiness").status_code == 401


# ------------------------------------------------------------- the probe


def test_the_probe_never_leaks_more_than_it_has_to() -> None:
    """The detail is for a human deciding what to fix, not a place to echo a
    credential back over the wire."""
    ok, detail = session_host_probe(ReadableHost())()
    assert ok and "live session" in detail

    ok, detail = session_host_probe(RefusingHost())()
    assert not ok
    assert len(detail) < 300


def test_readiness_and_start_cannot_disagree(supervised: Any) -> None:
    """Derived from the same preflight the start action runs. Two
    implementations of "can this start" would drift, and the one people read
    would be the wrong one."""
    ready = supervised.get("/api/readiness?project_id=p", headers=hdr()).json()
    preflight = supervised.get("/api/projects/p/preflight", headers=hdr()).json()

    assert ready["projects"][0]["ready_to_start"] == preflight["ready"]
    assert ready["projects"][0]["summary"] == preflight["summary"]
