"""Tests for running work as attachable hosted terminal sessions.

The session host is faked at the HTTP boundary; git and the checks are
real, against a real temporary repository. The agent is simulated by a
callback that edits the worktree — which is exactly what a CLI agent does.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_harness import providers as P
from agent_harness.executor import Checks
from agent_harness.model_client import ModelClient, Response, Route
from agent_harness.session_executor import AgentSpec, SessionExecutor
from agent_harness.session_host import IDLE, RUNNING, WAITING, Session
from agent_harness.work import DONE, FAILED, WorkQueue, WorkRecord
from conftest import make_queue


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
    (path / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


class FakeDevEnv:
    """A session host whose sessions do whatever the test says.

    `agent` is called with the session's cwd when the session is created —
    standing in for the CLI agent editing the worktree.
    """

    def __init__(
        self,
        agent: Callable[[Path], None] | None = None,
        exit_code: int = 0,
        activity_script: Sequence[str] = (),
    ) -> None:
        self.agent = agent
        self.exit_code = exit_code
        self.activity_script = list(activity_script)
        self.created: list[dict[str, Any]] = []
        self.polls = 0
        self.killed: list[str] = []

    # -- the session-host surface the executor uses ---------------------

    def create_session(
        self,
        name: str,
        command: Sequence[str],
        cwd: str,
        env: Mapping[str, str] | None = None,
        scrollback_bytes: int | None = None,
    ) -> Session:
        self.created.append(
            {"name": name, "command": list(command), "cwd": cwd, "env": dict(env or {})}
        )
        if self.agent:
            self.agent(Path(cwd))
        return Session(id="sess-1", name=name, activity=RUNNING, cwd=cwd)

    def get_session(self, session_id: str, with_scrollback: bool = False) -> Session:
        self.polls += 1
        return Session(id=session_id, name="s", activity=IDLE, exit_code=self.exit_code)

    def wait_for_exit(
        self,
        session_id: str,
        *,
        timeout: float = 3600.0,
        poll_seconds: float = 5.0,
        on_waiting: Callable[[Session], None] | None = None,
    ) -> Session:
        for state in self.activity_script:
            if state == WAITING and on_waiting:
                on_waiting(Session(id=session_id, name="s", activity=WAITING))
        if self.exit_code is None:
            return Session(id=session_id, name="s", activity=RUNNING)
        return Session(id=session_id, name="s", activity=IDLE, exit_code=self.exit_code)

    def kill_session(self, session_id: str) -> None:
        self.killed.append(session_id)


def reviewer(verdict: str) -> ModelClient:
    def transport(
        route: Route, messages: Sequence[Mapping[str, Any]], options: Mapping[str, Any]
    ) -> Response:
        return Response(200, {}, json.dumps({"choices": [{"message": {"content": verdict}}]}))

    return ModelClient(
        roles={"reviewer": Route("m", "https://e", P.GENERIC)},
        transport=transport,
        sleep=lambda _s: None,
    )


def add_multiply(tree: Path) -> None:
    """What a well-behaved CLI agent does: edit the worktree, commit nothing."""
    calc = tree / "calc.py"
    calc.write_text(calc.read_text() + "\n\ndef multiply(a, b):\n    return a * b\n")


def build(
    repo: Path,
    tmp_path: Path,
    devenv: FakeDevEnv,
    *,
    verdict: str = "APPROVED\nfine",
    checks: Checks | None = None,
    events: list[dict[str, Any]] | None = None,
    github: Any = None,
) -> tuple[SessionExecutor, WorkQueue]:
    queue = make_queue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    executor = SessionExecutor(
        queue,
        devenv,
        repo,
        agent=AgentSpec(command=("claude", "-p", "{prompt_file}"), poll_seconds=0),
        checks=checks or Checks(),
        reviewer=reviewer(verdict),
        github=github,
        worktrees=tmp_path / "trees",
        ui_base_url="https://devenv.example",
        on_event=(events.append if events is not None else None),
        push=False,
    )
    return executor, queue


def add_item(queue: WorkQueue, item_id: str = "W1") -> None:
    queue.add(
        [
            WorkRecord(
                item_id=item_id,
                title="Add multiply",
                brief="Add a multiply function to calc.py.",
                issue=3,
            )
        ]
    )


# --------------------------------------------------------------- happy path


def test_the_agent_runs_as_a_session_and_the_work_lands(repo: Path, tmp_path: Path) -> None:
    devenv = FakeDevEnv(agent=add_multiply)
    executor, queue = build(repo, tmp_path, devenv)
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == DONE, outcome.reason
    assert outcome.session_id == "sess-1"
    assert "multiply" in git(repo, "show", "harness/w1:calc.py")


def test_the_agent_is_launched_with_the_prompt_file_in_its_own_worktree(
    repo: Path, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    def capture(tree: Path) -> None:
        seen["prompt"] = (tree / ".harness-prompt.md").read_text()
        add_multiply(tree)

    devenv = FakeDevEnv(agent=capture)
    executor, queue = build(repo, tmp_path, devenv, checks=Checks(commands=[["true"]]))
    add_item(queue)
    executor.run_once()

    created = devenv.created[0]
    assert created["command"][0] == "claude"
    assert created["command"][2].endswith(".harness-prompt.md")
    # Its own tree, not the shared repo -- two agents in one working tree is
    # a data race that corrupts both.
    assert created["cwd"] != str(repo)
    assert "W1" in created["cwd"]
    # The brief reached the agent, and so did how it will be judged.
    assert "Add a multiply function" in seen["prompt"]
    assert "`true` must pass" in seen["prompt"]
    assert "Do not commit" in seen["prompt"]


def test_the_prompt_file_is_not_left_in_the_commit(repo: Path, tmp_path: Path) -> None:
    devenv = FakeDevEnv(agent=add_multiply)
    executor, queue = build(repo, tmp_path, devenv)
    add_item(queue)
    executor.run_once()
    assert ".harness-prompt.md" not in git(repo, "show", "--name-only", "harness/w1")


def test_each_item_gets_its_own_worktree_and_it_is_cleaned_up(repo: Path, tmp_path: Path) -> None:
    devenv = FakeDevEnv(agent=add_multiply)
    executor, queue = build(repo, tmp_path, devenv)
    add_item(queue)
    executor.run_once()
    # The branch survives so the work is inspectable; the tree does not, or
    # the disk fills with copies of the repo.
    assert "harness/w1" in git(repo, "branch", "--list", "harness/w1")
    assert not (tmp_path / "trees" / "W1").exists()


# ------------------------------------------------------- the attach story


def test_the_session_id_is_emitted_so_a_human_can_attach(repo: Path, tmp_path: Path) -> None:
    """The whole reason for running agents as hosted sessions."""
    events: list[dict[str, Any]] = []
    devenv = FakeDevEnv(agent=add_multiply)
    executor, queue = build(repo, tmp_path, devenv, events=events)
    add_item(queue)
    executor.run_once()
    started = next(e for e in events if e["outcome"] == "agent_started")
    assert started["session_id"] == "sess-1"
    assert started["session_url"] == "https://devenv.example/t/sess-1"


def test_waiting_for_input_is_surfaced_and_extends_the_lease(repo: Path, tmp_path: Path) -> None:
    """An agent asking a question is neither finished nor hung. Treating it
    as either loses the work or wastes the wait."""
    events: list[dict[str, Any]] = []
    devenv = FakeDevEnv(agent=add_multiply, activity_script=[WAITING])
    executor, queue = build(repo, tmp_path, devenv, events=events)
    add_item(queue)
    executor.run_once()
    waiting = [e for e in events if e["outcome"] == "waiting_for_input"]
    assert waiting
    assert waiting[0]["session_url"].endswith("/t/sess-1")


# ------------------------------------------------------------- failures


def test_a_nonzero_exit_fails_the_item_without_reviewing(repo: Path, tmp_path: Path) -> None:
    devenv = FakeDevEnv(agent=add_multiply, exit_code=2)
    executor, queue = build(repo, tmp_path, devenv)
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == FAILED
    assert "exited 2" in outcome.reason
    assert "review" not in outcome.stages


def test_an_agent_that_changed_nothing_is_reported_as_such(repo: Path, tmp_path: Path) -> None:
    """A CLI agent that decided the task was impossible leaves a clean tree.
    That is a real answer, not a failure to paper over."""
    devenv = FakeDevEnv(agent=None)
    executor, queue = build(repo, tmp_path, devenv)
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == FAILED
    assert "no changes" in outcome.reason


def test_failing_checks_stop_before_the_reviewer(repo: Path, tmp_path: Path) -> None:
    devenv = FakeDevEnv(agent=add_multiply)
    executor, queue = build(
        repo, tmp_path, devenv, checks=Checks(commands=[["sh", "-c", "echo boom >&2; exit 1"]])
    )
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == FAILED
    assert "boom" in outcome.reason
    assert "review" not in outcome.stages


def test_a_rejected_review_does_not_commit(repo: Path, tmp_path: Path) -> None:
    devenv = FakeDevEnv(agent=add_multiply)
    executor, queue = build(repo, tmp_path, devenv, verdict="REJECTED\nwrong function")
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == FAILED
    assert "wrong function" in outcome.reason
    assert git(repo, "log", "--oneline", "harness/w1").count("\n") == 1  # base only


def test_no_reviewer_configured_is_a_rejection_not_an_approval(repo: Path, tmp_path: Path) -> None:
    """Unreviewed work must never be silently treated as reviewed."""
    devenv = FakeDevEnv(agent=add_multiply)
    queue = make_queue(str(tmp_path / "w.sqlite"))
    executor = SessionExecutor(
        queue,
        devenv,
        repo,
        reviewer=None,
        worktrees=tmp_path / "trees",
        push=False,
    )
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == FAILED
    assert "No reviewer is configured" in outcome.reason


def test_a_timeout_leaves_the_session_alive(repo: Path, tmp_path: Path) -> None:
    """Killing it would destroy the agent's context — the one thing that
    makes the item resumable by a human."""
    devenv = FakeDevEnv(agent=add_multiply, exit_code=None)  # type: ignore[arg-type]
    executor, queue = build(repo, tmp_path, devenv)
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == FAILED
    assert "did not finish" in outcome.reason
    assert devenv.killed == []


# ------------------------------------------------------------- integration


def test_real_checks_run_inside_the_worktree(repo: Path, tmp_path: Path) -> None:
    """The agent's change must be verified where the agent made it, not in
    the pristine repo."""

    def add_broken(tree: Path) -> None:
        (tree / "calc.py").write_text("def add(a, b):\n    return a - b\n")

    devenv = FakeDevEnv(agent=add_broken)
    executor, queue = build(
        repo, tmp_path, devenv, checks=Checks(commands=[["python", "-m", "pytest", "-q"]])
    )
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == FAILED
    assert "pytest" in outcome.reason


def test_dependent_work_is_stacked(repo: Path, tmp_path: Path) -> None:
    devenv = FakeDevEnv(agent=add_multiply)
    executor, queue = build(repo, tmp_path, devenv)
    queue.add(
        [
            WorkRecord(item_id="W1", title="one", brief="b"),
            WorkRecord(item_id="W2", title="two", brief="b", depends_on=["W1"]),
        ]
    )
    first = executor.run_once()
    assert first is not None and first.state == DONE

    def add_divide(tree: Path) -> None:
        calc = tree / "calc.py"
        calc.write_text(calc.read_text() + "\n\ndef divide(a, b):\n    return a / b\n")

    devenv.agent = add_divide
    second = executor.run_once()
    assert second is not None
    assert second.state == DONE, second.reason
    assert second.base == "harness/w1"
    final = git(repo, "show", "harness/w2:calc.py")
    assert "multiply" in final and "divide" in final
