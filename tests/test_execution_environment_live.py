"""Acceptance tests for the configured Docker backend.

These use the backend itself, not a mocked subprocess. They run when a live
Docker daemon and acceptance image are explicitly supplied.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_harness.adapters.docker import DockerEnvironmentFactory, DockerItemEnvironment
from agent_harness.execution_environment import EnvironmentMount


def _image() -> str:
    return os.environ.get("HARNESS_STAGE2_IMAGE", "").strip()


def _docker_ready() -> bool:
    if not _image() or shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, timeout=5, check=False
    )
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_ready(),
    reason="HARNESS_STAGE2_IMAGE and a reachable Docker daemon are required",
)


def test_live_backend_confines_worktree_and_keeps_controller_credentials_out(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    dependency = tmp_path / "dependency"
    sibling = tmp_path / "sibling-secret.txt"
    worktree.mkdir()
    dependency.mkdir()
    (worktree / "inside.txt").write_text("inside\n")
    (dependency / "readme.txt").write_text("declared\n")
    sibling.write_text("must stay on host\n")

    backend = DockerEnvironmentFactory()
    environment = backend.create(
        worktree,
        image=_image(),
        mounts=(EnvironmentMount(dependency.resolve(), "/opt/dependency"),),
        environment={"DECLARED": "yes", "HOST_SIBLING": str(sibling.resolve())},
        network="none",
    )
    os.environ["HARNESS_STAGE2_CONTROLLER_SECRET"] = "must-not-enter"
    assert isinstance(environment, DockerItemEnvironment)
    container = ""
    try:
        environment.start()
        container = environment.container
        visible = environment.run(
            "cat /workspace/inside.txt; test -f /opt/dependency/readme.txt; "
            'test -z "$HARNESS_STAGE2_CONTROLLER_SECRET"; '
            'test "$DECLARED" = yes; test ! -e "$HOST_SIBLING"; '
            "! wget -q -O - --timeout=3 https://example.com; "
            "printf changed > /workspace/result.txt",
            cwd=worktree,
            timeout=30,
        )
        assert visible.returncode == 0, visible.stderr
        assert (worktree / "result.txt").read_text() == "changed"
    finally:
        environment.close()
        os.environ.pop("HARNESS_STAGE2_CONTROLLER_SECRET", None)
    assert container
    assert not environment.container
    inspected = subprocess.run(
        ["docker", "inspect", container], capture_output=True, text=True, check=False
    )
    assert inspected.returncode != 0, inspected.stdout


def test_live_backend_allows_outbound_network_when_configured(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    environment = DockerEnvironmentFactory().create(worktree, image=_image(), network="bridge")
    try:
        environment.start()
        result = environment.run(
            "wget -q -O - --timeout=10 https://example.com >/dev/null",
            cwd=worktree,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
    finally:
        environment.close()
