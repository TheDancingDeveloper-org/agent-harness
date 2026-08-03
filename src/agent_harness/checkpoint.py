"""Durable candidate checkpoints taken before an expensive review gate.

Checkpoints are deliberately separate from publication.  A candidate that
passed the cheap gates must survive a worker crash, but it has not earned a
remote branch or pull request yet.  Implementations may keep that candidate
inside a durable checkout or copy it to an independently backed-up directory;
neither choice changes the executor's gate ordering.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Checkpoint:
    """An immutable handle to a candidate that passed its cheap gates."""

    project_id: str
    item_id: str
    attempt: int
    commit: str
    location: str
    created_at: float
    digest: str | None = None


class CheckpointStore(Protocol):
    """Storage boundary used immediately before the reviewer is called."""

    def save(
        self,
        repo: Path,
        *,
        project_id: str,
        item_id: str,
        attempt: int,
    ) -> Checkpoint: ...


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed executable and shell-free argv
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def _component(value: str) -> str:
    """Make an identifier safe as one path/ref component without conflating it."""

    readable = _SAFE.sub("-", value).strip("-.") or "item"
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    return f"{readable[:48]}-{digest}"


class GitRefCheckpointStore:
    """Keep candidates as private refs in the supplied durable checkout.

    This is the zero-configuration implementation.  A private ref is not
    GitHub publication and cannot be mistaken for reviewed work, while still
    keeping the commit reachable across process restart and garbage
    collection.
    """

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self.now = now

    def save(
        self,
        repo: Path,
        *,
        project_id: str,
        item_id: str,
        attempt: int,
    ) -> Checkpoint:
        commit = _git(repo, "rev-parse", "HEAD").strip()
        ref = (
            "refs/agent-harness/checkpoints/"
            f"{_component(project_id)}/{_component(item_id)}/{attempt}"
        )
        _git(repo, "update-ref", ref, commit)
        return Checkpoint(project_id, item_id, attempt, commit, ref, self.now())


class GitBundleCheckpointStore:
    """Copy each candidate to immutable content-addressed Git storage.

    ``root`` is always supplied by the deployment.  Saving uses a temporary
    file plus an atomic rename, so a successful return means the complete
    bundle exists.  Existing bundles are verified and reused, making replay
    idempotent.  The directory can be backed up and restored independently of
    both the operational queue and GitHub.
    """

    def __init__(self, root: Path, *, now: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.now = now

    def save(
        self,
        repo: Path,
        *,
        project_id: str,
        item_id: str,
        attempt: int,
    ) -> Checkpoint:
        commit = _git(repo, "rev-parse", "HEAD").strip()
        directory = self.root / _component(project_id) / _component(item_id)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{attempt}-{commit}.bundle"
        if not destination.exists():
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            try:
                _git(repo, "bundle", "create", str(temporary), "HEAD")
                _git(repo, "bundle", "verify", str(temporary))
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        _git(repo, "bundle", "verify", str(destination))
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        return Checkpoint(
            project_id,
            item_id,
            attempt,
            commit,
            str(destination),
            self.now(),
            digest,
        )

    def restore(self, checkpoint: Checkpoint, repo: Path, *, ref: str) -> None:
        """Restore a recorded commit under an explicit caller-selected ref."""

        bundle = Path(checkpoint.location)
        if hashlib.sha256(bundle.read_bytes()).hexdigest() != checkpoint.digest:
            raise ValueError(f"checkpoint digest does not match {bundle}")
        _git(repo, "fetch", str(bundle), f"{checkpoint.commit}:{ref}")
