"""Docker/OCI execution backend.

The backend creates one short-lived container per item and uses ``docker exec``
for the loop's individual commands.  The controller's environment is never
inherited: only explicitly supplied variable names and values are passed to
the container.  The Docker socket is not mounted, and the container is
non-root with all Linux capabilities dropped.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..execution_environment import (
    API_VERSION,
    EnvironmentMount,
    EnvironmentResult,
    EnvironmentSpec,
    ExecutionEnvironment,
)


class DockerEnvironmentError(RuntimeError):
    """The configured Docker backend could not create or use its container."""


@dataclass
class DockerItemEnvironment:
    spec: EnvironmentSpec
    container: str = ""
    digest: str | None = None
    started: bool = False

    name = "docker"
    api_version = API_VERSION
    version = "docker-cli"

    @property
    def cwd(self) -> str:
        return "/workspace"

    def _docker(self, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        if shutil.which("docker") is None:
            raise DockerEnvironmentError("docker is not installed or is not on PATH")
        try:
            return subprocess.run(
                ["docker", *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DockerEnvironmentError(f"docker command failed to start: {exc}") from exc

    def check(self) -> tuple[bool, str]:
        """The same answer the factory gives, from the same helpers.

        This was a second, divergent copy of the daemon check, and it kept the
        `--format` defect after the factory's copy was fixed: an unreachable
        daemon reported a Go template error instead of naming the daemon.
        """
        if shutil.which("docker") is None:
            return False, "docker is not installed or is not on PATH"
        result = self._docker("info", timeout=10)
        if result.returncode != 0:
            return False, f"Docker daemon is unreachable: {_diagnosis(result.stderr)}"
        return True, f"Docker daemon {_server_version(result.stdout) or 'available'}"

    def _ensure_started(self) -> None:
        if self.started:
            return
        if not self.spec.worktree.is_dir():
            raise DockerEnvironmentError(f"item worktree does not exist: {self.spec.worktree}")
        self.container = "agent-harness-" + uuid.uuid4().hex
        args = [
            "create",
            "--name",
            self.container,
            "--label",
            "agent_harness.managed=true",
            "--label",
            f"agent_harness.worktree={self.spec.worktree}",
            "--user",
            self.spec.user,
            "--workdir",
            "/workspace",
            "--network",
            self.spec.network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            str(self.spec.pids_limit),
            "--memory",
            self.spec.memory_limit,
            "--cpus",
            self.spec.cpus,
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={self.spec.tmpfs_size}",
            "-v",
            f"{self.spec.worktree}:/workspace:rw",
        ]
        if self.spec.rootfs_read_only:
            args.append("--read-only")
        for mount in self.spec.mounts:
            mode = "rw" if mount.writable else "ro"
            args.extend(("-v", f"{mount.source}:{mount.target}:{mode}"))
        for name, value in sorted(self.spec.environment.items()):
            args.extend(("-e", f"{name}={value}"))
        # Keep the container alive without running user code.  Commands are
        # separately audited and executed by `exec`, not interpolated here.
        args.extend((self.spec.image, "/bin/sh", "-c", "while :; do sleep 3600; done"))
        created = self._docker(*args, timeout=30)
        if created.returncode != 0:
            raise DockerEnvironmentError(created.stderr.strip()[-1000:])
        started = self._docker("start", self.container, timeout=30)
        if started.returncode != 0:
            self.close()
            raise DockerEnvironmentError(started.stderr.strip()[-1000:])
        inspected = self._docker("inspect", "--format", "{{.Image}}", self.container, timeout=30)
        self.digest = inspected.stdout.strip() if inspected.returncode == 0 else None
        self.started = True

    def start(self) -> None:
        self._ensure_started()

    def run(self, command: str, *, cwd: Path, timeout: int) -> EnvironmentResult:
        self._ensure_started()
        try:
            relative = cwd.resolve().relative_to(self.spec.worktree.resolve())
        except ValueError as exc:
            raise DockerEnvironmentError(
                f"command cwd {cwd} is outside the item worktree {self.spec.worktree}"
            ) from exc
        target = "/workspace" if str(relative) == "." else f"/workspace/{relative}"
        # `timeout` is inside the container so a client-side timeout cannot
        # leave a test process running after docker exec has gone away.
        #
        # `-s TERM` and bare seconds, not `--signal=TERM 30s`: the long option
        # and the unit suffix are GNU coreutils, and an agent image is not
        # required to ship those. Against BusyBox -- Alpine, which is the
        # small acceptance image -- the GNU form made EVERY command fail with
        # `timeout: unrecognized option: signal=TERM` and returncode 1, which
        # reads as the agent's command failing rather than the harness's own
        # wrapper being unportable. Found on the first live run against a real
        # daemon. Both GNU and BusyBox accept this form.
        wrapped = f"timeout -s TERM {timeout} /bin/sh -lc {shlex.quote(command)}"
        result = self._docker(
            "exec",
            "--user",
            self.spec.user,
            "--workdir",
            target,
            self.container,
            "/bin/sh",
            "-lc",
            wrapped,
            timeout=timeout + 10,
        )
        timed_out = result.returncode == 124
        return EnvironmentResult(result.stdout, result.stderr, result.returncode, timed_out)

    def template_vars(self) -> Mapping[str, str]:
        # Do not pass controller environment variables into the model prompt.
        return {
            "system": "container",
            "node": "docker",
            "release": self.spec.image,
            "version": self.version,
            "machine": "container",
            "processor": "container",
            "cwd": self.cwd,
        }

    def describe(self) -> Mapping[str, Any]:
        return self.spec.describe(backend=self.name, digest=self.digest)

    def close(self) -> None:
        if self.container:
            self._docker("rm", "--force", "--volumes", self.container, timeout=30)
        self.container = ""
        self.started = False


def _diagnosis(stderr: str) -> str:
    """The line a person can act on, not the last line the CLI printed.

    Go template noise is dropped: it is a consequence of the daemon being
    absent, never the reason, and letting it win means readiness blames
    reflection for a stopped service.
    """
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    useful = [
        line
        for line in lines
        if not line.startswith("template:") and "reflect:" not in line and line != "ERROR:"
    ]
    return (useful or lines or ["no reason given"])[0].removeprefix("ERROR: ").strip()


def _server_version(stdout: str) -> str:
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Server Version:"):
            return stripped.split(":", 1)[1].strip()
    return ""


class DockerEnvironmentFactory:
    name = "docker"
    api_version = API_VERSION
    version = "docker-cli"

    def check(self) -> tuple[bool, str]:
        """Is a daemon reachable, and what should an operator be told?

        `docker info` is asked **without** `--format`. With a format string,
        an unreachable daemon does not produce the message a person needs: the
        CLI still renders a mostly-nil `Info` struct, so the last line of
        stderr is a Go template error about "indirection through nil pointer
        to embedded struct field Info", and the line that actually says the
        daemon could not be reached is buried above it. Readiness then reports
        a reflect error, which names the wrong component — the failure mode
        this repository keeps paying for.
        """
        if shutil.which("docker") is None:
            return False, "docker is not installed or is not on PATH"
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"Docker daemon check failed: {exc}"
        if result.returncode != 0:
            return False, f"Docker daemon is unreachable: {_diagnosis(result.stderr)}"
        return True, f"Docker daemon {_server_version(result.stdout) or 'available'}"

    def reap(self, worktree: Path) -> None:
        """Remove containers left by a killed controller for one item tree."""
        if shutil.which("docker") is None:
            return
        try:
            listed = subprocess.run(
                [
                    "docker",
                    "ps",
                    "--all",
                    "--quiet",
                    "--filter",
                    "label=agent_harness.managed=true",
                    "--filter",
                    f"label=agent_harness.worktree={worktree.resolve()}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if listed.returncode != 0:
                return
            for container in listed.stdout.splitlines():
                container = container.strip()
                if container:
                    subprocess.run(
                        ["docker", "rm", "--force", "--volumes", container],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
        except (OSError, subprocess.SubprocessError):
            # Reaping is recovery work.  A backend that is unavailable cannot
            # make the item safer by turning cleanup into a worker crash; the
            # next start/check reports backend readiness authoritatively.
            return

    def create(
        self,
        worktree: Path,
        *,
        image: str,
        mounts: tuple[EnvironmentMount, ...] = (),
        environment: Mapping[str, str] | None = None,
        network: str = "bridge",
    ) -> ExecutionEnvironment:
        return DockerItemEnvironment(
            EnvironmentSpec(
                image=image,
                worktree=worktree.resolve(),
                mounts=mounts,
                environment=environment or {},
                network=network,
            )
        )


BACKEND = DockerEnvironmentFactory()
