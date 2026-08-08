"""Typed read services shared by JSON and browser controllers.

Presentation code must not know which SQLite file owns a fact. This module is
the narrow read boundary over the event, audit and queue stores; it returns the
same Pydantic models the public API publishes, so HTML cannot quietly invent a
second interpretation of project or work state.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .events import MODEL_CALL, RATE_LIMIT_CLASSES, UNCLASSIFIED
from .process_metrics import ProcessMetricsSource
from .project_service import project_spec
from .providers import MEANING
from .schemas import (
    AbandonedSessionEvidence,
    AnalyticsDashboard,
    AttemptStageEvidence,
    AuditCost,
    AuditCostRow,
    AuditDelivery,
    AuditDeliveryRow,
    AuditHealth,
    AuditRollupRow,
    AuditRollups,
    Baseline,
    BaselineList,
    DependencyEdgeModel,
    DependencyGraphReport,
    Event,
    EventFilters,
    EventPage,
    FleetControl,
    GateEvidence,
    GatewayLog,
    GatewayLogPage,
    HoldList,
    HoldView,
    ItemReadiness,
    LatestEvent,
    OpenQuestion,
    PlanParseResult,
    PlanPromotionEvidence,
    ProcessMetrics,
    ProjectList,
    ProjectSummary,
    ProposalModel,
    RateLimits,
    ReadinessReasonModel,
    RemoteReviewEvidence,
    RunnerProgressEvidence,
    WorkerInventory,
    WorkerInventoryItem,
    WorkEvidence,
    WorkItem,
    WorkList,
)
from .store import EventStore
from .work import BLOCKED, CLAIMED, DRAINING, HELD, WorkQueue, WorkRecord

ANALYTICS_WINDOWS: dict[str, float | None] = {
    "1h": 3600,
    "24h": 86400,
    "72h": 3 * 86400,
    "7d": 7 * 86400,
    "all": None,
}


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _redacted_text(value: Any, redact: Any) -> str | None:
    """Defence-in-depth at projection time; omit text if the filter fails."""
    if value is None:
        return None
    try:
        clean = redact(str(value))
    except Exception:  # noqa: BLE001 - returning the raw value would expose it
        return None
    return None if clean is None else str(clean)


class HarnessQueries:
    """Read the control plane without exposing storage layout to controllers."""

    def __init__(
        self,
        store: EventStore,
        queue: WorkQueue | None,
        *,
        audit: Any | None = None,
        fleet: Any | None = None,
        process_metrics: ProcessMetricsSource | None = None,
    ) -> None:
        self.store = store
        self.queue = queue
        self.audit = audit
        self.fleet = fleet
        self.process_metrics_source = process_metrics

    def projects(self) -> ProjectList:
        if self.queue is None:
            return ProjectList(projects=[])
        return ProjectList(
            projects=[
                summary
                for project in self.queue.projects()
                if (summary := self.project(project.project_id)) is not None
            ]
        )

    def project(self, project_id: str) -> ProjectSummary | None:
        queue = self.queue
        if queue is None:
            return None
        project = queue.get_project(project_id)
        if project is None:
            return None
        state, reason, previous = queue.control_detail(project_id)
        worker_health = self._worker_health(project_id)
        return ProjectSummary(
            project=project_spec(project),
            counts=queue.counts(project_id=project_id),
            control=FleetControl(state=state, reason=reason),
            previous_state=previous,
            stale=len(queue.stale(project_id=project_id)),
            workers=(self.fleet.running().get(project_id, 0) if self.fleet is not None else 0),
            draining_items=(
                [
                    item.item_id
                    for item in queue.items(project_id=project_id)
                    if item.state == CLAIMED
                ]
                if state == DRAINING
                else []
            ),
            **worker_health,
        )

    def work(self, project_id: str | None = None) -> WorkList:
        queue = self.queue
        if queue is None:
            return WorkList(
                configured=False,
                reason="no work queue is attached to this harness",
            )
        latest = self._latest_by_item(project_id)
        return WorkList(
            configured=True,
            counts=queue.counts(project_id=project_id),
            stale=[record.item_id for record in queue.stale(project_id=project_id)],
            items=[
                self._item_model(
                    record,
                    latest.get(record.item_id),
                    queue.holds.current(record.project_id, record.item_id)
                    if record.state == HELD
                    else None,
                    queue.now(),
                )
                for record in queue.items(project_id=project_id)
            ],
        )

    def item(self, project_id: str, item_id: str) -> WorkItem | None:
        queue = self.queue
        if queue is None:
            return None
        record = queue.get(item_id, project_id=project_id)
        if record is None:
            return None
        return self._item_model(
            record,
            self._latest_by_item(project_id).get(item_id),
            queue.holds.current(project_id, item_id) if record.state == HELD else None,
            queue.now(),
        )

    def holds(self, project_id: str | None = None) -> HoldList:
        queue = self.queue
        if queue is None:
            return HoldList()
        queue.expire_holds()
        now = queue.now()
        return HoldList(
            open=[HoldView(**hold.as_dict(now)) for hold in queue.holds.open_holds(project_id)]
        )

    def inception(self, project_id: str) -> ProposalModel | None:
        """Return the current inception proposal through the typed read boundary."""
        if self.queue is None:
            return None
        from .inception import Inception

        proposal = Inception(self.queue).current(project_id)
        if proposal is None:
            return None
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
                    id=question.id,
                    question=question.question,
                    severity=question.severity,
                    why_it_matters=question.why_it_matters,
                    answer=question.answer,
                    deferred_reason=question.deferred_reason,
                    resolved_by=question.resolved_by,
                )
                for question in proposal.questions
            ],
            feedback=proposal.feedback,
            item_count=proposal.item_count(),
            blocking_open=len(proposal.blocking_open()),
        )

    def inception_plan(self, project_id: str, name: str | None = None) -> str | None:
        """Render the current proposal without creating queue rows or files."""
        if self.queue is None:
            return None
        from .inception import Inception

        try:
            return Inception(self.queue).plan_markdown(project_id, name)
        except ValueError:
            return None

    @staticmethod
    def plan_parse_markdown(markdown: str) -> PlanParseResult:
        """Parse a document using the same loss-reporting parser as the API."""
        from .plan import parse_plan
        from .plan_service import parse_result

        return parse_result(parse_plan(markdown))

    def plan_parse(self, path: str) -> PlanParseResult | None:
        """Read and parse a configured plan path, without writing anything."""
        target = Path(path)
        if not target.is_file():
            return None
        return self.plan_parse_markdown(target.read_text(encoding="utf-8"))

    def graph(self, project_id: str) -> DependencyGraphReport | None:
        if self.queue is None or self.queue.get_project(project_id) is None:
            return None
        report = self.queue.graph.report(project_id)
        records = {record.item_id: record for record in self.queue.items(project_id=project_id)}
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
                self._readiness_model(state, records.get(state.item_id))
                for state in report.not_ready
            ],
        )

    def overrides(self, project_id: str) -> list[dict[str, Any]]:
        """Revision-scoped dependency decisions for the graph audit panel."""
        if self.queue is None or self.queue.get_project(project_id) is None:
            return []
        return self.queue.graph.overrides(project_id)

    @staticmethod
    def _readiness_model(state: Any, record: WorkRecord | None) -> ItemReadiness:
        return ItemReadiness(
            project_id=state.project_id,
            item_id=state.item_id,
            ready=state.ready,
            graph_revision=state.revision,
            admitted_revision=record.admitted_revision if record is not None else None,
            reasons=[ReadinessReasonModel(**reason.__dict__) for reason in state.reasons],
            advisory=[ReadinessReasonModel(**reason.__dict__) for reason in state.advisory],
            overridden=state.overridden,
            override_reason=state.override_reason,
            explanation=state.explain(),
        )

    def events(self, since_id: int = 0, limit: int = 200) -> EventPage:
        return self.filtered_events(since_id=since_id, limit=limit)

    def filtered_events(
        self,
        since_id: int = 0,
        limit: int = 200,
        filters: EventFilters | None = None,
        *,
        live: bool = False,
    ) -> EventPage:
        """Read filtered history without changing monotonic cursor meaning.

        Filtering happens after the exclusive id cursor. The reader keeps
        scanning ordered chunks until it has enough matches, so a sparse
        filter cannot cause matching events to disappear at a page boundary.
        """
        filters = filters or EventFilters()
        source = self.audit if live and self.audit is not None else self.store
        cursor = since_id
        matched: list[dict[str, Any]] = []
        while len(matched) < limit:
            rows = source.since_id(cursor, limit=min(1000, max(limit, 200)))
            if not rows:
                break
            for row in rows:
                cursor = int(row["id"])
                event = self._audit_event_fields(row) if source is self.audit else row
                if self._event_matches(event, filters):
                    matched.append(event)
                    if len(matched) >= limit:
                        break
            if len(rows) < min(1000, max(limit, 200)):
                break
        return EventPage(
            events=[Event(**row) for row in matched],
            # Advance over scanned non-matches. A cursor is a position in the
            # append-only stream, not the id of the last displayed row.
            cursor=cursor,
        )

    def live_events(self, since_id: int = 0, limit: int = 200) -> EventPage:
        """The event source the running deployment actually writes.

        Under supervised `serve`, the audit store is the live sink. A plain
        ingest-and-serve deployment has only the legacy event store. The
        fallback is explicit and preserves each store's monotonic cursor.
        """
        return self.filtered_events(since_id, limit, live=True)

    def process_metrics(self) -> ProcessMetrics:
        """Observe this service process without consulting session-host state."""
        source = self.process_metrics_source
        if source is None:
            raise RuntimeError("no process metrics source is attached")
        sample = source.sample()
        fleet = self.fleet
        if fleet is None:
            active_workers = 0
        elif hasattr(fleet, "workers"):
            active_workers = len(fleet.workers())
        else:
            active_workers = sum(fleet.running().values())
        return ProcessMetrics(
            sampled_at=sample.sampled_at,
            started_at=sample.started_at,
            uptime_seconds=sample.uptime_seconds,
            pid=sample.pid,
            thread_count=sample.thread_count,
            cpu_seconds=sample.cpu_seconds,
            mode="supervised" if fleet is not None else "monitoring-only",
            active_workers=active_workers,
        )

    def gateway_logs(
        self,
        since_id: int = 0,
        limit: int = 200,
        *,
        project_id: str | None = None,
    ) -> GatewayLogPage:
        """Project model calls from the live redacted event source.

        Model answer bodies and arbitrary event data are intentionally not in
        the schema. The audit store is the live source when attached; a plain
        ingest-and-serve deployment falls back to its event store. Neither
        route asks a session host where it writes files.
        """
        from .routing_service import safe_endpoint

        source = self.audit if self.audit is not None else self.store
        source_name = "live_audit" if self.audit is not None else "ingested_events"
        degraded = bool(getattr(source, "degraded", False))
        cursor = since_id
        matched: list[GatewayLog] = []
        chunk_size = min(1000, max(limit, 200))
        while not degraded and len(matched) < limit:
            rows = source.since_id(cursor, limit=chunk_size)
            if not rows:
                break
            for raw in rows:
                cursor = int(raw["id"])
                row = self._audit_event_fields(raw) if source is self.audit else raw
                if row.get("kind") != MODEL_CALL:
                    continue
                data = row.get("data") or {}
                if project_id is not None and str(data.get("project_id") or "") != project_id:
                    continue
                endpoint = row.get("endpoint")
                redact = source.redact
                detail = _redacted_text(data.get("detail"), redact)
                if detail is not None:
                    detail = detail[:2000]
                clean_endpoint = _redacted_text(endpoint, redact)
                matched.append(
                    GatewayLog(
                        id=cursor,
                        ts=float(row["ts"]),
                        project_id=_redacted_text(data.get("project_id"), redact),
                        item_id=_redacted_text(data.get("item_id"), redact),
                        worker=_redacted_text(row.get("worker"), redact),
                        role=_redacted_text(row.get("role"), redact),
                        model=_redacted_text(row.get("model"), redact),
                        endpoint=(
                            safe_endpoint(clean_endpoint) if clean_endpoint is not None else None
                        ),
                        outcome=_redacted_text(row.get("outcome"), redact),
                        error_class=_redacted_text(row.get("error_class"), redact),
                        latency_s=row.get("latency_s"),
                        attempt=_optional_int(data.get("attempt")),
                        detail=detail,
                    )
                )
                if len(matched) >= limit:
                    break
            if len(rows) < chunk_size:
                break
        return GatewayLogPage(
            configured=not degraded,
            degraded=degraded,
            source=source_name,
            logs=matched,
            cursor=cursor,
        )

    @staticmethod
    def _event_matches(event: dict[str, Any], filters: EventFilters) -> bool:
        data = event.get("data") or {}

        def value(name: str) -> Any:
            return event.get(name) if event.get(name) is not None else data.get(name)

        for name in (
            "project_id",
            "item_id",
            "worker",
            "endpoint",
            "role",
            "model",
            "outcome",
            "error_class",
            "reason_kind",
        ):
            expected = getattr(filters, name)
            if expected is not None and str(value(name) or "") != expected:
                return False
        ts = float(event.get("ts", 0.0))
        if filters.start_ts is not None and ts < filters.start_ts:
            return False
        return not (filters.end_ts is not None and ts > filters.end_ts)

    def worker_inventory(self, project_id: str | None = None) -> WorkerInventory:
        """Project runtime, durable claims, failures and session evidence.

        The queue remains authoritative for claims and the fleet remains
        authoritative for live threads. This method only joins their read
        projections; it never creates a worker record or infers supervision
        from a claimed row in a monitoring-only deployment.
        """
        fleet = self.fleet
        if fleet is None:
            return WorkerInventory(
                configured=False,
                mode="monitoring-only",
                reason="no worker pool is attached; this deployment is monitoring-only",
            )
        queue = self.queue
        if queue is None:
            return WorkerInventory(
                configured=True,
                mode="supervised",
                reason="worker pool is attached but no work queue is configured",
            )
        snapshots = list(fleet.workers(project_id)) if hasattr(fleet, "workers") else []
        claims = queue.claimed(project_id=project_id)
        sessions = queue.abandoned_sessions()
        by_item: dict[str, list[AbandonedSessionEvidence]] = {}
        for row in sessions:
            row_project = row.get("project_id")
            if project_id is not None and row_project != project_id:
                continue
            by_item.setdefault(str(row["item_id"]), []).append(AbandonedSessionEvidence(**row))
        latest = self._latest_by_item(project_id)
        used_claims: set[int] = set()
        items: list[WorkerInventoryItem] = []
        now = queue.now()

        def row_for_claim(
            record: WorkRecord, worker_id: str, started_at: float | None
        ) -> WorkerInventoryItem:
            event = latest.get(record.item_id) or {}
            data = event.get("data") or {}
            return WorkerInventoryItem(
                worker_id=worker_id,
                project_id=record.project_id,
                state="stale_claim" if record.lease_until < now else "running",
                claim_owner=record.owner,
                item_id=record.item_id,
                lease_until=record.lease_until,
                heartbeat_at=record.updated_at,
                stage=event.get("outcome") or data.get("stage"),
                started_at=started_at,
                item_started_at=record.first_started_at or None,
                abandoned_sessions=by_item.get(record.item_id, []),
            )

        for snapshot in snapshots:
            match = next(
                (
                    (index, record)
                    for index, record in enumerate(claims)
                    if index not in used_claims and record.owner == snapshot.claim_owner
                ),
                None,
            )
            if match is None:
                items.append(
                    WorkerInventoryItem(
                        worker_id=snapshot.worker_id,
                        project_id=snapshot.project_id,
                        state="running",
                        claim_owner=snapshot.claim_owner,
                        started_at=snapshot.started_at,
                    )
                )
                continue
            index, record = match
            used_claims.add(index)
            items.append(row_for_claim(record, snapshot.worker_id, snapshot.started_at))

        for index, record in enumerate(claims):
            if index in used_claims:
                continue
            items.append(row_for_claim(record, record.owner or "unknown", None))

        failures = fleet.failures(project_id)
        for failure in failures:
            items.append(
                WorkerInventoryItem(
                    worker_id=failure.worker or "unknown",
                    project_id=failure.project_id,
                    state="failed",
                    claim_owner=failure.worker,
                    failure=failure.error,
                    failed_at=failure.at,
                    abandoned_sessions=[
                        session
                        for item_id in failure.released
                        for session in by_item.get(item_id, [])
                    ],
                )
            )
        return WorkerInventory(configured=True, mode="supervised", workers=items)

    def analytics(self, window: str = "7d", project_id: str | None = None) -> AnalyticsDashboard:
        """Build the complete analytics projection for the browser client.

        Audit is optional by design. When it is absent, rate limits can still
        use the live event store, while spend, delivery, baselines and rollups
        remain explicitly empty and the health model tells the operator why.
        """
        if window not in ANALYTICS_WINDOWS:
            raise ValueError(
                f"unknown window {window!r}; expected one of {sorted(ANALYTICS_WINDOWS)}"
            )
        span = ANALYTICS_WINDOWS[window]
        since = None if span is None else time.time() - span
        audit = self.audit
        source = audit if audit is not None else self.store
        oldest, newest = source.span()
        partial = bool(since is not None and oldest is not None and oldest > since)
        by_class = source.rate_limits_by_class(since)
        classified = {name: by_class.get(name, 0) for name in RATE_LIMIT_CLASSES}
        rate_limits = RateLimits(
            window=window,
            classified=classified,
            meaning={name: MEANING[name] for name in RATE_LIMIT_CLASSES},
            unclassified=by_class.get(UNCLASSIFIED, 0),
            total=sum(classified.values()),
            denominator=(
                source.rate_limit_denominator(since)
                if hasattr(source, "rate_limit_denominator")
                else sum(by_class.values())
            ),
            by_worker=source.group_counts("worker", since),
            by_endpoint=source.group_counts("endpoint", since),
            by_role=source.group_counts("role", since),
        )
        if audit is None:
            cost = AuditCost(window=window, partial=partial)
            delivery = AuditDelivery(window=window, partial=partial)
            baselines = BaselineList()
            rollups = AuditRollups()
            health = AuditHealth(configured=False, degraded=True, events=0)
        else:
            cost_rows = [
                AuditCostRow(**row) for row in audit.cost(since=since, project_id=project_id)
            ]
            priced = [row.cost_usd for row in cost_rows if row.cost_usd is not None]
            cost = AuditCost(
                window=window,
                rows=cost_rows,
                total_cost_usd=sum(priced) if priced else None,
                total_unpriced=sum(row.unpriced for row in cost_rows),
                denominator=sum(row.calls for row in cost_rows),
                partial=partial,
            )
            delivery_rows = [
                AuditDeliveryRow(**row)
                for row in audit.delivery(since=since, project_id=project_id)
            ]
            delivery = AuditDelivery(
                window=window,
                rows=delivery_rows,
                denominator=audit.delivery_denominator(since=since, project_id=project_id),
                partial=partial,
            )
            baselines = BaselineList(
                baselines=[Baseline(**row) for row in audit.baselines(project_id=project_id)]
            )
            rollups = AuditRollups(
                rows=[AuditRollupRow(**row) for row in audit.rollups(project_id=project_id)],
                rolled_up_through=audit.rolled_up_through(),
            )
            health = AuditHealth(
                configured=True,
                degraded=audit.degraded,
                path=str(audit.path),
                events=audit.count(),
                oldest=oldest,
                newest=newest,
                schema_version=getattr(audit, "SCHEMA_VERSION", None),
            )
        return AnalyticsDashboard(
            window=window,
            project_id=project_id,
            rate_limits=rate_limits,
            cost=cost,
            delivery=delivery,
            audit_health=health,
            baselines=baselines,
            rollups=rollups,
        )

    def evidence(self, project_id: str, item_id: str) -> WorkEvidence | None:
        queue = self.queue
        if queue is None or queue.get(item_id, project_id=project_id) is None:
            return None
        if self.audit is not None:
            event_rows = self.audit.item_events(project_id, item_id)
            events = [Event(**self._audit_event_fields(row)) for row in event_rows]
        else:
            events = [Event(**row) for row in self.store.item_events(project_id, item_id)]
        stages = [
            AttemptStageEvidence(
                attempt=attempt,
                stage=stage.stage,
                admitted_revision=stage.admitted_revision,
                mode=stage.mode,
                recorded_at=stage.recorded_at,
                artefact=dict(stage.artefact),
            )
            for attempt, stage in queue.attempts_log.history(project_id, item_id)
        ]
        holds = [
            HoldView(**hold.as_dict(queue.now()))
            for hold in queue.holds.history(project_id, item_id)
        ]
        runner_progress, gates, promotions, remote_reviews = self._typed_item_events(events)
        return WorkEvidence(
            project_id=project_id,
            item_id=item_id,
            events=events,
            stages=stages,
            holds=holds,
            runner_progress=runner_progress,
            gates=gates,
            promotions=promotions,
            remote_reviews=remote_reviews,
        )

    @staticmethod
    def _typed_item_events(
        events: list[Event],
    ) -> tuple[
        list[RunnerProgressEvidence],
        list[GateEvidence],
        list[PlanPromotionEvidence],
        list[RemoteReviewEvidence],
    ]:
        progress: list[RunnerProgressEvidence] = []
        gates: list[GateEvidence] = []
        promotions: list[PlanPromotionEvidence] = []
        reviews: list[RemoteReviewEvidence] = []
        gate_stages = {"checks_passed", "checks_failed", "fix_available"}
        promotion_stages = {
            "plan_promotion",
            "plan_promoted",
            "plan_promotion_conflict",
            "plan_promotion_deferred",
        }

        def text(value: Any) -> str | None:
            return None if value is None else str(value)

        def argv(value: Any) -> list[str]:
            return [str(part) for part in value] if isinstance(value, list) else []

        for event in events:
            data = event.data
            raw_evidence = data.get("evidence")
            evidence = raw_evidence if isinstance(raw_evidence, dict) else {}
            if event.outcome in gate_stages:
                command = argv(evidence.get("command"))
                if not command:
                    command = argv(data.get("command"))
                commands_raw = evidence.get("commands")
                commands = (
                    [argv(one) for one in commands_raw] if isinstance(commands_raw, list) else []
                )
                if not commands and command:
                    commands = [command]
                applied = evidence.get("applied")
                applied_fixes = (
                    [dict(one) for one in applied if isinstance(one, dict)]
                    if isinstance(applied, list)
                    else []
                )
                gates.append(
                    GateEvidence(
                        event_id=event.id,
                        outcome=str(evidence.get("outcome") or event.outcome),
                        ts=event.ts,
                        detail=text(data.get("detail") or evidence.get("detail")),
                        command=command,
                        commands=commands,
                        fix=argv(evidence.get("fix") or evidence.get("fix_declared")),
                        applied=applied_fixes,
                    )
                )
                continue
            if event.outcome in promotion_stages:
                promotions.append(
                    PlanPromotionEvidence(
                        event_id=event.id,
                        ts=event.ts,
                        status=str(data.get("status") or event.outcome),
                        plan_branch=text(data.get("plan_branch")),
                        base_sha=text(data.get("base_sha")),
                        item_sha=text(data.get("item_sha")),
                        old_head_sha=text(data.get("old_head_sha")),
                        new_head_sha=text(data.get("new_head_sha")),
                        target_sha=text(data.get("target_sha")),
                        detail=text(data.get("detail")),
                    )
                )
                continue
            if event.outcome == "remote_review_received":
                reviews.append(
                    RemoteReviewEvidence(
                        event_id=event.id,
                        ts=event.ts,
                        source=str(data.get("source") or event.source),
                        remote_id=str(data.get("remote_id") or ""),
                        disposition=str(data.get("disposition") or ""),
                        status=str(data.get("status") or ""),
                        duplicate=bool(data.get("duplicate", False)),
                        correction_item_id=text(data.get("correction_item_id")),
                        detail=text(data.get("detail")),
                    )
                )
                continue
            progress.append(
                RunnerProgressEvidence(
                    event_id=event.id,
                    stage=str(event.outcome or event.kind),
                    ts=event.ts,
                    detail=text(data.get("detail")),
                    worker=event.worker,
                    attempt=_optional_int(data.get("attempt")),
                    evidence=dict(evidence),
                )
            )
        return progress, gates, promotions, reviews

    def _latest_by_item(self, project_id: str | None = None) -> dict[str, dict[str, Any]]:
        if self.audit is not None:
            rows = self.audit.latest_by_item(project_id=project_id)
            if rows:
                return {
                    item_id: {**row, "data": json.loads(row.get("data") or "{}")}
                    for item_id, row in rows.items()
                }
        latest: dict[str, dict[str, Any]] = {}
        for event in self.store.recent(kind="work", limit=2000):
            data = event["data"]
            item_id = data.get("item_id")
            if project_id is not None and data.get("project_id") not in (None, project_id):
                continue
            if item_id and item_id not in latest:
                latest[item_id] = event
        return latest

    def _worker_health(self, project_id: str) -> dict[str, Any]:
        if self.fleet is None or not hasattr(self.fleet, "failures"):
            return {}
        failures = self.fleet.failures(project_id)
        return {
            "worker_failures": len(failures),
            "last_worker_error": failures[-1].error if failures else None,
        }

    @staticmethod
    def _audit_event_fields(row: dict[str, Any]) -> dict[str, Any]:
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

    @staticmethod
    def _item_model(
        record: WorkRecord,
        event: dict[str, Any] | None,
        hold: Any | None = None,
        now: float | None = None,
    ) -> WorkItem:
        now = time.time() if now is None else now
        return WorkItem(
            project_id=record.project_id,
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
