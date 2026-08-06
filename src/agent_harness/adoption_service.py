"""Shared application boundary for adopting an already configured project.

The adoption engine owns evidence ranking and reconciliation. This module
only resolves persisted project inputs and wires optional remote inspection;
API and browser controllers use the same boundary so neither can invent a
different meaning for approval or a different source of repository paths.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adoption import (
    Adoption,
    AdoptionReport,
    ExternalCandidate,
    GitHubAdoptionInspector,
    InspectionSnapshot,
    git_branches,
)
from .events import WORK, Event
from .plan import ParsedPlan, WorkItem, parse_plan_file
from .plan_service import parse_result
from .redaction import Redact, redact_text
from .routing_service import safe_endpoint
from .schemas import AdoptionReportModel, PlanParseResult
from .work import Project, WorkQueue


class AdoptionConfigurationError(ValueError):
    """The persisted project does not supply a required adoption input."""


class AdoptionInspectionFailure(RuntimeError):
    """A configured external evidence source could not be read safely."""


class AdoptionReconciliationFailure(RuntimeError):
    """Approved reconciliation failed after it may have started mutating state."""


class _SafeGitHubAdoptionInspector:
    """Keep adapter diagnostics out of HTTP while preserving failure phase."""

    def __init__(self, delegate: GitHubAdoptionInspector) -> None:
        self.delegate = delegate

    def inspect(self, items: list[WorkItem]) -> InspectionSnapshot:
        try:
            return self.delegate.inspect(items)
        except Exception as exc:
            raise AdoptionInspectionFailure(
                "the configured remote repository could not be inspected; no proposal was saved"
            ) from exc

    def backfill_marker(self, candidate: ExternalCandidate, item_id: str) -> None:
        try:
            self.delegate.backfill_marker(candidate, item_id)
        except Exception as exc:
            raise AdoptionReconciliationFailure(
                "an approved external adoption change failed; queue or earlier remote "
                "changes may already have landed, so inspect the report and current "
                "state before retrying"
            ) from exc


@dataclass(frozen=True)
class AdoptionContext:
    """Resolved engine plus the exact persisted paths it operates on."""

    project: Project
    plan: ParsedPlan
    adopter: Adoption
    input_digest: str
    inspect_remote: bool


def resolve_adoption(
    queue: WorkQueue,
    project_id: str,
    *,
    github_factory: Callable[[str], Any] | None = None,
    inspect_remote: bool = True,
    branches: Callable[[Path], list[str]] = git_branches,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> AdoptionContext:
    """Resolve configuration without accepting paths from the HTTP caller."""
    project = queue.get_project(project_id)
    if project is None:
        raise AdoptionConfigurationError(f"project {project_id!r} is not configured")
    missing = [name for name in ("work_dir", "plan_path") if not getattr(project, name)]
    if missing:
        raise AdoptionConfigurationError(
            f"project {project_id!r} needs persisted {', '.join(missing)} before adoption"
        )
    assert project.work_dir is not None
    assert project.plan_path is not None
    external = None
    if inspect_remote and project.repo:
        if github_factory is None:
            from .github import GitHub

            github = GitHub(project.repo)
        else:
            github = github_factory(project.repo)
        external = _SafeGitHubAdoptionInspector(GitHubAdoptionInspector(github))
    try:
        plan_bytes = Path(project.plan_path).read_bytes()
        plan = parse_plan_file(project.plan_path)
    except OSError as exc:
        raise AdoptionConfigurationError(
            f"configured plan {project.plan_path!r} cannot be read: {exc}"
        ) from exc
    input_digest = hashlib.sha256(
        json.dumps(
            {
                "project_id": project.project_id,
                "work_dir": str(Path(project.work_dir).resolve()),
                "plan_path": project.plan_path,
                "configured_repo": project.repo,
                "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
                "inspect_remote": inspect_remote,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return AdoptionContext(
        project=project,
        plan=plan,
        adopter=Adoption(
            queue,
            project.work_dir,
            external=external,
            branches=branches,
            on_event=on_event,
        ),
        input_digest=input_digest,
        inspect_remote=inspect_remote,
    )


def inspect_project(context: AdoptionContext) -> AdoptionReport:
    """Persist a proposal; create no queue rows and perform no remote write."""
    return _inspect(context, persist=True, emit=True)


def fresh_report(context: AdoptionContext) -> AdoptionReport:
    """Re-inspect without persisting or emitting, for apply-time drift refusal."""
    try:
        return _inspect(context, persist=False, emit=False)
    except AdoptionInspectionFailure as exc:
        raise AdoptionInspectionFailure(
            "the configured remote repository could not be re-inspected; "
            "the existing proposal remains and nothing was applied"
        ) from exc


def _inspect(context: AdoptionContext, *, persist: bool, emit: bool) -> AdoptionReport:
    return context.adopter.inspect(
        context.project.project_id,
        context.plan,
        persist=persist,
        plan_path=context.project.plan_path or "",
        configured_repo=context.project.repo,
        input_digest=context.input_digest,
        inspect_remote=context.inspect_remote,
        emit=emit,
    )


def reconcile_project(context: AdoptionContext, *, dry_run: bool = False) -> AdoptionReport:
    """Delegate reconciliation and report that a failed apply may be partial."""
    try:
        return context.adopter.reconcile(context.project.project_id, dry_run=dry_run)
    except ValueError:
        raise
    except AdoptionReconciliationFailure:
        raise
    except Exception as exc:
        raise AdoptionReconciliationFailure(
            "adoption reconciliation failed; queue or remote changes may already have "
            "landed, so inspect the report and current state before retrying"
        ) from exc


def adoption_event_sink(sink: Any, *, source: str) -> Callable[[dict[str, Any]], None]:
    """Translate engine events into the append-only store used by this deployment."""

    def append(raw: dict[str, Any]) -> None:
        known = {"ts", "kind", "worker", "role", "model", "endpoint", "outcome"}
        sink.append(
            [
                Event(
                    ts=float(raw.get("ts", 0.0)),
                    kind=WORK,
                    source=source,
                    worker=raw.get("worker"),
                    role=raw.get("role"),
                    model=raw.get("model"),
                    endpoint=raw.get("endpoint"),
                    outcome=raw.get("outcome"),
                    data={key: value for key, value in raw.items() if key not in known},
                )
            ]
        )

    return append


def report_model(
    report: AdoptionReport,
    *,
    redact: Redact,
    parsed: ParsedPlan | None = None,
) -> AdoptionReportModel:
    """Return the public allowlisted, redacted adoption projection."""
    clean = redact_text(report.to_dict(), redact)
    if not isinstance(clean, dict):  # pragma: no cover - structure is fixed by dataclass
        raise TypeError("adoption report did not serialize to an object")
    for item in clean.get("items") or []:
        if not isinstance(item, dict):
            continue
        for candidate in item.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            # Bodies are used internally for explicit marker backfill. They
            # can contain arbitrary remote prose and are not a public field.
            candidate.pop("body", None)
            url = candidate.get("url")
            if isinstance(url, str) and url:
                candidate["url"] = safe_endpoint(url)
    parse: PlanParseResult | None = None
    if parsed is not None:
        clean_parse = redact_text(parse_result(parsed).model_dump(mode="json"), redact)
        parse = PlanParseResult.model_validate(clean_parse)
    return AdoptionReportModel.model_validate(
        {
            **clean,
            "digest": report.content_digest(),
            "proposed_drops": redact_text(report.proposed_drops(), redact),
            "unconfirmed_drops": redact_text(report.unconfirmed_drops(), redact),
            "parse": parse,
        }
    )
