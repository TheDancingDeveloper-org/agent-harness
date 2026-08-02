"""The harness's HTTP API. Headless — no HTML, no templates, no GUI.

The GUI lives in the session host (AIDevEnv is the reference one), which
already owns tabs, auth, push notifications, mobile and the terminal sessions
the agents run in. A second web UI here would mean a second URL and a second
login to do the same job worse.

What this DOES own is a documented API. Every route is typed, every field has
a description, and the OpenAPI document is served alongside Swagger UI — so a
person with `curl`, an agent with a shell, or a generated client can all drive
the harness without reading its source.

Auth is a bearer token. Deployed inside a session host it is the SAME token
that reaches the GUI: one credential, one thing to rotate, and no second
secret to keep track of. The service fails closed — with no token configured
every authenticated route refuses, because coming up open is not an acceptable
default for something reachable over a network.

    /docs          Swagger UI, with an Authorize button
    /redoc         ReDoc
    /openapi.json  the schema itself
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import __version__
from .audit import AuditStore
from .events import RATE_LIMIT_CLASSES, UNCLASSIFIED
from .providers import MEANING
from .schemas import (
    AddItemsRequest,
    AddItemsResult,
    AuditCost,
    AuditCostRow,
    AuditDelivery,
    AuditDeliveryRow,
    AuditHealth,
    AuditRollupRow,
    AuditRollups,
    Baseline,
    BaselineList,
    Event,
    EventPage,
    FleetControl,
    Health,
    LatestEvent,
    NewBaseline,
    PlanItem,
    PlanParseResult,
    PlanSyncRequest,
    PlanSyncResult,
    ProjectList,
    ProjectSpec,
    ProjectSummary,
    RateLimits,
    RetryResult,
    RoleMap,
    RoleRoute,
    SetFleetControl,
    Summary,
    WaitingItem,
    WorkItem,
    WorkList,
)
from .store import EventStore
from .work import (
    CLAIMED,
    DONE,
    FAILED,
    PENDING,
    RUNNING,
    STOPPED,
    Project,
    WorkQueue,
    WorkRecord,
)

WINDOWS = {"1h": 3600, "24h": 86400, "72h": 3 * 86400, "7d": 7 * 86400, "all": None}

#: Where the live role map is stored. Shared through the queue's database
#: because the API and the worker are different processes.
ROLE_MAP_KEY = "role_map"

DESCRIPTION = """\
Plans work, claims it, runs it as an agent in a terminal session, and records
what happened.

**Auth** — every route except `/healthz` needs `Authorization: Bearer <token>`.
Deployed inside a session host this is the same token that reaches the GUI.
Use **Authorize** above to try these against a live instance.

**Reading the numbers.** Two things this API is careful about, because both are
easy to get wrong and expensive when you do:

* Rate limits are *classified*. `rpm` means you are going too fast, and is
  retried. `window_cap` and `terminal_cap` mean a spend budget is exhausted and
  are never retried, because retrying cannot make budget appear. Anything
  recorded before classification existed is reported as `unclassified` and is
  never folded into a class — that breakdown does not exist and cannot be
  recovered by re-parsing.
* A claim is a **lease**, not a lock. A worker that dies releases its item by
  doing nothing at all; `lease_until` is when that happens.
"""

TAGS = [
    {"name": "work", "description": "The backlog: what needs doing, and what is happening to it."},
    {"name": "plan", "description": "Turning a plan document into a backlog."},
    {"name": "control", "description": "Starting and stopping the fleet."},
    {"name": "observability", "description": "What the fleet did, and why anything failed."},
    {"name": "meta", "description": "Health and version."},
]

bearer = HTTPBearer(auto_error=False, description="The session host's API token.")


def create_api(
    store: EventStore,
    queue: WorkQueue | None = None,
    token: str | None = None,
    root_path: str = "",
    audit: AuditStore | None = None,
) -> FastAPI:
    """Build the API.

    `root_path` is the prefix this service is reached under when it sits
    behind a proxy (the session host mounts it at `/api/harness`). Setting it
    makes the OpenAPI document and Swagger UI emit URLs the *client* can
    actually call, rather than the ones the app sees internally.
    """
    app = FastAPI(
        title="agent-harness",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        root_path=root_path,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.store = store
    app.state.queue = queue
    app.state.audit = audit
    app.state.token = token

    def require_token(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        expected = request.app.state.token
        if not expected:
            raise HTTPException(status_code=503, detail="no auth token configured")
        supplied = credentials.credentials if credentials else ""
        if not supplied or not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    def since_for(window: str) -> float | None:
        if window not in WINDOWS:
            raise HTTPException(
                status_code=400,
                detail=f"unknown window {window!r}; expected one of {sorted(WINDOWS)}",
            )
        span = WINDOWS[window]
        return None if span is None else time.time() - span

    def need_queue() -> WorkQueue:
        queue: WorkQueue | None = app.state.queue
        if queue is None:
            raise HTTPException(status_code=409, detail="no work queue is attached")
        return queue

    # ---------------------------------------------------------------- meta

    @app.get("/healthz", tags=["meta"], summary="Liveness and wiring", response_model=Health)
    def healthz() -> Health:
        """Unauthenticated and cheap — a deploy check must not depend on the
        store being populated, or on holding a credential."""
        return Health(
            ok=True,
            events=store.count(),
            queue=app.state.queue is not None,
            authenticated=bool(app.state.token),
            version=__version__,
        )

    # ---------------------------------------------------------------- work

    @app.get("/api/work", tags=["work"], summary="The whole backlog", response_model=WorkList)
    def work(
        project_id: str | None = Query(
            None, description="Limit to one project. Omit for every project."
        ),
        _: None = Depends(require_token),
    ) -> WorkList:
        """Items, counts and stale claims in ONE call.

        Deliberately not three endpoints: a client on a flaky connection
        should not need a successful fan-out to show anything at all.
        """
        queue: WorkQueue | None = app.state.queue
        if queue is None:
            return WorkList(
                configured=False,
                reason="no work queue is attached to this harness",
            )
        latest = _latest_by_item(store)
        return WorkList(
            configured=True,
            counts=queue.counts(project_id=project_id),
            stale=[r.item_id for r in queue.stale(project_id=project_id)],
            items=[
                _item_model(r, latest.get(r.item_id)) for r in queue.items(project_id=project_id)
            ],
        )

    @app.get(
        "/api/work/{item_id}",
        tags=["work"],
        summary="One item",
        response_model=WorkItem,
        responses={404: {"description": "No such item"}},
    )
    def work_item(
        item_id: str = PathParam(description="Plan id, e.g. `T4`."),
        project_id: str = Query("default", description="Which project the item is in."),
        _: None = Depends(require_token),
    ) -> WorkItem:
        record = need_queue().get(item_id, project_id=project_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no item {item_id!r}")
        return _item_model(record, _latest_by_item(store).get(item_id))

    @app.post(
        "/api/work",
        tags=["work"],
        summary="Add items to the queue",
        response_model=AddItemsResult,
    )
    def add_items(
        request: AddItemsRequest,
        _: None = Depends(require_token),
    ) -> AddItemsResult:
        """Add work directly, without going through a plan document.

        Existing ids are refreshed, never reset: re-adding cannot un-finish
        work that is already done.
        """
        queue = need_queue()
        added = queue.add(
            [
                WorkRecord(
                    item_id=item.item_id,
                    title=item.title,
                    brief=item.brief,
                    issue=item.issue,
                    depends_on=list(item.depends_on),
                )
                for item in request.items
            ],
            project_id=request.project_id,
        )
        return AddItemsResult(added=added, total=len(queue.items(project_id=request.project_id)))

    @app.post(
        "/api/work/{item_id}/retry",
        tags=["work"],
        summary="Re-queue an item",
        response_model=RetryResult,
        responses={
            404: {"description": "No such item"},
            409: {"description": "The item's claim is still live"},
        },
    )
    def retry(
        item_id: str = PathParam(description="Plan id, e.g. `T4`."),
        project_id: str = Query("default", description="Which project the item is in."),
        _: None = Depends(require_token),
    ) -> RetryResult:
        """Put a finished or failed item back to `pending`.

        Refuses while a claim is live: yanking an item out from under a
        running agent produces two workers on one item, which is worse than
        one stuck item. A stale lease expires on its own and is retryable
        without anyone intervening.
        """
        queue = need_queue()
        record = queue.get(item_id, project_id=project_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no item {item_id!r}")
        if record.state == CLAIMED and record.lease_until > time.time():
            raise HTTPException(
                status_code=409,
                detail=f"{item_id} is claimed by {record.owner} and its lease is live; "
                "wait for the lease to expire rather than racing it",
            )
        queue.release(item_id, PENDING, error=None, project_id=project_id)
        return RetryResult(ok=True, item_id=item_id, state="pending")

    # ------------------------------------------------------------- control

    @app.get(
        "/api/control",
        tags=["control"],
        summary="Is the fleet claiming work?",
        response_model=FleetControl,
    )
    def get_control(_: None = Depends(require_token)) -> FleetControl:
        state, reason = need_queue().control()
        return FleetControl(state=state, reason=reason)

    # ----------------------------------------------------------------- audit

    def audit_store() -> AuditStore:
        store_: AuditStore | None = app.state.audit
        if store_ is None:
            raise HTTPException(status_code=409, detail="no audit store is attached")
        return store_

    def audit_window(window: str) -> tuple[float | None, bool]:
        """Window start, and whether history actually covers it.

        The second value is the honest part. A chart labelled "last 30 days"
        drawn from three days of history is not wrong about the data, it is
        wrong about the question -- and nothing in the numbers reveals it.
        """
        since = since_for(window)
        oldest, _ = audit_store().span()
        partial = bool(since is not None and oldest is not None and oldest > since)
        return since, partial

    @app.get(
        "/api/audit/health",
        tags=["observability"],
        summary="Is history actually being recorded?",
        response_model=AuditHealth,
    )
    def audit_health(_: None = Depends(require_token)) -> AuditHealth:
        """Whether the audit store is attached, writable, and how much it holds.

        Worth checking deliberately: writes are dropped rather than raised
        when the store is degraded, precisely so observation cannot stop
        work — which means nothing else will tell you.
        """
        store_: AuditStore | None = app.state.audit
        if store_ is None:
            return AuditHealth(configured=False, degraded=True)
        oldest, newest = store_.span()
        return AuditHealth(
            configured=True,
            degraded=store_.degraded,
            path=str(store_.path),
            events=store_.count(),
            oldest=oldest,
            newest=newest,
            schema_version=AuditStore.SCHEMA_VERSION,
        )

    @app.get(
        "/api/audit/events",
        tags=["observability"],
        summary="Raw history, paged by row id",
        response_model=EventPage,
    )
    def audit_events(
        since_id: int = Query(0, description="Exclusive. Pass back the previous `cursor`."),
        limit: int = Query(200, le=1000),
        _: None = Depends(require_token),
    ) -> EventPage:
        """Paged by id rather than timestamp: two events in the same
        millisecond must still have a total order, or a page boundary can
        silently skip one."""
        rows = audit_store().since_id(since_id, limit=limit)
        return EventPage(
            events=[Event(**_audit_event_fields(r)) for r in rows],
            cursor=rows[-1]["id"] if rows else since_id,
        )

    @app.get(
        "/api/audit/cost",
        tags=["observability"],
        summary="Spend by project, role and model",
        response_model=AuditCost,
    )
    def audit_cost(
        window: str = Query("7d", description=f"One of {sorted(WINDOWS)}."),
        project_id: str | None = Query(None),
        _: None = Depends(require_token),
    ) -> AuditCost:
        """What it cost, computed from the prices recorded at the time.

        Calls whose price was unknown are counted in `unpriced` and never
        folded into the total — a sum that quietly omits them reads as
        complete and is not.
        """
        since, partial = audit_window(window)
        rows = audit_store().cost(since=since, project_id=project_id)
        priced = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
        return AuditCost(
            window=window,
            rows=[AuditCostRow(**r) for r in rows],
            total_cost_usd=sum(priced) if priced else None,
            total_unpriced=sum(r["unpriced"] or 0 for r in rows),
            partial=partial,
        )

    @app.get(
        "/api/audit/delivery",
        tags=["observability"],
        summary="What was delivered, by project and outcome",
        response_model=AuditDelivery,
    )
    def audit_delivery(
        window: str = Query("7d", description=f"One of {sorted(WINDOWS)}."),
        project_id: str | None = Query(None),
        _: None = Depends(require_token),
    ) -> AuditDelivery:
        since, partial = audit_window(window)
        rows = audit_store().delivery(since=since, project_id=project_id)
        return AuditDelivery(
            window=window,
            rows=[AuditDeliveryRow(**r) for r in rows],
            partial=partial,
        )

    @app.get(
        "/api/audit/rollups",
        tags=["observability"],
        summary="Daily aggregates -- the long series",
        response_model=AuditRollups,
    )
    def audit_rollups(
        project_id: str | None = Query(None),
        since_day: str | None = Query(None, description="ISO date, inclusive."),
        _: None = Depends(require_token),
    ) -> AuditRollups:
        """Immutable daily rows, kept forever.

        Raw events are thinned after ~90 days, so anything older than that
        lives only here. A day is never rewritten once published: if it could
        be, every historical figure would be provisional and no report could
        be reproduced.
        """
        store_ = audit_store()
        return AuditRollups(
            rows=[
                AuditRollupRow(**r)
                for r in store_.rollups(project_id=project_id, since_day=since_day)
            ],
            rolled_up_through=store_.rolled_up_through(),
        )

    @app.get(
        "/api/audit/baselines",
        tags=["observability"],
        summary="Recorded baselines",
        response_model=BaselineList,
    )
    def list_baselines(
        project_id: str | None = Query(None),
        _: None = Depends(require_token),
    ) -> BaselineList:
        """Without a baseline, "better than before" has no before."""
        return BaselineList(
            baselines=[Baseline(**b) for b in audit_store().baselines(project_id=project_id)]
        )

    @app.post(
        "/api/audit/baselines",
        tags=["observability"],
        summary="Record a baseline",
        response_model=Baseline,
        responses={409: {"description": "That baseline id already exists"}},
    )
    def create_baseline(
        request: NewBaseline,
        _: None = Depends(require_token),
    ) -> Baseline:
        """Record a dated measurement to compare against.

        Immutable: re-recording under an existing id is refused rather than
        overwritten. A baseline that can be edited is not a baseline, it is a
        target that moves to wherever the current numbers are.
        """
        store_ = audit_store()
        created = store_.record_baseline(
            request.baseline_id,
            request.project_id,
            label=request.label,
            window_days=request.window_days,
            recorded_at=time.time(),
            items_done=request.items_done,
            cost_usd=request.cost_usd,
            notes=request.notes,
        )
        if not created:
            raise HTTPException(
                status_code=409,
                detail=f"baseline {request.baseline_id!r} already exists; "
                "baselines are immutable, so record a new one rather than replacing it",
            )
        match = [
            b
            for b in store_.baselines(project_id=request.project_id)
            if b["baseline_id"] == request.baseline_id
        ]
        return Baseline(**match[0])

    # -------------------------------------------------------------- projects

    @app.get(
        "/api/projects",
        tags=["work"],
        summary="Every project, with counts and control state",
        response_model=ProjectList,
    )
    def list_projects(_: None = Depends(require_token)) -> ProjectList:
        """One call for the overview screen. A per-project fan-out would make
        the first thing a user sees depend on N successful requests."""
        queue = need_queue()
        out = []
        for project in queue.projects():
            state, reason, previous = queue.control_detail(project.project_id)
            out.append(
                ProjectSummary(
                    project=_project_spec(project),
                    counts=queue.counts(project_id=project.project_id),
                    control=FleetControl(state=state, reason=reason),
                    previous_state=previous,
                    stale=len(queue.stale(project_id=project.project_id)),
                )
            )
        return ProjectList(projects=out)

    @app.post(
        "/api/projects",
        tags=["work"],
        summary="Register a project",
        response_model=ProjectSummary,
    )
    def create_project(
        spec: ProjectSpec,
        _: None = Depends(require_token),
    ) -> ProjectSummary:
        """Register or update a project, durably.

        It starts **stopped**. Registering a project must not begin spending
        money on it, and nothing here starts a worker -- only an explicit
        start does.
        """
        queue = need_queue()
        queue.add_project(
            Project(
                project_id=spec.project_id,
                name=spec.name,
                repo=spec.repo,
                work_dir=spec.work_dir,
                base_branch=spec.base_branch,
                checks=list(spec.checks),
                plan_path=spec.plan_path,
                roles={k: v.model_dump() for k, v in spec.roles.items()} if spec.roles else None,
                max_workers=spec.max_workers,
            )
        )
        return _project_summary(queue, spec.project_id)

    @app.get(
        "/api/projects/{project_id}",
        tags=["work"],
        summary="One project",
        response_model=ProjectSummary,
    )
    def get_project(
        project_id: str = PathParam(description="Project id."),
        _: None = Depends(require_token),
    ) -> ProjectSummary:
        return _project_summary(need_queue(), project_id)

    @app.post(
        "/api/projects/{project_id}/start",
        tags=["control"],
        summary="Continue execution for one project",
        response_model=ProjectSummary,
    )
    def start_project(
        project_id: str = PathParam(description="Project id."),
        _: None = Depends(require_token),
    ) -> ProjectSummary:
        """The only thing that lets a project claim work.

        Nothing calls this on boot. An auto-resuming fleet turns a routine
        restart into unattended spend against a stack nobody has looked at
        yet, and a crash-looping deploy would restart the fleet on every loop.
        Resuming is a decision, so it is a request.
        """
        queue = need_queue()
        if queue.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
        queue.set_control(RUNNING, reason=None, project_id=project_id)
        return _project_summary(queue, project_id)

    @app.post(
        "/api/projects/{project_id}/stop",
        tags=["control"],
        summary="Stop claiming for one project",
        response_model=ProjectSummary,
    )
    def stop_project(
        request: SetFleetControl | None = None,
        project_id: str = PathParam(description="Project id."),
        _: None = Depends(require_token),
    ) -> ProjectSummary:
        """Stop taking new work. **Nothing in flight is interrupted.**"""
        queue = need_queue()
        if queue.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
        queue.set_control(
            STOPPED,
            reason=request.reason if request else None,
            project_id=project_id,
        )
        return _project_summary(queue, project_id)

    @app.post(
        "/api/control",
        tags=["control"],
        summary="Pause, drain or resume the fleet",
        response_model=FleetControl,
    )
    def set_control(
        request: SetFleetControl,
        _: None = Depends(require_token),
    ) -> FleetControl:
        """Stop or resume claiming, at the next item boundary.

        **Nothing in flight is interrupted.** Killing an agent mid-item
        destroys the context that makes its work resumable and leaves a
        half-finished worktree behind; waiting for it to finish is strictly
        better. `drain` and `pause` behave identically to a worker — the
        difference is what you meant, which matters to whoever finds the fleet
        stopped and has to decide whether to resume it.
        """
        queue = need_queue()
        queue.set_control(request.state, request.reason)
        state, reason = queue.control()
        return FleetControl(state=state, reason=reason)

    @app.get(
        "/api/roles",
        tags=["control"],
        summary="Where each role's calls go",
        response_model=RoleMap,
    )
    def get_roles(_: None = Depends(require_token)) -> RoleMap:
        stored = need_queue().get_setting(ROLE_MAP_KEY) or {}
        return RoleMap(roles={name: RoleRoute(**route) for name, route in stored.items()})

    @app.put(
        "/api/roles",
        tags=["control"],
        summary="Change the role map without a redeploy",
        response_model=RoleMap,
    )
    def set_roles(request: RoleMap, _: None = Depends(require_token)) -> RoleMap:
        """Takes effect on the next model call.

        This is possible only because a call site names a **role**, never a
        model. Routing a role somewhere else is then a data change rather than
        a code change — which is what lets you move the implementer to a
        cheaper tier, or the reviewer to a different vendor, while the fleet
        is running.

        A reviewer on the same vendor as the implementer means some share of
        reviews is a model grading its own work. Nothing here enforces that;
        it is your call, and it is worth making deliberately.
        """
        queue = need_queue()
        queue.set_setting(
            ROLE_MAP_KEY,
            {name: route.model_dump() for name, route in request.roles.items()},
        )
        return request

    # ---------------------------------------------------------------- plan

    @app.post(
        "/api/plan/parse",
        tags=["plan"],
        summary="Parse a plan without writing anything",
        response_model=PlanParseResult,
        responses={404: {"description": "No such file"}},
    )
    def plan_parse(
        path: str = Query(..., description="Path to the plan markdown."),
        _: None = Depends(require_token),
    ) -> PlanParseResult:
        """Read a plan and report what it contains — including what it could
        NOT read.

        Parsing prose is lossy, so skipped headings and duplicate ids are part
        of the answer rather than a footnote: a parser that quietly finds
        three items in a fifty-item plan looks like it worked.
        """
        from .plan import parse_plan_file

        target = Path(path)
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"no plan at {path!r}")
        parsed = parse_plan_file(target)
        return PlanParseResult(
            items=[
                PlanItem(
                    id=i.id,
                    title=i.title,
                    body=i.body,
                    labels=i.labels,
                    milestone=i.milestone,
                    depends_on=i.depends_on,
                    done=i.done,
                    line=i.line,
                )
                for i in parsed.items
            ],
            skipped=[f"line {n}: {title}" for n, title in parsed.skipped],
            duplicate_ids=parsed.duplicate_ids(),
            unresolved_dependencies=parsed.unresolved_dependencies(),
        )

    @app.post(
        "/api/plan/sync",
        tags=["plan"],
        summary="Sync a plan to GitHub issues",
        response_model=PlanSyncResult,
        responses={
            404: {"description": "No such file"},
            409: {"description": "The plan states an id more than once"},
            502: {"description": "GitHub rejected the request"},
        },
    )
    def plan_sync(
        request: PlanSyncRequest,
        _: None = Depends(require_token),
    ) -> PlanSyncResult:
        """Create or update one issue per work item.

        Defaults to `dry_run=true`, because this writes to a real repository.
        Never closes, reopens or deletes: an item vanishing from a document is
        usually an edit, sometimes a mistake, and never grounds for the
        harness to decide work stopped mattering.
        """
        from .github import GitHub, GitHubError, sync
        from .plan import parse_plan_file

        target = Path(request.path)
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"no plan at {request.path!r}")
        parsed = parse_plan_file(target)
        duplicates = parsed.duplicate_ids()
        if duplicates and not request.allow_duplicates:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "the plan states these ids more than once; each becomes one issue",
                    "duplicate_ids": duplicates,
                },
            )
        try:
            report = sync(GitHub(request.repo), parsed.deduplicated(), dry_run=request.dry_run)
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return PlanSyncResult(
            created=report.created,
            updated=report.updated,
            unchanged=report.unchanged,
            orphaned=report.orphaned,
            labels_created=report.labels_created,
            milestones_created=report.milestones_created,
            dry_run=request.dry_run,
        )

    # ------------------------------------------------------- observability

    @app.get(
        "/api/errors",
        tags=["observability"],
        summary="Rate limits by class",
        response_model=RateLimits,
    )
    def errors(
        window: str = Query("24h", description="One of 1h, 24h, 72h, 7d, all."),
        _: None = Depends(require_token),
    ) -> RateLimits:
        """The question a fleet operator most often cannot answer: of all those
        429s, how many meant *slow down* and how many meant *your budget is
        gone*?"""
        since = since_for(window)
        by_class = store.rate_limits_by_class(since)
        classified = {c: by_class.get(c, 0) for c in RATE_LIMIT_CLASSES}
        return RateLimits(
            window=window,
            classified=classified,
            meaning={c: MEANING[c] for c in RATE_LIMIT_CLASSES},
            unclassified=by_class.get(UNCLASSIFIED, 0),
            total=sum(classified.values()),
            by_worker=store.group_counts("worker", since),
            by_endpoint=store.group_counts("endpoint", since),
            by_role=store.group_counts("role", since),
        )

    @app.get(
        "/api/events",
        tags=["observability"],
        summary="Event stream, paged",
        response_model=EventPage,
    )
    def events(
        since_id: int = Query(0, description="Cursor from the previous page."),
        limit: int = Query(200, ge=1, le=1000),
        _: None = Depends(require_token),
    ) -> EventPage:
        """Append-only history, oldest first.

        Paged by row id rather than timestamp: two events in the same
        millisecond must still have a total order, or a poll silently drops
        one.
        """
        rows = store.since_id(since_id, limit=limit)
        return EventPage(
            events=[Event(**row) for row in rows],
            cursor=rows[-1]["id"] if rows else since_id,
        )

    @app.get(
        "/api/summary",
        tags=["observability"],
        summary="One-glance status",
        response_model=Summary,
    )
    def summary(_: None = Depends(require_token)) -> Summary:
        """Enough for a tab badge or a status line."""
        queue: WorkQueue | None = app.state.queue
        counts = queue.counts() if queue else {}
        waiting = [
            event
            for event in store.recent(kind="work", limit=200)
            if event["outcome"] == "waiting_for_input"
        ]
        return Summary(
            running=counts.get(CLAIMED, 0),
            pending=counts.get(PENDING, 0),
            done=counts.get(DONE, 0),
            failed=counts.get(FAILED, 0),
            stale=len(queue.stale()) if queue else 0,
            abandoned_sessions=len(queue.abandoned_sessions()) if queue else 0,
            waiting_for_input=[
                WaitingItem(
                    item_id=e["data"].get("item_id"),
                    session_url=e["data"].get("session_url"),
                )
                for e in waiting[:5]
            ],
        )

    return app


def _latest_by_item(store: EventStore) -> dict[str, dict[str, Any]]:
    """Newest event per work item. One scan — doing it per item would be a
    query per row."""
    latest: dict[str, dict[str, Any]] = {}
    for event in store.recent(kind="work", limit=2000):
        item_id = event["data"].get("item_id")
        if item_id and item_id not in latest:
            latest[item_id] = event
    return latest


def _audit_event_fields(row: dict[str, Any]) -> dict[str, Any]:
    """An audit row as the wire Event model.

    The audit table has columns the event model does not (cost, tokens,
    prices); they travel in `data`, where they already are, rather than
    widening the public event shape for every consumer.
    """
    return {
        "id": row["id"],
        "ts": row["ts"],
        "kind": row["kind"],
        "source": row["source"],
        "worker": row["worker"],
        "role": row["role"],
        "model": row["model"],
        "endpoint": row["endpoint"],
        "outcome": row["outcome"],
        "error_class": row["error_class"],
        "latency_s": row["latency_s"],
        "data": json.loads(row["data"] or "{}"),
    }


def _project_spec(project: Project) -> ProjectSpec:
    return ProjectSpec(
        project_id=project.project_id,
        name=project.name,
        repo=project.repo,
        work_dir=project.work_dir,
        base_branch=project.base_branch,
        checks=list(project.checks),
        plan_path=project.plan_path,
        roles={k: RoleRoute(**v) for k, v in project.roles.items()} if project.roles else None,
        max_workers=project.max_workers,
    )


def _project_summary(queue: WorkQueue, project_id: str) -> ProjectSummary:
    project = queue.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
    state, reason, previous = queue.control_detail(project_id)
    return ProjectSummary(
        project=_project_spec(project),
        counts=queue.counts(project_id=project_id),
        control=FleetControl(state=state, reason=reason),
        previous_state=previous,
        stale=len(queue.stale(project_id=project_id)),
    )


def _item_model(record: WorkRecord, event: dict[str, Any] | None) -> WorkItem:
    return WorkItem(
        item_id=record.item_id,
        title=record.title,
        brief=record.brief,
        issue=record.issue,
        depends_on=list(record.depends_on),
        state=record.state,
        owner=record.owner,
        lease_until=record.lease_until,
        attempts=record.attempts,
        last_error=record.last_error,
        branch=record.branch,
        pr_url=record.pr_url,
        updated_at=record.updated_at,
        latest=(
            LatestEvent(
                outcome=event["outcome"] or "",
                detail=event["data"].get("detail"),
                ts=event["ts"],
                session_id=event["data"].get("session_id"),
                session_url=event["data"].get("session_url"),
            )
            if event
            else None
        ),
    )
