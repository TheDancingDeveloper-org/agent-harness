"""Generic, metadata-resolved runners for repository-aware model roles.

A role runner answers one bounded question by letting a routed model inspect
an environment over several turns.  Core owns this contract and the lookup;
the implementation belongs to an adapter.  The separation is load-bearing:
adding a runner must not add an adapter import (or even an adapter's dotted
module path) to the execution path.

Runners are selected by name through the ``agent_harness.role_runners`` entry
point group.  Reading the available names imports nothing.  Resolving one
loads only the module that declared that name and checks the contract version
before any item is claimed.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .budgets import Budget, Spend
from .guard import CommandGuard
from .model_client import ModelClient

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "agent_harness.role_runners"
SETTING_KEY = "role_runner"
API_VERSION = 1


class RoleRunnerError(RuntimeError):
    """A named runner cannot safely be used by this build."""


class UnknownRoleRunner(RoleRunnerError):
    """No installed distribution declared the selected runner name."""


class IncompatibleRoleRunner(RoleRunnerError):
    """The runner implements a different version of the core contract."""


Report = Callable[[str, str, Mapping[str, Any]], None]
Account = Callable[[Spend], None]


@dataclass(frozen=True)
class RoleRunRequest:
    """Everything one role loop is allowed to depend on, supplied explicitly."""

    role: str
    task: str
    repo: Path
    project_id: str
    item_id: str
    attempt: int
    client: ModelClient
    guard: CommandGuard
    budget: Budget = field(default_factory=Budget)
    step_limit: int = 80
    command_timeout: int = 300
    writable: bool = True
    report: Report | None = None
    account: Account | None = None


@dataclass(frozen=True)
class RoleRunResult:
    """The loop's terminal answer and the usage the item must account for."""

    exit_status: str
    submission: str = ""
    calls: int = 0
    spend: Spend = field(default_factory=Spend)


class RoleRunner(Protocol):
    """One installed implementation of the repository-aware role loop."""

    @property
    def name(self) -> str: ...

    @property
    def api_version(self) -> int: ...

    @property
    def version(self) -> str: ...

    def run(self, request: RoleRunRequest, /) -> RoleRunResult: ...


def _declared_targets() -> dict[str, str]:
    """Return ``name -> module:attribute`` without importing a runner."""
    from importlib.metadata import entry_points

    try:
        return {point.name: point.value for point in entry_points(group=ENTRY_POINT_GROUP)}
    except Exception:  # noqa: BLE001 - broken metadata is a named readiness failure
        log.warning("could not read %s entry points", ENTRY_POINT_GROUP, exc_info=True)
        return {}


def names() -> list[str]:
    """Every installed runner name, without loading any declaring module."""
    return sorted(_declared_targets())


def _load_target(name: str, target: str) -> RoleRunner:
    module_name, _, attribute = target.partition(":")
    module = importlib.import_module(module_name)
    found: Any = getattr(module, attribute) if attribute else module
    if callable(found) and not hasattr(found, "run"):
        found = found()
    if not callable(getattr(found, "run", None)):
        raise TypeError(f"{target!r} does not provide run(request)")
    declared = str(getattr(found, "name", ""))
    if declared and declared != name:
        log.info("runner %r declares name %r; resolving it as %r", target, declared, name)
    version = getattr(found, "api_version", None)
    if version != API_VERSION:
        raise IncompatibleRoleRunner(
            f"role runner {name!r} uses contract version {version!r}; "
            f"this harness requires {API_VERSION}"
        )
    return found  # type: ignore[no-any-return]


def resolve(name: str) -> RoleRunner:
    """Load the one selected runner, failing before work is claimed."""
    target = _declared_targets().get(name)
    if target is None:
        available = ", ".join(names()) or "none"
        raise UnknownRoleRunner(
            f"unknown role runner {name!r}; installed names: {available} "
            f"(entry-point group {ENTRY_POINT_GROUP})"
        )
    try:
        return _load_target(name, target)
    except RoleRunnerError:
        raise
    except Exception as exc:
        raise RoleRunnerError(
            f"role runner {name!r} could not load from {target!r}: {exc}"
        ) from exc


def describe(runner: RoleRunner) -> str:
    """A stable readiness sentence naming both sides of the contract."""
    version = str(getattr(runner, "version", "unknown"))
    return (
        f"{runner.name} {version}; role-runner contract "
        f"{runner.api_version}/{API_VERSION} compatible"
    )


def probe(name: str) -> tuple[bool, str]:
    """Resolve a configured runner without calling a model or making a tree."""
    try:
        runner = resolve(name)
        detail = describe(runner)
    except Exception as exc:  # noqa: BLE001 - a readiness answer, never a crash
        return (False, str(exc))
    return (True, detail)
