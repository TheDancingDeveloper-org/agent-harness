"""Stage 2 contract tests for the item execution boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_harness.adapters.docker import DockerEnvironmentFactory, DockerItemEnvironment
from agent_harness.execution_environment import EnvironmentMount, EnvironmentSpec
from agent_harness.execution_environments import names, probe, resolve


def test_docker_backend_is_selected_by_installed_metadata() -> None:
    assert "docker" in names()
    ok, detail = probe("docker")
    assert ok or "daemon" in detail.lower() or "docker api" in detail.lower(), detail
    backend = resolve("docker")
    assert backend.name == "docker"


def test_environment_spec_rejects_host_networking_and_unsafe_mounts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="host networking"):
        EnvironmentSpec(image="rust:1", worktree=tmp_path.resolve(), network="host")
    with pytest.raises(ValueError, match="safe absolute"):
        EnvironmentMount(tmp_path.resolve(), "/workspace/../host")
    with pytest.raises(ValueError, match="protected"):
        EnvironmentMount(tmp_path.resolve(), "/workspace")
    with pytest.raises(ValueError, match="valid variable"):
        EnvironmentSpec(
            image="rust:1", worktree=tmp_path.resolve(), environment={"not-valid": "secret"}
        )


def test_environment_evidence_names_secrets_but_never_records_values(tmp_path: Path) -> None:
    spec = EnvironmentSpec(
        image="registry.invalid/rust@sha256:abc",
        worktree=tmp_path.resolve(),
        mounts=(EnvironmentMount(tmp_path.resolve(), "/opt/toolchain"),),
        environment={"PATH": "/usr/bin", "TOKEN": "must-not-be-recorded"},
        network="bridge",
    )
    evidence = spec.describe(backend="docker", digest="sha256:resolved")
    rendered = repr(evidence)
    assert evidence["image_digest"] == "sha256:resolved"
    assert evidence["environment_names"] == ["PATH", "TOKEN"]
    assert "must-not-be-recorded" not in rendered
    assert evidence["mounts"][0]["writable"] is False
    assert evidence["security"]["no_new_privileges"] is True


def test_docker_backend_constructs_an_isolated_container_and_tears_it_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1] == "inspect":
            return subprocess.CompletedProcess(argv, 0, "sha256:resolved\n", "")
        if argv[1] == "exec":
            return subprocess.CompletedProcess(argv, 0, "ok\n", "")
        return subprocess.CompletedProcess(argv, 0, "container-id\n", "")

    monkeypatch.setattr("agent_harness.adapters.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("agent_harness.adapters.docker.subprocess.run", fake_run)
    source = tmp_path / "deps"
    source.mkdir()
    item = tmp_path / "item"
    item.mkdir()
    environment = DockerItemEnvironment(
        EnvironmentSpec(
            image="rust@sha256:image",
            worktree=item.resolve(),
            mounts=(EnvironmentMount(source.resolve(), "/opt/deps"),),
            environment={"SAFE": "yes"},
            network="bridge",
        )
    )

    environment.start()
    result = environment.run("printf ok", cwd=item, timeout=7)
    environment.close()

    create = next(call for call in calls if call[1] == "create")
    assert "--read-only" in create
    assert "--label" in create
    assert "agent_harness.managed=true" in create
    assert any(value.startswith("agent_harness.worktree=") for value in create)
    assert "--cap-drop" in create and create[create.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in create
    assert any("/opt/deps:ro" in value for value in create)
    assert "SAFE=yes" in create
    assert "TOKEN" not in " ".join(create)
    exec_call = next(call for call in calls if call[1] == "exec")
    assert "7s" in " ".join(exec_call)
    assert result.stdout == "ok\n"
    assert any(call[1:4] == ["rm", "--force", "--volumes"] for call in calls)


def test_docker_reaps_only_containers_for_the_requested_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["ps", "--all"]:
            return subprocess.CompletedProcess(argv, 0, "old-item\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("agent_harness.adapters.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("agent_harness.adapters.docker.subprocess.run", fake_run)
    worktree = tmp_path / "item"
    worktree.mkdir()

    DockerEnvironmentFactory().reap(worktree)

    listing = next(call for call in calls if call[1:3] == ["ps", "--all"])
    assert f"label=agent_harness.worktree={worktree.resolve()}" in listing
    assert ["docker", "rm", "--force", "--volumes", "old-item"] in calls


def test_docker_start_failure_removes_the_created_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1] == "start":
            return subprocess.CompletedProcess(argv, 1, "", "start failed")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("agent_harness.adapters.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("agent_harness.adapters.docker.subprocess.run", fake_run)
    item = tmp_path / "item"
    item.mkdir()
    environment = DockerItemEnvironment(EnvironmentSpec(image="rust:1", worktree=item.resolve()))

    with pytest.raises(Exception, match="start failed"):
        environment.start()

    assert any(call[1] == "rm" for call in calls)
    assert environment.container == ""
