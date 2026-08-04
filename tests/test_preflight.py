"""Can this project actually finish an item?

The failure this prevents is not an error -- it is a fleet that claims work,
spends the implementer's tokens, and fails every item while reporting
`running`. A nonproductive fleet and a productive one look identical from
outside until the bill arrives.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_harness.preflight import (
    Answer,
    _is_clean_tree,
    clean_checks_probe,
    preflight_project,
    remembered,
    role_reachability_probe,
)
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
    kw.setdefault("disk_probe", lambda path, floor: (True, f"100 GiB free at {path}"))
    kw.setdefault("clean_probe", lambda path: (True, "clean"))
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


def test_disk_space_is_reported_and_a_configured_floor_blocks() -> None:
    report = run(
        project(min_free_disk_gb=50),
        disk_probe=lambda path, floor: (
            False,
            f"12.0 GiB free on volume holding {path}; configured floor {floor:.1f} GiB",
        ),
    )
    assert not report.ready
    disk = next(c for c in report.checks if c.name == "disk space")
    assert "12.0 GiB free" in disk.detail and "50.0 GiB" in disk.detail


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


def test_the_opt_in_clean_base_probe_blocks_on_the_first_failed_check(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True)
    p = project(work_dir=str(repo), checks=["false", "true"])

    report = run(p, checks_probe=clean_checks_probe(p))

    assert not report.ready
    base = next(c for c in report.checks if c.name == "base checks")
    assert "`false` failed on base branch" in base.detail


# -------------------------------------------------- does the model answer?


class Model:
    """A route, as much of one as a probe needs to name it."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.endpoint = "https://e"


def answering(seconds: float = 0.2) -> Any:
    return lambda route: Answer(True, f"{route.model} answered", seconds)


def test_a_configured_model_that_never_answers_blocks_the_start() -> None:
    """The reported cost: a reviewer that is advertised and not served passed
    every gate, and each item then ran to completion and failed at the last
    step -- six attempts with escalating backoff, per item, to establish a
    condition that was true before the run started."""
    probe = role_reachability_probe(
        {"reviewer": Model("claude-sonnet-4-6")},
        lambda route: Answer(False, f"{route.model} returned HTTP 504", 1.0),
    )

    report = run(project(), role_probe=probe)

    assert not report.ready
    assert "claude-sonnet-4-6" in report.summary() and "504" in report.summary()


def test_a_model_that_is_merely_slow_warns_and_is_not_refused() -> None:
    """A generous deadline and a warning. A late answer is still an answer,
    and refusing one would be this gate overruling an operator about an
    endpoint the harness does not own."""
    probe = role_reachability_probe(
        {"reviewer": Model("slow-model")}, answering(seconds=14.0), timeout=10.0, patience=20.0
    )

    report = run(project(), role_probe=probe)

    assert report.ready
    assert [c.name for c in report.warnings] == ["model latency"]
    assert "14s" in next(c for c in report.warnings if c.name == "model latency").detail


def test_a_model_that_answers_promptly_is_reported_and_passes() -> None:
    report = run(
        project(), role_probe=role_reachability_probe({"reviewer": Model("m")}, answering())
    )

    assert report.ready
    check = next(c for c in report.checks if c.name == "role reachability")
    assert check.ok and "m answered" in check.detail


def test_a_request_that_never_returns_is_a_failure_not_a_hang() -> None:
    """The report must not wait for the thing it is reporting on. A probe
    thread that never returns would otherwise hold the HTTP request open for
    as long as the model takes to not answer."""
    released = threading.Event()

    def never(route: Any) -> Answer:
        released.wait(30.0)
        return Answer(True, "eventually", 30.0)

    probe = role_reachability_probe({"reviewer": Model("wedged")}, never, patience=0.2)
    try:
        report = run(project(), role_probe=probe)
    finally:
        released.set()

    assert not report.ready
    assert "wedged did not answer within 0.2s" in report.summary()


def test_the_roles_are_asked_in_parallel_not_one_after_another() -> None:
    """Three roles, one deadline. Serial probes would multiply the worst case
    by the number of roles, which is how a five-second refusal becomes a
    request nobody waits for."""
    arrived = threading.Barrier(3, timeout=10.0)

    def wait_for_the_others(route: Any) -> Answer:
        arrived.wait()
        return Answer(True, f"{route.model} answered", 0.1)

    probe = role_reachability_probe(
        {"planner": Model("a"), "implementer": Model("b"), "reviewer": Model("c")},
        wait_for_the_others,
    )

    assert run(project(), role_probe=probe).ready


def test_the_same_model_is_not_asked_again_on_the_next_poll() -> None:
    """Readiness is polled. A probe on every poll would make a dashboard a
    load generator against the model endpoint, and bill for it."""
    asked: list[str] = []
    clock = [1000.0]

    def ask_once(route: Any) -> Answer:
        asked.append(route.model)
        return Answer(True, f"{route.model} answered", 0.1)

    ask = remembered(ask_once, ttl=60.0, now=lambda: clock[0])
    probe = role_reachability_probe({"reviewer": Model("m")}, ask)

    probe()
    probe()
    assert asked == ["m"]

    clock[0] += 61.0
    probe()
    assert asked == ["m", "m"]


def test_project_registration_rejects_shell_check_syntax(client: Any) -> None:
    response = client.post(
        "/api/projects",
        headers=hdr(),
        json={"project_id": "bad", "name": "bad", "checks": ["npm test && npm build"]},
    )
    assert response.status_code == 422
    assert "argv, not shell" in response.text


def test_start_can_opt_into_the_same_blocking_base_check(client: Any) -> None:
    client.queue.add_project(project(checks=["false"]))
    response = client.post("/api/projects/p/start?check_base=true", headers=hdr())
    assert response.status_code == 409
    assert "base checks" in response.json()["detail"]


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


# ------------------------------- a checkout a run would destroy (#147)


def _repo(path: Path) -> Path:
    """A real git repository, because this probe reads real git output."""
    path.mkdir(parents=True, exist_ok=True)
    for argv in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(path), *argv], check=True, capture_output=True)
    (path / "tracked.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "initial"], check=True, capture_output=True
    )
    return path


def test_a_clean_checkout_passes(tmp_path: Path) -> None:
    ok, detail = _is_clean_tree(str(_repo(tmp_path / "r")))
    assert ok
    assert detail == "clean"


def test_an_untracked_file_is_reported_as_about_to_be_deleted(tmp_path: Path) -> None:
    """The measured near-miss: 136 uncommitted files, two of them whole crates,
    in a checkout a headless run was about to `git clean -fd`."""
    repo = _repo(tmp_path / "r")
    (repo / "new-crate").mkdir()
    (repo / "new-crate" / "lib.rs").write_text("pub fn f() {}\n")

    ok, detail = _is_clean_tree(str(repo))

    assert not ok
    assert "1 untracked path(s)" in detail
    assert "DELETED" in detail, "the word matters — this is not recoverable"
    assert "--allow-dirty" in detail


def test_a_modified_tracked_file_is_reported_as_about_to_be_reverted(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r")
    (repo / "tracked.txt").write_text("two\n")

    ok, detail = _is_clean_tree(str(repo))

    assert not ok
    assert "1 uncommitted change(s)" in detail


def test_a_dirty_checkout_blocks_a_start(tmp_path: Path) -> None:
    report = run(project(), clean_probe=lambda path: (False, "3 uncommitted change(s)"))

    assert not report.ready
    assert any(c["name"] == "clean checkout" and not c["ok"] for c in report.as_dict()["checks"])


def test_allow_dirty_lets_it_start_and_still_says_what_is_at_risk(tmp_path: Path) -> None:
    """Overridable, never silent: the detail survives into the report so the
    decision is auditable rather than merely made."""
    report = run(
        project(),
        clean_probe=lambda path: (False, "3 uncommitted change(s)"),
        allow_dirty=True,
    )

    assert report.ready
    check = next(c for c in report.as_dict()["checks"] if c["name"] == "clean checkout")
    assert check["ok"]
    assert "3 uncommitted change(s)" in check["detail"]
    assert "Allowed by --allow-dirty" in check["detail"]
