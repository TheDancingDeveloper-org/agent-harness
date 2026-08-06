"""#155: a formatter's fix may be applied and its check re-run. A test's may not.

The observation behind this file is measured, not asserted: on `rdpapp`, four of
seven attempts at one item were refused by `cargo fmt --all -- --check` and by
nothing else, while being substantively correct every time. A formatter in check
mode is column arithmetic, no model can compute what `rustfmt` would have done,
and the operator had already declared the command that fixes it deterministically.

So the harness may now run a **declared** fix and re-run the check. The boundary
is the whole point, and most of this file is about the boundary rather than
about the formatter:

- the fix is run and the check re-run, and **the re-run is the verdict**;
- a genuine test failure is not cleared by having a fix run, and still refuses
  the item — the most important test here;
- a "fix" that deletes what was failing escalates instead of passing;
- a fix cannot buy one gate by breaking another that had already passed;
- nothing the harness changed is invisible: it is in the event stream, in the
  paths on the result, and in the reviewer's prompt.

No formatter is named anywhere. The fixture formatter is a two-line Python
script, exactly as `cargo fmt`, `ruff format` and `prettier --write` are all
just configured commands.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_harness import outcomes as O
from agent_harness import providers as P
from agent_harness.executor import Checks, Executor
from agent_harness.model_client import ModelClient, Response, RetryPolicy, Route
from agent_harness.work import DONE, FAILED, WorkQueue, WorkRecord
from conftest import make_queue

# The implementer's answer: correct work that the fixture formatter objects to,
# which is the shape of every attempt the issue reports.
DIFF = """\
diff --git a/hello.txt b/hello.txt
index 3b18e51..8c7e5a6 100644
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-HELLO WORLD
+hello harness
"""


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
    (path / "hello.txt").write_text("HELLO WORLD\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


def python(*code: str) -> list[str]:
    return [sys.executable, "-c", "; ".join(code)]


#: A formatter, in the only sense that matters here: it computes one canonical
#: rendering of what the file already says. Shouting is this project's house
#: style, and no model can be taught to guess a house style reliably.
FORMAT_CHECK = python(
    "import pathlib, sys",
    "t = pathlib.Path('hello.txt').read_text()",
    "sys.exit(0 if t == t.upper() else 1)",
)
FORMAT_FIX = python(
    "import pathlib",
    "p = pathlib.Path('hello.txt')",
    "p.write_text(p.read_text().upper())",
)

#: A test suite. It fails because the work is wrong, and no rewriting of
#: whitespace will make it pass.
FAILING_TEST = python("import sys", "sys.stderr.write('assert 1 == 2')", "sys.exit(1)")


class Capturing:
    """A scripted model that keeps every prompt, so the reviewer's can be read."""

    def __init__(self, replies: Mapping[str, str]) -> None:
        self.replies = dict(replies)
        self.prompts: dict[str, str] = {}

    def __call__(
        self, route: Route, messages: Sequence[Mapping[str, Any]], options: Mapping[str, Any]
    ) -> Response:
        role = str(route.options.get("role", route.model))
        self.prompts[role] = str(messages[-1]["content"])
        body = json.dumps({"choices": [{"message": {"content": self.replies.get(role, "ok")}}]})
        return Response(200, {}, body)


APPROVING = {"planner": "plan", "implementer": DIFF, "reviewer": "APPROVED\nfine"}


def build(
    repo: Path,
    tmp_path: Path,
    checks: Checks,
    *,
    events: list[dict[str, Any]] | None = None,
) -> tuple[Executor, WorkQueue, Capturing]:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    queue = make_queue(str(tmp_path / "state" / "w.sqlite"), lease_seconds=100.0)
    transport = Capturing(APPROVING)
    client = ModelClient(
        roles={
            role: Route(f"model-{role}", "https://api.example", P.GENERIC, options={"role": role})
            for role in ("planner", "implementer", "reviewer")
        },
        transport=transport,
        policy=RetryPolicy(max_attempts=2, backoff_seconds=0.001),
        sleep=lambda _s: None,
    )
    executor = Executor(
        queue,
        client,
        repo,
        checks=checks,
        on_event=(events.append if events is not None else None),
        push=False,
    )
    queue.add(
        [
            WorkRecord(
                item_id="T1",
                title="Change the greeting",
                brief="Change hello.txt to say 'hello harness'.",
            )
        ]
    )
    return executor, queue, transport


# --------------------------------------------- the case the issue is about


def test_a_formatter_failure_is_cleared_by_its_declared_fix_and_the_item_proceeds(
    repo: Path, tmp_path: Path
) -> None:
    """The correct change that only the formatter objected to now lands.

    Before this, `fix_available` was terminal: the item was refused, and the
    next attempt paid for a planner and an implementer to get the column
    arithmetic wrong somewhere else.
    """
    checks = Checks(
        commands=[FORMAT_CHECK],
        fixes={" ".join(FORMAT_CHECK): FORMAT_FIX},
        apply_fixes=True,
    )
    executor, queue, _ = build(repo, tmp_path, checks)

    executor.run_once()

    record = queue.get("T1")
    assert record is not None
    assert record.state == DONE, record.last_error
    # And the formatted text is what was committed — the reviewer read the code
    # that would have been merged, which is the argument for allowing this.
    assert (repo / "hello.txt").read_text() == "HELLO HARNESS\n"


def test_a_declared_fix_is_still_only_recorded_unless_the_operator_turned_it_on(
    repo: Path, tmp_path: Path
) -> None:
    """Off by default. An upgrade changes nothing until somebody says so."""
    (repo / "hello.txt").write_text("hello harness\n")
    checks = Checks(commands=[FORMAT_CHECK], fixes={" ".join(FORMAT_CHECK): FORMAT_FIX})
    result = checks.run(repo)
    assert result.outcome == O.FIX_AVAILABLE
    assert result.applied == ()
    assert (repo / "hello.txt").read_text() == "hello harness\n", "the fix ran; it must not have"


# ------------------------- the boundary: a test is not mechanically fixable


def test_a_genuine_test_failure_is_not_cleared_by_running_a_fix(repo: Path, tmp_path: Path) -> None:
    """**The important one.** Running a fix must not launder a failing test.

    A test failure is a statement about behaviour. The fix runs, the check is
    re-run, and the re-run is the verdict: it still fails, so the item is
    refused exactly as it was before any of this existed, and it costs an
    attempt exactly as it did.
    """
    # The fix leaves a mark on a file that is already tracked, so the test can
    # prove it *ran*. A test asserting that a failure survives a fix nobody
    # applied would assert nothing at all.
    fix = python("import pathlib", "pathlib.Path('hello.txt').write_text('THE FIX RAN\\n')")
    checks = Checks(
        commands=[FAILING_TEST],
        fixes={" ".join(FAILING_TEST): fix},
        apply_fixes=True,
    )
    executor, queue, _ = build(repo, tmp_path, checks)

    executor.run_once()

    record = queue.get("T1")
    assert record is not None
    assert record.state == FAILED, "a failing test refuses the item, fix or no fix"
    assert record.attempts == 1, "and costs an attempt, exactly as it always did"
    assert "assert 1 == 2" in (record.last_error or ""), "the real failure is what is reported"

    # The gate is asked twice and answers twice. It is not offered as fixable a
    # second time: the fix is spent, and a result that still advertised one
    # would invite the grinding-down this whole boundary forbids.
    result = checks.run(repo)
    assert result.outcome == O.FAIL
    assert result.fix == ()
    assert result.applied[0].cleared is False
    assert result.applied[0].fix == tuple(fix)
    assert result.applied[0].paths == ("hello.txt",), "the fix ran, and it still did not help"
    assert (repo / "hello.txt").read_text() == "THE FIX RAN\n"


def test_a_fix_that_deletes_what_was_failing_escalates_rather_than_passing(
    repo: Path, tmp_path: Path
) -> None:
    """The laundering attempt that would otherwise work, refused structurally.

    A check that reads a file, and a "fix" that deletes it, is the shape of
    every attempt to make a gate stop asking. A formatter rewrites files that
    exist; it never adds, removes or renames one. So that is the line, and
    crossing it needs a person rather than passing the item.
    """
    check = python(
        "import pathlib, sys",
        "sys.exit(0 if not pathlib.Path('hello.txt').exists() else 1)",
    )
    fix = python("import pathlib", "pathlib.Path('hello.txt').unlink()")
    checks = Checks(commands=[check], fixes={" ".join(check): fix}, apply_fixes=True)

    result = checks.run(repo)

    assert result.outcome == O.ESCALATE, "the check would have passed; it must not be allowed to"
    assert "hello.txt" in result.detail
    assert "must not be declared" in result.detail
    assert result.applied[0].cleared is False


def test_a_fix_cannot_buy_one_gate_by_breaking_another_that_already_passed(
    repo: Path, tmp_path: Path
) -> None:
    """Every declared check passes on the tree as it now stands, or none does.

    Without re-running what ran before the fix, the guarantee would only be
    "every check passed at some point" — and a fix that repaired the formatter
    by breaking the test suite that had already run would ship.
    """
    guard = python(
        "import pathlib, sys",
        "sys.exit(0 if 'HELLO' in pathlib.Path('hello.txt').read_text() else 1)",
    )
    second = python(
        "import pathlib, sys",
        "sys.exit(0 if pathlib.Path('hello.txt').read_text() == 'gone\\n' else 1)",
    )
    fix = python("import pathlib", "pathlib.Path('hello.txt').write_text('gone\\n')")
    checks = Checks(
        commands=[guard, second],
        fixes={" ".join(second): fix},
        apply_fixes=True,
    )

    result = checks.run(repo)

    assert result.outcome == O.FAIL
    assert result.command == tuple(guard), "the gate the fix broke is the one that reports"
    assert result.applied[0].cleared is True, "and what the harness did is on the record"


def test_a_fix_naming_a_path_outside_the_worktree_is_refused_before_it_runs(
    repo: Path, tmp_path: Path
) -> None:
    """A fix is a command against the item's own tree, and nothing else."""
    outside = tmp_path / "not-in-the-tree"
    check = python("import sys", "sys.exit(1)")
    checks = Checks(
        commands=[check],
        fixes={" ".join(check): ["formatter", "--write", str(outside)]},
        apply_fixes=True,
    )

    result = checks.run(repo)

    assert result.outcome == O.ESCALATE
    assert "outside the item's worktree" in result.detail
    assert not outside.exists()
    assert result.applied == (), "nothing ran, so nothing is recorded as having run"


# ------------------------------------------ what the harness changed is visible


def test_the_fix_is_announced_in_the_event_stream_with_the_paths_it_rewrote(
    repo: Path, tmp_path: Path
) -> None:
    """An operator must never have to diff two commits to learn this."""
    events: list[dict[str, Any]] = []
    checks = Checks(
        commands=[FORMAT_CHECK],
        fixes={" ".join(FORMAT_CHECK): FORMAT_FIX},
        apply_fixes=True,
    )
    executor, _, _ = build(repo, tmp_path, checks, events=events)

    executor.run_once()

    announced = [e for e in events if e.get("outcome") == "check_fix_applied"]
    assert len(announced) == 1
    assert "hello.txt" in announced[0]["detail"]
    assert "cleared it" in announced[0]["detail"]


def test_the_reviewer_is_told_the_harness_modified_the_tree(repo: Path, tmp_path: Path) -> None:
    """Do not silently mutate a diff a reviewer is about to read.

    The reviewer is given the post-fix diff — that is the code that would be
    merged — *and* told which lines in it the harness rather than the agent
    produced. A reviewer that believes every line was the agent's is being
    misled by omission.
    """
    checks = Checks(
        commands=[FORMAT_CHECK],
        fixes={" ".join(FORMAT_CHECK): FORMAT_FIX},
        apply_fixes=True,
    )
    executor, _, transport = build(repo, tmp_path, checks)

    executor.run_once()

    prompt = transport.prompts["reviewer"]
    assert "The harness itself modified this tree" in prompt
    assert "hello.txt" in prompt
    assert " ".join(FORMAT_FIX) in prompt
    # The diff it is shown is the tree as it now stands, not as it stood before
    # the fix — otherwise it would be reviewing something that never existed.
    assert "+HELLO HARNESS" in prompt


def test_a_fix_that_ran_and_failed_is_announced_too(repo: Path, tmp_path: Path) -> None:
    """The tree an operator will open is the one the fix ran against.

    Announcing only the fixes that worked would leave them looking at edits
    nothing in the stream accounts for.
    """
    events: list[dict[str, Any]] = []
    fix = python("import pathlib", "pathlib.Path('hello.txt').write_text('MOVED ALONG\\n')")
    checks = Checks(
        commands=[FAILING_TEST],
        fixes={" ".join(FAILING_TEST): fix},
        apply_fixes=True,
    )
    executor, _, _ = build(repo, tmp_path, checks, events=events)

    executor.run_once()

    announced = [e for e in events if e.get("outcome") == "check_fix_applied"]
    assert len(announced) == 1
    assert "did NOT clear it" in announced[0]["detail"]
    assert "hello.txt" in announced[0]["detail"]


# ------------------------------------------------------------ configuration


def test_the_permission_is_persisted_with_the_project(tmp_path: Path) -> None:
    """It is a property of the project, so a restart does not silently drop it
    back to off — or, worse, leave it on when nobody remembers saying so."""
    from agent_harness.runtime import _checks_for
    from agent_harness.work import Project

    queue = make_queue(str(tmp_path / "w.sqlite"))
    queue.add_project(
        Project(
            project_id="widgets",
            name="Widgets",
            checks=["fmt --check"],
            fixes={"fmt --check": ["fmt"]},
            apply_fixes=True,
        )
    )
    project = queue.get_project("widgets")
    assert project is not None
    assert project.apply_fixes is True
    assert _checks_for(project).apply_fixes is True


def test_the_permission_with_nothing_to_permit_is_refused() -> None:
    """`apply_fixes` is permission to run the fixes you named, not a mode. With
    nothing named it does nothing, and reading it as "the harness will repair
    failures" would be exactly the wrong thing to believe."""
    import pydantic

    from agent_harness.schemas import ProjectSpec

    with pytest.raises(pydantic.ValidationError, match="no fixes are declared"):
        ProjectSpec(project_id="p", name="P", checks=["fmt --check"], apply_fixes=True)
