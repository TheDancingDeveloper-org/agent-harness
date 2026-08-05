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
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .browser_session import BrowserSession, BrowserSessions
from .events import WORK, Event
from .inception import Inception
from .query_service import HarnessQueries
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
        )

    def render(request: Request, template: str, **context: object) -> HTMLResponse:
        session = sessions.get(request.cookies.get("harness_session"))
        if session is not None and not secrets.compare_digest(
            session.token_fingerprint, sessions.fingerprint(request.app.state.token or "")
        ):
            sessions.revoke(session.session_id)
            session = None
        return templates.TemplateResponse(
            request=request,
            name=template,
            context={
                "session": session,
                "root_path": request.scope.get("root_path", ""),
                **context,
            },
        )

    def require_session(request: Request) -> BrowserSession:
        return sessions.require(request)

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

    def inception_for(request: Request) -> Inception:
        queue = request.app.state.queue
        if queue is None:
            raise HTTPException(status_code=503, detail="work queue is not configured")
        return Inception(queue, model_client=request.app.state.model_client)

    def project_redirect(request: Request, project_id: str) -> RedirectResponse:
        return RedirectResponse(
            url=str(request.url_for("plans")) + f"?project_id={project_id}", status_code=303
        )

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
    def events_page(request: Request) -> HTMLResponse:
        require_session(request)
        return render(
            request,
            "events.html",
            title="Events",
            events=queries(request).live_events(),
        )

    @app.get("/analytics", name="analytics", response_class=HTMLResponse, include_in_schema=False)
    def analytics_page(request: Request) -> HTMLResponse:
        require_session(request)
        audit = request.app.state.audit
        return render(
            request,
            "analytics.html",
            title="Analytics",
            rate_limits=(audit.rate_limits_by_class() if audit is not None else {}),
            costs=(audit.cost() if audit is not None else []),
            delivery=(audit.delivery() if audit is not None else []),
            audit_health=(
                {"configured": True, "degraded": audit.degraded, "events": audit.count()}
                if audit is not None
                else {"configured": False, "degraded": True, "events": 0}
            ),
        )

    @app.get("/plans", name="plans", response_class=HTMLResponse, include_in_schema=False)
    def plans_page(request: Request, project_id: str | None = None) -> HTMLResponse:
        require_session(request)
        query = queries(request)
        projects = query.projects()
        selected = project_id or (
            projects.projects[0].project.project_id if projects.projects else None
        )
        proposal = query.inception(selected) if selected else None
        selected_summary = query.project(selected) if selected else None
        plan_path = selected_summary.project.plan_path if selected_summary else None
        return render(
            request,
            "plans.html",
            title="Plans",
            projects=projects,
            project_id=selected,
            proposal=proposal,
            plan_markdown=query.inception_plan(selected, selected) if selected else None,
            plan_path=plan_path,
            parse_result=query.plan_parse(plan_path) if plan_path else None,
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
        return render(
            request,
            "settings.html",
            title="Settings",
            readiness=readiness,
            mode=("supervised" if request.app.state.fleet is not None else "monitoring-only"),
            queue_configured=configured,
        )

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

        async def stream() -> AsyncIterator[str]:
            nonlocal last_id
            while True:
                if await request.is_disconnected():
                    return
                page = queries(request).live_events(last_id, limit=200)
                if page.events:
                    for event in page.events:
                        last_id = event.id
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
