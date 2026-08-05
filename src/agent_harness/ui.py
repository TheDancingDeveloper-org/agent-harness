"""Server-rendered browser control plane.

This module contains only HTTP presentation and browser-session concerns. All
state reads come from :class:`HarnessQueries`; no route opens SQLite or applies
a queue transition. Mutating controls are deliberately introduced by a later
milestone after their shared command/audit services are available.
"""

from __future__ import annotations

import asyncio
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
from .query_service import HarnessQueries

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
        session = sessions.create(operator="operator")
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
        body = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        sessions.require_csrf(request, session, body.get("csrf_token", [""])[0])
        sessions.revoke(session.session_id)
        response = RedirectResponse(url=request.url_for("login"), status_code=303)
        response.delete_cookie("harness_session", path=request.scope.get("root_path", "") or "/")
        return response

    @app.get("/projects", name="projects", response_class=HTMLResponse, include_in_schema=False)
    def projects_page(request: Request) -> HTMLResponse:
        require_session(request)
        return render(
            request, "projects.html", title="Projects", projects=queries(request).projects()
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

    @app.get("/holds", name="holds", response_class=HTMLResponse, include_in_schema=False)
    def holds_page(request: Request, project_id: str | None = None) -> HTMLResponse:
        require_session(request)
        return render(
            request, "holds.html", title="Holds", holds=queries(request).holds(project_id)
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
    def plans_page(request: Request) -> HTMLResponse:
        require_session(request)
        return render(
            request,
            "placeholder.html",
            title="Plans",
            message=(
                "Plan review is available through the typed API while its review wizard "
                "is being delivered."
            ),
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
        return render(request, "fragments/project_cards.html", projects=queries(request).projects())

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
