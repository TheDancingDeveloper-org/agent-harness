"""Shared global role-routing configuration for API and browser clients."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .schemas import RoleMap, RoleMapView, RoleRoute, RoutedRole
from .work import WorkQueue

ROLE_MAP_KEY = "role_map"


class RoleConfigurationConflict(Exception):
    """The global role map changed after an operator reviewed a replacement."""


def stored_role_map(queue: WorkQueue) -> dict[str, Any] | None:
    """Return the exact persisted value used for an optimistic browser review."""
    stored = queue.get_setting(ROLE_MAP_KEY)
    if stored is None:
        return None
    if not isinstance(stored, dict):
        raise ValueError("the stored role map is not an object")
    return stored


def role_map_payload(role_map: RoleMap) -> dict[str, dict[str, Any]]:
    """Persist every public RoleRoute field, including fallback and pricing metadata."""
    return {name: route.model_dump(mode="json") for name, route in role_map.roles.items()}


def role_map_digest(stored: dict[str, Any] | None) -> str:
    encoded = json.dumps(stored, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def configure_roles(
    queue: WorkQueue,
    role_map: RoleMap,
    *,
    expected: dict[str, Any] | None | object = ...,
) -> None:
    """Persist a validated map, optionally as an atomic reviewed replacement."""
    payload = role_map_payload(role_map)
    if expected is ...:
        queue.set_setting(ROLE_MAP_KEY, payload)
        return
    if not queue.compare_and_set_setting(ROLE_MAP_KEY, expected, payload):
        raise RoleConfigurationConflict("role routing changed after review")


def role_map_view(state: Any, queue: WorkQueue) -> RoleMapView:
    return role_map_view_for(state, stored_role_map(queue) or {})


def role_map_view_for(state: Any, stored: dict[str, Any]) -> RoleMapView:
    """Annotate configured roles with actual executor use and independence."""
    from .model_client import reviewer_independence, routes_from_map
    from .runtime import ExecutorRoles

    executor = getattr(state, "executor_roles", None) or ExecutorRoles()
    routes = routes_from_map(stored, default_preset=getattr(state, "default_preset", ""))
    independent, why = reviewer_independence(routes, implemented_by=executor.implemented_by)
    return RoleMapView(
        reviewer_independent=independent,
        reviewer_note=why,
        roles={
            name: RoutedRole(
                **RoleRoute(**route).model_dump(),
                used=executor.calls_role(name),
                unused_reason=executor.unused_reason(name),
            )
            for name, route in stored.items()
        },
    )


def safe_endpoint(endpoint: str) -> str:
    """Render route identity without URL credentials, query strings, or fragments."""
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port_number = parsed.port
    except ValueError:
        port_number = None
    port = f":{port_number}" if port_number is not None else ""
    netloc = f"{hostname}{port}" if hostname else "redacted-host"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
