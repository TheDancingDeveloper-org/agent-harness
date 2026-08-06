"""The direct executor, end to end, against a real repository and real gates.

Same principle as `test_agent_loop_e2e.py`, pointed at the other executor: the
model is scripted, everything else is real — a real temporary git repository, a
real `git apply`, real check commands that are shell scripts, and the real
queue. The item is driven exactly as `run` drives it (claim, plan, implement,
apply, check, review, commit) and the assertions are about the git state and
the queue row afterwards, never about internals.

That matters here more than anywhere, because the change protocol was reopened
(D10, 2026-08-05) and the implementer now returns **edit blocks** that
`edits.to_diff` renders into a unified diff. The rendering is new and the
things downstream of it are not: `git apply` compares bytes, and a diff that is
wrong about the file is refused with no rung of the ladder able to help. Every
bug this file was written to catch is of exactly that shape — an edit the model
got *right*, lost between matching the text and applying the patch:

1. a file whose last line has no newline rendered as `-beta+gamma` on one
   line, and `git apply: error: corrupt patch at line 6`;
2. a CRLF file rendered as an LF diff — `patch does not apply`, forever, for an
   edit that had already matched and that no retry could fix;
3. an empty file that exists (`__init__.py`) rendered as `--- /dev/null`, which
   git refuses with `already exists in working directory` — while `plan_edits`
   deliberately permits an empty SEARCH against exactly that file;
4. the diff computed against the **working tree**, which still holds the
   previous item's branch, rather than against the base the patch is applied
   to. The second item to touch a file was blamed for naming text that is
   present on `main` and absent from its predecessor's branch.

None of the four is visible from `edits.py` alone, and none of them is visible
from a unit test that stops at the diff. They are all one `git apply` away.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_harness import providers as P
from agent_harness.executor import Checks, ContextPolicy, Executor
from agent_harness.model_client import ModelClient, Response, RetryPolicy, Route
from agent_harness.work import BLOCKED, DONE, FAILED, WorkQueue, WorkRecord
from conftest import make_queue

ROLES = ("planner", "implementer", "reviewer")
APPROVE = "APPROVED\nIt does what was asked.\n\n4. Follow-ups\n- none"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def plan(*targets: str) -> str:
    """A planner answer in the shape the parser actually requires."""
    return json.dumps(
        {
            "plan": "change the named file",
            "targets": [{"path": path, "reason": "this is where the work is"} for path in targets],
            "cannot_identify_target": None,
        }
    )


def edit(path: str, search: str, replace: str) -> str:
    """One edit block, in the form the implementer prompt asks for."""
    return f"{path}\n<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repository, with the file shapes that break a rendered diff.

    Every one of these is ordinary: a file saved without a trailing newline, a
    file checked in from Windows, an empty package marker. None of them is
    exotic, and each one used to cost an item.
    """
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")

    (path / "greeting.txt").write_text("hello world\n")
    (path / "nonewline.txt").write_bytes(b"alpha\nbeta")
    (path / "windows.txt").write_bytes(b"alpha\r\nbeta\r\n")
    (path / "pkg").mkdir()
    (path / "pkg" / "__init__.py").write_text("")

    # Real check commands, as scripts, so the gate is a subprocess and a
    # filesystem rather than a stub that agrees with the diff.
    scripts = {
        "check-greeting.sh": 'grep -q "^hello harness$" greeting.txt\n',
        "check-nonewline.sh": "printf 'alpha\\ngamma' | cmp -s - nonewline.txt\n",
        "check-windows.sh": "printf 'alpha\\r\\ngamma\\r\\n' | cmp -s - windows.txt\n",
        "check-marker.sh": 'grep -q "^VERSION" pkg/__init__.py\n',
    }
    for name, body in scripts.items():
        script = path / name
        script.write_text(f"#!/bin/sh\nset -e\n{body}")
        script.chmod(0o755)

    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


class Scripted:
    """Replies per role, in order, and every prompt kept.

    A list is consumed one reply per call, so a test can drive two items
    through one executor and give the implementer a different answer for each.
    """

    def __init__(self, replies: Mapping[str, Any]) -> None:
        self.replies = {
            role: list(value) if isinstance(value, list) else [value]
            for role, value in replies.items()
        }
        self.prompts: dict[str, list[str]] = {role: [] for role in ROLES}

    def __call__(
        self, route: Route, messages: Sequence[Mapping[str, Any]], options: Mapping[str, Any]
    ) -> Response:
        role = str(route.options.get("role", route.model))
        self.prompts.setdefault(role, []).append(str(messages[-1]["content"]))
        queued = self.replies.get(role) or ["ok"]
        reply = queued.pop(0) if len(queued) > 1 else queued[0]
        return Response(200, {}, json.dumps({"choices": [{"message": {"content": reply}}]}))


def build(
    repo: Path,
    tmp_path: Path,
    replies: Mapping[str, Any],
    *,
    checks: Checks | None = None,
    events: list[dict[str, Any]] | None = None,
    policy: ContextPolicy | None = None,
) -> tuple[Executor, WorkQueue, Scripted]:
    queue = make_queue(str(tmp_path / "queue.sqlite"), lease_seconds=100.0)
    transport = Scripted(replies)
    client = ModelClient(
        roles={
            role: Route(f"model-{role}", "https://api.example", P.GENERIC, options={"role": role})
            for role in ROLES
        },
        transport=transport,
        policy=RetryPolicy(max_attempts=2, backoff_seconds=0.001),
        sleep=lambda _s: None,
    )
    executor = Executor(
        queue,
        client,
        repo,
        checks=checks or Checks(),
        on_event=(events.append if events is not None else None),
        push=False,
        context_policy=policy or ContextPolicy(),
    )
    return executor, queue, transport


def add(queue: WorkQueue, item_id: str, brief: str, title: str = "Do the thing") -> None:
    queue.add([WorkRecord(item_id=item_id, title=title, brief=brief)])


def committed(repo: Path, branch: str, path: str) -> bytes:
    """A file's bytes as they were actually committed, not as the tree has them."""
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{branch}:{path}"],
        capture_output=True,
        check=True,
    ).stdout


def stages_of(events: list[dict[str, Any]]) -> list[str]:
    return [str(event.get("outcome")) for event in events]


# --------------------------------------------------------------- the loop


def test_edit_blocks_go_all_the_way_to_a_reviewed_commit(repo: Path, tmp_path: Path) -> None:
    """The whole pipeline on the format it now asks for.

    The check is a real script that fails before the change and passes after,
    so this cannot pass on a run that never applied anything.
    """
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "planner": plan("greeting.txt"),
            "implementer": edit("greeting.txt", "hello world", "hello harness"),
            "reviewer": APPROVE,
        },
        checks=Checks(commands=[["./check-greeting.sh"]]),
    )
    add(queue, "T1", "Make greeting.txt say 'hello harness'.")

    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == DONE, outcome.reason
    assert outcome.verdict == "approved"
    record = queue.get("T1")
    assert record is not None and record.state == DONE
    assert record.branch == "harness/t1"
    assert committed(repo, "harness/t1", "greeting.txt") == b"hello harness\n"
    # Nothing lands on the default branch, ever.
    assert committed(repo, "main", "greeting.txt") == b"hello world\n"


def test_a_file_with_no_trailing_newline_survives_the_round_trip(
    repo: Path, tmp_path: Path
) -> None:
    """Bug 1. `difflib` marks a missing final newline by omitting one.

    Emitted straight into the patch, the `-` line and the `+` line beneath it
    run together as `-beta+gamma`, and git reports `corrupt patch at line 6`.
    The edit was correct, matched the file exactly, and was lost in the
    rendering — so no retry and no rung of the ladder could recover it.
    """
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "planner": plan("nonewline.txt"),
            "implementer": edit("nonewline.txt", "beta", "gamma"),
            "reviewer": APPROVE,
        },
        checks=Checks(commands=[["./check-nonewline.sh"]]),
    )
    add(queue, "T1", "Change beta to gamma in nonewline.txt.")

    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == DONE, outcome.reason
    # Byte for byte: the file had no final newline and still has none.
    assert committed(repo, "harness/t1", "nonewline.txt") == b"alpha\ngamma"


def test_a_crlf_file_is_edited_without_losing_its_line_endings(repo: Path, tmp_path: Path) -> None:
    """Bug 2, and the worst of the four, because no attempt can clear it.

    `read_text` translates on the way in, so the SEARCH block matches a CRLF
    file happily — and then the diff is rendered in LF and handed to `git
    apply`, which compares bytes and refuses it. Every attempt at that file
    failed identically, and nothing the model could write would have changed
    that.
    """
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "planner": plan("windows.txt"),
            "implementer": edit("windows.txt", "beta", "gamma"),
            "reviewer": APPROVE,
        },
        checks=Checks(commands=[["./check-windows.sh"]]),
    )
    add(queue, "T1", "Change beta to gamma in windows.txt.")

    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == DONE, outcome.reason
    assert committed(repo, "harness/t1", "windows.txt") == b"alpha\r\ngamma\r\n"


def test_an_empty_file_that_exists_is_written_to_not_created(repo: Path, tmp_path: Path) -> None:
    """Bug 3. `plan_edits` allows an empty SEARCH against an empty file.

    It says so in as many words — an empty file is indistinguishable from an
    absent one for the purpose of "put this text there". The renderer then
    called it `/dev/null`, which is a *creation*, and git refuses a creation
    of something that is already on disk: `already exists in working
    directory`. An empty `__init__.py` or `mod.rs` is not an unusual file.
    """
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "planner": plan("pkg/__init__.py"),
            "implementer": edit("pkg/__init__.py", "", 'VERSION = "1.0"'),
            "reviewer": APPROVE,
        },
        checks=Checks(commands=[["./check-marker.sh"]]),
    )
    add(queue, "T1", "Give pkg/__init__.py a VERSION.")

    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == DONE, outcome.reason
    assert committed(repo, "harness/t1", "pkg/__init__.py") == b'VERSION = "1.0"\n'


def test_the_second_item_is_edited_against_its_base_not_its_predecessors_branch(
    repo: Path, tmp_path: Path
) -> None:
    """Bug 4, and the one that scales with how well the fleet is doing.

    Every item's branch is cut from the base, and the implementer is shown the
    base — `select_repo_context` reads through `git show base:path` precisely
    so that "the tree still holds the previous item's branch" cannot mislead
    it. The edit blocks were then resolved against the working tree, which
    does still hold the previous item's branch.

    So the second item to touch a file names text that is present on `main`,
    is shown that text, and is told it does not occur in the file. The model
    was right and the harness blamed it — and the better the previous item
    did, the more certain the next one is to fail.
    """
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "planner": plan("greeting.txt"),
            "implementer": [
                edit("greeting.txt", "hello world", "hello harness"),
                edit("greeting.txt", "hello world", "hello everyone"),
            ],
            "reviewer": APPROVE,
        },
    )
    add(queue, "T1", "Make greeting.txt say 'hello harness'.")
    add(queue, "T2", "Make greeting.txt say 'hello everyone'.")

    first = executor.run_once()
    assert first is not None
    assert first.state == DONE, first.reason

    second = executor.run_once()

    assert second is not None
    assert second.state == DONE, second.reason
    assert committed(repo, "harness/t2", "greeting.txt") == b"hello everyone\n"
    # Cut from the base, not stacked on its predecessor: T2 declared no
    # dependency on T1, so T1's change must not be in its branch.
    assert committed(repo, "harness/t1", "greeting.txt") == b"hello harness\n"


def test_a_new_file_in_a_directory_that_does_not_exist_yet_is_created(
    repo: Path, tmp_path: Path
) -> None:
    """`git apply` makes the leading directories; nothing else has to."""
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "planner": plan("greeting.txt"),
            "implementer": edit("src/deep/module.py", "", "def go() -> int:\n    return 1"),
            "reviewer": APPROVE,
        },
    )
    add(queue, "T1", "Add src/deep/module.py with a `go` function.")

    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == DONE, outcome.reason
    assert (
        committed(repo, "harness/t1", "src/deep/module.py") == b"def go() -> int:\n    return 1\n"
    )


def test_a_later_edit_may_depend_on_what_an_earlier_one_wrote(repo: Path, tmp_path: Path) -> None:
    """Blocks are applied in order, so the second sees the first's result.

    Asserted through `git apply` rather than through `plan_edits`, because the
    rendered diff has to describe the *combined* change as one file diff with
    line numbers that survive the earlier edit's shift.
    """
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "planner": plan("greeting.txt"),
            "implementer": (
                edit("greeting.txt", "hello world", "hello harness\ngoodbye world")
                + edit("greeting.txt", "goodbye world", "goodbye harness")
            ),
            "reviewer": APPROVE,
        },
        checks=Checks(commands=[["./check-greeting.sh"]]),
    )
    add(queue, "T1", "Rewrite both greetings.")

    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == DONE, outcome.reason
    assert committed(repo, "harness/t1", "greeting.txt") == b"hello harness\ngoodbye harness\n"


def test_a_unified_diff_is_still_accepted_when_there_are_no_edit_blocks(
    repo: Path, tmp_path: Path
) -> None:
    """The fallback. Refusing it would turn a format preference into an outage.

    Every attempt recorded before D10 was reopened holds a diff, and a
    `--implementer` pointed at something with its own habits still works.
    """
    diff = (
        "diff --git a/greeting.txt b/greeting.txt\n"
        "--- a/greeting.txt\n"
        "+++ b/greeting.txt\n"
        "@@ -1 +1 @@\n"
        "-hello world\n"
        "+hello harness\n"
    )
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "planner": plan("greeting.txt"),
            "implementer": f"Here is the patch:\n```diff\n{diff}```\n",
            "reviewer": APPROVE,
        },
        checks=Checks(commands=[["./check-greeting.sh"]]),
    )
    add(queue, "T1", "Make greeting.txt say 'hello harness'.")

    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == DONE, outcome.reason
    assert committed(repo, "harness/t1", "greeting.txt") == b"hello harness\n"


# ------------------------------------------------------- the refusal paths


def test_text_the_file_does_not_contain_costs_an_attempt_and_leaves_no_branch(
    repo: Path, tmp_path: Path
) -> None:
    """A bad edit costs an attempt rather than corrupting a tree.

    And the working tree is left exactly as it was found, because the next
    item inherits it.
    """
    events: list[dict[str, Any]] = []
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "planner": plan("greeting.txt"),
            "implementer": edit("greeting.txt", "text that is not there", "something"),
            "reviewer": APPROVE,
        },
        events=events,
    )
    add(queue, "T1", "Change something.")

    outcome = executor.run_once()

    assert outcome is not None and outcome.state == FAILED
    assert "edits_rejected" in stages_of(events)
    record = queue.get("T1")
    assert record is not None and record.attempts == 1
    assert "harness/t1" not in git(repo, "branch", "--list", "harness/t1")
    assert git(repo, "status", "--porcelain") == ""
    # Nothing was reviewed, because nothing was applied.
    assert "review" not in outcome.stages


def test_edits_that_change_nothing_are_reported_as_no_change(repo: Path, tmp_path: Path) -> None:
    """Well-formed and inert. `to_diff` returns `""` and that is not a patch.

    The distinction is worth keeping: "the model wrote a broken patch" and
    "the model wrote a patch that does nothing" lead to different places.
    """
    events: list[dict[str, Any]] = []
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "planner": plan("greeting.txt"),
            "implementer": edit("greeting.txt", "hello world", "hello world"),
            "reviewer": APPROVE,
        },
        events=events,
    )
    add(queue, "T1", "Change nothing at all.")

    outcome = executor.run_once()

    assert outcome is not None and outcome.state == FAILED
    assert "no_diff" in stages_of(events)
    assert "change nothing" in outcome.reason
    assert git(repo, "status", "--porcelain") == ""


def test_ambiguous_text_is_refused_rather_than_applied_to_the_first_match(
    repo: Path, tmp_path: Path
) -> None:
    """The failure that is hardest to see in review, refused before the tree moves.

    Driven end to end because the guarantee people care about is that the
    repository is untouched, not that a function raised.
    """
    (repo / "twice.txt").write_text("call();\nx = 1;\ncall();\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "twice")

    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "planner": plan("twice.txt"),
            "implementer": edit("twice.txt", "call();", "other();"),
            "reviewer": APPROVE,
        },
    )
    add(queue, "T1", "Replace the call.")

    outcome = executor.run_once()

    assert outcome is not None and outcome.state == FAILED
    assert "not a location" in outcome.reason
    assert (repo / "twice.txt").read_text() == "call();\nx = 1;\ncall();\n"


def test_a_failing_check_refuses_the_item_before_the_reviewer_is_paid(
    repo: Path, tmp_path: Path
) -> None:
    """The ordering the module docstring defends, asserted from outside.

    A real script says no, and the reviewer is never called — spending the
    dearest gate to be told what the cheapest one already said is the failure
    this ordering exists to avoid.
    """
    executor, queue, transport = build(
        repo,
        tmp_path,
        {
            "planner": plan("greeting.txt"),
            "implementer": edit("greeting.txt", "hello world", "hello nobody"),
            "reviewer": APPROVE,
        },
        checks=Checks(commands=[["./check-greeting.sh"]]),
    )
    add(queue, "T1", "Make greeting.txt say 'hello harness'.")

    outcome = executor.run_once()

    assert outcome is not None and outcome.state == FAILED
    assert transport.prompts["reviewer"] == [], "the expensive gate ran after a failing check"
    assert "harness/t1" not in git(repo, "branch", "--list", "harness/t1")
    assert git(repo, "show", "main:greeting.txt") == "hello world\n"


def test_a_rejected_review_keeps_the_checkpoint_and_fails_the_item(
    repo: Path, tmp_path: Path
) -> None:
    """Approval is what a proposal needs, and a rejection is not a crash.

    The commit still exists: it passed the cheap gates and was checkpointed
    before the reviewer was called, which is rule 5 in `AGENTS.md`.
    """
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "planner": plan("greeting.txt"),
            "implementer": edit("greeting.txt", "hello world", "hello harness"),
            "reviewer": "REJECTED\n\n3. Why\nIt does something adjacent to what was asked.",
        },
        checks=Checks(commands=[["./check-greeting.sh"]]),
    )
    add(queue, "T1", "Make greeting.txt say 'hello harness'.")

    outcome = executor.run_once()

    assert outcome is not None and outcome.state == FAILED
    assert outcome.verdict == "rejected"
    assert committed(repo, "harness/t1", "greeting.txt") == b"hello harness\n"
    record = queue.get("T1")
    assert record is not None and "adjacent" in (record.last_error or "")


# ------------------------------------------- the ceiling (#152) and the fix (#155)


def test_a_target_too_large_for_the_ceiling_blocks_without_spending_an_attempt(
    repo: Path, tmp_path: Path
) -> None:
    """#152. The implementer is never asked to edit a file it has not seen.

    An attempt is not consumed, because no attempt can change the condition:
    only raising the ceiling or splitting the file can, and both are a
    person's decision.
    """
    (repo / "huge.txt").write_text("x" * 5000 + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "huge")

    executor, queue, transport = build(
        repo,
        tmp_path,
        {
            "planner": plan("huge.txt"),
            "implementer": edit("huge.txt", "x", "y"),
            "reviewer": APPROVE,
        },
        policy=ContextPolicy(budget=1000),
    )
    add(queue, "T1", "Change huge.txt.")

    outcome = executor.run_once()

    assert outcome is not None and outcome.state == BLOCKED
    assert transport.prompts["implementer"] == [], "an unseen target reached the implementer"
    record = queue.get("T1")
    assert record is not None and record.attempts == 0, "a ceiling is not a failed attempt"


def test_a_declared_fix_clears_its_gate_and_its_rewrite_reaches_the_commit(
    repo: Path, tmp_path: Path
) -> None:
    """#155, from outside: the re-run is the verdict and the fix is in the diff.

    Both halves matter. A fix that clears a gate but whose rewrite never
    reaches the commit would leave the reviewer reading one tree and the
    branch holding another.
    """
    shout = repo / "shout.sh"
    shout.write_text('#!/bin/sh\ntr "a-z" "A-Z" < greeting.txt > .t && mv .t greeting.txt\n')
    shout.chmod(0o755)
    style = repo / "check-shout.sh"
    style.write_text('#!/bin/sh\n[ "$(cat greeting.txt)" = "$(tr "a-z" "A-Z" < greeting.txt)" ]\n')
    style.chmod(0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "house style")

    checks = Checks(
        commands=[["./check-shout.sh"]],
        fixes={"./check-shout.sh": ["./shout.sh"]},
        apply_fixes=True,
    )
    events: list[dict[str, Any]] = []
    executor, queue, transport = build(
        repo,
        tmp_path,
        {
            "planner": plan("greeting.txt"),
            "implementer": edit("greeting.txt", "hello world", "hello harness"),
            "reviewer": APPROVE,
        },
        checks=checks,
        events=events,
    )
    add(queue, "T1", "Make greeting.txt greet the harness.")

    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == DONE, outcome.reason
    assert committed(repo, "harness/t1", "greeting.txt") == b"HELLO HARNESS\n"
    assert "check_fix_applied" in stages_of(events)
    # Never silent: the reviewer is told the harness edited the tree.
    assert "modified this tree" in transport.prompts["reviewer"][0]


def test_a_fix_that_does_not_clear_its_gate_still_refuses_the_item(
    repo: Path, tmp_path: Path
) -> None:
    """The gate is not ground down. One fix, one re-run, and the re-run decides."""
    useless = repo / "useless.sh"
    useless.write_text("#!/bin/sh\nexit 0\n")
    useless.chmod(0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a fix that fixes nothing")

    checks = Checks(
        commands=[["./check-greeting.sh"]],
        fixes={"./check-greeting.sh": ["./useless.sh"]},
        apply_fixes=True,
    )
    executor, queue, transport = build(
        repo,
        tmp_path,
        {
            "planner": plan("greeting.txt"),
            "implementer": edit("greeting.txt", "hello world", "hello nobody"),
            "reviewer": APPROVE,
        },
        checks=checks,
    )
    add(queue, "T1", "Make greeting.txt say 'hello harness'.")

    outcome = executor.run_once()

    assert outcome is not None and outcome.state == FAILED
    assert transport.prompts["reviewer"] == []
    assert "did not clear this" in outcome.reason


# ------------------------------------------------------------ what is shown


def test_the_implementer_is_shown_the_base_and_asked_for_edit_blocks(
    repo: Path, tmp_path: Path
) -> None:
    """The prompt is a gate of its own: it decides what the model can be right about.

    Asserted here rather than on the constant, because what matters is what
    reached the transport after the context was selected against the base.
    """
    executor, queue, transport = build(
        repo,
        tmp_path,
        {
            "planner": plan("greeting.txt"),
            "implementer": edit("greeting.txt", "hello world", "hello harness"),
            "reviewer": APPROVE,
        },
        checks=Checks(commands=[["./check-greeting.sh"]]),
    )
    add(queue, "T1", "Make greeting.txt say 'hello harness'.")
    executor.run_once()

    asked = transport.prompts["implementer"][0]
    assert "<<<<<<< SEARCH" in asked, "the implementer was not asked for edit blocks"
    assert "Do not write a unified diff" in asked
    assert "hello world" in asked, "the file it must edit was not in the prompt"
    assert "./check-greeting.sh" in asked, "the gates were not named to the writer"
