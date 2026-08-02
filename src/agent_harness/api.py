"""The harness's JSON API. No HTML, no templates, no GUI.

The GUI lives in the session host — AIDevEnv is the reference one — which
already owns tabs, auth, push notifications, mobile and the terminal sessions
the agents run in. A second web UI here would mean a second URL, a second
login, no notifications and no phone story: worse, for the same work.

So this is the seam. The host's server proxies these routes, holding the
harness token so the browser never sees it, and renders the result as a Work
tab. Everything here is JSON a UI can consume directly.

Auth is a bearer token, and the service **fails closed** — with none
configured every route refuses, because coming up open is not an acceptable
default for something on a network, even a private one.
"""

from __future__ import annotations

import dataclasses
import secrets
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .events import RATE_LIMIT_CLASSES, UNCLASSIFIED
from .providers import MEANING
from .store import EventStore
from .work import CLAIMED, DONE, FAILED, PENDING, WorkQueue

WINDOWS = {"1h": 3600, "24h": 86400, "72h": 3 * 86400, "7d": 7 * 86400, "all": None}


def create_api(
    store: EventStore,
    queue: WorkQueue | None = None,
    token: str | None = None,
) -> FastAPI:
    app = FastAPI(title="agent-harness", docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.queue = queue
    app.state.token = token

    def require_token(request: Request) -> None:
        expected = request.app.state.token
        if not expected:
            raise HTTPException(status_code=503, detail="no auth token configured")
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    def since_for(window: str) -> float | None:
        if window not in WINDOWS:
            raise HTTPException(status_code=400, detail=f"unknown window {window!r}")
        span = WINDOWS[window]
        return None if span is None else time.time() - span

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Unauthenticated and cheap. A deploy check must not depend on the
        store being populated."""
        return JSONResponse(
            {
                "ok": True,
                "events": store.count(),
                "queue": queue is not None,
                "authenticated": bool(app.state.token),
            }
        )

    # ---------------------------------------------------------------- work

    @app.get("/api/work")
    def work(_: None = Depends(require_token)) -> JSONResponse:
        """Everything the Work tab renders in one call.

        One request rather than three: a phone on a flaky connection should
        not need a successful fan-out to show anything.
        """
        queue = app.state.queue
        if queue is None:
            return JSONResponse(
                {
                    "configured": False,
                    "reason": "no work queue is attached to this harness",
                    "items": [],
                    "counts": {},
                    "stale": [],
                }
            )
        latest: dict[str, dict[str, Any]] = {}
        for event in store.recent(kind="work", limit=2000):
            item_id = event["data"].get("item_id")
            if item_id and item_id not in latest:
                latest[item_id] = {
                    "outcome": event["outcome"],
                    "detail": event["data"].get("detail"),
                    "ts": event["ts"],
                    # The deep link: which terminal is doing this work.
                    "session_id": event["data"].get("session_id"),
                    "session_url": event["data"].get("session_url"),
                }
        return JSONResponse(
            {
                "configured": True,
                "counts": queue.counts(),
                "stale": [r.item_id for r in queue.stale()],
                "items": [
                    {**dataclasses.asdict(record), "latest": latest.get(record.item_id)}
                    for record in queue.all()
                ],
            }
        )

    @app.post("/api/work/{item_id}/retry")
    def retry(item_id: str, _: None = Depends(require_token)) -> JSONResponse:
        """Put a finished-or-failed item back in the queue.

        Refuses to touch a CLAIMED item: something may still be working on
        it, and yanking it out from under a live agent produces two workers
        on one item, which is worse than a stuck one. A stale claim expires
        on its own and becomes retryable without anyone intervening.
        """
        queue = app.state.queue
        if queue is None:
            raise HTTPException(status_code=409, detail="no work queue attached")
        record = queue.get(item_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no item {item_id!r}")
        if record.state == CLAIMED and record.lease_until > time.time():
            raise HTTPException(
                status_code=409,
                detail=f"{item_id} is claimed by {record.owner} and its lease is live; "
                "wait for the lease to expire rather than racing it",
            )
        queue.release(item_id, PENDING, error=None)
        return JSONResponse({"ok": True, "item_id": item_id, "state": PENDING})

    # -------------------------------------------------------------- errors

    @app.get("/api/errors")
    def errors(window: str = Query("24h"), _: None = Depends(require_token)) -> JSONResponse:
        """Rate limits by class.

        `unclassified` is reported separately and never folded into a class:
        it means the record predates classification, and inventing a class
        for it would fabricate the measurement.
        """
        since = since_for(window)
        by_class = store.rate_limits_by_class(since)
        classified = {c: by_class.get(c, 0) for c in RATE_LIMIT_CLASSES}
        return JSONResponse(
            {
                "window": window,
                "classified": classified,
                "meaning": {c: MEANING[c] for c in RATE_LIMIT_CLASSES},
                "unclassified": by_class.get(UNCLASSIFIED, 0),
                "total": sum(classified.values()),
                "by_worker": store.group_counts("worker", since),
                "by_endpoint": store.group_counts("endpoint", since),
                "by_role": store.group_counts("role", since),
            }
        )

    @app.get("/api/events")
    def events(
        since_id: int = Query(0), limit: int = Query(200), _: None = Depends(require_token)
    ) -> JSONResponse:
        """Events after `since_id`, oldest first.

        Cursor is the row id, not a timestamp: two events in the same
        millisecond must still have a total order or a poll silently drops
        one.
        """
        rows = store.since_id(since_id, limit=min(limit, 1000))
        return JSONResponse(
            {
                "events": rows,
                "cursor": rows[-1]["id"] if rows else since_id,
            }
        )

    @app.get("/api/summary")
    def summary(_: None = Depends(require_token)) -> JSONResponse:
        """The one-line answer for a tab badge: is anything running, is
        anything stuck, is anything waiting on me."""
        queue = app.state.queue
        counts = queue.counts() if queue else {}
        stale = len(queue.stale()) if queue else 0
        waiting = [
            event
            for event in store.recent(kind="work", limit=200)
            if event["outcome"] == "waiting_for_input"
        ]
        return JSONResponse(
            {
                "running": counts.get(CLAIMED, 0),
                "pending": counts.get(PENDING, 0),
                "done": counts.get(DONE, 0),
                "failed": counts.get(FAILED, 0),
                "stale": stale,
                # An agent asking a question is the one thing that genuinely
                # needs a human, so it gets its own field rather than being
                # buried in a count.
                "waiting_for_input": [
                    {
                        "item_id": e["data"].get("item_id"),
                        "session_url": e["data"].get("session_url"),
                    }
                    for e in waiting[:5]
                ],
            }
        )

    return app
