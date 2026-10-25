from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_harness.admission_service import AdmissionError, apply, preview, revision_preview
from agent_harness.plan_revisions import revision_items
from agent_harness.work import DONE, WorkQueue


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def fixture(tmp_path: Path) -> tuple[WorkQueue, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("demo\n")
    git(repo, "add", "README")
    git(repo, "commit", "-qm", "initial")
    git(repo, "branch", "-M", "main")
    plan = tmp_path / "PLAN.md"
    plan.write_text(Path("examples/PLAN.md").read_text(encoding="utf-8"), encoding="utf-8")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    return queue, repo, plan


def test_apply_creates_stopped_project_revision_work_and_graph(tmp_path: Path) -> None:
    queue, repo, plan = fixture(tmp_path)
    proposal = preview(queue, project_id="widgets", plan_path=plan, worktree=repo)

    revision = apply(
        queue,
        proposal=proposal,
        plan_path=plan,
        worktree=repo,
        operator="operator",
        expected_base_sha=proposal.base_sha,
        expected_current_revision=None,
    )

    assert revision == 1
    assert queue.control("widgets")[0] == "stopped"
    assert queue.get_project("widgets") is not None
    assert len(queue.items(project_id="widgets")) == 4
    assert queue.plan("widgets") is None
    assert queue.graph.report("widgets").edges
    assert all(
        record.branch == f"refs/heads/harness/widgets/r1/{record.item_id}"
        for record in queue.items(project_id="widgets")
    )


@pytest.mark.parametrize("failure", ["project", "revision", "items", "before_commit"])
def test_apply_rolls_back_every_logical_failure(tmp_path: Path, failure: str) -> None:
    queue, repo, plan = fixture(tmp_path)
    proposal = preview(queue, project_id="widgets", plan_path=plan, worktree=repo)

    with pytest.raises(RuntimeError, match="injected admission failure"):
        apply(
            queue,
            proposal=proposal,
            plan_path=plan,
            worktree=repo,
            operator="operator",
            expected_base_sha=proposal.base_sha,
            expected_current_revision=None,
            fail_at=failure,
        )
    reopened = WorkQueue(str(tmp_path / "queue.sqlite"))
    assert reopened.get_project("widgets") is None
    assert reopened.items(project_id="widgets") == []
    assert not reopened.graph.report("widgets").edges


def test_apply_replay_is_idempotent_and_stale_proposal_is_refused(tmp_path: Path) -> None:
    queue, repo, plan = fixture(tmp_path)
    proposal = preview(queue, project_id="widgets", plan_path=plan, worktree=repo)
    assert (
        apply(
            queue,
            proposal=proposal,
            plan_path=plan,
            worktree=repo,
            operator="op",
            expected_base_sha=proposal.base_sha,
            expected_current_revision=None,
        )
        == 1
    )
    assert (
        apply(
            queue,
            proposal=proposal,
            plan_path=plan,
            worktree=repo,
            operator="op",
            expected_base_sha=proposal.base_sha,
            expected_current_revision=None,
        )
        == 1
    )
    assert len(queue.items(project_id="widgets")) == 4

    changed = plan.read_text(encoding="utf-8").replace("Widget service", "Changed", 1)
    plan.write_text(changed, encoding="utf-8")
    with pytest.raises(AdmissionError, match="proposal facts changed"):
        apply(
            queue,
            proposal=proposal,
            plan_path=plan,
            worktree=repo,
            operator="op",
            expected_base_sha=proposal.base_sha,
            expected_current_revision=None,
        )


def test_apply_does_not_need_git_write_or_remote_access(tmp_path: Path) -> None:
    queue, repo, plan = fixture(tmp_path)
    proposal = preview(queue, project_id="widgets", plan_path=plan, worktree=repo)
    calls: list[tuple[str, ...]] = []

    def read_only_git(argv: tuple[str, ...], cwd: Path) -> str:
        calls.append(argv)
        return git(repo, *argv)

    apply(
        queue,
        proposal=proposal,
        plan_path=plan,
        worktree=repo,
        operator="op",
        expected_base_sha=proposal.base_sha,
        expected_current_revision=None,
        git=read_only_git,
    )
    assert all(
        argv[:2] == ("rev-parse", "--show-toplevel") or argv[:2] == ("rev-parse", "--verify")
        for argv in calls
    )


def test_revision_requires_explicit_removal_and_keeps_history_inactive(tmp_path: Path) -> None:
    queue, repo, plan = fixture(tmp_path)
    first = preview(queue, project_id="widgets", plan_path=plan, worktree=repo)
    apply(
        queue,
        proposal=first,
        plan_path=plan,
        worktree=repo,
        operator="op",
        expected_base_sha=first.base_sha,
        expected_current_revision=None,
    )
    revised = plan.read_text(encoding="utf-8").replace(
        "### W4: Document the accepted local change",
        "### W4_REMOVED: Document the accepted local change",
    )
    plan.write_text(revised, encoding="utf-8")
    second = preview(queue, project_id="widgets", plan_path=plan, worktree=repo)
    with pytest.raises(AdmissionError, match="omitted items"):
        revision_preview(queue, admission=second, plan_path=plan, worktree=repo)
    assert (
        apply(
            queue,
            proposal=second,
            plan_path=plan,
            worktree=repo,
            operator="op",
            expected_base_sha=second.base_sha,
            expected_current_revision=1,
            removed_items={"W4": "superseded by the API documentation item"},
        )
        == 2
    )
    assert [item.item_id for item in queue.items(project_id="widgets")] == ["W1", "W2", "W3"]
    historical = queue.get("W4", project_id="widgets")
    assert historical is not None and historical.active is False
    assert any(
        row["item_id"] == "W4" and row["active"] == 0 for row in revision_items(queue, "widgets", 2)
    )


def test_changed_done_item_requires_explicit_reopen(tmp_path: Path) -> None:
    queue, repo, plan = fixture(tmp_path)
    first = preview(queue, project_id="widgets", plan_path=plan, worktree=repo)
    apply(
        queue,
        proposal=first,
        plan_path=plan,
        worktree=repo,
        operator="op",
        expected_base_sha=first.base_sha,
        expected_current_revision=None,
    )
    assert queue.release("W1", DONE, project_id="widgets")
    revised = plan.read_text(encoding="utf-8").replace(
        "- The migration applies to a clean and representative existing local database.",
        "- The migration applies to a clean and representative local database, "
        "with rollback evidence.",
    )
    plan.write_text(revised, encoding="utf-8")
    second = preview(queue, project_id="widgets", plan_path=plan, worktree=repo)
    with pytest.raises(AdmissionError, match="explicit reopen"):
        revision_preview(queue, admission=second, plan_path=plan, worktree=repo)
    classified = revision_preview(
        queue,
        admission=second,
        plan_path=plan,
        worktree=repo,
        reopen_items={"W1"},
    )
    change = next(item for item in classified.changes if item.item_id == "W1")
    assert change.kind == "changed"
    assert change.new_state == "pending"
