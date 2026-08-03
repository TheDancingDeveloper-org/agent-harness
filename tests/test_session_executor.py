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
from agent_harness.executor import Checks, is_disk_exhaustion
from agent_harness.model_client import ModelClient, Response, RetryExhausted, Route
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


def test_completed_and_failed_items_remove_their_worktrees(repo: Path, tmp_path: Path) -> None:
    for exit_code, item_id in ((0, "W1"), (1, "W2")):
        executor, queue = build(repo, tmp_path, FakeDevEnv(agent=add_multiply, exit_code=exit_code))
        add_item(queue, item_id=item_id)
        executor.run_once()
        assert not (tmp_path / "trees" / item_id).exists()


def test_orphaned_worktrees_are_reaped_before_a_worker_starts(repo: Path, tmp_path: Path) -> None:
    executor, queue = build(repo, tmp_path, FakeDevEnv())
    add_item(queue, item_id="W1")
    tree = tmp_path / "trees" / "W1"
    tree.parent.mkdir()
    git(repo, "worktree", "add", "-b", "harness/w1", str(tree), "main")
    assert tree.exists()

    assert executor.reap_orphaned_worktrees() == ["W1"]
    assert not tree.exists()


def test_an_expired_claim_does_not_protect_an_orphaned_worktree(repo: Path, tmp_path: Path) -> None:
    clock = [1000.0]
    queue = make_queue(str(tmp_path / "w.sqlite"), lease_seconds=10.0, now=lambda: clock[0])
    executor = SessionExecutor(
        queue,
        FakeDevEnv(),
        repo,
        reviewer=reviewer("APPROVED\nfine"),
        worktrees=tmp_path / "trees",
        push=False,
        now=lambda: clock[0],
    )
    add_item(queue)
    queue.set_control("running")
    assert queue.claim("dead-worker") is not None
    tree = tmp_path / "trees" / "W1"
    tree.parent.mkdir()
    git(repo, "worktree", "add", "-b", "harness/w1", str(tree), "main")
    clock[0] += 11.0

    assert executor.reap_orphaned_worktrees() == ["W1"]
    assert not tree.exists()


def test_out_of_disk_check_output_has_a_distinct_class() -> None:
    assert is_disk_exhaustion("link failed: No space left on device (os error 28)")
    assert not is_disk_exhaustion("assertion failed: expected 2")


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


def test_a_rejected_review_fails_the_item_but_keeps_the_evidence(
    repo: Path, tmp_path: Path
) -> None:
    """The contract changed with the draft-PR checkpoint (T40), deliberately.

    Work is now committed after the cheap gates and BEFORE review, because
    review is the slowest and most failure-prone step and a worker killed
    during it used to lose everything that had already passed.

    So a rejected item still has a commit. What it must not have is any
    claim of approval: the branch stays, the draft stays a draft, and the
    verdict says why. A rejected attempt that keeps its evidence is a lead;
    one that vanishes is work somebody has to redo to find the same answer.
    """
    devenv = FakeDevEnv(agent=add_multiply)
    executor, queue = build(repo, tmp_path, devenv, verdict="REJECTED\nwrong function")
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == FAILED
    assert "wrong function" in outcome.reason
    # The commit exists -- that is the checkpoint.
    assert git(repo, "log", "--oneline", "harness/w1").count("\n") == 2
    # ...and it does not claim to have been reviewed.
    message = git(repo, "log", "-1", "--format=%B", "harness/w1")
    assert "Reviewed: not yet" in message
    assert "APPROVED" not in message
    # The item never reached `pr` -- nothing was marked ready for review.
    assert "pr" not in outcome.stages


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


# ------------------------------------------------ draft-PR checkpoint (T40)


def test_a_worker_killed_during_review_loses_no_work(repo: Path, tmp_path: Path) -> None:
    """T40's acceptance criterion, as the failure it prevents.

    Review is the slowest, most failure-prone step. Before the checkpoint it
    ran before anything was committed, so a worker killed during it lost
    everything that had already passed the cheap gates -- and the next attempt
    paid for all of it again.
    """
    devenv = FakeDevEnv(agent=add_multiply)

    class DyingReviewer:
        """Stands in for the worker being killed mid-review."""

        def call(self, role: str, messages: object, **kw: object) -> str:
            raise RuntimeError("worker killed during review")

    queue = make_queue(str(tmp_path / "w.sqlite"))
    executor = SessionExecutor(
        queue,
        devenv,
        repo,
        reviewer=DyingReviewer(),  # type: ignore[arg-type]
        worktrees=tmp_path / "trees",
        push=False,
    )
    add_item(queue)
    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == FAILED
    # The commit survived the reviewer dying. That is the whole point.
    assert "commit" in outcome.stages
    assert git(repo, "log", "--oneline", "harness/w1").count("\n") == 2
    assert "Reviewed: not yet" in git(repo, "log", "-1", "--format=%B", "harness/w1")


def test_the_checkpoint_commit_never_claims_to_be_reviewed(repo: Path, tmp_path: Path) -> None:
    """An unreviewed candidate presenting itself as reviewed is the one
    outcome worse than losing it."""
    devenv = FakeDevEnv(agent=add_multiply)
    executor, queue = build(repo, tmp_path, devenv, verdict="APPROVED\nfine")
    add_item(queue)
    executor.run_once()

    log = git(repo, "log", "--format=%B", "harness/w1")
    checkpoint = [m for m in log.split("harness-item:") if "Reviewed: not yet" in m]
    assert checkpoint, "no checkpoint commit was made"


def test_an_approved_item_is_marked_ready_and_a_rejected_one_is_not(
    repo: Path, tmp_path: Path
) -> None:
    """Approval is what takes a draft out of draft. Nothing else does."""

    class RecordingGitHub:
        def __init__(self) -> None:
            self.created: list[dict[str, object]] = []
            self.ready: list[str] = []
            self.comments: list[str] = []

        def create_pr(self, *, title: str, body: str, head: str, base: str, draft: bool = False):  # type: ignore[no-untyped-def]
            self.created.append({"head": head, "draft": draft})
            return f"https://github.com/o/r/pull/{len(self.created)}"

        def mark_pr_ready(self, pr: str) -> None:
            self.ready.append(pr)

        def comment_pr(self, pr: str, body: str) -> None:
            self.comments.append(body)

    for verdict, expect_ready in (("APPROVED\nfine", True), ("REJECTED\nno", False)):
        devenv = FakeDevEnv(agent=add_multiply)
        github = RecordingGitHub()
        queue = make_queue(str(tmp_path / f"w-{expect_ready}.sqlite"))
        executor = SessionExecutor(
            queue,
            devenv,
            repo,
            reviewer=reviewer(verdict),
            github=github,
            worktrees=tmp_path / f"trees-{expect_ready}",
            push=False,
        )
        add_item(queue)
        executor.run_once()

        assert github.created, f"no PR opened for {verdict!r}"
        assert github.created[0]["draft"] is True, "the checkpoint PR was not a draft"
        # The verdict is on the PR either way -- a rejected draft that says
        # why is a lead; one that says nothing is litter.
        assert github.comments, "the verdict was never recorded on the PR"
        assert bool(github.ready) is expect_ready


def test_a_reviewer_failure_after_the_draft_keeps_its_checkpoint(
    repo: Path, tmp_path: Path
) -> None:
    class RecordingGitHub:
        def find_open_pr(self, head: str) -> None:
            return None

        def create_pr(self, **_kw: object) -> str:
            return "https://github.com/o/r/pull/42"

    executor, queue = build(
        repo, tmp_path, FakeDevEnv(agent=add_multiply), github=RecordingGitHub()
    )

    def exhausted(*_args: object, **_kwargs: object) -> None:
        raise RetryExhausted(
            "reviewer retries exhausted; last was transient",
            role="reviewer",
            kind=P.TRANSIENT,
            endpoint="https://e",
            model="m",
        )

    executor.reviewer.call = exhausted  # type: ignore[method-assign,union-attr]
    add_item(queue)

    outcome = executor.run_once()

    assert outcome is not None and outcome.state == "pending"
    row = queue.get("W1")
    assert row is not None
    assert row.pr_url == "https://github.com/o/r/pull/42"
    assert row.branch == "harness/w1"
    assert row.attempts == 1


# ------------------------------------------ a worker that dies mid-session


class DevEnvThatDiesAfterStarting(FakeDevEnv):
    """Creates the session, then the host connection goes away.

    This is the reported failure: the worker thread exits while the session
    is still running, so nothing is waiting on a live PTY.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.deleted: list[str] = []

    def wait_for_exit(self, session_id: str, **kwargs: Any) -> Session:
        raise RuntimeError("session host connection lost")

    def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)


def test_a_session_left_by_a_failed_worker_is_recorded_not_leaked(
    repo: Path, tmp_path: Path
) -> None:
    """The item was already released on failure. The session was not: it kept
    running with nobody waiting on it, possibly with an agent still spending
    tokens, and an unrecorded survivor is indistinguishable from a leak."""
    events: list[dict[str, Any]] = []
    executor, queue = build(repo, tmp_path, DevEnvThatDiesAfterStarting(), events=events)
    add_item(queue)

    outcome = executor.run_once()

    assert outcome is not None and outcome.state == FAILED
    record = queue.get("W1")
    assert record is not None and record.state == FAILED and record.owner is None

    abandoned = queue.abandoned_sessions()
    assert [s["session_id"] for s in abandoned] == ["sess-1"]
    assert "connection lost" in abandoned[0]["reason"]
    assert any(e["outcome"] == "session_orphaned" for e in events)


def test_the_recorded_session_is_reapable(repo: Path, tmp_path: Path) -> None:
    """Recording it is only half the job: the reaper is what stops survivors
    accumulating forever."""
    devenv = DevEnvThatDiesAfterStarting()
    executor, queue = build(repo, tmp_path, devenv)
    add_item(queue)
    executor.run_once()

    executor.session_max_age = 0.0
    report = executor.reap()
    assert report is not None
    assert report.reaped == ["sess-1"]
    assert devenv.killed == ["sess-1"]


def test_a_failure_before_any_session_records_nothing(repo: Path, tmp_path: Path) -> None:
    """No session, nothing to own. A phantom record would send whoever reads
    it looking for a terminal that never existed."""

    class DevEnvThatCannotStart(FakeDevEnv):
        def create_session(self, *args: Any, **kwargs: Any) -> Session:
            raise RuntimeError("no capacity")

    executor, queue = build(repo, tmp_path, DevEnvThatCannotStart())
    add_item(queue)
    executor.run_once()
    assert queue.abandoned_sessions() == []
