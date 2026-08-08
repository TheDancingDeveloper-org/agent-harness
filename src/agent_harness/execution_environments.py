"""Metadata lookup for item-scoped execution environments."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from .execution_environment import EnvironmentMount, ExecutionEnvironment

log = logging.getLogger(__name__)
ENTRY_POINT_GROUP = "agent_harness.execution_environments"
API_VERSION = 1


class ExecutionEnvironmentFactory(Protocol):
    name: str
    api_version: int
    version: str

    def check(self) -> tuple[bool, str]: ...

    def create(
        self,
        worktree: Path,
        *,
        image: str,
        mounts: tuple[EnvironmentMount, ...] = (),
        environment: Mapping[str, str] | None = None,
        network: str = "bridge",
    ) -> ExecutionEnvironment: ...


class UnknownEnvironment(RuntimeError):
    """No installed execution backend has the configured name."""


def _targets() -> dict[str, str]:
    from importlib.metadata import entry_points

    try:
        return {point.name: point.value for point in entry_points(group=ENTRY_POINT_GROUP)}
    except Exception:  # noqa: BLE001
        log.warning("could not read %s entry points", ENTRY_POINT_GROUP, exc_info=True)
        return {}


def names() -> list[str]:
    return sorted(_targets())


def resolve(name: str) -> ExecutionEnvironmentFactory:
    target = _targets().get(name)
    if target is None:
        raise UnknownEnvironment(
            f"unknown execution backend {name!r}; installed names: "
            f"{', '.join(names()) or 'none'} ({ENTRY_POINT_GROUP})"
        )
    module_name, _, attribute = target.partition(":")
    try:
        found: Any = getattr(importlib.import_module(module_name), attribute)
        if callable(found) and not hasattr(found, "create"):
            found = found()
    except Exception as exc:  # noqa: BLE001
        raise UnknownEnvironment(f"execution backend {name!r} could not load: {exc}") from exc
    compatible = (
        callable(getattr(found, "create", None))
        and getattr(found, "api_version", None) == API_VERSION
    )
    has_check = callable(getattr(found, "check", None))
    if not compatible or not has_check:
        raise UnknownEnvironment(
            f"execution backend {name!r} does not implement contract {API_VERSION} "
            "with a runtime readiness check"
        )
    return found  # type: ignore[no-any-return]


def probe(name: str) -> tuple[bool, str]:
    try:
        backend = resolve(name)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    ok, detail = backend.check()
    return (
        ok,
        f"{backend.name} {backend.version}; execution contract {API_VERSION} compatible; {detail}",
    )
