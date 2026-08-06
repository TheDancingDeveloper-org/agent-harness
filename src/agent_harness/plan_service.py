"""Shared, reviewable plan-to-backlog operations for API and browser callers.

The parser and GitHub adapter already provide the durable behavior. This module
keeps the browser's preview and apply steps on that same path while binding an
apply to the exact local plan bytes and the persisted project configuration it
was reviewed against.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .github import GitHubError, sync
from .plan import ParsedPlan, parse_plan_file
from .schemas import PlanParseResult, PlanSyncResult


class PlanSyncConflict(Exception):
    """The plan is unsafe to sync or a reviewed preview is no longer current."""

    def __init__(
        self,
        message: str,
        *,
        reason_kind: str = "plan_conflict",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_kind = reason_kind
        self.details = details or {}


class PlanSyncFailure(Exception):
    """The external backlog rejected a requested read or write."""

    reason_kind = "github_refused"


def plan_digest(path: str | Path) -> str:
    """Fingerprint the exact plan bytes, not its lossy parsed representation."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_result(parsed: ParsedPlan) -> PlanParseResult:
    """The same loss-reporting parse projection published by the JSON API."""
    report = parsed.dependency_report()
    return PlanParseResult(
        items=[
            {
                "id": item.id,
                "title": item.title,
                "body": item.body,
                "labels": item.labels,
                "milestone": item.milestone,
                "depends_on": item.depends_on,
                "done": item.done,
                "line": item.line,
            }
            for item in parsed.items
        ],
        skipped=[f"line {line}: {title}" for line, title in parsed.skipped],
        duplicate_ids=parsed.duplicate_ids(),
        unresolved_dependencies=report.unresolved,
        external_dependencies=report.external,
        decision_dependencies=report.decisions,
        cross_project_dependencies=report.cross_project,
        malformed_dependencies=report.malformed,
        dependency_cycles=[list(cycle) for cycle in report.cycles],
        unattached_arrows=[f"line {line}: {text}" for line, text in report.unattached_arrows],
    )


def _sync_result(report: Any, *, dry_run: bool) -> PlanSyncResult:
    return PlanSyncResult(
        created=list(report.created),
        updated=list(report.updated),
        unchanged=list(report.unchanged),
        orphaned=list(report.orphaned),
        labels_created=list(report.labels_created),
        milestones_created=list(report.milestones_created),
        dry_run=dry_run,
    )


def _blocking_findings(parsed: ParsedPlan) -> bool:
    report = parsed.dependency_report()
    return bool(report.unresolved or report.malformed or report.cycles or report.unattached_arrows)


def _validated_plan(path: str | Path, *, allow_duplicates: bool = False) -> ParsedPlan:
    target = Path(path)
    if not target.is_file():
        raise PlanSyncConflict(f"configured plan is missing: {target}", reason_kind="plan_missing")
    parsed = parse_plan_file(target)
    if not parsed.items:
        raise PlanSyncConflict(
            "the configured plan contains no recognized work items",
            reason_kind="no_plan_items",
        )
    duplicates = parsed.duplicate_ids()
    if duplicates and not allow_duplicates:
        raise PlanSyncConflict(
            "the plan states an id more than once; each id becomes one issue",
            reason_kind="duplicate_ids",
            details={"duplicate_ids": duplicates},
        )
    if _blocking_findings(parsed):
        report = parsed.dependency_report()
        raise PlanSyncConflict(
            "the plan has malformed, unresolved, cyclic, or unattached dependencies",
            reason_kind="dependency_findings",
            details={
                "unresolved_dependencies": report.unresolved,
                "malformed_dependencies": report.malformed,
                "dependency_cycles": [list(cycle) for cycle in report.cycles],
                "unattached_arrows": [
                    f"line {line}: {text}" for line, text in report.unattached_arrows
                ],
            },
        )
    return parsed


def execute(
    path: str | Path,
    github: Any,
    *,
    dry_run: bool,
    allow_duplicates: bool = False,
) -> PlanSyncResult:
    """Validate and sync through the one contract shared by JSON and HTML."""
    parsed = _validated_plan(path, allow_duplicates=allow_duplicates)
    try:
        report = sync(github, parsed.deduplicated(), dry_run=dry_run)
    except GitHubError as exc:
        raise PlanSyncFailure(str(exc)) from exc
    return _sync_result(report, dry_run=dry_run)


def preview(path: str | Path, github: Any) -> tuple[str, PlanParseResult, PlanSyncResult]:
    """Parse and perform a read-only remote preview; never write anything."""
    target = Path(path)
    parsed = _validated_plan(target)
    parsed_view = parse_result(parsed)
    return plan_digest(target), parsed_view, execute(target, github, dry_run=True)


def apply(
    path: str | Path,
    repo: str,
    github: Any,
    *,
    expected_digest: str,
    expected_preview: PlanSyncResult,
) -> PlanSyncResult:
    """Re-preview, compare, then perform the one explicitly confirmed write."""
    target = Path(path)
    if not target.is_file():
        raise PlanSyncConflict("the reviewed plan is no longer present", reason_kind="plan_missing")
    actual_repo = getattr(github, "repo", repo)
    if actual_repo != repo:
        raise PlanSyncConflict(
            "the resolved repository differs from the reviewed repository",
            reason_kind="repository_changed",
        )
    current_digest = plan_digest(target)
    if current_digest != expected_digest:
        raise PlanSyncConflict(
            "the plan changed after review; preview it again",
            reason_kind="plan_changed",
        )
    current_preview = execute(target, github, dry_run=True)
    if current_preview.model_dump(mode="json") != expected_preview.model_dump(mode="json"):
        raise PlanSyncConflict(
            "the remote backlog changed after review; preview it again",
            reason_kind="remote_preview_changed",
        )
    return execute(target, github, dry_run=False)
