"""Stage 2 contract tests for the item execution boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_harness.adapters.docker import (
    DockerEnvironmentFactory,
    DockerItemEnvironment,
    _diagnosis,
    _server_version,
)
from agent_harness.execution_environment import EnvironmentMount, EnvironmentSpec
from agent_harness.execution_environments import names, probe, resolve


def test_docker_backend_is_selected_by_installed_metadata() -> None:
    assert "docker" in names()
    ok, detail = probe("docker")
    assert ok or "daemon" in detail.lower() or "docker api" in detail.lower(), detail
    backend = resolve("docker")
    assert backend.name == "docker"


def test_readiness_names_the_daemon_not_a_go_template_error() -> None:
    """What an operator reads when the fleet will not start.

    Asked with `--format`, an unreachable daemon makes the CLI render a nil
    `Info` struct, so the LAST line of stderr is a Go template error and the
    line that says the daemon could not be reached is above it. Reporting the
    last line blamed reflection for a stopped service -- found when this image
    was first built, where the CLI is installed and no daemon answers.
    """
    stderr = (
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
        "Is the docker daemon running?\n"
        'template: :1:2: executing "" at <.ServerVersion>: reflect: '
        "indirection through nil pointer to embedded struct field Info\n"
    )

    assert _diagnosis(stderr).startswith("Cannot connect to the Docker daemon")
    assert "reflect:" not in _diagnosis(stderr)
    assert "template:" not in _diagnosis(stderr)
    # Nothing usable at all still says which component is being reported on.
    assert _diagnosis("") == "no reason given"
    assert _diagnosis("template: bad\n") == "template: bad"


def test_readiness_reports_the_server_version_when_a_daemon_answers() -> None:
    info = (
        "Client:\n Version: 28.0.1\nServer:\n Server Version: 28.0.1\n Storage Driver: overlay2\n"
    )

    assert _server_version(info) == "28.0.1"
    assert _server_version("Client:\n Version: 28.0.1\n") == ""


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
    # Portable `timeout`, not GNU's. An agent image is not required to ship
    # coreutils, and asserting the GNU spelling is what let the unportable
    # form reach a real daemon (see the BusyBox test below).
    assert "timeout -s TERM 7 " in " ".join(exec_call)
    assert result.stdout == "ok\n"
    assert any(call[1:4] == ["rm", "--force", "--volumes"] for call in calls)


def test_the_command_timeout_works_on_busybox_not_only_gnu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper must not assume GNU coreutils inside the agent image.

    The first live run against a real daemon used Alpine, whose BusyBox
    `timeout` rejects `--signal=TERM` and a `30s` suffix. Every command in the
    sandbox returned 1 with `timeout: unrecognized option: signal=TERM`, which
    reads as the agent's command failing rather than the harness's wrapper
    being unportable -- the exact misattribution this repository keeps paying
    for. `-s TERM` with bare seconds is accepted by both implementations.
    """
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1] == "inspect":
            return subprocess.CompletedProcess(argv, 0, "sha256:resolved\n", "")
        return subprocess.CompletedProcess(argv, 0, "container-id\n", "")

    monkeypatch.setattr("agent_harness.adapters.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("agent_harness.adapters.docker.subprocess.run", fake_run)
    item = tmp_path / "item"
    item.mkdir()
    environment = DockerItemEnvironment(
        EnvironmentSpec(image="alpine:3.21", worktree=item.resolve(), network="none")
    )

    environment.start()
    environment.run("printf ok", cwd=item, timeout=30)

    wrapper = " ".join(next(call for call in calls if call[1] == "exec"))
    assert "timeout -s TERM 30 " in wrapper
    assert "--signal" not in wrapper, "GNU-only long option is back"
    assert "30s" not in wrapper, "GNU-only unit suffix is back"


def test_the_item_environment_and_its_factory_report_the_daemon_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two copies of one check drifted, and only one of them was fixed."""

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            1,
            "",
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock.\n"
            'template: :1:2: executing "" at <.ServerVersion>: reflect: '
            "indirection through nil pointer to embedded struct field Info\n",
        )

    monkeypatch.setattr("agent_harness.adapters.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("agent_harness.adapters.docker.subprocess.run", fake_run)
    item = tmp_path / "item"
    item.mkdir()
    environment = DockerItemEnvironment(EnvironmentSpec(image="alpine:3.21", worktree=item))

    for ok, detail in (environment.check(), DockerEnvironmentFactory().check()):
        assert ok is False
        assert "Cannot connect to the Docker daemon" in detail
        assert "reflect:" not in detail and "template:" not in detail


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
