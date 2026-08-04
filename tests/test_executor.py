"""Executor tests.

These run the real loop against a **real temporary git repository** with a
scripted model. Mocking git here would test the mock: the things most likely
to be wrong are whether a patch actually applies, whether a failed attempt
leaves the tree dirty, and whether a branch really gets created — none of
which a fake can tell you.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_harness import providers as P
from agent_harness.executor import (
    APPROVED,
    Checks,
    ContextPolicy,
    Executor,
    PlannerResult,
    PlannerTarget,
    apply_diff,
    extract_diff,
    parse_planner_result,
    recount_hunks,
    repo_context,
    select_repo_context,
    unplaceable_hunks,
    validate_diff,
)
from agent_harness.model_client import (
    ModelClient,
    Response,
    RetryExhausted,
    RetryPolicy,
    Route,
)
from agent_harness.outcomes import PASSED, CheckResult
from agent_harness.work import DONE, FAILED, PENDING, WorkQueue, WorkRecord
from conftest import make_queue

DIFF = """\
diff --git a/hello.txt b/hello.txt
index 3b18e51..8c7e5a6 100644
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello world
+hello harness
"""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    (path / "hello.txt").write_text("hello world\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


class ScriptedModel:
    """Replies per role, so a test can make the reviewer reject or the
    implementer return junk without touching the network."""

    def __init__(self, replies: Mapping[str, str]) -> None:
        self.replies = dict(replies)
        self.roles: list[str] = []

    def __call__(
        self, route: Route, messages: Sequence[Mapping[str, Any]], options: Mapping[str, Any]
    ) -> Response:
        role = route.options.get("role", route.model)
        self.roles.append(str(role))
        content = self.replies.get(str(role), "ok")
        body = json.dumps({"choices": [{"message": {"content": content}}]})
        return Response(200, {}, body)


def build(
    repo: Path,
    tmp_path: Path,
    replies: Mapping[str, str],
    *,
    checks: Checks | None = None,
    events: list[dict[str, Any]] | None = None,
    github: Any = None,
    artifacts: Path | None = None,
) -> tuple[Executor, WorkQueue, ScriptedModel]:
    queue = make_queue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    transport = ScriptedModel(replies)
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
        checks=checks or Checks(),
        github=github,
        on_event=(events.append if events is not None else None),
        push=False,
        artifacts=artifacts,
    )
    return executor, queue, transport


def add_item(queue: WorkQueue, item_id: str = "T1") -> None:
    queue.add(
        [
            WorkRecord(
                item_id=item_id,
                title="Change the greeting",
                brief="Change hello.txt to say 'hello harness'.",
                issue=7,
            )
        ]
    )


def test_transient_retry_exhaustion_returns_an_item_to_pending(repo: Path, tmp_path: Path) -> None:
    executor, queue, _transport = build(
        repo,
        tmp_path,
        {"planner": "plan", "implementer": DIFF, "reviewer": "APPROVED\nfine"},
    )

    def exhausted(*_args: object, **_kwargs: object) -> None:
        raise RetryExhausted(
            "planner retries exhausted; last was transient",
            role="planner",
            kind=P.TRANSIENT,
            endpoint="https://api.example",
            model="model-planner",
        )

    executor.client.call = exhausted  # type: ignore[assignment]
    add_item(queue)

    outcome = executor.run_once()

    assert outcome is not None and outcome.state == PENDING
    record = queue.get("T1")
    assert record is not None and record.state == PENDING
    assert record.attempts == 1
    assert "transient" in (record.last_error or "")


# ------------------------------------------------------------ diff handling


def test_a_fenced_diff_is_extracted() -> None:
    assert extract_diff(f"Here you go:\n```diff\n{DIFF}```\nDone.") == DIFF


def test_a_bare_diff_after_prose_is_still_accepted() -> None:
    """A correct patch preceded by commentary is still a correct patch;
    rejecting it would discard real work over formatting."""
    extracted = extract_diff(f"I'll change the file.\n\n{DIFF}")
    assert extracted is not None
    assert extracted.startswith("diff --git")


def test_a_reply_with_no_diff_returns_none() -> None:
    assert extract_diff("I cannot do this because the file does not exist.") is None


def test_a_diff_missing_its_final_newline_is_repaired() -> None:
    """`git apply` rejects it outright — an infuriating way to lose an
    otherwise-good patch."""
    assert extract_diff(DIFF.rstrip("\n")).endswith("\n")  # type: ignore[union-attr]


def test_a_clean_diff_applies(repo: Path) -> None:
    applied, how = apply_diff(repo, DIFF)
    assert applied
    assert (repo / "hello.txt").read_text() == "hello harness\n"
    assert how == "git apply"


# A hunk header that understates its context only *fails* on a file with
# more lines than the hunk claims -- which is why this needs its own,
# larger fixture. On a one-line file the same header is simply correct.
MINIMAL_HEADER_DIFF = """\
diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,2 +1,2 @@
 def f():
-    return 1
+    return 99
"""


@pytest.fixture
def multiline_repo(repo: Path) -> Path:
    (repo / "m.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add m.py")
    return repo


def test_a_hand_written_hunk_header_is_rescued(multiline_repo: Path) -> None:
    """The most common model error by far: a hunk header that understates
    its context. Measured — `--unidiff-zero` is what forgives it, and the
    obvious `--ignore-whitespace` does not."""
    applied, how = apply_diff(multiline_repo, MINIMAL_HEADER_DIFF)
    assert applied, how
    assert "unidiff-zero" in how
    assert "return 99" in (multiline_repo / "m.py").read_text()


def test_a_strict_apply_really_does_reject_that_header(multiline_repo: Path) -> None:
    """Guards the rung above: if plain `git apply` accepted it, the test
    would pass for the wrong reason and the rung would look load-bearing
    when it is not."""
    result = subprocess.run(
        ["git", "-C", str(multiline_repo), "apply", "-"],
        input=MINIMAL_HEADER_DIFF,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


# The live failure. `git apply` refuses `-0,0` against a file that exists;
# `--unidiff-zero` accepts it and puts the lines at line 1, above the module
# docstring, which then stops being a docstring at all.
UNPLACEABLE_DIFF = """\
diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -0,0 +1,2 @@
+def multiply(a, b):
+    return a * b
"""


@pytest.fixture
def docstring_repo(repo: Path) -> Path:
    (repo / "calc.py").write_text('"""A tiny module."""\n\n\ndef add(a, b):\n    return a + b\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add calc.py")
    return repo


def test_a_zero_context_hunk_against_an_existing_file_is_refused(docstring_repo: Path) -> None:
    """`--unidiff-zero` "succeeds" here by inserting at line 1, and nothing
    downstream can tell: Python does not care where a string literal sits, so
    the checks stay green while `calc.__doc__` has quietly become None.

    A `-0,0` header against a file with content in it is false about the tree
    before anything is applied, and there is no placement in it to recover --
    which is what separates it from the malformed headers the ladder exists
    for."""
    applied, how = apply_diff(docstring_repo, UNPLACEABLE_DIFF)

    assert not applied, "a hunk with no verifiable placement was applied anyway"
    assert "calc.py" in how and "-0,0" in how
    assert (docstring_repo / "calc.py").read_text().startswith('"""A tiny module."""')


def test_the_fuzzy_rung_does_not_rescue_it_either(docstring_repo: Path) -> None:
    """Opting in to fuzz buys tolerance for whitespace damage, not a licence
    to guess where a hunk that never said belongs."""
    applied, how = apply_diff(docstring_repo, UNPLACEABLE_DIFF, allow_fuzzy=True)

    assert not applied, how
    assert (docstring_repo / "calc.py").read_text().startswith('"""A tiny module."""')


def test_creating_a_new_file_is_still_a_zero_context_hunk_that_applies(repo: Path) -> None:
    """The honest use of `-0,0`, and by far the common one. Refusing it would
    stop the harness adding files at all."""
    new_file = """\
diff --git a/added.py b/added.py
new file mode 100644
--- /dev/null
+++ b/added.py
@@ -0,0 +1,2 @@
+def multiply(a, b):
+    return a * b
"""
    applied, how = apply_diff(repo, new_file)

    assert applied, how
    assert (repo / "added.py").read_text().startswith("def multiply")


def test_an_empty_file_can_still_be_filled_in(repo: Path) -> None:
    """`-0,0` is the honest header for a file that really has no lines, so the
    check is about content and not merely about the path existing."""
    (repo / "blank.py").write_text("")
    diff = UNPLACEABLE_DIFF.replace("calc.py", "blank.py")

    assert unplaceable_hunks(repo, diff) == []


def test_whitespace_damage_on_a_removal_line_is_not_rescued_by_default(
    repo: Path,
) -> None:
    """`git apply` cannot rescue this at any flag combination, and the rung
    that could (`patch --fuzz`) is opt-in — so by default this is an honest
    failure rather than a guess."""
    damaged = DIFF.replace("-hello world", "-hello world ")
    applied, how = apply_diff(repo, damaged)
    assert not applied
    assert "git apply" in how  # the errors say what was tried


def test_whitespace_damage_is_rescued_when_fuzzy_is_enabled(repo: Path) -> None:
    damaged = DIFF.replace("-hello world", "-hello world ")
    applied, how = apply_diff(repo, damaged, allow_fuzzy=True)
    if applied:  # `patch` is not installed everywhere
        assert "patch" in how
        assert (repo / "hello.txt").read_text() == "hello harness\n"
    else:
        assert "not available" in how


def test_the_rung_that_worked_is_reported(repo: Path) -> None:
    """A fleet suddenly relying on the fuzzy fallback is a fleet whose
    implementer has got worse at diffs — invisible unless recorded."""
    _applied, how = apply_diff(repo, DIFF)
    assert how == "git apply"


def test_a_nonsense_diff_reports_why_it_failed(repo: Path) -> None:
    applied, why = apply_diff(repo, "diff --git a/nope.txt b/nope.txt\n@@ bad @@\n")
    assert not applied
    assert why


# ------------------------------------------------------------- the loop


def test_a_full_pass_commits_on_a_branch(repo: Path, tmp_path: Path) -> None:
    executor, queue, model = build(
        repo,
        tmp_path,
        {
            "planner": "I will edit hello.txt.",
            "implementer": f"```diff\n{DIFF}```",
            "reviewer": "APPROVED\nThe change does what was asked.",
        },
    )
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == DONE
    assert outcome.verdict == APPROVED
    assert outcome.branch == "harness/t1"
    assert model.roles == ["planner", "implementer", "reviewer"]
    assert "harness/t1" in git(repo, "branch", "--list", "harness/t1")
    assert "hello harness" in git(repo, "show", "harness/t1:hello.txt")


def test_the_checkpoint_commit_records_the_item_without_claiming_a_verdict(
    repo: Path, tmp_path: Path
) -> None:
    """The commit is durable before review, so it records provenance while
    being explicit that no verdict existed at checkpoint time."""
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "implementer": f"```diff\n{DIFF}```",
            "reviewer": "APPROVED\nLooks right.",
        },
    )
    add_item(queue)
    executor.run_once()
    message = git(repo, "log", "-1", "--format=%B", "harness/t1")
    assert "harness-item: T1" in message
    assert "Reviewed: not yet" in message
    assert "Looks right" not in message


def test_cheap_checks_run_before_the_expensive_review(repo: Path, tmp_path: Path) -> None:
    """Paying a model to tell you the build is broken is paying the dearest
    gate to catch what the cheapest one already caught."""
    executor, queue, model = build(
        repo,
        tmp_path,
        {
            "implementer": f"```diff\n{DIFF}```",
            "reviewer": "APPROVED",
        },
        checks=Checks(commands=[["false"]]),
    )
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == FAILED
    assert "reviewer" not in model.roles  # never paid for
    assert "checks" in outcome.stages
    assert "review" not in outcome.stages


def test_a_failing_check_reports_the_output_not_just_failure(repo: Path, tmp_path: Path) -> None:
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "implementer": f"```diff\n{DIFF}```",
        },
        checks=Checks(commands=[["sh", "-c", "echo the-actual-error >&2; exit 1"]]),
    )
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert "the-actual-error" in outcome.reason


def test_a_rejected_review_keeps_an_unreviewed_checkpoint(repo: Path, tmp_path: Path) -> None:
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "implementer": f"```diff\n{DIFF}```",
            "reviewer": "REJECTED\nIt changes the wrong string.",
        },
    )
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == FAILED
    assert "wrong string" in outcome.reason
    assert git(repo, "branch", "--list", "harness/t1").strip()
    message = git(repo, "log", "-1", "--format=%B", "harness/t1")
    assert "Reviewed: not yet" in message
    assert "APPROVED" not in message


def test_a_failed_attempt_leaves_the_tree_clean(repo: Path, tmp_path: Path) -> None:
    """Without this, one bad diff contaminates every item after it."""
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "implementer": f"```diff\n{DIFF}```",
            "reviewer": "REJECTED\nno",
        },
    )
    add_item(queue)
    executor.run_once()
    assert git(repo, "status", "--porcelain").strip() == ""
    assert (repo / "hello.txt").read_text() == "hello harness\n"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "harness/t1"


def test_an_implementer_that_returns_no_diff_fails_the_item_cheaply(
    repo: Path, tmp_path: Path
) -> None:
    executor, queue, model = build(
        repo,
        tmp_path,
        {
            "implementer": "I don't think this change is a good idea.",
            "reviewer": "APPROVED",
        },
    )
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == FAILED
    assert "no diff" in outcome.reason
    assert "reviewer" not in model.roles


def test_a_diff_that_does_not_apply_fails_before_any_check_runs(repo: Path, tmp_path: Path) -> None:
    ran: list[str] = []

    class Recording(Checks):
        def run(self, repo: Path) -> CheckResult:
            ran.append("checked")
            return PASSED

    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "implementer": "```diff\ndiff --git a/x b/x\n@@ nonsense @@\n```",
        },
        checks=Recording(),
    )
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == FAILED
    assert ran == []


# --------------------------------------------------------------- claims


def test_the_item_is_released_so_a_restart_can_resume(repo: Path, tmp_path: Path) -> None:
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "implementer": f"```diff\n{DIFF}```",
            "reviewer": "APPROVED",
        },
    )
    add_item(queue)
    executor.run_once()
    record = queue.get("T1")
    assert record is not None
    assert record.state == DONE
    assert record.owner is None
    assert record.branch == "harness/t1"


def test_an_unexpected_error_fails_the_item_without_killing_the_loop(
    repo: Path, tmp_path: Path
) -> None:
    executor, queue, _ = build(repo, tmp_path, {})
    add_item(queue)
    queue.add([WorkRecord(item_id="T2", title="second", brief="b")])

    def explode(*_a: object, **_k: object) -> None:
        raise RuntimeError("boom")

    executor.client.call = explode  # type: ignore[assignment]
    outcomes = executor.run(limit=2)
    assert [o.state for o in outcomes] == [FAILED, FAILED]
    assert len(outcomes) == 2  # the loop kept going


def test_run_stops_when_there_is_nothing_left(repo: Path, tmp_path: Path) -> None:
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "implementer": f"```diff\n{DIFF}```",
            "reviewer": "APPROVED",
        },
    )
    add_item(queue)
    assert len(executor.run()) == 1


def test_nothing_to_do_returns_none_rather_than_blocking(repo: Path, tmp_path: Path) -> None:
    executor, _queue, _ = build(repo, tmp_path, {})
    assert executor.run_once() is None


# --------------------------------------------------------------- events


def test_every_stage_is_emitted(repo: Path, tmp_path: Path) -> None:
    """This is what makes 'what is it doing right now?' answerable."""
    events: list[dict[str, Any]] = []
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "implementer": f"```diff\n{DIFF}```",
            "reviewer": "APPROVED",
        },
        events=events,
    )
    add_item(queue)
    executor.run_once()
    outcomes = [e["outcome"] for e in events]
    assert "started" in outcomes
    assert "applied" in outcomes
    assert "checks_passed" in outcomes
    assert f"review_{APPROVED}" in outcomes
    assert "done" in outcomes
    assert all(e["item_id"] == "T1" for e in events)
    assert all(e["kind"] == "work" for e in events)


def test_a_broken_event_sink_does_not_fail_the_item(repo: Path, tmp_path: Path) -> None:
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "implementer": f"```diff\n{DIFF}```",
            "reviewer": "APPROVED",
        },
    )
    executor.on_event = lambda _e: (_ for _ in ()).throw(OSError("disk full"))
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None and outcome.state == DONE


# ------------------------------------------------------------------- pr


def test_a_pr_is_opened_and_linked_to_the_issue(repo: Path, tmp_path: Path) -> None:
    class FakeGitHub:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.comments: list[str] = []
            self.ready: list[str] = []

        def create_pr(self, **kw: Any) -> str:
            self.calls.append(kw)
            return "https://github.com/o/r/pull/9"

        def comment_pr(self, pr: str, body: str) -> None:
            self.comments.append(body)

        def mark_pr_ready(self, pr: str) -> None:
            self.ready.append(pr)

    github = FakeGitHub()
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "implementer": f"```diff\n{DIFF}```",
            "reviewer": "APPROVED\nfine",
        },
        github=github,
    )
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.pr_url == "https://github.com/o/r/pull/9"
    assert "Closes #7" in github.calls[0]["body"]
    assert github.calls[0]["draft"] is True
    assert "fine" in github.comments[0]  # the verdict travels with it
    assert github.ready == ["https://github.com/o/r/pull/9"]


def test_a_pr_failure_does_not_lose_the_committed_work(repo: Path, tmp_path: Path) -> None:
    """The branch is already committed by then. Failing the item would
    discard work that passed every gate."""

    class BrokenGitHub:
        def create_pr(self, **_kw: str) -> str:
            raise RuntimeError("github is down")

    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "implementer": f"```diff\n{DIFF}```",
            "reviewer": "APPROVED",
        },
        github=BrokenGitHub(),
    )
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == DONE
    assert outcome.pr_url is None
    assert "hello harness" in git(repo, "show", "harness/t1:hello.txt")


# ------------------------------------------------------------- never land


def test_the_default_branch_is_never_touched(repo: Path, tmp_path: Path) -> None:
    """Every item produces a proposal, so a wrong answer is reviewable
    rather than landed."""
    before = git(repo, "rev-parse", "main").strip()
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "implementer": f"```diff\n{DIFF}```",
            "reviewer": "APPROVED",
        },
    )
    add_item(queue)
    executor.run_once()
    assert git(repo, "rev-parse", "main").strip() == before


# ------------------------------------------------------------ stacking


def test_dependent_work_is_stacked_on_its_dependency(multiline_repo: Path, tmp_path: Path) -> None:
    """The bug the first realistic run exposed: an item whose diff was
    written against its dependency's tree, applied to a base without it.
    Basing the branch on the dependency is what makes the diff apply to the
    tree it was actually written for."""
    first = """\
diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -4,3 +4,7 @@ def f():
 
 def g():
     return 2
+
+
+def h():
+    return 3
"""
    second = """\
diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -8,3 +8,7 @@ def g():
 
 def h():
     return 3
+
+
+def i():
+    return 4
"""
    queue = make_queue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    queue.add(
        [
            WorkRecord(item_id="A1", title="add h", brief="add h"),
            WorkRecord(item_id="A2", title="add i", brief="add i", depends_on=["A1"]),
        ]
    )
    replies = {"A1": first, "A2": second}
    current = {"id": "A1"}

    def transport(
        route: Route, messages: Sequence[Mapping[str, Any]], options: Mapping[str, Any]
    ) -> Response:
        role = route.options["role"]
        content = (
            f"```diff\n{replies[current['id']]}```" if role == "implementer" else "APPROVED\nfine"
        )
        return Response(200, {}, json.dumps({"choices": [{"message": {"content": content}}]}))

    client = ModelClient(
        roles={
            r: Route(f"m-{r}", "https://e", P.GENERIC, options={"role": r})
            for r in ("planner", "implementer", "reviewer")
        },
        transport=transport,
        sleep=lambda _s: None,
    )
    executor = Executor(queue, client, multiline_repo, push=False)

    current["id"] = "A1"
    first_outcome = executor.run_once()
    assert first_outcome is not None and first_outcome.state == DONE
    assert first_outcome.base == "main"

    current["id"] = "A2"
    second_outcome = executor.run_once()
    assert second_outcome is not None, "A2 should be claimable once A1 is done"
    assert second_outcome.state == DONE, second_outcome.reason
    # Stacked on A1's branch, not on main...
    assert second_outcome.base == "harness/a1"
    # ...so the result contains BOTH changes, which is the whole point.
    final = git(multiline_repo, "show", "harness/a2:m.py")
    assert "def h()" in final
    assert "def i()" in final


def test_fuzzy_application_is_off_by_default(multiline_repo: Path) -> None:
    """A misplaced hunk that reports success is worse than an honest
    failure. This is the diff that fooled the first end-to-end run."""
    damaged = MINIMAL_HEADER_DIFF.replace("-    return 1", "-        return 1")
    applied, _how = apply_diff(multiline_repo, damaged)
    assert not applied


def test_fuzzy_application_can_be_opted_into(multiline_repo: Path) -> None:
    damaged = MINIMAL_HEADER_DIFF.replace("-    return 1", "-        return 1")
    applied, how = apply_diff(multiline_repo, damaged, allow_fuzzy=True)
    if applied:
        assert "patch" in how  # and the caller is told which rung


# ------------------------------------------- malformed output vs bad base

#: Truncated mid-hunk: the header promises three lines of each side and the
#: patch stops after two. This is the shape behind `git apply: error: corrupt
#: patch at line N` -- the model's reply was cut off, and nothing about the
#: repository can fix it.
TRUNCATED_DIFF = """\
diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,3 +1,3 @@
 def f():
-    return 1
"""

#: A body line with no prefix at all. `git apply` calls this corrupt too, and
#: no rung of the ladder rescues it.
MISPREFIXED_DIFF = """\
diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,2 +1,2 @@
def f():
-    return 1
+    return 99
"""

#: Structurally perfect, and about a file that is not there. Nothing is wrong
#: with the model's output; the patch and the tree simply disagree.
ABSENT_FILE_DIFF = """\
diff --git a/absent.txt b/absent.txt
--- a/absent.txt
+++ b/absent.txt
@@ -1 +1 @@
-before
+after
"""


def test_a_good_diff_has_nothing_to_report() -> None:
    assert validate_diff(DIFF) == []


def test_the_most_commonly_rescued_damage_is_not_called_fatal() -> None:
    """The validator must never refuse what the ladder repairs. A hand-written
    hunk header is the single most common model error and `--unidiff-zero`
    forgives it; rejecting it here would throw away real work to be tidy."""
    assert not [p for p in validate_diff(MINIMAL_HEADER_DIFF) if p.fatal]


def test_a_patch_cut_off_mid_hunk_is_still_refused() -> None:
    """This fixture is the dangerous shape: its `+` replacement never arrived,
    so a recounted header would apply the deletion alone. It must stay fatal.

    Told apart from a miscounted header by the shortfalls differing: a context
    line counts on both sides, so over-counting context is short by the same
    amount on each, while a reply cut off loses a mix.
    """
    problems = validate_diff(TRUNCATED_DIFF)
    assert problems and problems[0].fatal
    assert "cut off mid-hunk" in problems[0].detail


def test_a_header_that_merely_over_counts_context_is_not_fatal() -> None:
    """The live case, four runs running: a complete body under a header that
    counted the file's trailing newline. Recountable, so not refused."""
    problems = validate_diff(OVERCOUNTED_DIFF)

    assert problems and not problems[0].fatal
    assert "cut off" not in problems[0].detail


def test_a_line_with_no_diff_prefix_is_recognised() -> None:
    problems = validate_diff(MISPREFIXED_DIFF)
    assert problems and problems[0].fatal
    assert problems[0].line == 5  # points AT the damage, not at the file


def test_git_agrees_these_are_corrupt(repo: Path) -> None:
    """Guards the two fixtures above: if `git apply` accepted either, the
    validator would be refusing patches that work."""
    for damaged in (TRUNCATED_DIFF, MISPREFIXED_DIFF):
        applied, _how = apply_diff(repo, damaged, allow_fuzzy=True)
        assert not applied


def test_malformed_output_never_reaches_git(multiline_repo: Path, tmp_path: Path) -> None:
    """The item fails as a MODEL failure, before a branch exists. Previously
    this reached `git apply`, and the outcome said only `corrupt patch at line
    549` -- which reads like the repository's fault."""
    events: list[dict[str, Any]] = []
    executor, queue, model = build(
        multiline_repo,
        tmp_path,
        {"implementer": f"```diff\n{TRUNCATED_DIFF}```", "reviewer": "APPROVED"},
        events=events,
    )
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == FAILED
    assert "did not produce a usable diff" in outcome.reason
    assert "apply" not in outcome.stages
    assert "reviewer" not in model.roles  # never paid for
    assert git(multiline_repo, "branch", "--list", "harness/t1").strip() == ""
    stages = [e["outcome"] for e in events]
    assert "patch_malformed" in stages


def test_the_event_carries_the_lines_that_broke_it(multiline_repo: Path, tmp_path: Path) -> None:
    """A bounded excerpt, not the whole patch: enough to see the damage
    without turning the event store into a diff archive."""
    events: list[dict[str, Any]] = []
    executor, queue, _ = build(
        multiline_repo,
        tmp_path,
        {"implementer": f"```diff\n{MISPREFIXED_DIFF}```"},
        events=events,
    )
    add_item(queue)
    executor.run_once()
    detail = next(e["detail"] for e in events if e["outcome"] == "patch_malformed")
    assert "def f():" in detail  # the offending line itself
    assert len(detail) <= 2000


def test_a_malformed_patch_is_kept_for_inspection(multiline_repo: Path, tmp_path: Path) -> None:
    """Regenerating it costs another call and produces a different patch, so
    the failed one is the only copy of what actually happened."""
    artifacts = tmp_path / "artifacts"
    executor, queue, _ = build(
        multiline_repo,
        tmp_path,
        {"implementer": f"```diff\n{TRUNCATED_DIFF}```"},
        artifacts=artifacts,
    )
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    kept = list(artifacts.glob("T1-*.patch"))
    assert len(kept) == 1
    assert kept[0].read_text() == TRUNCATED_DIFF
    assert str(kept[0]) in outcome.reason


def test_a_patch_that_will_not_apply_is_kept_and_blamed_on_the_tree(
    repo: Path, tmp_path: Path
) -> None:
    """The other half: this patch is well-formed, so the disagreement is with
    the repository. Different diagnosis, different fix -- and the patch is
    preserved either way."""
    artifacts = tmp_path / "artifacts"
    executor, queue, _ = build(
        repo,
        tmp_path,
        {"implementer": f"```diff\n{ABSENT_FILE_DIFF}```"},
        artifacts=artifacts,
    )
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None
    assert outcome.state == FAILED
    assert "the diff did not apply" in outcome.reason
    assert "did not produce a usable diff" not in outcome.reason
    assert len(list(artifacts.glob("T1-*.patch"))) == 1


def test_with_nowhere_to_keep_it_the_item_still_fails_cleanly(
    multiline_repo: Path, tmp_path: Path
) -> None:
    """No artifact directory is a legitimate deployment -- the harness owns no
    directory layout and will not invent one. The diagnostic still gets out."""
    events: list[dict[str, Any]] = []
    executor, queue, _ = build(
        multiline_repo,
        tmp_path,
        {"implementer": f"```diff\n{TRUNCATED_DIFF}```"},
        events=events,
        artifacts=None,
    )
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None and outcome.state == FAILED
    assert "patch kept at" not in outcome.reason
    assert any(e["outcome"] == "patch_malformed" for e in events)


# ------------------------------------------------- what the reviewer sees


#: A context-free hunk `git apply` refuses and `--unidiff-zero` rescues -- the
#: rung doing the most work in the ladder. Its header names a real position
#: (after line 1), so the placement is honest; what the model's *text* does
#: not contain is the surrounding code, which is why the reviewer is shown the
#: applied diff instead. A `-0,0` header would not get this far: it is refused
#: before the ladder, because nothing about it is checkable (#133).
ZERO_CONTEXT_DIFF = """\
diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1,0 +2 @@
+a new last line
"""


class PromptCapturingModel(ScriptedModel):
    """Keeps the prompt each role was given, so a test can assert on it."""

    def __init__(self, replies: Mapping[str, str]) -> None:
        super().__init__(replies)
        self.prompts: dict[str, str] = {}

    def __call__(
        self, route: Route, messages: Sequence[Mapping[str, Any]], options: Mapping[str, Any]
    ) -> Response:
        role = str(route.options.get("role", route.model))
        self.prompts[role] = str(messages[-1].get("content", ""))
        return super().__call__(route, messages, options)


def test_the_reviewer_sees_the_applied_diff_not_the_proposed_one(
    repo: Path, tmp_path: Path
) -> None:
    """The regression.

    The tolerance ladder exists to rescue malformed patches, so a model's diff
    text and the change it produces are routinely different. Reviewing the
    text rejected good work for an artefact of the plumbing — and made the
    gate unable to catch a diff that claims more than it did, which is the one
    thing the review prompt says it is for.
    """
    executor, queue, transport = build(
        repo,
        tmp_path,
        {"planner": "plan", "implementer": ZERO_CONTEXT_DIFF, "reviewer": "APPROVED\nfine"},
    )
    capturing = PromptCapturingModel(
        {"planner": "plan", "implementer": ZERO_CONTEXT_DIFF, "reviewer": "APPROVED\nfine"}
    )
    executor.client.transport = capturing
    add_item(queue)

    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == DONE, outcome.reason
    shown = capturing.prompts["reviewer"]
    # The real change: an insertion, with the existing line as context.
    assert "a new last line" in shown
    assert "hello world" in shown, "the reviewer must see the surrounding context that survived"
    # Not the model's context-free header, which carries none of that.
    assert "@@ -1,0 +2" not in shown, "the reviewer must not be shown the proposed hunk header"


def test_a_reviewer_can_tell_where_a_rescued_hunk_landed(repo: Path, tmp_path: Path) -> None:
    """Placement is the thing the model's text cannot carry (#133). The
    applied diff shows it, which is what makes rejecting misplacement possible
    at all."""
    executor, queue, _ = build(
        repo,
        tmp_path,
        {"planner": "plan", "implementer": ZERO_CONTEXT_DIFF, "reviewer": "APPROVED\nfine"},
    )
    capturing = PromptCapturingModel(
        {"planner": "plan", "implementer": ZERO_CONTEXT_DIFF, "reviewer": "APPROVED\nfine"}
    )
    executor.client.transport = capturing
    add_item(queue)

    executor.run_once()

    shown = capturing.prompts["reviewer"]
    # The new line appears AFTER the pre-existing one, and the diff says so.
    # The model's text says only "one line, somewhere near the top".
    assert shown.index(" hello world") < shown.index("+a new last line")


# --------------------------------------------- what the implementer is shown


def test_the_planner_result_names_ordered_targets_with_reasons() -> None:
    result = parse_planner_result(
        json.dumps(
            {
                "plan": "Change the greeting and test it.",
                "targets": [
                    {"path": "hello.txt", "reason": "contains the greeting"},
                    {"path": "tests/test_hello.py", "reason": "verifies it"},
                ],
                "cannot_identify_target": None,
            }
        )
    )

    assert result.plan == "Change the greeting and test it."
    assert [target.path for target in result.targets] == ["hello.txt", "tests/test_hello.py"]
    assert not result.uncertainties


def test_an_ambiguous_planner_result_says_so_without_inventing_a_target() -> None:
    result = parse_planner_result(
        json.dumps(
            {
                "plan": "Inspect the missing integration requirements.",
                "targets": [],
                "cannot_identify_target": "The task does not name the integration.",
            }
        )
    )

    assert result.targets == ()
    assert result.cannot_identify_target == "The task does not name the integration."


def test_malformed_planner_output_is_uncertainty_not_file_authority() -> None:
    result = parse_planner_result("I think ../../secrets may be relevant")

    assert result.targets == ()
    assert result.cannot_identify_target
    assert result.uncertainties


def test_planner_targets_are_confined_and_checked(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    (repo / "link.txt").symlink_to(outside)
    git(repo, "add", "link.txt")
    git(repo, "commit", "-q", "-m", "link")
    planner = PlannerResult(
        plan="inspect",
        targets=(
            PlannerTarget("../../outside.txt", "escape"),
            PlannerTarget("missing.txt", "not present"),
            PlannerTarget("link.txt", "escapes through a symlink"),
            PlannerTarget("hello.txt", "the real target"),
        ),
    )

    selected = select_repo_context(repo, planner=planner)

    assert selected.files[0] == "hello.txt"
    assert "secret" not in selected.text
    invalid = {target.path: target.uncertainty for target in selected.targets if not target.usable}
    assert "escapes" in (invalid["../../outside.txt"] or "")
    assert "missing" in (invalid["missing.txt"] or "")
    assert "escapes" in (invalid["link.txt"] or "")


def test_named_targets_precede_context_and_empty_or_generated_files_cost_nothing(
    repo: Path,
) -> None:
    (repo / "empty.stub").write_text("")
    generated = repo / "output"
    generated.mkdir()
    (generated / "result.json").write_text("generated payload\n")
    (repo / "nearby.txt").write_text("greeting conventions live here\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "context candidates")
    selected = select_repo_context(
        repo,
        WorkRecord(item_id="T", title="Change greeting", brief="Update greeting behaviour"),
        planner=PlannerResult(
            plan="Change and verify the greeting.",
            targets=(PlannerTarget("hello.txt", "contains the greeting"),),
        ),
        policy=ContextPolicy(budget=1000, generated_paths=("output",)),
    )

    assert selected.files[0] == "hello.txt"
    assert "--- empty.stub ---" not in selected.text
    assert "--- output/result.json ---" not in selected.text
    assert ("empty.stub", "empty file") in selected.omitted
    assert ("output/result.json", "configured generated artefact") in selected.omitted


def test_a_target_that_does_not_fit_is_reported_not_silently_omitted(repo: Path) -> None:
    (repo / "large.txt").write_text("target line\n" * 200)
    git(repo, "add", "large.txt")
    git(repo, "commit", "-q", "-m", "large target")

    selected = select_repo_context(
        repo,
        planner=PlannerResult(
            plan="change it",
            targets=(PlannerTarget("large.txt", "the requested implementation"),),
        ),
        policy=ContextPolicy(budget=100),
    )

    assert "large.txt" not in selected.files
    assert selected.truncated
    assert ("large.txt", "named target exceeds remaining content budget") in selected.omitted


def test_ngms_shaped_context_supplies_the_planner_target_not_empty_stubs(
    repo: Path,
) -> None:
    """Regression for #146, including an observable header at the target top."""
    (repo / "SECURITY.md").write_text("<!-- GPL header stays first -->\n\nSecurity policy.\n")
    stubs = repo / "docker" / "services"
    stubs.mkdir(parents=True)
    for index in range(80):
        (stubs / f"stub-{index:03}.txt").write_text("")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "NGMS-shaped context")

    # The historical smallest-first policy uses its whole allowance on the
    # empty service stubs and omits the file the task is about.
    tracked = git(repo, "ls-files").splitlines()
    old_parts: list[str] = []
    old_spent = 0
    for path in sorted(tracked, key=lambda name: (repo / name).stat().st_size):
        block = f"--- {path} ---\n{(repo / path).read_text()}\n"
        if old_spent + len(block) > 600:
            continue
        old_parts.append(block)
        old_spent += len(block)
    old_context = "".join(old_parts)
    assert "--- SECURITY.md ---" not in old_context, "fixture no longer reproduces #146"

    selected = select_repo_context(
        repo,
        WorkRecord(item_id="T27", title="Apply headers", brief="Apply the licence header."),
        planner=PlannerResult(
            plan="Update SECURITY.md without moving its existing header.",
            targets=(PlannerTarget("SECURITY.md", "contains the policy text to update"),),
        ),
        policy=ContextPolicy(budget=600),
    )

    assert selected.files[0] == "SECURITY.md"
    assert "<!-- GPL header stays first -->" in selected.text
    assert not any(path.startswith("docker/services") for path in selected.files)

    wrong_location = """\
diff --git a/SECURITY.md b/SECURITY.md
--- a/SECURITY.md
+++ b/SECURITY.md
@@ -0,0 +1 @@
+GPL-3.0-only
"""
    applied, reason = apply_diff(repo, wrong_location)
    assert not applied, "the fixture accepted a hunk with no observable placement"
    assert "SECURITY.md" in reason and "-0,0" in reason
    assert (repo / "SECURITY.md").read_text().startswith("<!-- GPL header stays first -->")


def test_context_selection_and_planner_targets_are_observable_events(
    repo: Path, tmp_path: Path
) -> None:
    events: list[dict[str, Any]] = []
    executor, queue, _ = build(
        repo,
        tmp_path,
        {
            "planner": json.dumps(
                {
                    "plan": "Edit and verify hello.txt.",
                    "targets": [{"path": "hello.txt", "reason": "contains greeting"}],
                    "cannot_identify_target": None,
                }
            ),
            "implementer": DIFF,
            "reviewer": "APPROVED\nfine",
        },
        events=events,
    )
    add_item(queue)

    executor.run_once()

    planner_event = next(event for event in events if event["outcome"] == "planner_targets")
    context_event = next(event for event in events if event["outcome"] == "context_selected")
    assert json.loads(planner_event["detail"])["targets"][0]["path"] == "hello.txt"
    context = json.loads(context_event["detail"])
    assert context["files"][0] == "hello.txt"
    assert context["character_budget"] == 60_000
    assert context["truncated"] is False
    assert context["fallback_relevance"] is False


def test_a_target_that_cannot_be_shown_stops_the_item_before_the_implementer(
    repo: Path, tmp_path: Path
) -> None:
    """A file larger than the whole budget is a stop, not a smaller prompt.

    Found importing a second repository whose one relevant source file is
    600 KB. The planner named it, the budget could not hold it, and the
    implementer was called anyway — with unrelated files in the space the
    target should have had. It answered, because a model asked to patch a file
    it has not seen writes a plausible diff rather than refusing.
    """
    events: list[dict[str, Any]] = []
    executor, queue, transport = build(
        repo,
        tmp_path,
        {
            "planner": json.dumps(
                {
                    "plan": "Edit the large file.",
                    "targets": [{"path": "large.txt", "reason": "is the thing to change"}],
                    "cannot_identify_target": None,
                }
            ),
            "implementer": DIFF,
            "reviewer": "APPROVED\nfine",
        },
        events=events,
    )
    executor.context_policy = ContextPolicy(budget=100)
    (repo / "large.txt").write_text("target line\n" * 200)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a file larger than the budget")
    add_item(queue)

    executor.run_once()

    assert "implementer" not in transport.roles, "the implementer was paid for an impossible task"
    stopped = next(event for event in events if event["outcome"] == "context_unavailable")
    assert "large.txt" in stopped["detail"]
    assert "2400 bytes" in stopped["detail"], "the size that did not fit is part of the answer"
    assert "--context-budget" in stopped["detail"], "and so is what to do about it"

    item = queue.get("T1")
    assert item is not None
    assert item.state == "blocked", "retrying cannot make the file smaller"
    assert item.disposition == "escalated"
    assert item.reason_kind == "context_unavailable"
    assert item.attempts == 0, "a ceiling this deployment set is not the item failing"


def test_the_implementer_is_told_which_checks_will_judge_it(repo: Path, tmp_path: Path) -> None:
    """The reviewer was told what the checks said; the writer was told nothing.

    Measured on rdpapp: a correct change — the right function, in the right
    place, with both tests — was refused by `cargo fmt --all -- --check`, a
    command the model was never shown. That costs an attempt and two model
    calls to discover something the harness knew before it asked.
    """
    checks = Checks(commands=[["cargo", "fmt", "--all", "--", "--check"]])
    executor, queue, _ = build(
        repo,
        tmp_path,
        {"planner": "plan", "implementer": DIFF, "reviewer": "APPROVED\nfine"},
        checks=checks,
    )
    capturing = PromptCapturingModel(
        {"planner": "plan", "implementer": DIFF, "reviewer": "APPROVED\nfine"}
    )
    executor.client.transport = capturing
    add_item(queue)

    executor.run_once()

    assert "cargo fmt --all -- --check" in capturing.prompts["implementer"]


def test_a_project_with_no_checks_says_nothing_about_them(repo: Path, tmp_path: Path) -> None:
    """No checks configured must read exactly as it did before."""
    executor, queue, _ = build(
        repo,
        tmp_path,
        {"planner": "plan", "implementer": DIFF, "reviewer": "APPROVED\nfine"},
    )
    capturing = PromptCapturingModel(
        {"planner": "plan", "implementer": DIFF, "reviewer": "APPROVED\nfine"}
    )
    executor.client.transport = capturing
    add_item(queue)

    executor.run_once()

    assert "run on your diff" not in capturing.prompts["implementer"]


def test_the_implementer_is_shown_the_repository(repo: Path, tmp_path: Path) -> None:
    """The regression for #135.

    The prompt asks for a diff that "applies cleanly at the repository root".
    With an empty context that is not a hard task, it is an impossible one: a
    model cannot write context lines for a file it has never seen, so it
    writes hunks with none and the tolerance ladder guesses where they go.
    """
    executor, queue, _ = build(
        repo,
        tmp_path,
        {"planner": "plan", "implementer": DIFF, "reviewer": "APPROVED\nfine"},
    )
    capturing = PromptCapturingModel(
        {"planner": "plan", "implementer": DIFF, "reviewer": "APPROVED\nfine"}
    )
    executor.client.transport = capturing
    add_item(queue)

    executor.run_once()

    shown = capturing.prompts["implementer"]
    assert "hello.txt" in shown, "the file listing must reach the implementer"
    assert "hello world" in shown, "and so must the contents it is being asked to patch"


def test_the_file_the_brief_names_is_included_whole(repo: Path) -> None:
    (repo / "big.txt").write_text("x\n" * 5000)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "big")
    record = WorkRecord(item_id="T1", title="Edit hello", brief="Change hello.txt greeting.")

    context = repo_context(repo, record, budget=200)

    # The budget is tiny, so only the file the brief names earns its place.
    assert "hello world" in context
    assert "--- big.txt ---" not in context
    # The listing is always there: a model should know what exists even when
    # the budget will not stretch to showing it.
    assert "big.txt" in context


def test_a_repository_with_no_tracked_files_yields_no_context(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    git(bare, "init", "-q", "-b", "main")
    assert repo_context(bare) == ""


def test_binary_files_are_listed_but_not_read(repo: Path) -> None:
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "logo")

    context = repo_context(repo)

    assert "logo.png" in context
    assert "--- logo.png ---" not in context


# ------------------------------------------------ hunk headers that miscount


#: The live failure, four runs running. The body is byte-for-byte correct;
#: the header claims one line too many on each side, because the model counted
#: the file's trailing newline as a line. `git apply` parses by the declared
#: counts, reaches the end of input early, and calls the patch corrupt.
OVERCOUNTED_DIFF = """\
diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1,2 +1,3 @@
 hello world
+a second line
"""


def test_a_header_that_over_declares_is_recounted_and_applied(repo: Path) -> None:
    """The regression. No rung of the ladder rescued this: tolerance needs
    something to be tolerant *with*, and the parser has already run out of
    input."""
    applied, how = apply_diff(repo, OVERCOUNTED_DIFF)

    assert applied, how
    assert "recounted" in how
    assert (repo / "hello.txt").read_text() == "hello world\na second line\n"


def test_the_unrecounted_patch_really_is_rejected(repo: Path) -> None:
    """Guards the test above: if git accepted it as written, the fix would
    look load-bearing when it is not."""
    result = subprocess.run(
        ["git", "-C", str(repo), "apply", "-"],
        input=OVERCOUNTED_DIFF,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "corrupt patch" in (result.stderr or "")


def test_a_correct_diff_is_applied_exactly_as_written(repo: Path) -> None:
    """Recounting is second, not first: a well-formed patch must not be
    rewritten on its way in."""
    applied, how = apply_diff(repo, DIFF)

    assert applied
    assert how == "git apply", "a correct diff should not have needed recounting"


def test_recounting_leaves_a_correct_header_alone() -> None:
    assert recount_hunks(DIFF) == DIFF


def test_recounting_fixes_both_sides_of_the_header() -> None:
    recounted = recount_hunks(OVERCOUNTED_DIFF)

    assert "@@ -1,1 +1,2 @@" in recounted


def test_recounting_does_not_move_anything(repo: Path) -> None:
    """The counts are a derivable property of the body, so this guesses at
    nothing -- unlike a `-0,0` header, where the placement was the only thing
    the header carried and there is nothing to recompute (#133)."""
    recounted = recount_hunks(OVERCOUNTED_DIFF)

    assert [line for line in recounted.splitlines() if line.startswith(("+", "-", " "))] == [
        line for line in OVERCOUNTED_DIFF.splitlines() if line.startswith(("+", "-", " "))
    ]


#: A patch that CREATES a file — the ordinary way to add a module. `new file
#: mode` is emitted by git and was not recognised, so the validator called it
#: corrupt body content and refused a perfectly good patch.
NEW_FILE_DIFF = """\
diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello world
+hello harness
diff --git a/added.py b/added.py
new file mode 100644
index 0000000..3e75765
--- /dev/null
+++ b/added.py
@@ -0,0 +1,2 @@
+def multiply(a, b):
+    return a * b
"""


def test_a_patch_that_creates_a_file_is_not_called_corrupt(repo: Path) -> None:
    """The regression, found by running a real backlog: the first item that
    tried to add a file failed with `line inside a hunk starts with 'n'`."""
    problems = validate_diff(NEW_FILE_DIFF)

    assert [p for p in problems if p.fatal] == [], [p.detail for p in problems]


def test_that_patch_actually_applies(repo: Path) -> None:
    """Guards the test above: a validator that accepts a patch git rejects is
    no better than one that rejects a patch git accepts."""
    applied, how = apply_diff(repo, NEW_FILE_DIFF)

    assert applied, how
    assert (repo / "added.py").read_text().startswith("def multiply")
    assert (repo / "hello.txt").read_text() == "hello harness\n"


def test_a_patch_that_deletes_a_file_is_not_called_corrupt() -> None:
    deletion = """\
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
index 3e75765..0000000
--- a/gone.txt
+++ /dev/null
@@ -1 +0,0 @@
-gone
"""
    assert [p for p in validate_diff(deletion) if p.fatal] == []


def test_a_rename_is_not_called_corrupt() -> None:
    rename = """\
diff --git a/old.txt b/new.txt
similarity index 100%
rename from old.txt
rename to new.txt
"""
    assert [p for p in validate_diff(rename) if p.fatal] == []


def test_a_no_newline_marker_does_not_shorten_a_recount() -> None:
    """It annotates the line above and counts towards neither side."""
    marked = """\
diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1,1 +1,1 @@
-hello world
+hello harness
\\ No newline at end of file
"""
    assert recount_hunks(marked) == marked
