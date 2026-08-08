"""Shared application service for project configuration.

The public JSON API and first-party browser both accept the typed
``ProjectSpec`` contract. Keeping persistence and live-pool reconciliation in
one service prevents the two controllers from assigning different behavior to
the same configuration.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .schemas import ProjectSpec, RoleRoute
from .work import Project, WorkQueue


class ProjectConfigurationConflict(Exception):
    """The project changed after the operator reviewed its replacement."""


def project_spec(project: Project) -> ProjectSpec:
    """The public contract view of one persisted project."""
    return ProjectSpec(
        project_id=project.project_id,
        name=project.name,
        repo=project.repo,
        work_dir=project.work_dir,
        base_branch=project.base_branch,
        checks=list(project.checks),
        fixes={key: list(value) for key, value in (project.fixes or {}).items()},
        apply_fixes=bool(project.apply_fixes),
        durability=project.durability,
        max_item_seconds=project.max_item_seconds,
        max_item_spend_usd=project.max_item_spend_usd,
        max_hold_seconds=project.max_hold_seconds,
        plan_path=project.plan_path,
        plan_branch=project.plan_branch,
        roles=(
            {name: RoleRoute(**route) for name, route in project.roles.items()}
            if project.roles
            else None
        ),
        max_workers=project.max_workers,
        max_attempts=project.max_attempts,
        min_free_disk_gb=project.min_free_disk_gb,
    )


def project_spec_digest(spec: ProjectSpec) -> str:
    """Stable semantic fingerprint used to reject stale browser reviews."""
    encoded = json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def configure_project(
    queue: WorkQueue,
    spec: ProjectSpec,
    *,
    fleet: Any | None = None,
    expected_updated_at: float | None = None,
) -> None:
    """Persist one validated project specification and reconcile its live pool."""
    project = Project(
        project_id=spec.project_id,
        name=spec.name,
        repo=spec.repo,
        work_dir=spec.work_dir,
        base_branch=spec.base_branch,
        checks=list(spec.checks),
        fixes={key: list(value) for key, value in spec.fixes.items()},
        apply_fixes=spec.apply_fixes,
        durability=spec.durability,
        max_item_seconds=spec.max_item_seconds,
        max_item_spend_usd=spec.max_item_spend_usd,
        max_hold_seconds=spec.max_hold_seconds,
        plan_path=spec.plan_path,
        plan_branch=spec.plan_branch,
        roles=(
            {name: route.model_dump() for name, route in spec.roles.items()} if spec.roles else None
        ),
        max_workers=spec.max_workers,
        max_attempts=spec.max_attempts,
        min_free_disk_gb=spec.min_free_disk_gb,
    )
    if expected_updated_at is None:
        queue.add_project(project)
    elif not queue.update_project(project, expected_updated_at=expected_updated_at):
        raise ProjectConfigurationConflict("project configuration changed after review")
    if fleet is not None and hasattr(fleet, "resize"):
        # A no-op for a stopped project: there is no pool to reconcile, and
        # the persisted budget is what the next explicit start reads.
        fleet.resize(spec.project_id)
