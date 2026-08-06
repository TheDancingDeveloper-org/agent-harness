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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import __version__
from .audit import AuditStore
from .events import RATE_LIMIT_CLASSES, UNCLASSIFIED
from .maintenance import DEFAULT_RETENTION_DAYS, run_maintenance
from .preflight import BaseChecks
from .providers import MEANING
from .reconcile import GitHubReconciler, items_by_pr
from .schemas import (
    AddItemsRequest,
    AddItemsResult,
    AnswerRequest,
    AnswerResult,
    AuditCost,
    AuditCostRow,
    AuditDelivery,
    AuditDeliveryRow,
    AuditHealth,
    AuditRollupRow,
    AuditRollups,
    BaseCheckStatus,
    Baseline,
    BaselineList,
    BlockRequest,
    BlockResult,
    DependencyEdgeModel,
    DependencyGraphReport,
    DependencyOverrideRequest,
    DependencyOverrideResult,
    Event,
    EventPage,
    ExecutionReadiness,
    FleetControl,
    Health,
    HoldList,
    HoldView,
    InceptionDraft,
    InceptionPlan,
    InceptionStart,
    ItemReadiness,
    LatestEvent,
    MaintenanceResult,
    NewBaseline,
    OpenQuestion,
    OverdueHold,
    PlanItem,
    PlanParseResult,
    PlanSyncRequest,
    PlanSyncResult,
    PreflightCheck,
    PreflightResult,
    ProjectList,
    ProjectReadiness,
    ProjectSpec,
    ProjectSummary,
    ProposalModel,
    RateLimits,
    ReadinessProbe,
    ReadinessReasonModel,
    ReconcileResult,
    ResolveQuestion,
    RetryResult,
    RoleMap,
    RoleMapView,
    RoleRoute,
    RoutedRole,
    RouteReachability,
    RoutesHealthView,
    ScopeRequest,
    SetFleetControl,
    StopProjectRequest,
    Summary,
    WaitingItem,
    WorkItem,
    WorkList,
)
from .store import EventStore
from .work import (
    BLOCKED,
    CLAIMED,
    DONE,
    DRAINING,
    FAILED,
    HELD,
    PENDING,
    STOPPED,
    Project,
    WorkQueue,
    WorkRecord,
)

WINDOWS = {"1h": 3600, "24h": 86400, "72h": 3 * 86400, "7d": 7 * 86400, "all": None}

#: Where the live role map is stored. Shared through the queue's database
#: because the API and the worker are different processes.
ROLE_MAP_KEY = "role_map"

#: How long a healthy model has to answer preflight's one-token probe, and
#: how long it has before it is treated as not answering at all. Anything in
#: between is reported as slow and not refused — a late model is usable, and
#: the harness has no business deciding otherwise about someone else's
#: endpoint.
MODEL_PROBE_TIMEOUT = 10.0
MODEL_PROBE_PATIENCE = 20.0

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
    fleet: Any | None = None,
    model_client: Any | None = None,
    session_host: Any | None = None,
    probes: Mapping[str, Any] | None = None,
    executor_roles: Any | None = None,
    default_preset: str = "",
) -> FastAPI:
    """Build the API.

    `root_path` is the prefix this service is reached under when it sits
    behind a proxy (the session host mounts it at `/api/harness`). Setting it
    makes the OpenAPI document and Swagger UI emit URLs the *client* can
    actually call, rather than the ones the app sees internally.

    `probes` overrides how readiness inspects the world -- `git_probe` and
    `github_probe`, as `preflight` defines them. Preflight was built with
    every probe injected precisely so a readiness gate could be tested; this
    layer used to hardcode the defaults, which put a real `gh` subprocess and
    a real filesystem read behind an HTTP route.

    `executor_roles` says which roles the executor this deployment builds can
    actually reach (`runtime.ExecutorRoles`). Without it the role map
    advertises every configured route as live, which in session mode was
    wrong for two of three stages: the agent process plans and implements,
    and no `planner` or `implementer` route is ever called.

    `default_preset` is the route preset this deployment's workers use for
    roles that name none. It matters here because readiness *probes* routes:
    a report built with a different wire protocol from the one the work uses
    would be asking a different URL, and would answer a question nobody asked.
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
    app.state.fleet = fleet
    app.state.model_client = model_client
    app.state.session_host = session_host
    # Everything preflight needs lives on THIS app. It used to be copied into
    # a module-level dictionary, so a second `create_api` in the same process
    # silently took over the first's wiring -- including, once roles were
    # routed per project, which roles it believed its executor could reach.
    app.state.probes = dict(probes or {})
    app.state.executor_roles = executor_roles
    app.state.default_preset = default_preset
    app.state.ask_model = _model_asker(model_client)
    app.state.base_checks = BaseChecks()
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
        latest = _latest_by_item(store, app.state.audit, project_id)
        return WorkList(
            configured=True,
            counts=queue.counts(project_id=project_id),
            stale=[r.item_id for r in queue.stale(project_id=project_id)],
            items=[
                _item_model(
                    r,
                    latest.get(r.item_id),
                    # Only for the held ones. A query per row would be a query
                    # per row, and the overwhelming majority are not held.
                    queue.holds.current(project_id or "default", r.item_id)
                    if r.state == HELD
                    else None,
                    queue.now(),
                )
                for r in queue.items(project_id=project_id)
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
        queue = need_queue()
        record = queue.get(item_id, project_id=project_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no item {item_id!r}")
        return _item_model(
            record,
            _latest_by_item(store, app.state.audit, project_id).get(item_id),
            queue.holds.current(project_id, item_id) if record.state == HELD else None,
            queue.now(),
        )

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
        """Put a finished, failed or exhausted item back to `pending`.

        Refuses while a claim is live: yanking an item out from under a
        running agent produces two workers on one item, which is worse than
        one stuck item. A stale lease expires on its own and is retryable
        without anyone intervening.

        `queue.requeue`, not `queue.release` — this is the operator's only
        lever over a wedged row, and it has to *mean* it. A release leaves the
        attempt counter where it was, so retrying an `exhausted` item put it
        back to `pending`, reported `ok`, and watched the very next claim scan
        retire it again before any worker saw it. It also leaves the durable
        attempt position, so the "retry" would resume into the verdict it was
        retrying. Requeuing clears both, and keeps `last_error`, which is the
        only record of why the item stopped.
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
        queue.requeue(item_id, project_id=project_id)
        return RetryResult(ok=True, item_id=item_id, state="pending")

    @app.get(
        "/api/holds",
        tags=["work"],
        summary="Every question waiting on a person",
        response_model=HoldList,
    )
    def list_holds(
        project_id: str = Query("", description="Limit to one project. Empty is all of them."),
        _: None = Depends(require_token),
    ) -> HoldList:
        """The inbox. Oldest first, which is the order to work through.

        This is the answer to issue #103. A silent-but-active session and a
        hung one used to look identical; a held item says what it is waiting
        for and how long it has waited, and anything not in this list that is
        making no progress is a hang rather than a question.
        """
        queue = need_queue()
        # Swept before reading, so the inbox never shows a question that has
        # already timed out. An operator reading a stale list would answer
        # into nothing.
        queue.expire_holds()
        # The queue's clock, not the wall clock. They are the same in
        # production and are deliberately not in a test, and an age computed
        # against a different clock from the one that stamped the question is
        # not an age.
        now = queue.now()
        return HoldList(
            open=[
                HoldView(**hold.as_dict(now)) for hold in queue.holds.open_holds(project_id or None)
            ]
        )

    @app.post(
        "/api/work/{item_id}/answer",
        tags=["work"],
        summary="Answer a held item's question",
        response_model=AnswerResult,
        responses={
            404: {"description": "No such item, or it has no open question"},
            409: {"description": "The resume token does not answer this question"},
        },
    )
    def answer_hold(
        request: AnswerRequest,
        item_id: str = PathParam(description="Plan id, e.g. `T4`."),
        project_id: str = Query("default", description="Which project the item is in."),
        _: None = Depends(require_token),
    ) -> AnswerResult:
        """Answer from anywhere, and hand the item back to the worker that asked.

        **From anywhere** is the point. The answer arrives from a phone, not
        from the terminal that asked, and the worker that asked may have died
        in the meantime — the hold survived it, and the item goes back to
        `claimed` with a fresh lease. If that worker really is gone, the lease
        expires as it always did and another worker continues the attempt.

        Nothing is written into a live session. `COORDINATION-PLANE.md` §5.1
        rules on why and the reason is exact: the process may be at a shell,
        and an answer becomes a command.
        """
        from .holds import Answer as HoldAnswer
        from .holds import HoldError

        queue = need_queue()
        try:
            hold = queue.answer_hold(
                item_id,
                request.resume_token,
                HoldAnswer(text=request.text, data=dict(request.data), who=request.who),
                project_id=project_id,
            )
        except HoldError as exc:
            # 404 for "there is nothing to answer", 409 for "you may not
            # answer this one". A person who typed an answer is told which.
            status = 404 if "no open question" in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return AnswerResult(
            ok=True,
            item_id=item_id,
            state="claimed",
            hold=HoldView(**hold.as_dict(queue.now())),
        )

    @app.post(
        "/api/work/{item_id}/block",
        tags=["work"],
        summary="Park an item on a human decision",
        response_model=BlockResult,
        responses={
            404: {"description": "No such item"},
            409: {"description": "The item's claim is live, or it is already done"},
        },
    )
    def block(
        request: BlockRequest,
        item_id: str = PathParam(description="Plan id, e.g. `D8`."),
        project_id: str = Query("default", description="Which project the item is in."),
        _: None = Depends(require_token),
    ) -> BlockResult:
        """Stop an item being claimed, and say why.

        A plan routinely contains work that is **a decision, not a task** —
        which database, whether to keep the old endpoint. The queue has always
        had a `blocked` state and honoured it, but nothing here could reach
        it: the only way to park a decision was to write to the database by
        hand, which goes around the validation and the audit trail that make
        this an API rather than a wrapper over SQL.

        Blocked items are not claimed, and neither is anything that depends on
        them, so blocking one decision holds back exactly the work that
        depended on the decision.

        Idempotent: blocking a blocked item updates the reason and reports the
        same state. `POST /api/work/{item_id}/retry` is the way back — it
        returns the item to `pending` once the decision is made.
        """
        queue = need_queue()
        record = queue.get(item_id, project_id=project_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no item {item_id!r}")
        if record.state == CLAIMED and record.lease_until > time.time() and not request.override:
            raise HTTPException(
                status_code=409,
                detail=f"{item_id} is claimed by {record.owner} and its lease is live; "
                "blocking it now abandons work in flight. Wait for the lease, or pass "
                "override=true and accept that.",
            )
        if record.state == DONE and not request.override:
            raise HTTPException(
                status_code=409,
                detail=f"{item_id} is already done; blocking it would un-finish "
                "completed work. Pass override=true if that is genuinely what you mean.",
            )
        reason = request.reason if request.who is None else f"{request.reason} (— {request.who})"
        queue.release(item_id, BLOCKED, error=reason, project_id=project_id)
        return BlockResult(ok=True, item_id=item_id, state="blocked", reason=reason)

    # ------------------------------------------------------ dependency graph

    @app.get(
        "/api/work/{item_id}/readiness",
        tags=["work"],
        summary="Why an item is or is not ready",
        response_model=ItemReadiness,
        responses={404: {"description": "No such item"}},
    )
    def item_readiness(
        item_id: str = PathParam(description="Plan id, e.g. `T4`."),
        project_id: str = Query("default", description="Which project the item is in."),
        _: None = Depends(require_token),
    ) -> ItemReadiness:
        """The dependency graph's answer for one item, with its reasons.

        This is the **same** evaluation `claim` makes and the executor
        repeats before the expensive gate. An item that will not start is the
        single most common thing an operator has to explain, and until now
        the only answer available was "it is pending".
        """
        queue = need_queue()
        record = queue.get(item_id, project_id=project_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no item {item_id!r}")
        return _readiness_model(queue.readiness(item_id, project_id=project_id), record)

    @app.get(
        "/api/graph",
        tags=["work"],
        summary="The dependency graph for one project",
        response_model=DependencyGraphReport,
    )
    def dependency_graph(
        project_id: str = Query("default", description="Which project's graph to report."),
        _: None = Depends(require_token),
    ) -> DependencyGraphReport:
        """Every edge, every cycle, and who is ready — in one call.

        Asked item by item, "which work can start and why not" is a report
        nobody assembles. A cycle in particular is invisible one item at a
        time: each member looks like it is merely waiting.
        """
        queue = need_queue()
        report = queue.graph.report(project_id)
        records = {r.item_id: r for r in queue.items(project_id=project_id)}
        return DependencyGraphReport(
            project_id=report.project_id,
            revision=report.revision,
            edges=[
                DependencyEdgeModel(
                    source_item=edge.source_item,
                    target_kind=edge.target_kind,
                    target_id=edge.target_id,
                    required=edge.required,
                    resolver=edge.resolver,
                    state=edge.state,
                    evidence=edge.evidence,
                    provenance=edge.provenance,
                    revision=edge.revision,
                )
                for edge in report.edges
            ],
            cycles=[list(cycle) for cycle in report.cycles],
            ready=list(report.ready),
            not_ready=[
                _readiness_model(state, records.get(state.item_id)) for state in report.not_ready
            ],
        )

    @app.post(
        "/api/work/{item_id}/dependency-override",
        tags=["work"],
        summary="Admit blocked work deliberately",
        response_model=DependencyOverrideResult,
        responses={404: {"description": "No such item"}},
    )
    def dependency_override(
        request: DependencyOverrideRequest,
        item_id: str = PathParam(description="Plan id, e.g. `T4`."),
        project_id: str = Query("default", description="Which project the item is in."),
        _: None = Depends(require_token),
    ) -> DependencyOverrideResult:
        """Record an operator decision to admit an item the graph blocks.

        The gate is not removed and nothing is marked satisfied: the edges
        keep their real state, and the override is recorded next to them with
        a reason. It applies to **the graph revision it was granted at**, so
        a later correction re-blocks the item rather than inheriting a
        judgement nobody made about it.

        This is the escape hatch §8.2 of the coordination plane requires so
        that a dependency discovered after a claim can be resolved *or*
        explicitly overridden, rather than leaving the work stuck with no
        supported way forward.
        """
        queue = need_queue()
        record = queue.get(item_id, project_id=project_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no item {item_id!r}")
        revision = queue.graph.record_override(
            project_id, item_id, reason=request.reason, who=request.who
        )
        return DependencyOverrideResult(
            ok=True,
            project_id=project_id,
            item_id=item_id,
            revision=revision,
            readiness=_readiness_model(queue.readiness(item_id, project_id=project_id), record),
        )

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

    # ------------------------------------------------------------- inception

    def inception_for() -> Any:
        from .inception import Inception

        return Inception(need_queue(), model_client=app.state.model_client)

    def _proposal_model(proposal: Any) -> ProposalModel:
        return ProposalModel(
            revision=proposal.revision,
            created_at=proposal.created_at,
            goal=proposal.goal,
            assumptions=proposal.assumptions,
            non_goals=proposal.non_goals,
            risks=proposal.risks,
            phases=proposal.phases,
            questions=[
                OpenQuestion(
                    **{
                        "id": q.id,
                        "question": q.question,
                        "severity": q.severity,
                        "why_it_matters": q.why_it_matters,
                        "answer": q.answer,
                        "deferred_reason": q.deferred_reason,
                        "resolved_by": q.resolved_by,
                    }
                )
                for q in proposal.questions
            ],
            feedback=proposal.feedback,
            item_count=proposal.item_count(),
            blocking_open=len(proposal.blocking_open()),
        )

    @app.post(
        "/api/inception",
        tags=["work"],
        summary="Describe a project in a paragraph",
        response_model=InceptionDraft,
    )
    def inception_start(
        request: InceptionStart,
        _: None = Depends(require_token),
    ) -> InceptionDraft:
        """Begin scoping. Nothing external exists yet and will not until you
        approve: no repository, no issues, no branches, no queue rows."""
        return InceptionDraft(**inception_for().start(request.project_id, request.overview))

    @app.post(
        "/api/inception/{project_id}/scope",
        tags=["work"],
        summary="Propose a scope, or revise the last one",
        response_model=ProposalModel,
    )
    def inception_scope(
        request: ScopeRequest,
        project_id: str = PathParam(description="Project id."),
        _: None = Depends(require_token),
    ) -> ProposalModel:
        """With `feedback`, revises the previous proposal rather than starting
        over -- a fresh scope loses whatever was already right and makes you
        re-argue points you had settled."""
        try:
            return _proposal_model(inception_for().scope(project_id, request.feedback))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get(
        "/api/inception/{project_id}",
        tags=["work"],
        summary="The current proposal",
        response_model=ProposalModel,
    )
    def inception_current(
        project_id: str = PathParam(description="Project id."),
        _: None = Depends(require_token),
    ) -> ProposalModel:
        proposal = inception_for().current(project_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="nothing has been scoped yet")
        return _proposal_model(proposal)

    @app.post(
        "/api/inception/{project_id}/questions/{question_id}",
        tags=["work"],
        summary="Answer, defer, or re-grade a question",
        response_model=ProposalModel,
    )
    def inception_resolve(
        request: ResolveQuestion,
        project_id: str = PathParam(description="Project id."),
        question_id: str = PathParam(description="Question id, e.g. `Q1`."),
        _: None = Depends(require_token),
    ) -> ProposalModel:
        """Silence never resolves a question: close it with an answer or an
        explicit deferral, and both are recorded with who and when."""
        try:
            return _proposal_model(
                inception_for().resolve(
                    project_id,
                    question_id,
                    answer=request.answer,
                    defer_reason=request.defer_reason,
                    severity=request.severity,
                    who=request.who,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/inception/{project_id}/approve",
        tags=["work"],
        summary="Approve the scope",
        response_model=ProposalModel,
        responses={409: {"description": "A blocking question is unanswered"}},
    )
    def inception_approve(
        project_id: str = PathParam(description="Project id."),
        _: None = Depends(require_token),
    ) -> ProposalModel:
        """The human gate. Refused while a **blocking** question is open;
        deferrable ones do not block."""
        try:
            return _proposal_model(inception_for().approve(project_id))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/api/inception/{project_id}/plan",
        tags=["work"],
        summary="The proposal as a PLAN.md",
        response_model=InceptionPlan,
    )
    def inception_plan(
        project_id: str = PathParam(description="Project id."),
        name: str | None = Query(None),
        _: None = Depends(require_token),
    ) -> InceptionPlan:
        """A real plan document, not queue rows.

        Writing straight to the queue would fork the pipeline into a generated
        path and a hand-written one that diverge forever. A PLAN.md runs
        through the machinery that already exists -- including the parser that
        reports what it could not read, so a proposal the harness cannot
        consume is caught before it creates a single issue.
        """
        try:
            return InceptionPlan(markdown=inception_for().plan_markdown(project_id, name))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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

    @app.post(
        "/api/audit/reconcile",
        tags=["observability"],
        summary="Pull merge and revert outcomes from GitHub",
        response_model=ReconcileResult,
    )
    def audit_reconcile(
        repo: str = Query(description="GitHub repo as `owner/name`."),
        _: None = Depends(require_token),
    ) -> ReconcileResult:
        """Record what the world did with the work.

        Everything the harness knows about quality is a proxy: a reviewer
        approved it, the checks passed. Whether it was merged, rejected or
        reverted happens outside the harness and has to be fetched.

        Append-only: a pull request merged today and reverted next week
        produces two facts, in order, both true when recorded — not one fact
        that changes its mind.
        """
        queue = app.state.queue
        mapping = items_by_pr(queue) if queue is not None else {}
        report = GitHubReconciler(repo, audit_store()).reconcile(mapping)
        return ReconcileResult(
            merged=report.merged,
            closed_unmerged=report.closed_unmerged,
            reverted=report.reverted,
            skipped=report.skipped,
            errors=report.errors,
        )

    @app.post(
        "/api/audit/maintenance",
        tags=["observability"],
        summary="Roll up and thin now",
        response_model=MaintenanceResult,
    )
    def audit_maintenance(
        retention_days: int = Query(
            DEFAULT_RETENTION_DAYS, ge=0, description="Raw events older than this may be thinned."
        ),
        _: None = Depends(require_token),
    ) -> MaintenanceResult:
        """Run a maintenance pass immediately.

        This also happens hourly in the background; the manual trigger exists
        so an operator does not have to wait an hour to see whether retention
        is working, which is exactly when they are most likely to want to know.
        """
        report = run_maintenance(audit_store(), retention_days=retention_days)
        return MaintenanceResult(
            rolled_up=report.rolled_up, thinned=report.thinned, errors=report.errors
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
                    workers=(
                        app.state.fleet.running().get(project.project_id, 0)
                        if app.state.fleet is not None
                        else 0
                    ),
                    **_worker_health(app.state.fleet, project.project_id),
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
                fixes={k: list(v) for k, v in spec.fixes.items()},
                apply_fixes=spec.apply_fixes,
                durability=spec.durability,
                max_item_seconds=spec.max_item_seconds,
                max_item_spend_usd=spec.max_item_spend_usd,
                plan_path=spec.plan_path,
                roles={k: v.model_dump() for k, v in spec.roles.items()} if spec.roles else None,
                max_workers=spec.max_workers,
                max_attempts=spec.max_attempts,
                min_free_disk_gb=spec.min_free_disk_gb,
            )
        )
        fleet_ = app.state.fleet
        if fleet_ is not None and hasattr(fleet_, "resize"):
            # A no-op for a stopped project: there is no pool to reconcile,
            # and the persisted budget is what the next start reads.
            fleet_.resize(spec.project_id)
        return _project_summary(queue, spec.project_id, app.state.fleet)

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
        # With the fleet, not without it: reading one project used to report
        # `workers: 0` unconditionally, so the single-project view contradicted
        # the list view whenever anything was actually running.
        return _project_summary(need_queue(), project_id, app.state.fleet)

    @app.post(
        "/api/projects/{project_id}/start",
        tags=["control"],
        summary="Continue execution for one project",
        response_model=ProjectSummary,
    )
    def start_project(
        project_id: str = PathParam(description="Project id."),
        force: bool = Query(
            False,
            description="Start even when preflight fails. Deliberately explicit: the "
            "checks exist because a fleet that cannot finish anything is "
            "indistinguishable from one that can, until the bill arrives.",
        ),
        check_base: bool = Query(
            False,
            description="Run configured checks on a clean base-branch worktree before "
            "starting. Expensive and therefore opt-in.",
        ),
        _: None = Depends(require_token),
    ) -> ProjectSummary:
        """The only thing that lets a project claim work.

        Nothing calls this on boot. An auto-resuming fleet turns a routine
        restart into unattended spend against a stack nobody has looked at
        yet, and a crash-looping deploy would restart the fleet on every loop.
        Resuming is a decision, so it is a request.
        """
        queue = need_queue()
        project = queue.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"no project {project_id!r}")

        report = _preflight(app.state, queue, project, check_base=check_base)
        if not report.ready and not force:
            # Refuses rather than setting a flag nobody acts on. Previously
            # this branch set RUNNING with no fleet attached -- the comment
            # right here said that "reads as working and is not", and it did
            # it anyway.
            raise HTTPException(
                status_code=409,
                detail=f"{project_id} is not ready to run — {report.summary()}. "
                "Fix these, or pass force=true to start anyway and accept that "
                "items may be unable to finish.",
            )

        fleet_ = app.state.fleet
        if fleet_ is None:
            raise HTTPException(
                status_code=409,
                detail="no worker pool is attached to this harness, so starting would "
                "mark the project running with nothing able to claim. Run the harness "
                "with a fleet, or use `agent-harness run` for a single worker.",
            )
        fleet_.start(project_id)
        return _project_summary(queue, project_id, app.state.fleet)

    @app.get(
        "/api/projects/{project_id}/preflight",
        tags=["control"],
        summary="Can this project actually finish an item?",
        response_model=PreflightResult,
    )
    def project_preflight(
        project_id: str = PathParam(description="Project id."),
        check_base: bool = Query(
            False,
            description="Run configured checks on a clean base-branch worktree. "
            "This is opt-in because it can be expensive.",
        ),
        _: None = Depends(require_token),
    ) -> PreflightResult:
        """Check before starting, and see exactly what is missing.

        A queue resumed without a reviewer, a checkout or GitHub write access
        claims work, spends the implementer's tokens, and fails every item --
        while the API reports `running`.
        """
        queue = need_queue()
        project = queue.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
        report = _preflight(app.state, queue, project, check_base=check_base)
        return PreflightResult(
            project_id=report.project_id,
            ready=report.ready,
            summary=report.summary(),
            checks=[PreflightCheck(**c.as_dict()) for c in report.checks],
        )

    @app.post(
        "/api/projects/{project_id}/preflight/base",
        tags=["control"],
        summary="Start a base-branch check run",
        response_model=BaseCheckStatus,
        responses={404: {"description": "No such project"}},
    )
    def start_base_checks(
        project_id: str = PathParam(description="Project id."),
        _: None = Depends(require_token),
    ) -> BaseCheckStatus:
        """Run the project's check list against a clean base worktree.

        Returns as soon as the run is *started*, because the run itself takes
        as long as a build and a request that waits for one is a request that
        dies at the first proxy timeout. Calling this while a run is in flight
        joins it rather than starting a second.
        """
        queue = need_queue()
        project = queue.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
        return _base_check_status(project_id, app.state.base_checks.start(project))

    @app.get(
        "/api/projects/{project_id}/preflight/base",
        tags=["control"],
        summary="The latest base-branch check run",
        response_model=BaseCheckStatus,
    )
    def base_checks_status(
        project_id: str = PathParam(description="Project id."),
        _: None = Depends(require_token),
    ) -> BaseCheckStatus:
        """Poll a run started by the POST above."""
        return _base_check_status(project_id, app.state.base_checks.status(project_id))

    @app.get(
        "/api/readiness",
        tags=["control"],
        summary="Can this harness execute anything, and why not?",
        response_model=ExecutionReadiness,
    )
    def readiness(
        project_id: str | None = Query(
            None,
            description="Limit to one project. Omit for all of them — each project costs "
            "one read of GitHub's permissions for its repo, so a large deployment "
            "polling this should name the project it cares about.",
        ),
        check_base: bool = Query(
            False,
            description="Run configured checks on clean base worktrees. "
            "Expensive and therefore opt-in.",
        ),
        _: None = Depends(require_token),
    ) -> ExecutionReadiness:
        """One read-only request that answers "could I start work right now?".

        `/healthz` cannot answer it and should not try: a monitoring-only
        deployment is perfectly healthy while being unable to run a single
        item, so a healthy service reads as an executable fleet. Until this
        existed the only way to find out was to attempt a **state-changing**
        start and read the 409.

        Nothing here writes. No worker is created, no session is started, no
        item is claimed, no control state changes, and no credential is
        echoed — the session host is probed with a read, which proves both
        reachability and that the token is accepted.

        It is not free: each routed model is asked for a one-token completion,
        because a model that is configured and does not answer is the failure
        this is for. The answer is remembered for a minute, so polling this
        does not become a load generator against the endpoint.
        """
        queue = need_queue()
        fleet_ = app.state.fleet
        host = app.state.session_host

        workers = ReadinessProbe(
            configured=fleet_ is not None,
            ok=fleet_ is not None,
            detail=(
                f"{sum(fleet_.running().values())} worker(s) running"
                if fleet_ is not None
                else "no worker pool is attached; this deployment is monitoring-only"
            ),
        )

        # Probed ONCE, then reused for every project: the answer is a property
        # of the deployment, not of the project, and asking N times would make
        # readiness cost more the more projects you have.
        probe = None
        if host is None:
            session_host_state = ReadinessProbe(
                configured=False,
                ok=False,
                detail="no session host is configured; agents cannot run as sessions",
            )
        else:
            from .preflight import session_host_probe

            ok, detail = session_host_probe(host)()
            session_host_state = ReadinessProbe(configured=True, ok=ok, detail=detail)
            probe = lambda ok=ok, detail=detail: (ok, detail)  # noqa: E731

        projects = [p for p in queue.projects() if project_id is None or p.project_id == project_id]
        if project_id is not None and not projects:
            raise HTTPException(status_code=404, detail=f"no project {project_id!r}")

        # Global first, then per project — because a project may override the
        # reviewer. Reading only the global route made this line contradict
        # the project reports underneath it: it announced that nothing could
        # be reviewed while every project routed a reviewer of its own.
        from .model_client import reviewer_independence

        preset = app.state.default_preset
        global_routes = _role_routes(queue, default_preset=preset)
        global_route = global_routes.get("reviewer")
        overriding = sorted(
            p.project_id
            for p in projects
            if (p.roles or {}).get("reviewer")
            and _role_routes(queue, p, default_preset=preset).get("reviewer")
        )
        # Not "some project can review": a reviewer nothing has is a reviewer
        # the projects without one still fail closed on.
        covered = global_route is not None or (bool(projects) and len(overriding) == len(projects))
        if global_route is not None:
            note = reviewer_independence(
                global_routes, implemented_by=_executor_roles(app.state).implemented_by
            )[1]
            reviewer_detail = f"reviewer routed to {global_route.model}; {note}"
            if overriding:
                reviewer_detail += f"; overridden by project(s): {', '.join(overriding)}"
        elif covered:
            reviewer_detail = (
                f"no global reviewer; every project routes its own: {', '.join(overriding)}"
            )
        else:
            reviewer_detail = (
                "no reviewer is routed; every review fails closed, so every item "
                "would fail after the implementation was paid for"
            )
        reviewer_state = ReadinessProbe(
            configured=global_route is not None or bool(overriding),
            ok=covered,
            detail=reviewer_detail,
        )

        reports = []
        for project in projects:
            report = _preflight(
                app.state, queue, project, session_host=probe, check_base=check_base
            )
            reports.append(
                ProjectReadiness(
                    project_id=project.project_id,
                    ready_to_start=report.ready,
                    summary=report.summary(),
                    blockers=[PreflightCheck(**c.as_dict()) for c in report.blockers],
                    warnings=[PreflightCheck(**c.as_dict()) for c in report.warnings],
                )
            )

        return ExecutionReadiness(
            mode="supervised" if fleet_ is not None else "monitoring-only",
            ready_to_start=any(r.ready_to_start for r in reports),
            workers=workers,
            session_host=session_host_state,
            reviewer=reviewer_state,
            projects=reports,
        )

    @app.post(
        "/api/projects/{project_id}/stop",
        tags=["control"],
        summary="Stop claiming for one project",
        response_model=ProjectSummary,
    )
    def stop_project(
        request: StopProjectRequest | None = None,
        project_id: str = PathParam(description="Project id."),
        _: None = Depends(require_token),
    ) -> ProjectSummary:
        """Stop taking new work. **Nothing in flight is interrupted.**"""
        queue = need_queue()
        if queue.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
        reason = request.reason if request else None
        fleet_ = app.state.fleet
        if fleet_ is not None:
            # The HTTP request only initiates the drain. The fleet joins in
            # the background: an agent stopped mid-item loses its context,
            # while a request held open for that join lies to any proxy whose
            # timeout expires first.
            if hasattr(fleet_, "request_stop"):
                fleet_.request_stop(project_id, reason=reason)
            else:  # compatibility for injected minimal fleet implementations
                fleet_.stop(project_id, reason=reason)
        else:
            queue.set_control(STOPPED, reason=reason, project_id=project_id)
        return _project_summary(queue, project_id, app.state.fleet)

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
        "/api/routes/health",
        tags=["control"],
        summary="Which endpoints and models are answering",
        response_model=RoutesHealthView,
    )
    def routes_health(_: None = Depends(require_token)) -> RoutesHealthView:
        """Reachability, retained from traffic this process already made.

        The classifier decides per call whether an endpoint said "slow down",
        "you are out of budget" or "no". That verdict picked the next retry
        and was then discarded, so the first question an operator asks — *what
        is up right now?* — could only be answered by leaving the harness and
        running `curl` by hand.

        **Nothing here probes.** Asking a model whether it answers costs money
        and can itself be rate limited; `doctor --probe-models` is where a
        deliberate, paid-for question belongs, and it reports *not asked*
        rather than passing. A route with no traffic is absent from this view,
        because unknown and healthy are different answers.

        Read `independence_possible` before trusting a review taken today: it
        is false when only one vendor is reachable, and no routing can satisfy
        the reviewer rule while that holds.
        """
        client = getattr(app.state, "model_client", None)
        routes = list(client.availability.all()) if client is not None else []
        # Counted by endpoint rather than by model. Two models from one
        # gateway are not two vendors, and treating them as such would report
        # independence that does not exist -- the precise error this exists to
        # make visible.
        vendors = len({r["endpoint"] for r in routes if r["answering"]})
        return RoutesHealthView(
            vendors_answering=vendors,
            independence_possible=vendors >= 2,
            routes=[RouteReachability(**r) for r in routes],
        )

    @app.get(
        "/api/roles",
        tags=["control"],
        summary="Where each role's calls go, and which of them run",
        response_model=RoleMapView,
    )
    def get_roles(_: None = Depends(require_token)) -> RoleMapView:
        """The map, annotated with what this deployment actually calls.

        A route nothing calls is not a harmless extra: this endpoint is how an
        operator answers "what am I paying for, and what is grading it?", and
        in session mode two of its three answers described a model that is
        never asked anything.
        """
        return _role_map_view(app.state, need_queue())

    @app.put(
        "/api/roles",
        tags=["control"],
        summary="Change the role map without a redeploy",
        response_model=RoleMapView,
    )
    def set_roles(request: RoleMap, _: None = Depends(require_token)) -> RoleMapView:
        """Takes effect on the next model call.

        This is possible only because a call site names a **role**, never a
        model. Routing a role somewhere else is then a data change rather than
        a code change — which is what lets you move the implementer to a
        cheaper tier, or the reviewer to a different vendor, while the fleet
        is running.

        The response says which of the roles just stored are actually called.
        Storing one that is not still succeeds — the non-session executor uses
        it, and so may a later deployment — but it changes nothing about what
        runs here, and echoing it back unqualified said otherwise.

        A reviewer on the same vendor as the implementer means some share of
        reviews is a model grading its own work. Nothing here enforces that;
        it is your call, and it is worth making deliberately.
        """
        queue = need_queue()
        queue.set_setting(
            ROLE_MAP_KEY,
            # Only the routing fields. `used` is computed from the deployment,
            # not configured, and storing it would let a stale answer be read
            # back later as though an operator had set it.
            {
                name: route.model_dump(include={"model", "endpoint", "provider"})
                for name, route in request.roles.items()
            },
        )
        return _role_map_view(app.state, queue)

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
        report = parsed.dependency_report()
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
            unresolved_dependencies=report.unresolved,
            external_dependencies=report.external,
            decision_dependencies=report.decisions,
            cross_project_dependencies=report.cross_project,
            malformed_dependencies=report.malformed,
            dependency_cycles=[list(cycle) for cycle in report.cycles],
            unattached_arrows=[f"line {n}: {text}" for n, text in report.unattached_arrows],
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
        error_store = app.state.audit if app.state.audit is not None else store
        by_class = error_store.rate_limits_by_class(since)
        classified = {c: by_class.get(c, 0) for c in RATE_LIMIT_CLASSES}
        return RateLimits(
            window=window,
            classified=classified,
            meaning={c: MEANING[c] for c in RATE_LIMIT_CLASSES},
            unclassified=by_class.get(UNCLASSIFIED, 0),
            total=sum(classified.values()),
            by_worker=error_store.group_counts("worker", since),
            by_endpoint=error_store.group_counts("endpoint", since),
            by_role=error_store.group_counts("role", since),
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
        # Deliberately NOT swept first, unlike `/api/holds`. Sweeping here
        # would expire the very holds this is meant to show and the field
        # would read empty for ever — the status line looking healthy while
        # an item waits is the thing #188 is about.
        now = queue.now() if queue else time.time()
        overdue = queue.holds.due(now) if queue else []
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
            holds_open=len(queue.holds.open_holds()) if queue else 0,
            holds_overdue=[
                OverdueHold(
                    project_id=hold.project_id,
                    item_id=hold.item_id,
                    question=hold.question,
                    age_seconds=round(hold.age(now), 1),
                    overdue_seconds=round(max(0.0, now - hold.expires_at), 1),
                    session_url=hold.session_url,
                )
                for hold in overdue
            ],
        )

    return app


def _latest_by_item(
    store: EventStore, audit: Any | None = None, project_id: str | None = None
) -> dict[str, dict[str, Any]]:
    """Newest event per work item. One scan — doing it per item would be a
    query per row.

    The audit store comes first when there is one. Under `serve` it is the
    only sink the fleet writes to: nothing ingests `events.jsonl` into the
    `EventStore`, so reading that alone reported `latest: null` for every item
    forever, no matter how much work ran. The ingest store remains the source
    for a harness run as a plain `ingest` + `serve` pair over log files.

    Scoped to one project. `(project_id, item_id)` is an item's identity and
    two projects each having a `T1` is explicitly supported, so keying on the
    item id alone showed one project's newest event as the other's `latest` —
    including its session deep link.
    """
    if audit is not None:
        rows = audit.latest_by_item(project_id=project_id)
        if rows:
            return {
                item_id: {**row, "data": json.loads(row.get("data") or "{}")}
                for item_id, row in rows.items()
            }
    latest: dict[str, dict[str, Any]] = {}
    for event in store.recent(kind="work", limit=2000):
        data = event["data"]
        item_id = data.get("item_id")
        if project_id is not None and data.get("project_id") not in (None, project_id):
            continue
        if item_id and item_id not in latest:
            latest[item_id] = event
    return latest


def _base_check_status(project_id: str, run: Any | None) -> BaseCheckStatus:
    """A base-check run as the wire model. `None` is a real state, not a 404:
    "nobody has asked yet" is the answer to what a fresh process knows."""
    if run is None:
        return BaseCheckStatus(project_id=project_id, state="not_run")
    return BaseCheckStatus(
        project_id=run.project_id,
        state=run.state,
        ok=run.ok,
        detail=run.detail,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


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


def _role_routes(
    queue: WorkQueue, project: Project | None = None, *, default_preset: str = ""
) -> dict[str, Any]:
    """The routes a project will actually use.

    The global map, with that project's own overrides applied **per role**.
    Every reader goes through here because they used to disagree: preflight
    took the project map *or* the global one and never merged them, while
    readiness read only the global one — so a project could pass preflight on
    its own reviewer override and then be executed against a different model,
    or refused for the absence of a global route it does not need.

    No API key is attached: these routes are for reading and reporting, and
    the transport supplies the credential when a call is actually made.

    `default_preset` must be this deployment's, because readiness *probes* these
    routes. A report built with a different wire protocol from the one the
    workers use would ask a different URL and answer a different question.
    """
    from .model_client import effective_routes, routes_from_map

    return effective_routes(
        routes_from_map(queue.get_setting(ROLE_MAP_KEY) or {}, default_preset=default_preset),
        routes_from_map(project.roles or {}, default_preset=default_preset)
        if project is not None
        else {},
    )


def _executor_roles(state: Any) -> Any:
    """What the attached executor can reach. Everything, unless told otherwise."""
    from .runtime import ExecutorRoles

    roles = getattr(state, "executor_roles", None)
    return roles if roles is not None else ExecutorRoles()


def _model_asker(client: Any) -> Any:
    """The thing preflight uses to ask a model whether it is there.

    Remembered briefly: readiness is polled, and a probe on every poll would
    make a dashboard a load generator against the model endpoint.
    """
    if client is None or not hasattr(client, "answers"):
        return None
    from .preflight import Answer, remembered

    def ask(route: Any) -> Any:
        started = time.time()
        ok, detail = client.answers(route, timeout=MODEL_PROBE_PATIENCE)
        return Answer(ok=ok, detail=detail, seconds=time.time() - started)

    return remembered(ask)


def _role_map_view(state: Any, queue: WorkQueue) -> RoleMapView:
    """The global map, annotated with what this deployment will call."""
    from .model_client import reviewer_independence

    stored = queue.get_setting(ROLE_MAP_KEY) or {}
    executor = _executor_roles(state)
    independent, why = reviewer_independence(
        _role_routes(queue, default_preset=getattr(state, "default_preset", "")),
        implemented_by=executor.implemented_by,
    )
    return RoleMapView(
        reviewer_independent=independent,
        reviewer_note=why,
        roles={
            name: RoutedRole(
                **RoleRoute(**route).model_dump(),
                used=executor.calls_role(name),
                unused_reason=executor.unused_reason(name),
            )
            for name, route in stored.items()
        },
    )


def _preflight(
    state: Any,
    queue: WorkQueue,
    project: Project,
    session_host: Any | None = None,
    *,
    check_base: bool = False,
) -> Any:
    """Build a preflight report for a project, using whatever is configured.

    `session_host` is a *probe*, not the host: readiness over many projects
    would otherwise ask the same host the same question once per project.
    """
    from .model_client import reviewer_independence
    from .preflight import (
        last_base_result_probe,
        preflight_project,
        role_reachability_probe,
        session_host_probe,
    )

    routes = _role_routes(queue, project, default_preset=getattr(state, "default_preset", ""))
    executor = _executor_roles(state)
    ask = getattr(state, "ask_model", None)
    host = getattr(state, "session_host", None)
    # Only the roles this executor calls. Probing a route nothing will ever
    # use spends tokens to answer a question about it, and could refuse a
    # start over a model no item depends on.
    reachable = {name: route for name, route in routes.items() if executor.calls_role(name)}
    kwargs: dict[str, Any] = {
        "has_fleet": getattr(state, "fleet", None) is not None,
        "reviewer_route": routes.get("reviewer"),
        "reviewer_independent": reviewer_independence(
            routes, implemented_by=executor.implemented_by
        ),
        "role_probe": (
            role_reachability_probe(
                reachable,
                ask,
                timeout=MODEL_PROBE_TIMEOUT,
                patience=MODEL_PROBE_PATIENCE,
            )
            if ask is not None and reachable
            else None
        ),
        "session_host": session_host or (session_host_probe(host) if host is not None else None),
        "checks_probe": (
            last_base_result_probe(state.base_checks, project.project_id)
            if check_base and getattr(state, "base_checks", None) is not None
            else None
        ),
    }
    selected_runner = str(queue.get_setting("role_runner") or "")
    if selected_runner:
        from .role_runners import probe as runner_probe

        kwargs["role_runner"] = lambda: runner_probe(selected_runner)
    # Injected probes win, so a test can answer any of these without a
    # network, a subprocess or a model.
    kwargs.update(getattr(state, "probes", None) or {})
    return preflight_project(project, **kwargs)


def _project_spec(project: Project) -> ProjectSpec:
    return ProjectSpec(
        project_id=project.project_id,
        name=project.name,
        repo=project.repo,
        work_dir=project.work_dir,
        base_branch=project.base_branch,
        checks=list(project.checks),
        fixes={k: list(v) for k, v in (project.fixes or {}).items()},
        apply_fixes=bool(project.apply_fixes),
        durability=project.durability,
        max_item_seconds=project.max_item_seconds,
        max_item_spend_usd=project.max_item_spend_usd,
        plan_path=project.plan_path,
        roles={k: RoleRoute(**v) for k, v in project.roles.items()} if project.roles else None,
        max_workers=project.max_workers,
        max_attempts=project.max_attempts,
        min_free_disk_gb=project.min_free_disk_gb,
    )


def _project_summary(queue: WorkQueue, project_id: str, fleet: Any | None = None) -> ProjectSummary:
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
        workers=(fleet.running().get(project_id, 0) if fleet is not None else 0),
        draining_items=(
            [item.item_id for item in queue.items(project_id=project_id) if item.state == CLAIMED]
            if state == DRAINING
            else []
        ),
        **_worker_health(fleet, project_id),
    )


def _worker_health(fleet: Any | None, project_id: str) -> dict[str, Any]:
    """What the fleet knows about workers that died on this project."""
    if fleet is None or not hasattr(fleet, "failures"):
        return {}
    failures = fleet.failures(project_id)
    return {
        "worker_failures": len(failures),
        "last_worker_error": failures[-1].error if failures else None,
    }


def _reason_model(reason: Any) -> ReadinessReasonModel:
    return ReadinessReasonModel(
        kind=reason.kind,
        explanation=reason.explanation,
        target_kind=reason.target_kind,
        target_id=reason.target_id,
        required=reason.required,
        resolver=reason.resolver,
        state=reason.state,
        evidence=reason.evidence,
    )


def _readiness_model(state: Any, record: WorkRecord | None) -> ItemReadiness:
    return ItemReadiness(
        project_id=state.project_id,
        item_id=state.item_id,
        ready=state.ready,
        graph_revision=state.revision,
        admitted_revision=record.admitted_revision if record is not None else None,
        reasons=[_reason_model(reason) for reason in state.reasons],
        advisory=[_reason_model(reason) for reason in state.advisory],
        overridden=state.overridden,
        override_reason=state.override_reason,
        explanation=state.explain(),
    )


def _item_model(
    record: WorkRecord,
    event: dict[str, Any] | None,
    hold: Any | None = None,
    now: float | None = None,
) -> WorkItem:
    now = time.time() if now is None else now
    return WorkItem(
        hold=HoldView(**hold.as_dict(now)) if hold is not None else None,
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
        # The queue stores one "why" per item. Which of the two it is depends
        # entirely on the state, and a client should not have to know that.
        blocked_reason=record.last_error if record.state == BLOCKED else None,
        budget_seconds=record.budget_seconds,
        budget_spend_usd=record.budget_spend_usd,
        spend_usd=record.spend_usd,
        unpriced_calls=record.unpriced_calls,
        first_started_at=record.first_started_at,
        held_until=record.held_until,
        disposition=record.disposition,
        reason_kind=record.reason_kind,
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
