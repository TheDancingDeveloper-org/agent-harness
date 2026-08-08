"""Shared application services for operator-triggered audit operations.

The public JSON API and the first-party browser both use these functions. The
browser adds an explicit review and operator attribution; neither controller
gets a second interpretation of reconciliation or retention behavior.
"""

from __future__ import annotations

from typing import Any

from .audit import AuditStore
from .maintenance import run_maintenance
from .reconcile import GitHubReconciler, items_by_pr
from .schemas import MaintenanceResult, ReconcileResult


def reconcile_repository(
    queue: Any, audit: AuditStore, repo: str, *, project_id: str | None = None
) -> ReconcileResult:
    """Record merge/revert facts for one resolved repository."""
    mapping = items_by_pr(queue) if queue is not None else {}
    if project_id is not None:
        mapping = {
            number: attribution
            for number, attribution in mapping.items()
            if attribution.get("project_id") == project_id
        }
    report = GitHubReconciler(repo, audit).reconcile(mapping)
    return ReconcileResult(
        merged=report.merged,
        closed_unmerged=report.closed_unmerged,
        reverted=report.reverted,
        skipped=report.skipped,
        errors=report.errors,
    )


def maintain_audit(audit: AuditStore, retention_days: int) -> MaintenanceResult:
    """Close rollups, then thin covered raw rows under one retention policy."""
    report = run_maintenance(audit, retention_days=retention_days)
    return MaintenanceResult(
        rolled_up=report.rolled_up,
        thinned=report.thinned,
        errors=report.errors,
    )
