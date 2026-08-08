"""The generic command-environment boundary used by role runners.

The model loop is deliberately kept in the harness process.  It asks an
execution environment to inspect, edit and test an item's checkout, while the
environment owns the operating-system boundary around those commands.  Core
defines only this contract; concrete backends are selected through installed
metadata.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

API_VERSION = 1
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROTECTED_TARGETS = frozenset({"/", "/workspace", "/proc", "/sys", "/dev"})


@dataclass(frozen=True)
class EnvironmentMount:
    """One explicitly declared host path made visible to an item."""

    source: Path
    target: str
    writable: bool = False

    def __post_init__(self) -> None:
        if not self.source.is_absolute():
            raise ValueError("execution-environment mount sources must be absolute")
        if not self.source.exists():
            raise ValueError(f"execution-environment mount source does not exist: {self.source}")
        target = Path(self.target)
        if not target.is_absolute() or ".." in target.parts:
            raise ValueError("execution-environment mount targets must be safe absolute paths")
        if str(target) in _PROTECTED_TARGETS or str(target).startswith("/workspace/"):
            raise ValueError(f"execution-environment mount target is protected: {target}")
        if target == Path("/var/run/docker.sock") or self.source.resolve().name == "docker.sock":
            raise ValueError("the Docker socket cannot be mounted into an item")


@dataclass(frozen=True)
class EnvironmentSpec:
    """The complete, auditable configuration for one item environment."""

    image: str
    worktree: Path
    mounts: tuple[EnvironmentMount, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)
    network: str = "bridge"
    command_timeout: int = 300
    memory_limit: str = "2g"
    cpus: str = "2"
    pids_limit: int = 512
    user: str = "1000:1000"
    rootfs_read_only: bool = True
    tmpfs_size: str = "512m"

    def __post_init__(self) -> None:
        if not self.image.strip():
            raise ValueError("an execution environment needs an image reference")
        if not self.worktree.is_absolute():
            raise ValueError("the item worktree must be an absolute path")
        if self.network not in {"bridge", "none", "host"}:
            raise ValueError(f"unsupported execution network policy: {self.network!r}")
        if self.network == "host":
            raise ValueError("host networking is not an accepted item-environment policy")
        if self.command_timeout <= 0 or self.pids_limit <= 0:
            raise ValueError("execution limits must be positive")
        names = set(self.environment)
        if any(_ENVIRONMENT_NAME.fullmatch(name) is None for name in names):
            raise ValueError("environment names must be valid variable names")

    def describe(self, *, backend: str, digest: str | None = None) -> dict[str, Any]:
        """Return evidence safe to persist; never include environment values."""
        return {
            "backend": backend,
            "image": self.image,
            "image_digest": digest,
            "worktree": {"source": str(self.worktree), "target": "/workspace", "writable": True},
            "mounts": [
                {"source": str(m.source), "target": m.target, "writable": m.writable}
                for m in self.mounts
            ],
            "environment_names": sorted(self.environment),
            "network": self.network,
            "limits": {
                "command_timeout": self.command_timeout,
                "memory": self.memory_limit,
                "cpus": self.cpus,
                "pids": self.pids_limit,
            },
            "identity": self.user,
            "security": {
                "rootfs_read_only": self.rootfs_read_only,
                "no_new_privileges": True,
                "capabilities_dropped": "ALL",
            },
        }


@dataclass(frozen=True)
class EnvironmentResult:
    """The result of one command executed inside an item environment."""

    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


class ExecutionEnvironment(Protocol):
    """One item-scoped environment, created before its first tool command."""

    @property
    def name(self) -> str: ...

    @property
    def api_version(self) -> int: ...

    @property
    def version(self) -> str: ...

    @property
    def cwd(self) -> str: ...

    def run(self, command: str, *, cwd: Path, timeout: int) -> EnvironmentResult: ...

    def start(self) -> None: ...

    def check(self) -> tuple[bool, str]: ...

    def template_vars(self) -> Mapping[str, str]: ...

    def describe(self) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


class LocalExecutionEnvironment:
    """The historical host backend, retained for fixtures and compatibility.

    This is explicitly not a security boundary. A real deployment must select
    an OS-enforced backend such as the shipped Docker backend.
    """

    name = "host"
    api_version = API_VERSION
    version = "compatibility"

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    @property
    def cwd(self) -> str:
        return str(self.repo)

    def run(self, command: str, *, cwd: Path, timeout: int) -> EnvironmentResult:
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _text(exc.stdout)
            stderr = _text(exc.stderr)
            return EnvironmentResult(stdout, stderr, 124, timed_out=True)
        return EnvironmentResult(result.stdout, result.stderr, result.returncode)

    def start(self) -> None:
        return None

    def check(self) -> tuple[bool, str]:
        return True, "host compatibility backend available"

    def template_vars(self) -> Mapping[str, str]:
        return {**platform.uname()._asdict(), **os.environ, "cwd": str(self.repo)}

    def describe(self) -> Mapping[str, Any]:
        return {
            "backend": self.name,
            "security_boundary": "none; compatibility backend only",
            "worktree": str(self.repo),
        }

    def close(self) -> None:
        return None


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value
