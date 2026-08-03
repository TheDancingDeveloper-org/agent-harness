"""Can this project actually finish an item?

The failure this prevents is not an error -- it is a fleet that claims work,
spends the implementer's tokens, and fails every item while reporting
`running`. A nonproductive fleet and a productive one look identical from
outside until the bill arrives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_harness.preflight import preflight_project
from agent_harness.work import Project, WorkQueue


def ok_git(path: str) -> tuple[bool, str]:
    return (True, path)


def no_git(path: str) -> tuple[bool, str]:
    return (False, f"{path} is not a git repository")


def ok_gh(repo: str) -> tuple[bool, str]:
    return (True, f"push access to {repo}")


def no_gh(repo: str) -> tuple[bool, str]:
    return (False, "no push permission")


def project(**kw: Any) -> Project:
    kw.setdefault("project_id", "p")
    kw.setdefault("name", "P")
    kw.setdefault("repo", "o/r")
    kw.setdefault("work_dir", "/work/p")
    kw.setdefault("checks", ["pytest -q"])
    return Project(**kw)


def run(p: Project, **kw: Any):  # type: ignore[no-untyped-def]
    kw.setdefault("has_fleet", True)
    kw.setdefault("reviewer_route", {"model": "m"})
    kw.setdefault("git_probe", ok_git)
    kw.setdefault("github_probe", ok_gh)
    return preflight_project(p, **kw)


def test_a_fully_configured_project_is_ready() -> None:
    report = run(project())
    assert report.ready
    assert report.summary() == "ready"


def test_no_worker_pool_blocks() -> None:
    """The false-running state, refused at source. Without a pool, starting
    can only set a flag nobody acts on."""
    report = run(project(), has_fleet=False)
    assert not report.ready
    assert "flag nobody acts on" in report.summary()


def test_no_reviewer_blocks_because_review_fails_closed() -> None:
    """Worse than not starting: it pays for the implementation first, then
    rejects it for a reason that has nothing to do with the work."""
    report = run(project(), reviewer_route=None)
    assert not report.ready
    assert "fails closed" in report.summary()


def test_no_github_write_blocks() -> None:
    """The definition of done is a pull request. Without write access every
    item is doomed at the last step, after all the cost."""
    report = run(project(), github_probe=no_gh)
    assert not report.ready
    assert "pull request" in report.summary()


def test_a_missing_checkout_blocks() -> None:
    report = run(project(), git_probe=no_git)
    assert not report.ready
    assert "not a git repository" in report.summary()


def test_an_unconfigured_project_names_everything_missing() -> None:
    """One 409 that lists all of it, not a game of whack-a-mole."""
    report = run(project(repo=None, work_dir=None), has_fleet=False, reviewer_route=None)
    names = {c.name for c in report.blockers}
    assert names == {"workers", "checkout", "github write", "reviewer"}


def test_missing_checks_warn_rather_than_block() -> None:
    """No checks is worse work, not impossible work -- and blocking on it
    would stop a project whose only gate is review, which is a real setup."""
    report = run(project(checks=[]))
    assert report.ready
    assert [c.name for c in report.warnings] == ["checks"]


def test_a_dependent_reviewer_warns_rather_than_blocks() -> None:
    """Running one model is a legitimate deliberate choice about your own
    budget. It must not be a surprise; it must also not be forbidden."""
    report = run(project(), reviewer_independent=(False, "same model"))
    assert report.ready
    assert any(c.name == "reviewer independence" for c in report.warnings)


# --------------------------------------------------------------- the API


@pytest.fixture
def client(tmp_path: Path):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from agent_harness.api import create_api
    from agent_harness.store import EventStore

    q = WorkQueue(str(tmp_path / "w.sqlite"))
    q.add_project(project())
    with TestClient(create_api(EventStore(tmp_path / "e.sqlite"), queue=q, token="tok")) as c:  # noqa: S106
        c.queue = q  # type: ignore[attr-defined]
        yield c


def hdr() -> dict[str, str]:
    return {"Authorization": "Bearer tok"}


def test_start_refuses_rather_than_reporting_a_false_running_state(client) -> None:  # type: ignore[no-untyped-def]
    """The reported bug. `start` used to set RUNNING with no fleet attached
    and return workers=0 -- which reads as success and is not."""
    response = client.post("/api/projects/p/start", headers=hdr())

    assert response.status_code == 409
    assert "not ready" in response.json()["detail"]
    # And crucially: it did NOT mark the project running.
    assert client.queue.control(project_id="p")[0] != "running"


def test_preflight_is_readable_before_starting(client) -> None:  # type: ignore[no-untyped-def]
    body = client.get("/api/projects/p/preflight", headers=hdr()).json()
    assert body["ready"] is False
    assert any(c["name"] == "workers" and not c["ok"] for c in body["checks"])
    assert all("detail" in c for c in body["checks"])


def test_force_is_available_but_must_be_asked_for(client: Any) -> None:
    """An operator who understands the risk can override. It is deliberately
    explicit, because the whole point is that the failure is invisible."""
    response = client.post("/api/projects/p/start?force=true", headers=hdr())
    # Still refused here -- no fleet exists at all, which force cannot conjure.
    assert response.status_code == 409
    assert "no worker pool" in response.json()["detail"]
