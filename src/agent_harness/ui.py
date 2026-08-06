"""Server-rendered browser control plane.

This module contains HTTP presentation, browser-session concerns and narrow
action adapters. State reads come from :class:`HarnessQueries`; action routes
delegate to the same queue/fleet contracts as the JSON API and append an
operator event. No route opens SQLite or weakens a gate.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .audit import AuditStore
from .audit_service import maintain_audit, reconcile_repository
from .browser_session import BrowserSession, BrowserSessions
from .events import WORK, Event
from .inception import Inception
from .maintenance import DEFAULT_RETENTION_DAYS
from .plan_service import PlanSyncConflict, PlanSyncFailure
from .plan_service import apply as apply_plan_sync
from .plan_service import preview as preview_plan_sync
from .project_service import (
    ProjectConfigurationConflict,
    configure_project,
    project_spec,
    project_spec_digest,
)
from .query_service import HarnessQueries
from .routing_service import (
    RoleConfigurationConflict,
    configure_roles,
    role_map_digest,
    role_map_payload,
    role_map_view,
    role_map_view_for,
    safe_endpoint,
    stored_role_map,
)
from .schemas import (
    BaseCheckStatus,
    PlanSyncResult,
    PreflightCheck,
    PreflightResult,
    ProjectSpec,
    RoleMap,
    RoleMapView,
)
from .work import BLOCKED, CLAIMED, DONE, PENDING

TEMPLATE_DIR = Path(__file__).with_name("templates")
STATIC_DIR = Path(__file__).with_name("static")


def install_ui(app: FastAPI) -> BrowserSessions:
    """Mount packaged UI routes onto an existing API app."""
    sessions = BrowserSessions()
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["datetime"] = _datetime
    app.state.browser_sessions = sessions
    app.state.ui_templates = templates

    app.mount("/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")

    def queries(request: Request) -> HarnessQueries:
        return HarnessQueries(
            request.app.state.store,
            request.app.state.queue,
            audit=request.app.state.audit,
            fleet=request.app.state.fleet,
            process_metrics=request.app.state.process_metrics,
        )

    def render(
        request: Request, template: str, *, status_code: int = 200, **context: object
    ) -> HTMLResponse:
        session = sessions.get(request.cookies.get("harness_session"))
        if session is not None and not secrets.compare_digest(
            session.token_fingerprint, sessions.fingerprint(request.app.state.token or "")
        ):
            sessions.revoke(session.session_id)
            session = None
        return templates.TemplateResponse(
            request=request,
            name=template,
            status_code=status_code,
            context={
                "session": session,
                "root_path": request.scope.get("root_path", ""),
                **context,
            },
        )

    def require_session(request: Request) -> BrowserSession:
        return sessions.require(request)

    def require_audit(request: Request) -> AuditStore:
        audit: AuditStore | None = request.app.state.audit
        if audit is None:
            raise HTTPException(status_code=409, detail="no audit store is attached")
        if audit.degraded:
            raise HTTPException(
                status_code=409,
                detail="audit store is degraded; the operator action cannot be recorded",
            )
        return audit

    async def form(request: Request) -> dict[str, str]:
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        return {key: entries[0] for key, entries in values.items()}

    def action_audit(
        request: Request, *, action: str, outcome: str, data: dict[str, object]
    ) -> None:
        """Record browser intent without ever retaining browser credentials."""
        event = Event(
            ts=time.time(),
            kind=WORK,
            source="browser",
            outcome=outcome,
            data={"action": action, "operator": sessions.require(request).operator, **data},
        )
        sink = request.app.state.audit or request.app.state.store
        sink.append([event])

    def refusal_audit(
        request: Request,
        *,
        action: str,
        reason_kind: str,
        data: dict[str, object],
    ) -> None:
        action_audit(
            request,
            action=action,
            outcome="operator_action_refused",
            data={"reason_kind": reason_kind, **data},
        )

    def inception_for(request: Request) -> Inception:
        queue = request.app.state.queue
        if queue is None:
            raise HTTPException(status_code=503, detail="work queue is not configured")
        return Inception(queue, model_client=request.app.state.model_client)

    def project_redirect(request: Request, project_id: str) -> RedirectResponse:
        return RedirectResponse(
            url=str(request.url_for("plans")) + f"?project_id={project_id}", status_code=303
        )

    def preflight_model(request: Request, project_id: str, *, check_base: bool) -> PreflightResult:
        queue = request.app.state.queue
        if queue is None:
            raise HTTPException(status_code=503, detail="work queue is not configured")
        project = queue.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        from .api import _preflight

        report = _preflight(request.app.state, queue, project, check_base=check_base)
        return PreflightResult(
            project_id=report.project_id,
            ready=report.ready,
            summary=report.summary(),
            checks=[PreflightCheck(**check.as_dict()) for check in report.checks],
        )

    def configured_project(request: Request, project_id: str) -> ProjectSpec:
        queue = request.app.state.queue
        if queue is None:
            raise HTTPException(status_code=503, detail="work queue is not configured")
        project = queue.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return project_spec(project)

    def configured_project_version(request: Request, project_id: str) -> float:
        queue = request.app.state.queue
        if queue is None:
            raise HTTPException(status_code=503, detail="work queue is not configured")
        project = queue.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return float(project.updated_at)

    def github_for(request: Request, repo: str) -> Any:
        factory = getattr(request.app.state, "github_factory", None)
        if factory is not None:
            return factory(repo)
        from .github import GitHub

        return GitHub(repo)

    def plans_context(
        request: Request,
        project_id: str | None,
        *,
        sync_preview: PlanSyncResult | None = None,
        sync_error: str | None = None,
    ) -> dict[str, Any]:
        query = queries(request)
        projects = query.projects()
        selected = project_id or (
            projects.projects[0].project.project_id if projects.projects else None
        )
        proposal = query.inception(selected) if selected else None
        selected_summary = query.project(selected) if selected else None
        plan_path = selected_summary.project.plan_path if selected_summary else None
        repo = selected_summary.project.repo if selected_summary else None
        return {
            "projects": projects,
            "project_id": selected,
            "proposal": proposal,
            "plan_markdown": query.inception_plan(selected, selected) if selected else None,
            "plan_path": plan_path,
            "repo": repo,
            "parse_result": query.plan_parse(plan_path) if plan_path else None,
            "sync_preview": sync_preview,
            "sync_error": sync_error,
        }

    def optional(value: str) -> str | None:
        return value.strip() or None

    def project_spec_from_form(current: ProjectSpec, body: dict[str, str]) -> ProjectSpec:
        """Validate browser input through the public API's exact contract."""
        data = current.model_dump(mode="json")
        data.update(
            {
                "project_id": current.project_id,
                "name": body.get("name", "").strip(),
                "repo": optional(body.get("repo", "")),
                "work_dir": optional(body.get("work_dir", "")),
                "base_branch": body.get("base_branch", "").strip(),
                "durability": body.get("durability", "").strip(),
                "plan_path": optional(body.get("plan_path", "")),
                "max_workers": body.get("max_workers", ""),
                "max_attempts": body.get("max_attempts", ""),
                "max_item_seconds": body.get("max_item_seconds", ""),
                "max_item_spend_usd": body.get("max_item_spend_usd", ""),
                "max_hold_seconds": body.get("max_hold_seconds", ""),
                "min_free_disk_gb": body.get("min_free_disk_gb", ""),
            }
        )
        if body.get("replace_checks") == "yes":
            data["checks"] = [
                line.strip() for line in body.get("checks", "").splitlines() if line.strip()
            ]
        if body.get("replace_fixes") == "yes":
            data["fixes"] = json.loads(body.get("fixes", "{}") or "{}")
        if body.get("replace_roles") == "yes":
            data["roles"] = json.loads(body.get("roles", "{}") or "{}") or None
        return ProjectSpec.model_validate(data)

    def changed_project_fields(before: ProjectSpec, after: ProjectSpec) -> list[str]:
        previous = before.model_dump(mode="json")
        proposed = after.model_dump(mode="json")
        return sorted(key for key, value in proposed.items() if previous.get(key) != value)

    def validation_message(exc: ValidationError | json.JSONDecodeError) -> str:
        """Describe invalid input without reflecting submitted values or secrets."""
        if isinstance(exc, json.JSONDecodeError):
            return "Replacement checks, fixes, and routes must use valid JSON where requested."
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )

    def role_rows(view: RoleMapView) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for name, route in sorted(view.roles.items()):
            data = route.model_dump(mode="json")
            data["name"] = name
            data["endpoint"] = safe_endpoint(route.endpoint)
            rows.append(data)
        return rows

    @app.get("/", include_in_schema=False)
    def root(request: Request) -> RedirectResponse:
        target = "projects" if sessions.get(request.cookies.get("harness_session")) else "login"
        return RedirectResponse(url=request.url_for(target), status_code=303)

    @app.get(
        "/login",
        name="login",
        response_class=HTMLResponse,
        response_model=None,
        include_in_schema=False,
    )
    def login_page(request: Request) -> HTMLResponse | RedirectResponse:
        if sessions.get(request.cookies.get("harness_session")) is not None:
            return RedirectResponse(url=request.url_for("projects"), status_code=303)
        response = render(
            request,
            "login.html",
            error=(
                "No harness token is configured. Set HARNESS_TOKEN and restart the service."
                if not request.app.state.token
                else None
            ),
        )
        if not request.app.state.token:
            response.status_code = 503
        return response

    @app.post("/login", response_class=HTMLResponse, response_model=None, include_in_schema=False)
    async def login(request: Request) -> HTMLResponse | RedirectResponse:
        if not request.app.state.token:
            raise HTTPException(status_code=503, detail="no auth token configured")
        client_key = request.client.host if request.client is not None else "unknown"
        if not sessions.allow_login(client_key):
            raise HTTPException(
                status_code=429, detail="too many failed login attempts; try again later"
            )
        body = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        supplied = body.get("token", [""])[0]
        expected = request.app.state.token
        if not expected or not supplied or not secrets.compare_digest(supplied, expected):
            sessions.record_login_failure(client_key)
            return render(
                request,
                "login.html",
                error="That harness token was not accepted. Check the service URL and try again.",
            )
        session = sessions.create(
            operator="operator", token_fingerprint=sessions.fingerprint(expected)
        )
        response = RedirectResponse(url=request.url_for("projects"), status_code=303)
        response.set_cookie(
            "harness_session",
            session.session_id,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            max_age=sessions.ttl_seconds,
            path=request.scope.get("root_path", "") or "/",
        )
        return response

    @app.post("/logout", name="logout", include_in_schema=False)
    async def logout(request: Request) -> RedirectResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        sessions.revoke(session.session_id)
        response = RedirectResponse(url=request.url_for("login"), status_code=303)
        response.delete_cookie("harness_session", path=request.scope.get("root_path", "") or "/")
        return response

    @app.get("/projects", name="projects", response_class=HTMLResponse, include_in_schema=False)
    def projects_page(request: Request) -> HTMLResponse:
        require_session(request)
        return render(
            request,
            "projects.html",
            title="Projects",
            projects=queries(request).projects(),
            mode=("supervised" if request.app.state.fleet is not None else "monitoring-only"),
        )

    @app.get(
        "/projects/{project_id}/configuration",
        name="project_configuration",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def project_configuration_page(request: Request, project_id: str) -> HTMLResponse:
        require_session(request)
        return render(
            request,
            "project_configuration.html",
            title="Project configuration",
            project=configured_project(request, project_id),
            error=None,
        )

    @app.post(
        "/ui/actions/project-configuration/review",
        name="project_configuration_review",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def project_configuration_review(request: Request) -> HTMLResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id = body.get("project_id", "").strip()
        current = configured_project(request, project_id)
        try:
            proposed = project_spec_from_form(current, body)
        except (ValidationError, json.JSONDecodeError) as exc:
            return render(
                request,
                "project_configuration.html",
                title="Project configuration",
                project=current,
                error=validation_message(exc),
                status_code=422,
            )
        changed = changed_project_fields(current, proposed)
        if not changed:
            raise HTTPException(status_code=409, detail="configuration is unchanged")
        review = sessions.create_review(
            session,
            kind="project_configuration",
            target_id=project_id,
            baseline_digest=project_spec_digest(current),
            baseline_version=configured_project_version(request, project_id),
            payload=proposed.model_dump(mode="json"),
        )
        return render(
            request,
            "project_configuration_review.html",
            title="Review project configuration",
            project_id=project_id,
            changed=changed,
            proposed=proposed,
            review=review,
        )

    @app.post(
        "/ui/actions/project-configuration/apply",
        name="project_configuration_apply",
        include_in_schema=False,
    )
    async def project_configuration_apply(request: Request) -> RedirectResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id = body.get("project_id", "").strip()
        review = sessions.consume_review(
            session,
            body.get("review_id", ""),
            kind="project_configuration",
            target_id=project_id,
        )
        current = configured_project(request, project_id)
        if not secrets.compare_digest(review.baseline_digest, project_spec_digest(current)):
            raise HTTPException(
                status_code=409,
                detail=(
                    "project configuration changed after review; review the current values again"
                ),
            )
        proposed = ProjectSpec.model_validate(review.payload)
        queue = request.app.state.queue
        assert queue is not None
        changed = changed_project_fields(current, proposed)
        try:
            configure_project(
                queue,
                proposed,
                fleet=request.app.state.fleet,
                expected_updated_at=review.baseline_version,
            )
        except ProjectConfigurationConflict as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    "project configuration changed after review; review the current values again"
                ),
            ) from exc
        action_audit(
            request,
            action="project_configuration",
            outcome="operator_configured_project",
            data={"project_id": project_id, "changed_fields": changed},
        )
        return RedirectResponse(
            url=request.url_for("project_configuration", project_id=project_id), status_code=303
        )

    @app.get(
        "/projects/{project_id}/preflight",
        name="preflight",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def preflight_page(request: Request, project_id: str, check_base: bool = False) -> HTMLResponse:
        require_session(request)
        status = request.app.state.base_checks.status(project_id)
        base = (
            BaseCheckStatus(
                project_id=project_id,
                state=status.state,
                ok=status.ok,
                detail=status.detail,
                started_at=status.started_at,
                finished_at=status.finished_at,
            )
            if status is not None
            else BaseCheckStatus(project_id=project_id, state="not_run", ok=None, detail="")
        )
        return render(
            request,
            "preflight.html",
            title="Preflight",
            project_id=project_id,
            result=preflight_model(request, project_id, check_base=check_base),
            base=base,
            check_base=check_base,
        )

    @app.get("/work", name="work_page", response_class=HTMLResponse, include_in_schema=False)
    def work_page(request: Request, project_id: str | None = None) -> HTMLResponse:
        require_session(request)
        return render(
            request,
            "work.html",
            title="Work",
            work=queries(request).work(project_id),
            project_id=project_id,
        )

    @app.get(
        "/work/{item_id}",
        name="work_item_page",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def work_item_page(request: Request, item_id: str, project_id: str = "default") -> HTMLResponse:
        require_session(request)
        query = queries(request)
        item = query.item(project_id, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="work item not found")
        return render(
            request,
            "work_item.html",
            title=item.title,
            item=item,
            evidence=query.evidence(project_id, item_id),
        )

    @app.post("/ui/actions/project-control", name="project_control_action", include_in_schema=False)
    async def project_control_action(request: Request) -> RedirectResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id = body.get("project_id", "")
        state = body.get("state", "")
        reason = body.get("reason", "").strip() or None
        if state not in {"paused", "draining", "stopped"}:
            raise HTTPException(status_code=409, detail="resume requires the explicit start gate")
        queue = request.app.state.queue
        if queue is None or queue.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        fleet = request.app.state.fleet
        if state == "stopped" and fleet is not None:
            if hasattr(fleet, "request_stop"):
                fleet.request_stop(project_id, reason=reason)
            else:
                fleet.stop(project_id, reason=reason)
        else:
            queue.set_control(state, reason, project_id=project_id)
        action_audit(
            request,
            action="project_control",
            outcome="operator_control_changed",
            data={"project_id": project_id, "state": state, "reason": reason or ""},
        )
        return RedirectResponse(url=request.url_for("projects"), status_code=303)

    @app.post("/ui/actions/work/retry", name="retry_action", include_in_schema=False)
    async def retry_action(request: Request) -> RedirectResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id, item_id = body.get("project_id", "default"), body.get("item_id", "")
        queue = request.app.state.queue
        record = queue.get(item_id, project_id=project_id) if queue is not None else None
        if record is None:
            raise HTTPException(status_code=404, detail="work item not found")
        if record.state == CLAIMED and record.lease_until > time.time():
            raise HTTPException(status_code=409, detail="the item's claim is still live")
        queue.release(item_id, PENDING, error=None, project_id=project_id)
        action_audit(
            request,
            action="retry",
            outcome="operator_retry",
            data={"project_id": project_id, "item_id": item_id},
        )
        return RedirectResponse(
            url=str(request.url_for("work_page")) + f"?project_id={project_id}", status_code=303
        )

    @app.post("/ui/actions/work/block", name="block_action", include_in_schema=False)
    async def block_action(request: Request) -> RedirectResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id, item_id = body.get("project_id", "default"), body.get("item_id", "")
        reason = body.get("reason", "").strip()
        if not reason:
            raise HTTPException(status_code=422, detail="a block reason is required")
        queue = request.app.state.queue
        record = queue.get(item_id, project_id=project_id) if queue is not None else None
        if record is None:
            raise HTTPException(status_code=404, detail="work item not found")
        override = body.get("override", "") == "true"
        if record.state == CLAIMED and record.lease_until > time.time() and not override:
            raise HTTPException(status_code=409, detail="the item's claim is still live")
        if record.state == DONE and not override:
            raise HTTPException(status_code=409, detail="the item is already done")
        queue.release(item_id, BLOCKED, error=reason, project_id=project_id)
        action_audit(
            request,
            action="block",
            outcome="operator_block",
            data={"project_id": project_id, "item_id": item_id, "reason": reason},
        )
        return RedirectResponse(
            url=str(request.url_for("work_page")) + f"?project_id={project_id}", status_code=303
        )

    @app.post("/ui/actions/hold/answer", name="answer_hold_action", include_in_schema=False)
    async def answer_hold_action(request: Request) -> RedirectResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id, item_id = body.get("project_id", "default"), body.get("item_id", "")
        text = body.get("text", "")
        raw_data = body.get("data", "{}") or "{}"
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422, detail="structured answer must be valid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise HTTPException(status_code=422, detail="structured answer must be a JSON object")
        queue = request.app.state.queue
        if queue is None:
            raise HTTPException(status_code=503, detail="work queue is not configured")
        try:
            from .holds import Answer as HoldAnswer

            queue.answer_hold(
                item_id,
                body.get("resume_token", ""),
                HoldAnswer(text=text, data=data, who=session.operator),
                project_id=project_id,
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        action_audit(
            request,
            action="answer_hold",
            outcome="operator_answered_hold",
            data={
                "project_id": project_id,
                "item_id": item_id,
                "has_text": bool(text),
                "data_keys": sorted(data),
            },
        )
        return RedirectResponse(url=request.url_for("holds"), status_code=303)

    @app.post("/ui/actions/inception/start", name="inception_start_action", include_in_schema=False)
    async def inception_start_action(request: Request) -> RedirectResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id = body.get("project_id", "").strip()
        overview = body.get("overview", "").strip()
        if not project_id or not overview:
            raise HTTPException(status_code=422, detail="project id and overview are required")
        try:
            inception_for(request).start(project_id, overview)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        action_audit(
            request,
            action="inception_start",
            outcome="operator_started_inception",
            data={"project_id": project_id},
        )
        return project_redirect(request, project_id)

    @app.post("/ui/actions/preflight/base", name="base_check_action", include_in_schema=False)
    async def base_check_action(request: Request) -> RedirectResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id = body.get("project_id", "").strip()
        queue = request.app.state.queue
        if queue is None:
            raise HTTPException(status_code=503, detail="work queue is not configured")
        project = queue.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        run = request.app.state.base_checks.start(project)
        action_audit(
            request,
            action="base_checks",
            outcome="operator_started_or_joined_base_checks",
            data={"project_id": project_id, "state": run.state},
        )
        return RedirectResponse(
            url=str(request.url_for("preflight", project_id=project_id)) + "?check_base=true",
            status_code=303,
        )

    @app.post(
        "/ui/actions/work/dependency-override",
        name="dependency_override_action",
        include_in_schema=False,
    )
    async def dependency_override_action(request: Request) -> RedirectResponse:
        """Record the same revision-scoped admission decision as the JSON API.

        The browser supplies no free-text identity: the authenticated session is
        the operator recorded in the graph. The reason remains mandatory, and
        the graph keeps its real blocked edge state after the override.
        """
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id = body.get("project_id", "").strip()
        item_id = body.get("item_id", "").strip()
        reason = body.get("reason", "").strip()
        if not project_id or not item_id:
            raise HTTPException(status_code=422, detail="project and item are required")
        if not reason:
            raise HTTPException(status_code=422, detail="an override reason is required")
        queue = request.app.state.queue
        if queue is None:
            raise HTTPException(status_code=503, detail="work queue is not configured")
        if queue.get(item_id, project_id=project_id) is None:
            raise HTTPException(status_code=404, detail="work item not found")
        revision = queue.graph.record_override(
            project_id, item_id, reason=reason, who=session.operator
        )
        action_audit(
            request,
            action="dependency_override",
            outcome="operator_overrode_dependency_gate",
            data={
                "project_id": project_id,
                "item_id": item_id,
                "revision": revision,
                "reason": reason,
            },
        )
        return RedirectResponse(
            url=str(request.url_for("graph")) + f"?project_id={project_id}", status_code=303
        )

    @app.post("/ui/actions/inception/scope", name="inception_scope_action", include_in_schema=False)
    async def inception_scope_action(request: Request) -> RedirectResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id = body.get("project_id", "").strip()
        if not project_id:
            raise HTTPException(status_code=422, detail="project id is required")
        try:
            inception_for(request).scope(project_id, body.get("feedback", "").strip() or None)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        action_audit(
            request,
            action="inception_scope",
            outcome="operator_requested_scope",
            data={"project_id": project_id},
        )
        return project_redirect(request, project_id)

    @app.post(
        "/ui/actions/inception/question",
        name="inception_question_action",
        include_in_schema=False,
    )
    async def inception_question_action(request: Request) -> RedirectResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id = body.get("project_id", "").strip()
        question_id = body.get("question_id", "").strip()
        answer = body.get("answer", "").strip() or None
        defer_reason = body.get("defer_reason", "").strip() or None
        severity = body.get("severity", "").strip() or None
        if not project_id or not question_id or not (answer or defer_reason or severity):
            raise HTTPException(
                status_code=422,
                detail="project, question, and an answer, deferral, or severity are required",
            )
        try:
            inception_for(request).resolve(
                project_id,
                question_id,
                answer=answer,
                defer_reason=defer_reason,
                severity=severity,
                who=session.operator,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        action_audit(
            request,
            action="inception_question",
            outcome="operator_resolved_question",
            data={"project_id": project_id, "question_id": question_id},
        )
        return project_redirect(request, project_id)

    @app.post(
        "/ui/actions/inception/approve",
        name="inception_approve_action",
        include_in_schema=False,
    )
    async def inception_approve_action(request: Request) -> RedirectResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id = body.get("project_id", "").strip()
        if not project_id:
            raise HTTPException(status_code=422, detail="project id is required")
        try:
            inception_for(request).approve(project_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        action_audit(
            request,
            action="inception_approve",
            outcome="operator_approved_scope",
            data={"project_id": project_id},
        )
        return project_redirect(request, project_id)

    @app.get("/holds", name="holds", response_class=HTMLResponse, include_in_schema=False)
    def holds_page(request: Request, project_id: str | None = None) -> HTMLResponse:
        require_session(request)
        hold_list = queries(request).holds(project_id)
        queue = request.app.state.queue
        resume_tokens = {
            (hold.project_id, hold.item_id): hold.resume_token
            for hold in (queue.holds.open_holds(project_id) if queue is not None else [])
        }
        return render(
            request,
            "holds.html",
            title="Holds",
            holds=hold_list,
            resume_tokens=resume_tokens,
        )

    @app.get("/events", name="events", response_class=HTMLResponse, include_in_schema=False)
    def events_page(
        request: Request,
        since_id: int = 0,
        limit: int = 100,
        project_id: str | None = None,
        item_id: str | None = None,
        worker: str | None = None,
        endpoint: str | None = None,
        role: str | None = None,
        model: str | None = None,
        outcome: str | None = None,
        error_class: str | None = None,
        reason_kind: str | None = None,
        start_ts: str | None = None,
        end_ts: str | None = None,
    ) -> HTMLResponse:
        require_session(request)
        from .schemas import EventFilters

        try:
            filters = EventFilters(
                project_id=project_id,
                item_id=item_id,
                worker=worker,
                endpoint=endpoint,
                role=role,
                model=model,
                outcome=outcome,
                error_class=error_class,
                reason_kind=reason_kind,
                start_ts=start_ts,
                end_ts=end_ts,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        event_page = queries(request).filtered_events(
            since_id=since_id, limit=min(max(limit, 1), 1000), filters=filters, live=True
        )
        stream_params = {
            key: value for key, value in filters.model_dump().items() if value is not None
        }
        stream_params["since_id"] = event_page.cursor
        return render(
            request,
            "events.html",
            title="Events",
            events=event_page,
            filters=filters,
            stream_url=str(request.url_for("event_stream")) + "?" + urlencode(stream_params),
        )

    @app.get("/workers", name="workers", response_class=HTMLResponse, include_in_schema=False)
    def workers_page(request: Request, project_id: str | None = None) -> HTMLResponse:
        require_session(request)
        return render(
            request,
            "workers.html",
            title="Workers",
            project_id=project_id,
            inventory=queries(request).worker_inventory(project_id),
        )

    @app.get("/analytics", name="analytics", response_class=HTMLResponse, include_in_schema=False)
    def analytics_page(
        request: Request,
        window: str = "7d",
        project_id: str | None = None,
    ) -> HTMLResponse:
        require_session(request)
        try:
            dashboard = queries(request).analytics(window=window, project_id=project_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return render(
            request,
            "analytics.html",
            title="Analytics",
            dashboard=dashboard,
            process=queries(request).process_metrics(),
            gateway_logs=queries(request).gateway_logs(limit=50, project_id=project_id),
            projects=queries(request).projects(),
            retention_days=DEFAULT_RETENTION_DAYS,
        )

    @app.post(
        "/ui/actions/audit-reconcile/review",
        name="audit_reconcile_review",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def audit_reconcile_review(request: Request) -> HTMLResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id = body.get("project_id", "").strip()
        reason = body.get("reason", "").strip()
        if not project_id:
            raise HTTPException(status_code=422, detail="a project is required")
        if not reason:
            raise HTTPException(status_code=422, detail="a reconciliation reason is required")
        require_audit(request)
        queue = request.app.state.queue
        if queue is None:
            raise HTTPException(status_code=503, detail="work queue is not configured")
        project = queue.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        if not project.repo:
            raise HTTPException(status_code=409, detail="project has no GitHub repository")
        review = sessions.create_review(
            session,
            kind="audit_reconcile",
            target_id=project_id,
            baseline_digest=project_spec_digest(project_spec(project)),
            baseline_version=float(project.updated_at),
            payload={"repo": project.repo, "reason": reason},
        )
        return render(
            request,
            "audit_action_review.html",
            title="Review GitHub reconciliation",
            action_kind="reconcile",
            review=review,
            project_id=project_id,
            repo=project.repo,
            reason=reason,
            retention_days=None,
        )

    @app.post(
        "/ui/actions/audit-reconcile/apply",
        name="audit_reconcile_apply",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def audit_reconcile_apply(request: Request) -> HTMLResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id = body.get("project_id", "").strip()
        try:
            review = sessions.consume_review(
                session,
                body.get("review_id", ""),
                kind="audit_reconcile",
                target_id=project_id,
            )
        except HTTPException:
            refusal_audit(
                request,
                action="audit_reconcile",
                reason_kind="invalid_or_expired_review",
                data={"project_id": project_id},
            )
            raise
        queue = request.app.state.queue
        if queue is None:
            raise HTTPException(status_code=503, detail="work queue is not configured")
        project = queue.get_project(project_id)
        repo = review.payload.get("repo")
        reason = review.payload.get("reason")
        if project is None:
            refusal_audit(
                request,
                action="audit_reconcile",
                reason_kind="project_missing",
                data={"project_id": project_id},
            )
            raise HTTPException(status_code=409, detail="project was removed after review")
        if not isinstance(repo, str) or not isinstance(reason, str) or not reason:
            refusal_audit(
                request,
                action="audit_reconcile",
                reason_kind="invalid_review",
                data={"project_id": project_id},
            )
            raise HTTPException(status_code=409, detail="reconciliation review is invalid")
        if (
            project.updated_at != review.baseline_version
            or project.repo != repo
            or not secrets.compare_digest(
                review.baseline_digest, project_spec_digest(project_spec(project))
            )
        ):
            refusal_audit(
                request,
                action="audit_reconcile",
                reason_kind="project_configuration_changed",
                data={"project_id": project_id, "repo": repo},
            )
            raise HTTPException(
                status_code=409,
                detail="project configuration changed after reconciliation review",
            )
        result = reconcile_repository(queue, require_audit(request), repo, project_id=project_id)
        action_audit(
            request,
            action="audit_reconcile",
            outcome="operator_reconciled_github",
            data={
                "project_id": project_id,
                "repo": repo,
                "reason": reason,
                "merged": result.merged,
                "closed_unmerged": result.closed_unmerged,
                "reverted": result.reverted,
                "skipped": result.skipped,
                "errors": result.errors,
            },
        )
        return render(
            request,
            "audit_action_result.html",
            title="GitHub reconciliation result",
            action_kind="reconcile",
            repo=repo,
            reason=reason,
            result=result,
        )

    @app.post(
        "/ui/actions/audit-maintenance/review",
        name="audit_maintenance_review",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def audit_maintenance_review(request: Request) -> HTMLResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        reason = body.get("reason", "").strip()
        if not reason:
            raise HTTPException(status_code=422, detail="a maintenance reason is required")
        require_audit(request)
        try:
            retention_days = int(body.get("retention_days", ""))
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="retention days must be an integer"
            ) from exc
        if retention_days < 0:
            raise HTTPException(status_code=422, detail="retention days must not be negative")
        review = sessions.create_review(
            session,
            kind="audit_maintenance",
            target_id="audit",
            baseline_digest=str(retention_days),
            baseline_version=0,
            payload={"retention_days": retention_days, "reason": reason},
        )
        return render(
            request,
            "audit_action_review.html",
            title="Review audit maintenance",
            action_kind="maintenance",
            review=review,
            project_id=None,
            repo=None,
            reason=reason,
            retention_days=retention_days,
        )

    @app.post(
        "/ui/actions/audit-maintenance/apply",
        name="audit_maintenance_apply",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def audit_maintenance_apply(request: Request) -> HTMLResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        try:
            review = sessions.consume_review(
                session,
                body.get("review_id", ""),
                kind="audit_maintenance",
                target_id="audit",
            )
        except HTTPException:
            refusal_audit(
                request,
                action="audit_maintenance",
                reason_kind="invalid_or_expired_review",
                data={},
            )
            raise
        retention_days = review.payload.get("retention_days")
        reason = review.payload.get("reason")
        if (
            not isinstance(retention_days, int)
            or retention_days < 0
            or not isinstance(reason, str)
            or not reason
        ):
            refusal_audit(
                request,
                action="audit_maintenance",
                reason_kind="invalid_review",
                data={},
            )
            raise HTTPException(status_code=409, detail="maintenance review is invalid")
        result = maintain_audit(require_audit(request), retention_days)
        action_audit(
            request,
            action="audit_maintenance",
            outcome="operator_ran_audit_maintenance",
            data={
                "reason": reason,
                "retention_days": retention_days,
                "rolled_up": result.rolled_up,
                "thinned": result.thinned,
                "errors": result.errors,
            },
        )
        return render(
            request,
            "audit_action_result.html",
            title="Audit maintenance result",
            action_kind="maintenance",
            repo=None,
            reason=reason,
            result=result,
        )

    @app.get("/plans", name="plans", response_class=HTMLResponse, include_in_schema=False)
    def plans_page(request: Request, project_id: str | None = None) -> HTMLResponse:
        require_session(request)
        return render(
            request,
            "plans.html",
            title="Plans",
            **plans_context(request, project_id),
        )

    @app.post(
        "/ui/actions/plan-sync/review",
        name="plan_sync_review",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def plan_sync_review(request: Request) -> HTMLResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id = body.get("project_id", "").strip()
        queue = request.app.state.queue
        if queue is None:
            raise HTTPException(status_code=503, detail="work queue is not configured")
        project = queue.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        if not project.repo or not project.plan_path:
            raise HTTPException(
                status_code=422,
                detail="configure both a repository and a plan path before syncing",
            )
        try:
            digest, parsed, preview = preview_plan_sync(
                project.plan_path, github_for(request, project.repo)
            )
        except PlanSyncConflict as exc:
            return render(
                request,
                "plans.html",
                title="Plans",
                status_code=409,
                **plans_context(request, project_id, sync_error=str(exc)),
            )
        except PlanSyncFailure as exc:
            return render(
                request,
                "plans.html",
                title="Plans",
                status_code=502,
                **plans_context(
                    request, project_id, sync_error=f"GitHub refused the preview: {exc}"
                ),
            )
        review = sessions.create_review(
            session,
            kind="plan_sync",
            target_id=project_id,
            baseline_digest=digest,
            baseline_version=float(project.updated_at),
            payload={
                "repo": project.repo,
                "plan_path": project.plan_path,
                "parsed": parsed.model_dump(mode="json"),
                "preview": preview.model_dump(mode="json"),
            },
        )
        return render(
            request,
            "plan_sync_review.html",
            title="Review plan sync",
            project_id=project_id,
            plan_path=project.plan_path,
            repo=project.repo,
            parsed=parsed,
            preview=preview,
            review=review,
        )

    @app.post(
        "/ui/actions/plan-sync/apply",
        name="plan_sync_apply",
        include_in_schema=False,
    )
    async def plan_sync_apply(request: Request) -> RedirectResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        project_id = body.get("project_id", "").strip()
        review = sessions.consume_review(
            session,
            body.get("review_id", ""),
            kind="plan_sync",
            target_id=project_id,
        )
        queue = request.app.state.queue
        if queue is None:
            raise HTTPException(status_code=503, detail="work queue is not configured")
        project = queue.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        payload = review.payload
        repo = payload.get("repo")
        plan_path = payload.get("plan_path")
        preview_payload = payload.get("preview")
        if (
            not isinstance(repo, str)
            or not isinstance(plan_path, str)
            or not isinstance(preview_payload, dict)
        ):
            refusal_audit(
                request,
                action="plan_sync",
                reason_kind="invalid_review",
                data={"project_id": project_id},
            )
            raise HTTPException(status_code=409, detail="plan review payload is invalid")
        if (
            project.updated_at != review.baseline_version
            or project.repo != repo
            or project.plan_path != plan_path
        ):
            refusal_audit(
                request,
                action="plan_sync",
                reason_kind="project_configuration_changed",
                data={"project_id": project_id},
            )
            raise HTTPException(status_code=409, detail="project changed after plan review")
        expected_preview = PlanSyncResult.model_validate(preview_payload)
        try:
            result = apply_plan_sync(
                plan_path,
                repo,
                github_for(request, repo),
                expected_digest=review.baseline_digest,
                expected_preview=expected_preview,
            )
        except PlanSyncConflict as exc:
            refusal_audit(
                request,
                action="plan_sync",
                reason_kind=exc.reason_kind,
                data={"project_id": project_id, "repo": repo, "plan_path": plan_path},
            )
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PlanSyncFailure as exc:
            refusal_audit(
                request,
                action="plan_sync",
                reason_kind=exc.reason_kind,
                data={"project_id": project_id, "repo": repo, "plan_path": plan_path},
            )
            raise HTTPException(status_code=502, detail=f"GitHub refused the sync: {exc}") from exc
        action_audit(
            request,
            action="plan_sync",
            outcome="operator_synced_plan",
            data={
                "project_id": project_id,
                "repo": repo,
                "plan_path": plan_path,
                "created": result.created,
                "updated": result.updated,
                "orphaned": result.orphaned,
            },
        )
        return RedirectResponse(
            url=str(request.url_for("plans")) + f"?project_id={project_id}", status_code=303
        )

    @app.get("/graph", name="graph", response_class=HTMLResponse, include_in_schema=False)
    def graph_page(request: Request, project_id: str | None = None) -> HTMLResponse:
        require_session(request)
        query = queries(request)
        projects = query.projects()
        selected = project_id or (
            projects.projects[0].project.project_id if projects.projects else None
        )
        return render(
            request,
            "graph.html",
            title="Dependency graph",
            projects=projects,
            project_id=selected,
            graph=query.graph(selected) if selected else None,
            overrides=(query.overrides(selected) if selected else []),
        )

    @app.get("/sessions", name="sessions", response_class=HTMLResponse, include_in_schema=False)
    def sessions_page(request: Request) -> HTMLResponse:
        require_session(request)
        return render(
            request,
            "placeholder.html",
            title="Sessions",
            message="Agent-harness-owned sessions are scheduled for Milestone 5.",
        )

    @app.get("/settings", name="settings", response_class=HTMLResponse, include_in_schema=False)
    def settings_page(request: Request) -> HTMLResponse:
        require_session(request)
        readiness = None
        queue = request.app.state.queue
        configured = queue is not None
        routes = role_map_view(request.app.state, queue) if queue is not None else None
        return render(
            request,
            "settings.html",
            title="Settings",
            readiness=readiness,
            mode=("supervised" if request.app.state.fleet is not None else "monitoring-only"),
            queue_configured=configured,
            routes=routes,
            role_rows=(role_rows(routes) if routes is not None else []),
        )

    @app.post(
        "/ui/actions/roles/review",
        name="roles_review",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def roles_review(request: Request) -> HTMLResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        queue = request.app.state.queue
        if queue is None:
            raise HTTPException(status_code=503, detail="work queue is not configured")
        try:
            submitted = json.loads(body.get("roles", "{}") or "{}")
            proposed = RoleMap.model_validate({"roles": submitted})
        except (ValidationError, json.JSONDecodeError) as exc:
            return render(
                request,
                "settings.html",
                title="Settings",
                status_code=422,
                readiness=None,
                mode=("supervised" if request.app.state.fleet is not None else "monitoring-only"),
                queue_configured=True,
                routes=role_map_view(request.app.state, queue),
                role_rows=role_rows(role_map_view(request.app.state, queue)),
                route_error=validation_message(exc),
            )
        current = stored_role_map(queue)
        payload = role_map_payload(proposed)
        review = sessions.create_review(
            session,
            kind="role_map",
            target_id="global",
            baseline_digest=role_map_digest(current),
            baseline_version=0,
            payload={"expected": current, "proposed": payload},
        )
        proposed_view = role_map_view_for(request.app.state, payload)
        return render(
            request,
            "roles_review.html",
            title="Review role routing",
            review=review,
            routes=proposed_view,
            role_rows=role_rows(proposed_view),
        )

    @app.post(
        "/ui/actions/roles/apply",
        name="roles_apply",
        include_in_schema=False,
    )
    async def roles_apply(request: Request) -> RedirectResponse:
        session = require_session(request)
        body = await form(request)
        sessions.require_csrf(request, session, body.get("csrf_token"))
        review = sessions.consume_review(
            session,
            body.get("review_id", ""),
            kind="role_map",
            target_id="global",
        )
        queue = request.app.state.queue
        if queue is None:
            raise HTTPException(status_code=503, detail="work queue is not configured")
        expected = review.payload.get("expected")
        proposed_payload = review.payload.get("proposed")
        if (expected is not None and not isinstance(expected, dict)) or not isinstance(
            proposed_payload, dict
        ):
            refusal_audit(
                request,
                action="role_configuration",
                reason_kind="invalid_review",
                data={"scope": "global"},
            )
            raise HTTPException(status_code=409, detail="role review payload is invalid")
        proposed = RoleMap.model_validate({"roles": proposed_payload})
        try:
            configure_roles(queue, proposed, expected=expected)
        except RoleConfigurationConflict as exc:
            refusal_audit(
                request,
                action="role_configuration",
                reason_kind="role_map_changed",
                data={"scope": "global"},
            )
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        action_audit(
            request,
            action="role_configuration",
            outcome="operator_configured_roles",
            data={"scope": "global", "roles": sorted(proposed.roles)},
        )
        return RedirectResponse(url=request.url_for("settings"), status_code=303)

    @app.get("/ui/fragments/projects", response_class=HTMLResponse, include_in_schema=False)
    def projects_fragment(request: Request) -> HTMLResponse:
        require_session(request)
        return render(
            request,
            "fragments/project_cards.html",
            projects=queries(request).projects(),
            mode=("supervised" if request.app.state.fleet is not None else "monitoring-only"),
        )

    @app.get("/ui/fragments/work", response_class=HTMLResponse, include_in_schema=False)
    def work_fragment(request: Request, project_id: str | None = None) -> HTMLResponse:
        require_session(request)
        return render(request, "fragments/work_rows.html", work=queries(request).work(project_id))

    @app.get("/api/events/stream", name="event_stream", include_in_schema=False)
    async def event_stream(request: Request) -> StreamingResponse:
        if sessions.get(request.cookies.get("harness_session")) is None:
            credentials = request.headers.get("Authorization", "")
            expected = request.app.state.token or ""
            if not credentials.startswith("Bearer ") or not secrets.compare_digest(
                credentials[7:], expected
            ):
                raise HTTPException(
                    status_code=401,
                    detail="browser session or bearer token required",
                )
        try:
            last_id = int(
                request.headers.get("Last-Event-ID", request.query_params.get("since_id", "0"))
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="event cursor must be an integer") from exc
        from .schemas import EventFilters

        try:
            filters = EventFilters(
                **{
                    key: request.query_params.get(key)
                    for key in (
                        "project_id",
                        "item_id",
                        "worker",
                        "endpoint",
                        "role",
                        "model",
                        "outcome",
                        "error_class",
                        "reason_kind",
                        "start_ts",
                        "end_ts",
                    )
                    if request.query_params.get(key) is not None
                }
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        async def stream() -> AsyncIterator[str]:
            nonlocal last_id
            while True:
                if await request.is_disconnected():
                    return
                page = queries(request).filtered_events(
                    last_id, limit=200, filters=filters, live=True
                )
                last_id = page.cursor
                if page.events:
                    for event in page.events:
                        yield f"id: {event.id}\nevent: harness\ndata: {event.model_dump_json()}\n\n"
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return sessions


def _datetime(value: float | None) -> str:
    if not value:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(value))
