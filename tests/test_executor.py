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
    Executor,
    apply_diff,
    extract_diff,
)
from agent_harness.model_client import ModelClient, Response, RetryPolicy, Route
from agent_harness.work import DONE, FAILED, WorkQueue, WorkRecord
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


def test_the_commit_records_the_item_and_the_verdict(repo: Path, tmp_path: Path) -> None:
    """So a commit can be traced back to the work that asked for it and the
    review that let it through, without the dashboard."""
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
    assert "Looks right" in message


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


def test_a_rejected_review_does_not_commit(repo: Path, tmp_path: Path) -> None:
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
    assert git(repo, "branch", "--list", "harness/t1").strip() == ""


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
    assert (repo / "hello.txt").read_text() == "hello world\n"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


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
        def run(self, repo: Path) -> tuple[bool, str]:
            ran.append("checked")
            return True, ""

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
            self.calls: list[dict[str, str]] = []

        def create_pr(self, **kw: str) -> str:
            self.calls.append(kw)
            return "https://github.com/o/r/pull/9"

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
    assert "fine" in github.calls[0]["body"]  # the verdict travels with it


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
