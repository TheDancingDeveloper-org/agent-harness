from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from agent_harness.admission_service import AdmissionError, preview
from agent_harness.work import Project, WorkQueue


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("demo\n")
    git(repo, "add", "README")
    git(repo, "commit", "-qm", "initial")
    git(repo, "branch", "-M", "main")
    return repo


def test_preview_is_read_only_and_bound_to_git_facts(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    plan = tmp_path / "PLAN.md"
    plan.write_text(Path("examples/PLAN.md").read_text(encoding="utf-8"), encoding="utf-8")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project("widgets", "Widgets", work_dir=str(repo)))
    before = queue.projects()[0]

    proposal = preview(queue, project_id="widgets", plan_path=plan, worktree=repo)

    assert proposal.base_sha == git(repo, "rev-parse", "HEAD")
    assert proposal.repository_identity == str(repo)
    assert proposal.current_revision is None
    assert queue.projects()[0] == before
    assert proposal.plan_digest == hashlib.sha256(plan.read_bytes()).hexdigest()


def test_invalid_plan_does_not_probe_git(tmp_path: Path) -> None:
    plan = tmp_path / "PLAN.md"
    plan.write_text("not a valid plan", encoding="utf-8")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project("widgets", "Widgets"))
    calls: list[tuple[str, ...]] = []

    def no_git(argv: tuple[str, ...], cwd: Path) -> str:
        calls.append(argv)
        return "never"

    with pytest.raises(AdmissionError):
        preview(queue, project_id="widgets", plan_path=plan, worktree=tmp_path, git=no_git)
    assert calls == []


def test_changed_base_fact_changes_proposal_digest(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    plan = tmp_path / "PLAN.md"
    plan.write_text(Path("examples/PLAN.md").read_text(encoding="utf-8"), encoding="utf-8")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project("widgets", "Widgets", work_dir=str(repo)))
    first = preview(queue, project_id="widgets", plan_path=plan, worktree=repo)
    (repo / "README").write_text("changed\n")
    git(repo, "add", "README")
    git(repo, "commit", "-qm", "second")
    second = preview(queue, project_id="widgets", plan_path=plan, worktree=repo)

    assert first.base_sha != second.base_sha
    assert first.proposal_digest != second.proposal_digest
