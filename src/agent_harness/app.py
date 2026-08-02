"""The read-only dashboard (P2).

Zero changes to the harness, and no writes to anything the harness reads.
If this service crashes, the fleet does not notice -- that property is what
makes it safe to run against live traffic, and nothing here may erode it.

Every route is a projection over the events table (plan §3.5). No route
writes to `events`; the only writer is the ingester.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import os
import secrets
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from .events import (
    RATE_LIMIT_CLASSES,
    RATE_LIMIT_MEANING,
    UNCLASSIFIED,
    is_known_class,
)
from .store import EventStore


@dataclasses.dataclass(frozen=True)
class Baseline:
    """A prior measurement to compare the current window against.

    Optional, and supplied by whoever is running the harness — there is no
    built-in number, because a baseline belongs to a workload and this
    service is not tied to one.

    `classified` is the field that matters. A baseline taken before error
    classification existed is a **total** with no per-class breakdown, and
    none can be recovered by re-parsing. Comparing it class-by-class against
    a classified window would fabricate a delta. When this is False, every
    view says so instead of quietly implying like-for-like.
    """

    total: int
    days: float
    window: str
    classified: bool = False
    label: str = "baseline"

    @property
    def per_day(self) -> float:
        return self.total / self.days if self.days else 0.0


TEMPLATES = Path(__file__).parent / "templates"

WINDOWS = {"1h": 3600, "24h": 86400, "72h": 3 * 86400, "7d": 7 * 86400, "all": None}


def _when(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _ago(ts: float | None, now: float | None = None) -> str:
    """Relative age, coarsened as it grows: seconds matter for a live fleet,
    days do not need a minute count."""
    if not ts:
        return "—"
    seconds = max(0.0, (now if now is not None else time.time()) - ts)
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h ago"
    return f"{seconds / 86400:.0f}d ago"


def create_app(
    store: EventStore,
    token: str | None = None,
    baseline: Baseline | None = None,
    stream_max_seconds: float = 300.0,
    stream_poll_seconds: float = 1.0,
) -> FastAPI:
    """Build the app.

    stream_max_seconds bounds how long one SSE connection is held before
    the server closes it and the browser reconnects. A stream held open
    forever is a stream that cannot be drained on shutdown and that pins a
    thread per idle tab; because reconnect carries Last-Event-ID, recycling
    it is gapless. Tests set it small so they terminate.
    """
    app = FastAPI(title="agent-harness", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES))
    # A dashboard for diagnosing failures renders times humans can read.
    templates.env.filters["when"] = _when
    templates.env.filters["ago"] = _ago
    app.state.store = store
    app.state.token = token
    app.state.baseline = baseline
    app.state.stream_max_seconds = stream_max_seconds
    app.state.stream_poll_seconds = stream_poll_seconds

    def require_token(request: Request) -> None:
        """Bearer auth on everything except /health.

        A missing configured token means the service refuses every request
        rather than serving openly -- failing closed is the only safe
        default for something reachable over a network, even a private one.
        """
        expected = request.app.state.token
        if not expected:
            raise HTTPException(status_code=503, detail="no auth token configured")
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        if not supplied:
            supplied = request.query_params.get("token", "")
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    def window_seconds(window: str) -> float | None:
        if window not in WINDOWS:
            raise HTTPException(status_code=400, detail=f"unknown window {window!r}")
        span = WINDOWS[window]
        return None if span is None else time.time() - span

    # ------------------------------------------------------------- health

    @app.get("/health")
    def health() -> JSONResponse:
        """Deliberately unauthenticated and deliberately cheap: it is what a
        deploy checks, and it must not depend on the store being populated."""
        store: EventStore = app.state.store
        try:
            count = store.count()
            oldest, newest = store.span()
            ok = True
        except Exception as exc:  # pragma: no cover - defensive
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return JSONResponse(
            {
                "ok": ok,
                "events": count,
                "oldest": oldest,
                "newest": newest,
                "authenticated": bool(app.state.token),
            }
        )

    # ------------------------------------------------------------- panels

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request, window: str = Query("24h"), _: None = Depends(require_token)
    ) -> HTMLResponse:
        """The landing page IS the errors panel.

        Rate-limit classification is the thing a fleet operator most often
        cannot see and most needs to, so it is what you get without asking
        for it."""
        return templates.TemplateResponse(
            request,
            "panels/errors.html",
            {"window": window, "windows": list(WINDOWS), **_errors_context(window)},
        )

    def _errors_context(window: str) -> dict[str, Any]:
        """The highest-value panel (T23): 429s by class.

        Reports the historical `unclassified` bucket separately and never
        folds it into a class, because doing so would fabricate exactly the
        number this panel exists to establish.
        """
        store: EventStore = app.state.store
        since = window_seconds(window)
        by_class = store.rate_limits_by_class(since)
        classified = {c: by_class.get(c, 0) for c in RATE_LIMIT_CLASSES}
        unclassified = by_class.get(UNCLASSIFIED, 0)
        other = {
            k: v
            for k, v in by_class.items()
            if k not in RATE_LIMIT_CLASSES and k != UNCLASSIFIED and is_known_class(k)
        }
        unknown = {k: v for k, v in by_class.items() if not is_known_class(k)}
        total_classified = sum(classified.values())

        span = store.span()
        days = 0.0
        if span[0] is not None and span[1] is not None and span[1] > span[0]:
            days = (span[1] - span[0]) / 86400
        return {
            "classified": classified,
            "class_meaning": RATE_LIMIT_MEANING,
            "unclassified": unclassified,
            "other_errors": other,
            "unknown_classes": unknown,
            "total_classified": total_classified,
            "per_day": (total_classified / days) if days >= 1 else None,
            "baseline": app.state.baseline,
            "by_worker": store.group_counts("worker", since),
            "by_endpoint": store.group_counts("endpoint", since),
            "by_role": store.group_counts("role", since),
            "over_time": store.rate_limits_over_time(since),
        }

    @app.get("/panel/errors", response_class=HTMLResponse)
    def panel_errors(
        request: Request, window: str = Query("24h"), _: None = Depends(require_token)
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "panels/errors.html",
            {"window": window, "windows": list(WINDOWS), **_errors_context(window)},
        )

    @app.get("/panel/fleet", response_class=HTMLResponse)
    def panel_fleet(
        request: Request, window: str = Query("24h"), _: None = Depends(require_token)
    ) -> HTMLResponse:
        store: EventStore = app.state.store
        since = window_seconds(window)
        return templates.TemplateResponse(
            request,
            "panels/fleet.html",
            {
                "window": window,
                "windows": list(WINDOWS),
                "workers": store.workers(since),
                "now": time.time(),
            },
        )

    @app.get("/panel/pipeline", response_class=HTMLResponse)
    def panel_pipeline(
        request: Request, window: str = Query("24h"), _: None = Depends(require_token)
    ) -> HTMLResponse:
        store: EventStore = app.state.store
        since = window_seconds(window)
        return templates.TemplateResponse(
            request,
            "panels/pipeline.html",
            {
                "window": window,
                "windows": list(WINDOWS),
                "applies": store.outcome_counts("patch_apply", since),
                "calls": store.outcome_counts("model_call", since),
                "recent": store.recent("patch_apply", limit=25, since=since),
            },
        )

    @app.get("/panel/quota", response_class=HTMLResponse)
    def panel_quota(
        request: Request, window: str = Query("24h"), _: None = Depends(require_token)
    ) -> HTMLResponse:
        """Quota & spend. The live `/quota` poll is not wired yet (it needs
        the gateway key, which this service does not hold), so this reports
        what the events *can* show -- per-role call volume and latency --
        and says plainly that spend is not among them rather than rendering
        an empty box that reads as zero."""
        store: EventStore = app.state.store
        since = window_seconds(window)
        return templates.TemplateResponse(
            request,
            "panels/quota.html",
            {
                "window": window,
                "windows": list(WINDOWS),
                "by_role": store.group_counts("role", since, rate_limits_only=False),
                "caps": {
                    c: store.rate_limits_by_class(since).get(c, 0)
                    for c in ("window_cap", "terminal_cap")
                },
            },
        )

    @app.get("/panel/verdicts", response_class=HTMLResponse)
    def panel_verdicts(
        request: Request, window: str = Query("24h"), _: None = Depends(require_token)
    ) -> HTMLResponse:
        store: EventStore = app.state.store
        since = window_seconds(window)
        return templates.TemplateResponse(
            request,
            "panels/verdicts.html",
            {
                "window": window,
                "windows": list(WINDOWS),
                "outcomes": store.outcome_counts("lesson", since),
                "recent": store.recent("lesson", limit=50, since=since),
            },
        )

    # ---------------------------------------------------------------- api

    @app.get("/api/errors")
    def api_errors(window: str = Query("24h"), _: None = Depends(require_token)) -> JSONResponse:
        context = _errors_context(window)
        return JSONResponse(
            {
                "window": window,
                "classified": context["classified"],
                "unclassified": context["unclassified"],
                "total_classified": context["total_classified"],
                # None when no baseline is configured. When present,
                # `classified` is the flag that stops a consumer treating a
                # pre-classification total as though it were a breakdown.
                "baseline": (
                    dataclasses.asdict(app.state.baseline) if app.state.baseline else None
                ),
            }
        )

    # ---------------------------------------------------------------- sse

    @app.get("/events/stream")
    async def stream(request: Request, _: None = Depends(require_token)) -> StreamingResponse:
        """SSE feed driving the HTMX swaps (T28).

        Resumes from Last-Event-ID, which is the events table's monotonic
        row id -- not a timestamp, because two events in the same
        millisecond must still have a total order for a resume to be gapless.
        """
        store: EventStore = app.state.store
        last_id = request.headers.get("last-event-id")
        try:
            cursor = int(last_id) if last_id else store.max_id()
        except ValueError:
            cursor = store.max_id()

        deadline = time.monotonic() + float(app.state.stream_max_seconds)
        poll = float(app.state.stream_poll_seconds)

        async def gen() -> AsyncIterator[bytes]:
            nonlocal cursor
            # Tell the client how long to wait before reconnecting, once.
            yield b"retry: 2000\n\n"
            while time.monotonic() < deadline:
                if await request.is_disconnected():
                    return
                rows = await asyncio.to_thread(store.since_id, cursor)
                for row in rows:
                    cursor = row["id"]
                    payload = json.dumps(
                        {
                            "id": row["id"],
                            "ts": row["ts"],
                            "kind": row["kind"],
                            "worker": row["worker"],
                            "role": row["role"],
                            "outcome": row["outcome"],
                            "error_class": row["error_class"],
                        }
                    )
                    yield f"id: {row['id']}\nevent: harness\ndata: {payload}\n\n".encode()
                if not rows:
                    # A comment keeps the connection warm through proxies
                    # without inventing an event that did not happen.
                    yield b": keepalive\n\n"
                    await asyncio.sleep(poll)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def app_from_env() -> FastAPI:
    store = EventStore(os.environ.get("HARNESS_DB", "harness.sqlite"))
    return create_app(store, token=os.environ.get("HARNESS_TOKEN"))
