"""The version-one, embedded TOML contract for a generic plan.

This module deliberately parses configuration only.  It does not inspect a
repository, load an adapter, resolve a model, or create queue state.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, cast

from .plan import ParsedPlan, parse_plan

REQUIRED_SECTIONS = (
    "Project identity and brief",
    "Scope and non-goals",
    "Rough architecture",
    "Data model",
    "GUI and interfaces",
    "Execution and local delivery",
    "Cross-cutting requirements",
    "Work items",
    "Local definition of done",
    "Assumptions, risks, and open questions",
)
_FENCE = re.compile(r"^\s*```harness\s*$", re.IGNORECASE)
_CLOSE = re.compile(r"^\s*```\s*$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*[A-Za-z0-9_]$")
_SAFE_TARGET = re.compile(
    r"^/(?!proc(?:/|$)|sys(?:/|$)|dev(?:/|$)|etc/shadow(?:/|$))[A-Za-z0-9._/@+:-]+$"
)


class PlanContractError(ValueError):
    """A plan does not meet the deterministic v1 contract."""


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanContractError(f"{path} must be a table")
    return value


def _keys(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PlanContractError(f"unknown field(s) at {path}: {', '.join(unknown)}")


def _required(value: Mapping[str, Any], name: str, path: str) -> Any:
    if name not in value:
        raise PlanContractError(f"missing required field {path}.{name}")
    return value[name]


def _string(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise PlanContractError(f"{path} must be a non-empty string")
    return value


def _argv(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise PlanContractError(f"{path} must be a non-empty argv array")
    return tuple(_string(part, f"{path}[{i}]") for i, part in enumerate(value))


def _argvs(value: Any, path: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise PlanContractError(f"{path} must be an array of argv arrays")
    return tuple(_argv(item, f"{path}[{i}]") for i, item in enumerate(value))


@dataclass(frozen=True)
class ImageSpec:
    strategy: str
    reference: str | None = None
    pull: bool | None = None
    source: str | None = None
    dockerfile: str | None = None
    context: str | None = None
    build_network: str | None = None
    immutable_base: str | None = None
    workdir: str | None = None
    copy: tuple[str, ...] = ()
    setup: tuple[tuple[str, ...], ...] = ()
    user: str | None = None


@dataclass(frozen=True)
class MountSpec:
    source: str
    target: str
    writable: bool = False


@dataclass(frozen=True)
class ProbeSpec:
    name: str
    command: tuple[str, ...]
    expect_regex: str | None = None


@dataclass(frozen=True)
class SecretSpec:
    name: str
    source: str
    scope: str


@dataclass(frozen=True)
class LimitsSpec:
    command_timeout_seconds: int
    memory: str
    cpus: str
    pids: int
    user: str
    rootfs_read_only: bool
    tmpfs_size: str


@dataclass(frozen=True)
class ExecutionSpec:
    backend: str
    network: str
    toolchains: tuple[str, ...]
    system_packages: tuple[str, ...]
    image: ImageSpec
    limits: LimitsSpec
    mounts: tuple[MountSpec, ...] = ()
    probes: tuple[ProbeSpec, ...] = ()
    secrets: tuple[SecretSpec, ...] = ()
    config: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentsSpec:
    role_runner: str
    required_roles: tuple[str, ...]
    max_workers: int
    max_attempts: int
    max_item_seconds: int
    max_item_spend_usd: float
    max_hold_seconds: int


@dataclass(frozen=True)
class RepositorySpec:
    base_ref: str
    integration_ref: str
    finalise_ref: str | None = None


@dataclass(frozen=True)
class ProjectSpec:
    key: str
    name: str


@dataclass(frozen=True)
class ChecksSpec:
    item: tuple[tuple[str, ...], ...]
    integration: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class LocalDeliverySpec:
    backend: str
    build: tuple[str, ...]
    build_context: str
    readiness: tuple[str, ...]
    readiness_context: str
    acceptance: tuple[str, ...]
    acceptance_context: str
    required_host_tools: tuple[str, ...]
    command_timeout_seconds: int
    readiness_timeout_seconds: int
    readiness_interval_seconds: int
    config: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanContract:
    version: int
    project: ProjectSpec
    repository: RepositorySpec
    agents: AgentsSpec
    execution: ExecutionSpec
    checks: ChecksSpec
    local_delivery: LocalDeliverySpec
    parsed: ParsedPlan
    sections: tuple[str, ...]

    def canonical(self) -> dict[str, Any]:
        """Return a stable JSON-compatible representation (argv stays argv)."""
        value = {
            item.name: getattr(self, item.name) for item in fields(self) if item.name != "parsed"
        }
        return cast(dict[str, Any], _jsonable(value))

    def canonical_json(self) -> str:
        return json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    return value


def _fenced_manifest(text: str) -> str:
    starts = [i for i, line in enumerate(text.splitlines()) if _FENCE.match(line)]
    if len(starts) != 1:
        raise PlanContractError("plan must contain exactly one fenced `harness` block")
    lines = text.splitlines()
    for end in range(starts[0] + 1, len(lines)):
        if _CLOSE.match(lines[end]):
            return "\n".join(lines[starts[0] + 1 : end])
    raise PlanContractError("harness fence is not closed")


def _positive(value: Any, path: str, *, zero: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or (value < 0 if zero else value <= 0):
        raise PlanContractError(f"{path} must be {'non-negative' if zero else 'positive'}")
    return value


def _strings(value: Any, path: str, *, nonempty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PlanContractError(f"{path} must be an array of strings")
    return tuple(_string(v, f"{path}[{i}]", nonempty=nonempty) for i, v in enumerate(value))


def _parse_manifest(raw: dict[str, Any]) -> tuple[Any, ...]:
    _keys(
        raw,
        {"version", "project", "repository", "agents", "execution", "checks", "local_delivery"},
        "manifest",
    )
    if raw.get("version") != 1:
        raise PlanContractError("manifest.version must be 1")
    project = _mapping(_required(raw, "project", "manifest"), "project")
    _keys(project, {"key", "name"}, "project")
    repository = _mapping(_required(raw, "repository", "manifest"), "repository")
    _keys(repository, {"base_ref", "integration_ref", "finalise_ref"}, "repository")
    refs = {
        k: _string(_required(repository, k, "repository"), f"repository.{k}")
        for k in ("base_ref", "integration_ref")
    }
    if any(not _REF.fullmatch(v) for v in refs.values()):
        raise PlanContractError("repository refs must be valid local ref names")
    finalise = repository.get("finalise_ref")
    if finalise is not None and (
        not _string(finalise, "repository.finalise_ref") or not _REF.fullmatch(finalise)
    ):
        raise PlanContractError("repository.finalise_ref must be a valid local ref name")
    if refs["base_ref"] == refs["integration_ref"] or finalise in refs.values():
        raise PlanContractError("repository refs must be distinct")

    agents = _mapping(_required(raw, "agents", "manifest"), "agents")
    _keys(
        agents,
        {
            "role_runner",
            "required_roles",
            "max_workers",
            "max_attempts",
            "max_item_seconds",
            "max_item_spend_usd",
            "max_hold_seconds",
        },
        "agents",
    )
    agent_values = {
        k: _required(agents, k, "agents")
        for k in (
            "role_runner",
            "required_roles",
            "max_workers",
            "max_attempts",
            "max_item_seconds",
            "max_item_spend_usd",
            "max_hold_seconds",
        )
    }
    execution = _parse_execution(_mapping(_required(raw, "execution", "manifest"), "execution"))
    checks = _mapping(_required(raw, "checks", "manifest"), "checks")
    _keys(checks, {"item", "integration"}, "checks")
    delivery = _parse_delivery(
        _mapping(_required(raw, "local_delivery", "manifest"), "local_delivery")
    )
    return (
        ProjectSpec(
            _string(_required(project, "key", "project"), "project.key"),
            _string(_required(project, "name", "project"), "project.name"),
        ),
        RepositorySpec(refs["base_ref"], refs["integration_ref"], finalise),
        AgentsSpec(
            _string(agent_values["role_runner"], "agents.role_runner"),
            _strings(agent_values["required_roles"], "agents.required_roles"),
            _positive(agent_values["max_workers"], "agents.max_workers"),
            _positive(agent_values["max_attempts"], "agents.max_attempts"),
            _positive(agent_values["max_item_seconds"], "agents.max_item_seconds"),
            float(agent_values["max_item_spend_usd"]),
            _positive(agent_values["max_hold_seconds"], "agents.max_hold_seconds"),
        ),
        execution,
        ChecksSpec(
            _argvs(agent_values := checks["item"], "checks.item"),
            _argvs(checks["integration"], "checks.integration"),
        ),
        delivery,
    )


def _parse_execution(raw: dict[str, Any]) -> ExecutionSpec:
    _keys(
        raw,
        {
            "backend",
            "network",
            "toolchains",
            "system_packages",
            "image",
            "limits",
            "mounts",
            "probes",
            "secrets",
            "config",
        },
        "execution",
    )
    image = _mapping(_required(raw, "image", "execution"), "execution.image")
    _keys(
        image,
        {
            "strategy",
            "reference",
            "pull",
            "source",
            "dockerfile",
            "context",
            "build_network",
            "immutable_base",
            "workdir",
            "copy",
            "setup",
            "user",
        },
        "execution.image",
    )
    strategy = _string(
        _required(image, "strategy", "execution.image.strategy"), "execution.image.strategy"
    )
    if strategy not in {"existing", "build"}:
        raise PlanContractError("execution.image.strategy must be existing or build")
    reference = image.get("reference")
    if strategy == "existing" and not reference:
        raise PlanContractError("existing images require execution.image.reference")
    setup = _argvs(image.get("setup", []), "execution.image.setup")
    limits = _mapping(_required(raw, "limits", "execution"), "execution.limits")
    _keys(
        limits,
        {
            "command_timeout_seconds",
            "memory",
            "cpus",
            "pids",
            "user",
            "rootfs_read_only",
            "tmpfs_size",
        },
        "execution.limits",
    )
    mounts = []
    for i, entry in enumerate(raw.get("mounts", [])):
        item = _mapping(entry, f"execution.mounts[{i}]")
        _keys(item, {"source", "target", "writable"}, f"execution.mounts[{i}]")
        source = _string(
            _required(item, "source", f"execution.mounts[{i}]"), f"execution.mounts[{i}].source"
        )
        target = _string(
            _required(item, "target", f"execution.mounts[{i}]"), f"execution.mounts[{i}].target"
        )
        if (
            source.startswith("/")
            or ".." in source.split("/")
            or not _SAFE_TARGET.fullmatch(target)
        ):
            raise PlanContractError(f"unsafe mount at execution.mounts[{i}]")
        mounts.append(MountSpec(source, target, bool(item.get("writable", False))))
    probes = []
    for i, entry in enumerate(raw.get("probes", [])):
        item = _mapping(entry, f"execution.probes[{i}]")
        _keys(item, {"name", "command", "expect_regex"}, f"execution.probes[{i}]")
        probes.append(
            ProbeSpec(
                _string(_required(item, "name", f"execution.probes[{i}]"), "probe.name"),
                _argv(_required(item, "command", f"execution.probes[{i}]"), "probe.command"),
                item.get("expect_regex"),
            )
        )
    secrets = []
    for i, entry in enumerate(raw.get("secrets", [])):
        item = _mapping(entry, f"execution.secrets[{i}]")
        _keys(item, {"name", "source", "scope"}, f"execution.secrets[{i}]")
        scope = _string(_required(item, "scope", "secret"), "secret.scope")
        if scope not in {"provisioning", "checks", "delivery"}:
            raise PlanContractError("secret scope must be provisioning, checks, or delivery")
        secrets.append(
            SecretSpec(
                _string(_required(item, "name", "secret"), "secret.name"),
                _string(_required(item, "source", "secret"), "secret.source"),
                scope,
            )
        )
    lim = LimitsSpec(
        _positive(limits["command_timeout_seconds"], "execution.limits.command_timeout_seconds"),
        _string(limits.get("memory", ""), "limits.memory"),
        _string(limits.get("cpus", ""), "limits.cpus"),
        _positive(limits.get("pids", 0), "limits.pids"),
        _string(limits.get("user", ""), "limits.user"),
        bool(limits.get("rootfs_read_only", False)),
        _string(limits.get("tmpfs_size", ""), "limits.tmpfs_size"),
    )
    return ExecutionSpec(
        _string(_required(raw, "backend", "execution"), "execution.backend"),
        _string(_required(raw, "network", "execution"), "execution.network"),
        _strings(_required(raw, "toolchains", "execution"), "execution.toolchains"),
        _strings(raw.get("system_packages", []), "execution.system_packages"),
        ImageSpec(
            strategy,
            reference,
            image.get("pull"),
            image.get("source"),
            image.get("dockerfile"),
            image.get("context"),
            image.get("build_network"),
            image.get("immutable_base"),
            image.get("workdir"),
            _strings(image.get("copy", []), "image.copy"),
            setup,
            image.get("user"),
        ),
        lim,
        tuple(mounts),
        tuple(probes),
        tuple(secrets),
        _freeze(raw.get("config", {})),
    )


def _parse_delivery(raw: dict[str, Any]) -> LocalDeliverySpec:
    allowed = {
        "backend",
        "build",
        "build_context",
        "readiness",
        "readiness_context",
        "acceptance",
        "acceptance_context",
        "required_host_tools",
        "command_timeout_seconds",
        "readiness_timeout_seconds",
        "readiness_interval_seconds",
        "config",
    }
    _keys(raw, allowed, "local_delivery")
    contexts = {
        k: _string(_required(raw, k, "local_delivery"), f"local_delivery.{k}")
        for k in ("build_context", "readiness_context", "acceptance_context")
    }
    if (
        contexts["build_context"] not in {"runner", "host"}
        or contexts["readiness_context"] not in {"runner", "host"}
        or contexts["acceptance_context"] not in {"runner", "host"}
    ):
        raise PlanContractError("delivery contexts must be runner or host")
    return LocalDeliverySpec(
        _string(_required(raw, "backend", "local_delivery"), "local_delivery.backend"),
        _argv(_required(raw, "build", "local_delivery"), "local_delivery.build"),
        contexts["build_context"],
        _argv(_required(raw, "readiness", "local_delivery"), "local_delivery.readiness"),
        contexts["readiness_context"],
        _argv(_required(raw, "acceptance", "local_delivery"), "local_delivery.acceptance"),
        contexts["acceptance_context"],
        _strings(raw.get("required_host_tools", []), "local_delivery.required_host_tools"),
        _positive(_required(raw, "command_timeout_seconds", "local_delivery"), "delivery timeout"),
        _positive(
            _required(raw, "readiness_timeout_seconds", "local_delivery"), "readiness timeout"
        ),
        _positive(
            _required(raw, "readiness_interval_seconds", "local_delivery"), "readiness interval"
        ),
        _freeze(raw.get("config", {})),
    )


def _validate_prose(parsed: ParsedPlan, text: str) -> None:
    headings = [
        re.sub(r"\s+", " ", m.group(1).strip()).casefold()
        for m in re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE)
    ]
    required = {h.casefold() for h in REQUIRED_SECTIONS}
    missing = required - set(headings)
    duplicate = sorted(h for h in required if headings.count(h) > 1)
    if missing or duplicate:
        raise PlanContractError(
            "required sections missing: "
            f"{', '.join(sorted(missing))}; duplicates: {', '.join(duplicate)}"
        )
    if not parsed.items:
        raise PlanContractError("Work items must contain at least one item")
    lines = text.splitlines()
    starts = {item.id: item.line - 1 for item in parsed.items}
    ordered = sorted(starts.values())
    for item in parsed.items:
        body = item.body
        if not re.search(r"(?im)^\*\*Deliverable:\*\*\s*\S", body):
            raise PlanContractError(f"item {item.id} needs a non-empty Deliverable")
        if not re.search(r"(?im)^\*\*Acceptance:\*\*", body):
            raise PlanContractError(f"item {item.id} needs an Acceptance list")
        start = starts[item.id]
        end = next((line for line in ordered if line > start), len(lines))
        item_source = "\n".join(lines[start:end])
        if not item.depends_on and not re.search(
            r"(?im)^(?:depends\s+on|dependenc(?:y|ies))\s*:\s*none\s*\.?\s*$",
            item_source,
        ):
            raise PlanContractError(
                f"item {item.id} must declare dependencies or `depends on: none`"
            )


def _normalise_dependencies(parsed: ParsedPlan) -> None:
    """Apply the v1 meaning of the exact ``none`` declaration."""
    for item in parsed.items:
        item.depends_on = [token for token in item.depends_on if token.strip().casefold() != "none"]


def parse_plan_contract(text: str) -> PlanContract:
    manifest = _fenced_manifest(text)
    try:
        raw = tomllib.loads(manifest)
    except tomllib.TOMLDecodeError as exc:
        line = getattr(exc, "lineno", "?")
        column = getattr(exc, "colno", "?")
        message = getattr(exc, "msg", str(exc))
        raise PlanContractError(
            f"invalid harness TOML at line {line}, column {column}: {message}"
        ) from exc
    parsed = parse_plan(text)
    _normalise_dependencies(parsed)
    _validate_prose(parsed, text)
    project, repository, agents, execution, checks, delivery = _parse_manifest(raw)
    return PlanContract(
        1,
        project,
        repository,
        agents,
        execution,
        checks,
        delivery,
        parsed,
        tuple(REQUIRED_SECTIONS),
    )
