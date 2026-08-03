"""A checkpoint is durable internal evidence, not premature publication."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_harness.checkpoint import GitBundleCheckpointStore, GitRefCheckpointStore


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
    (path / "candidate.txt").write_text("candidate\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "candidate")
    return path


def test_private_ref_keeps_the_exact_commit_reachable(repo: Path) -> None:
    checkpoint = GitRefCheckpointStore(now=lambda: 12.0).save(
        repo, project_id="a/project", item_id="T 1", attempt=2
    )

    assert checkpoint.created_at == 12.0
    assert checkpoint.location.startswith("refs/agent-harness/checkpoints/")
    assert git(repo, "rev-parse", checkpoint.location).strip() == checkpoint.commit
    assert not checkpoint.location.startswith("refs/heads/")


def test_bundle_survives_source_checkout_removal_and_restores(repo: Path, tmp_path: Path) -> None:
    store = GitBundleCheckpointStore(tmp_path / "durable", now=lambda: 20.0)
    checkpoint = store.save(repo, project_id="p", item_id="T1", attempt=1)
    replay = store.save(repo, project_id="p", item_id="T1", attempt=1)
    assert replay.location == checkpoint.location
    assert replay.digest == checkpoint.digest

    restored = tmp_path / "restored"
    restored.mkdir()
    git(restored, "init", "-q", "-b", "main")
    store.restore(checkpoint, restored, ref="refs/heads/recovered")
    assert git(restored, "show", "recovered:candidate.txt") == "candidate\n"


def test_bundle_restore_rejects_tampering(repo: Path, tmp_path: Path) -> None:
    store = GitBundleCheckpointStore(tmp_path / "durable")
    checkpoint = store.save(repo, project_id="p", item_id="T1", attempt=1)
    Path(checkpoint.location).write_bytes(b"not a git bundle")

    with pytest.raises(ValueError, match="digest"):
        store.restore(checkpoint, tmp_path / "unused", ref="refs/heads/recovered")
