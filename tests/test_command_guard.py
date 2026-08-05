"""A refusal is a disposition, not an exception and not a crash.

Two halves, deliberately separated.

The unit half asserts the guard's *screening*: that a pattern catches the ways
one command can be written, that the boundary catches a path reaching out of the
item's tree, and — as important — that ordinary commands are not refused. A guard
that fires on legitimate work gets turned off, and a guard that is off protects
nothing.

The integration half asserts what an operator sees. A blocked command must reach
the queue as `blocked_by_policy` with the rule that fired, must be terminal, and
must not be indistinguishable from a worker that fell over. None of this is a
statement about a model behaving; every test here drives a guard that has already
decided, which is the point of the whole mechanism.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_harness import doctor as D
from agent_harness.executor import Checks
from agent_harness.guard import (
    DEFAULT_REFUSALS,
    GUARD_KEY,
    CommandGuard,
    CommandRefused,
)
from agent_harness.outcomes import (
    BLOCKED,
    BLOCKED_BY_POLICY,
    COMMAND_BLOCKED,
    CRASHED,
    ESCALATE,
    NEEDS_A_PERSON,
    PATH_ESCAPE,
    WORKER_ERROR,
)
from agent_harness.work import WorkQueue
from conftest import make_queue
from test_session_executor import FakeDevEnv, add_item, add_multiply, build, git


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repository, as every executor test in this suite uses.

    Declared rather than imported from the session-executor tests: importing
    another module's fixture makes the name a redefinition in this one, and
    seven lines are cheaper than the indirection.
    """
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


# --------------------------------------------------------------- the patterns


@pytest.mark.parametrize(
    "argv",
    [
        ["sudo", "rm", "app.py"],
        # An absolute path to the program is not an evasion.
        ["/usr/bin/sudo", "-n", "true"],
        # Nor is a flag inserted before the subcommand, nor a remote named first.
        ["git", "-c", "user.name=x", "push", "--force", "origin", "main"],
        # Nor is the short spelling, bundled or not.
        ["git", "push", "-f", "origin", "main"],
        ["git", "push", "-fq", "origin", "main"],
    ],
)
def test_the_default_refusals_hold_however_the_command_is_written(argv: list[str]) -> None:
    """The rule matches the command, not the spelling someone happened to use.

    A refusal list that only catches the exact string it was written as is a
    list that catches accidents and nothing else.
    """
    refusal = CommandGuard().screen(argv, cwd=Path("/tmp"))
    assert refusal is not None
    assert refusal.reason_kind == COMMAND_BLOCKED
    assert refusal.rule in DEFAULT_REFUSALS


@pytest.mark.parametrize(
    "argv",
    [
        ["uv", "run", "pytest", "-q"],
        ["python", "-m", "pytest", "tests/unit"],
        # The ordinary push the harness exists to do.
        ["git", "push", "origin", "HEAD"],
        ["ruff", "check", "."],
    ],
)
def test_ordinary_work_is_not_refused(argv: list[str], tmp_path: Path) -> None:
    """The cost of a false refusal is the whole item, so it is asserted."""
    assert CommandGuard().screen(argv, cwd=tmp_path) is None


# -------------------------------------------------------------- the boundary


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-rf", "/"],
        ["cat", "../../../etc/passwd"],
        ["cp", "key", "~/.ssh/authorized_keys"],
        ["tar", "-cf", "/tmp/elsewhere/out.tar", "."],
        # The path hidden in the second half of one token.
        ["dd", "if=/dev/zero", "of=/dev/sda"],
    ],
)
def test_a_command_may_not_reach_outside_the_item_tree(argv: list[str], tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    refusal = CommandGuard().screen(argv, cwd=tree)
    assert refusal is not None
    assert refusal.reason_kind == PATH_ESCAPE
    assert str(tree.resolve()) in refusal.detail


def test_paths_inside_the_tree_are_the_normal_case(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    guard = CommandGuard()
    assert guard.screen(["pytest", "tests/unit", "--cov=src/pkg"], cwd=tree) is None
    assert guard.screen(["python", "./scripts/build.py"], cwd=tree) is None
    # argv[0] is the program, and an absolute path to an interpreter is how a
    # check is normally written.
    assert guard.screen(["/usr/bin/python3", "-m", "pytest"], cwd=tree) is None


def test_the_boundary_can_be_widened_but_the_widening_is_explicit(tmp_path: Path) -> None:
    assert CommandGuard(confine=False).screen(["cat", "/etc/passwd"], cwd=tmp_path) is None


# ---------------------------------------------------------------- configuring


def test_a_deployment_policy_is_configuration_not_code(tmp_path: Path) -> None:
    """A refusal list belongs in the database, alongside the role map."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.set_setting(GUARD_KEY, CommandGuard(refusals=("sh -c",)).as_settings())
    guard = CommandGuard.from_settings(queue.get_setting(GUARD_KEY))
    assert guard.configured
    refusal = guard.screen(["sh", "-c", "echo hi"], cwd=tmp_path)
    assert refusal is not None
    assert refusal.rule == "sh -c"
    # The built-in default is still in force alongside it.
    assert guard.screen(["sudo", "true"], cwd=tmp_path) is not None


def test_an_unconfigured_guard_still_refuses_but_does_not_claim_to_be_chosen() -> None:
    guard = CommandGuard.from_settings(None)
    assert guard.active
    assert not guard.configured


# ------------------------------------------------------- what an operator sees


def test_a_refused_check_never_reaches_a_subprocess(tmp_path: Path) -> None:
    """The refusal happens before the command exists, not after it has run."""
    with pytest.raises(CommandRefused) as caught:
        Checks(commands=[["sudo", "make", "install"]]).run(tmp_path)
    assert caught.value.refusal.reason_kind == COMMAND_BLOCKED


def test_a_refusal_produces_a_disposition_not_a_generic_error(
    repo: Path,
    tmp_path: Path,
) -> None:
    """The whole point of the issue, asserted end to end.

    An operator reading the queue must be able to tell "refused by policy" from
    "the worker fell over" without opening a log — so the row itself has to say
    it, in the taxonomy `outcomes.py` already defines.
    """
    devenv = FakeDevEnv(agent=add_multiply)
    executor, queue = build(
        repo, tmp_path, devenv, checks=Checks(commands=[["sudo", "make", "install"]])
    )
    add_item(queue)

    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.stop is not None
    assert outcome.disposition == BLOCKED_BY_POLICY
    assert outcome.reason_kind == COMMAND_BLOCKED
    # Not a crash, and not a gate's verdict about the diff.
    assert outcome.disposition != CRASHED
    assert outcome.reason_kind != WORKER_ERROR
    # And it is on the row, not only in the returned object.
    row = queue.get("W1")
    assert row is not None
    assert row.state == BLOCKED
    assert row.disposition == BLOCKED_BY_POLICY
    assert row.reason_kind == COMMAND_BLOCKED
    assert "sudo" in (row.last_error or "")
    assert outcome.disposition in NEEDS_A_PERSON


def test_a_refusal_is_terminal_and_the_item_is_not_handed_back(
    repo: Path,
    tmp_path: Path,
) -> None:
    """Owner decision, 2026-08-05: blocked, stopped, not returned to the agent.

    The cost accepted is visible here — the item is gone until a person looks
    at it, even though a permitted equivalent may have existed.
    """
    devenv = FakeDevEnv(agent=add_multiply)
    executor, queue = build(
        repo, tmp_path, devenv, checks=Checks(commands=[["sudo", "make", "install"]])
    )
    add_item(queue)
    executor.run_once()

    assert queue.claim("someone-else") is None
    assert executor.run_once() is None


def test_a_refused_agent_command_never_starts_a_session(
    repo: Path,
    tmp_path: Path,
) -> None:
    """The guard runs before the host is asked, because after it there is no say.

    `$HARNESS_AGENT_COMMAND` is configuration, and configuration is the seat
    where a whole fleet gets pointed at something it should not run.
    """
    from agent_harness.session_executor import AgentSpec

    devenv = FakeDevEnv(agent=add_multiply)
    executor, queue = build(repo, tmp_path, devenv)
    executor.agent = AgentSpec(command=("sudo", "claude", "-p", "{prompt_file}"), poll_seconds=0)
    add_item(queue)

    outcome = executor.run_once()

    assert devenv.created == []
    assert outcome is not None
    assert outcome.disposition == BLOCKED_BY_POLICY


def test_the_event_stream_names_the_rule_that_fired(
    repo: Path,
    tmp_path: Path,
) -> None:
    events: list[dict[str, Any]] = []
    executor, queue = build(
        repo,
        tmp_path,
        FakeDevEnv(agent=add_multiply),
        checks=Checks(commands=[["sudo", "make", "install"]]),
        events=events,
    )
    add_item(queue)
    executor.run_once()

    blocked = [e for e in events if e["outcome"] == "command_blocked"]
    assert blocked, [e["outcome"] for e in events]
    assert blocked[0]["error_class"] == COMMAND_BLOCKED


def test_the_headless_executor_refuses_on_the_same_terms(tmp_path: Path) -> None:
    """Both executors, one policy. A guard on one path is a gap on the other."""
    from test_executor import DIFF
    from test_executor import add_item as add_headless_item
    from test_executor import build as build_headless
    from test_executor import git as headless_git

    work = tmp_path / "hrepo"
    work.mkdir()
    headless_git(work, "init", "-q", "-b", "main")
    headless_git(work, "config", "user.email", "t@t")
    headless_git(work, "config", "user.name", "t")
    (work / "hello.txt").write_text("hello world\n")
    headless_git(work, "add", "-A")
    headless_git(work, "commit", "-q", "-m", "initial")

    executor, queue, _ = build_headless(
        work,
        tmp_path,
        {"implementer": f"```diff\n{DIFF}```", "reviewer": "APPROVED"},
        checks=Checks(commands=[["sudo", "make", "install"]]),
    )
    add_headless_item(queue)
    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.disposition == BLOCKED_BY_POLICY
    assert outcome.state == BLOCKED


def test_without_a_guard_the_same_item_runs_the_command(
    repo: Path,
    tmp_path: Path,
) -> None:
    """What the guard is actually buying, stated as the counterfactual.

    With every refusal switched off, the same item runs `false` — the harness
    executes what it is given — and stops as an ordinary checks failure. The
    disposition in the tests above therefore comes from the guard and from
    nothing else.
    """
    off = CommandGuard(defaults=False, confine=False)
    executor, queue = build(
        repo,
        tmp_path,
        FakeDevEnv(agent=add_multiply),
        checks=Checks(commands=[["false"]], guard=off),
    )
    executor.guard = off
    add_item(queue)

    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.disposition != BLOCKED_BY_POLICY


# ------------------------------------------------ the declared fix (#155/#197)


def test_a_declared_fix_is_screened_before_it_is_run(repo: Path) -> None:
    """A fix is argv the harness runs on an item's behalf, so policy applies.

    `unconfined_argument` (#155) already refuses a fix that reaches outside the
    tree, and that answer is left alone. This is the dimension it cannot see: a
    fix naming a program this deployment will not run at all. No formatter needs
    one, and a fix that asks for one must not get it.
    """
    check = ["python", "-c", "import sys; sys.exit(1)"]
    checks = Checks(
        commands=[check],
        fixes={" ".join(check): ["sudo", "formatter", "--write", "."]},
        apply_fixes=True,
    )

    with pytest.raises(CommandRefused) as caught:
        checks.run(repo)

    assert caught.value.refusal.reason_kind == COMMAND_BLOCKED
    assert caught.value.refusal.rule == "sudo"


def test_a_fix_reaching_outside_the_tree_still_escalates_as_it_did(repo: Path) -> None:
    """The narrower gate keeps its own answer; the guard did not take it over.

    Two mechanisms refusing the same command with different dispositions would
    be two policies disagreeing, so the specific one answers first.
    """
    check = ["python", "-c", "import sys; sys.exit(1)"]
    checks = Checks(
        commands=[check],
        fixes={" ".join(check): ["formatter", "--write", "/etc/hosts"]},
        apply_fixes=True,
    )

    result = checks.run(repo)

    assert result.outcome == ESCALATE
    assert "outside the item's worktree" in result.detail


def test_the_fix_path_asks_the_gate_through_the_screened_call(repo: Path) -> None:
    """A post-fix re-run is a check running, so it is screened like one.

    Asserted by screening at `_run_one` rather than in `run`: every way a gate
    is asked — first pass, re-run after a fix, and the re-run of gates the fix
    invalidated — goes through the one call.
    """
    checks = Checks(commands=[["sudo", "make", "check"]], apply_fixes=True)
    with pytest.raises(CommandRefused):
        checks.run(repo)


# --------------------------------------------------------------------- doctor


def _guard_finding(report: D.Report) -> D.Finding:
    found = [f for f in report.environment if f.name == "command guard"]
    assert found, [f.name for f in report.environment]
    return found[0]


def test_doctor_reports_an_unconfigured_guard_as_not_configured(tmp_path: Path) -> None:
    """A guard nobody enabled is not a guard, and must not read as a pass."""
    queue = make_queue(str(tmp_path / "w.sqlite"))
    finding = _guard_finding(D.diagnose(queue, []))
    assert finding.state == D.WARN
    assert not finding.ok
    assert "not configured" in finding.detail


def test_doctor_reports_a_configured_guard_in_the_same_voice(tmp_path: Path) -> None:
    queue = make_queue(str(tmp_path / "w.sqlite"))
    queue.set_setting(GUARD_KEY, CommandGuard(refusals=("sh -c",), configured=True).as_settings())
    finding = _guard_finding(D.diagnose(queue, []))
    assert finding.state == D.OK
    assert "sh -c" in finding.detail


def test_doctor_reports_a_switched_off_guard_as_a_failure(tmp_path: Path) -> None:
    queue = make_queue(str(tmp_path / "w.sqlite"))
    queue.set_setting(
        GUARD_KEY, CommandGuard(defaults=False, confine=False, configured=True).as_settings()
    )
    finding = _guard_finding(D.diagnose(queue, []))
    assert finding.state == D.FAIL
