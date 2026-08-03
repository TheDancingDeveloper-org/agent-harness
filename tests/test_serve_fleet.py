"""The supervised deployment: one process that owns the API *and* the workers.

`serve` exposed the API and could not execute; `run` executed and exposed no
API. A session host asking the harness to start queued work therefore had
nowhere to send the request — the API refused, correctly, because starting
would only have set a flag with no worker behind it.

Everything here runs against a real queue, a real `Fleet` and the real
executor factory. The session host is faked at its own boundary and the
reviewer's transport is scripted, so no model is called and no network is
touched — which is the only way this wiring can be tested at all.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_harness import providers as P
from agent_harness.api import ROLE_MAP_KEY, create_api
from agent_harness.fleet import Fleet
from agent_harness.model_client import ModelClient, Response, Route
from agent_harness.runtime import NotExecutable, session_executor_factory
from agent_harness.session_executor import AgentSpec, SessionExecutor
from agent_harness.session_host import IDLE, RUNNING, Session
from agent_harness.store import EventStore
from agent_harness.work import DONE, STOPPED, Project, WorkQueue, WorkRecord

TOKEN = "tok"  # noqa: S105 - a fixture, not a credential


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


class FakeHost:
    """A session host whose agent edits the worktree and exits cleanly."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create_session(
        self,
        name: str,
        command: Sequence[str],
        cwd: str,
        env: Mapping[str, str] | None = None,
        scrollback_bytes: int | None = None,
    ) -> Session:
        self.created.append({"name": name, "cwd": cwd, "command": list(command)})
        calc = Path(cwd) / "calc.py"
        calc.write_text(calc.read_text() + "\n\ndef multiply(a, b):\n    return a * b\n")
        return Session(id=f"sess-{len(self.created)}", name=name, activity=RUNNING, cwd=cwd)

    def get_session(self, session_id: str, with_scrollback: bool = False) -> Session:
        return Session(id=session_id, name="s", activity=IDLE, exit_code=0)

    def wait_for_exit(
        self,
        session_id: str,
        *,
        timeout: float = 3600.0,
        poll_seconds: float = 5.0,
        on_waiting: Callable[[Session], None] | None = None,
    ) -> Session:
        return Session(id=session_id, name="s", activity=IDLE, exit_code=0)


def reviewer(verdict: str = "APPROVED\nfine") -> ModelClient:
    def transport(
        route: Route, messages: Sequence[Mapping[str, Any]], options: Mapping[str, Any]
    ) -> Response:
        return Response(200, {}, json.dumps({"choices": [{"message": {"content": verdict}}]}))

    return ModelClient(
        roles={"reviewer": Route("reviewing-model", "https://e", P.GENERIC)},
        transport=transport,
        sleep=lambda _s: None,
    )


def wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def project_for(repo: Path) -> Project:
    return Project(
        project_id="p",
        name="P",
        repo=None,  # no GitHub in a test; the executor simply opens no PR
        work_dir=str(repo),
        base_branch="main",
        checks=["true"],
        max_workers=1,
    )


# --------------------------------------------------------------- the factory


def test_the_factory_builds_an_executor_from_the_project_row(repo: Path, tmp_path: Path) -> None:
    """Project-shaped configuration comes from the project, not from a flag:
    a supervised deployment serves several projects and cannot have one
    checkout on its command line."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.add_project(project_for(repo))
    build = session_executor_factory(queue, host=FakeHost(), agent=AgentSpec())

    executor = build("p")

    assert isinstance(executor, SessionExecutor)
    assert executor.repo == repo
    assert executor.base_branch == "main"
    assert [list(c) for c in executor.checks.commands] == [["true"]]


def test_building_creates_no_workers_and_claims_nothing(repo: Path, tmp_path: Path) -> None:
    """Registering or booting a project must never start work. Only the API's
    start action does."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.add_project(project_for(repo))
    queue.add([WorkRecord(item_id="T1", title="t", brief="b")], project_id="p")

    session_executor_factory(queue, host=FakeHost())("p")

    assert queue.get("T1", project_id="p").state == "pending"  # type: ignore[union-attr]
    assert queue.control(project_id="p")[0] == STOPPED


def test_a_project_with_no_checkout_is_refused_at_build_time(tmp_path: Path) -> None:
    """Rather than returning an executor that fails every item, which costs
    money to discover."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.add_project(Project(project_id="p", name="P"))
    build = session_executor_factory(queue, host=FakeHost())

    with pytest.raises(NotExecutable, match="work_dir"):
        build("p")


# ------------------------------------------------------- API start -> work


@pytest.fixture
def served(repo: Path, tmp_path: Path) -> Any:
    """The whole supervised deployment: API, queue and a real fleet."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    queue.add_project(project_for(repo))
    queue.set_setting(
        ROLE_MAP_KEY,
        {"reviewer": {"model": "reviewing-model", "endpoint": "https://e", "provider": "generic"}},
    )
    host = FakeHost()
    client = reviewer()
    fleet = Fleet(
        queue,
        session_executor_factory(
            queue,
            host=host,
            agent=AgentSpec(command=("claude", "-p", "{prompt_file}"), poll_seconds=0),
            reviewer=client,
            push=False,
        ),
        poll_seconds=0.01,
    )
    store = EventStore(tmp_path / "e.sqlite")
    api = TestClient(create_api(store, queue=queue, token=TOKEN, fleet=fleet, model_client=client))
    with api as c:
        holder: Any = c
        holder.queue, holder.fleet, holder.host = queue, fleet, host
        try:
            yield c
        finally:
            fleet.stop_all()


def hdr() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_the_api_can_start_real_workers_and_reports_them(served: Any) -> None:
    """The gap this closes. `serve` could expose the API or execute work,
    never both, so the documented Work tab had no way to start anything.

    `force` because the preflight's GitHub check asks the real `gh` for push
    permission on a repository that does not exist here. The refusal path is
    covered in test_preflight.py; what is under test here is that a start
    which passes the gate attaches live workers.
    """
    response = served.post("/api/projects/p/start?force=true", headers=hdr())

    assert response.status_code == 200
    assert wait_for(lambda: served.fleet.running().get("p", 0) == 1)
    listed = served.get("/api/projects", headers=hdr()).json()["projects"]
    assert [p["workers"] for p in listed] == [1]


def test_started_workers_actually_execute_the_backlog(served: Any) -> None:
    """Live worker counts would be a lie if nothing behind them ran. The
    agent runs as a session, the checks run, the reviewer approves, the work
    is committed on its own branch."""
    served.queue.add([WorkRecord(item_id="T1", title="Add multiply", brief="b")], project_id="p")

    served.post("/api/projects/p/start?force=true", headers=hdr())

    def finished() -> bool:
        record = served.queue.get("T1", project_id="p")
        return record is not None and record.state == DONE

    assert wait_for(finished), "the item never finished"
    assert served.host.created, "no terminal session was ever created"
    item = served.get("/api/work/T1?project_id=p", headers=hdr()).json()
    assert item["branch"] == "harness/t1"


def test_stopping_drains_rather_than_kills(served: Any) -> None:
    served.post("/api/projects/p/start?force=true", headers=hdr())
    assert wait_for(lambda: served.fleet.running().get("p", 0) == 1)

    response = served.post(
        "/api/projects/p/stop", headers=hdr(), json={"state": "stopped", "reason": "deploying"}
    )

    assert response.status_code == 200
    assert served.fleet.running().get("p", 0) == 0
    state, reason = served.queue.control(project_id="p")
    assert state == STOPPED and reason == "deploying"


def test_monitoring_only_is_still_supported(repo: Path, tmp_path: Path) -> None:
    """A dashboard over someone else's harness should not need a session
    host, a model key or a checkout. Everything readable stays readable; only
    starting is refused."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.add_project(project_for(repo))
    store = EventStore(tmp_path / "e.sqlite")
    with TestClient(create_api(store, queue=queue, token=TOKEN)) as c:
        assert c.get("/api/work", headers=hdr()).status_code == 200
        assert c.get("/api/projects/p", headers=hdr()).json()["workers"] == 0
        refused = c.post("/api/projects/p/start?force=true", headers=hdr())
    assert refused.status_code == 409
    assert "no worker pool" in refused.json()["detail"]
