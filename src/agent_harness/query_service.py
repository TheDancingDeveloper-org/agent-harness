"""Typed read services shared by JSON and browser controllers.

Presentation code must not know which SQLite file owns a fact. This module is
the narrow read boundary over the event, audit and queue stores; it returns the
same Pydantic models the public API publishes, so HTML cannot quietly invent a
second interpretation of project or work state.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .schemas import (
    AttemptStageEvidence,
    Event,
    EventPage,
    FleetControl,
    HoldList,
    HoldView,
    LatestEvent,
    ProjectList,
    ProjectSpec,
    ProjectSummary,
    RoleRoute,
    WorkEvidence,
    WorkItem,
    WorkList,
)
from .store import EventStore
from .work import BLOCKED, CLAIMED, DRAINING, HELD, WorkQueue, WorkRecord


class HarnessQueries:
    """Read the control plane without exposing storage layout to controllers."""

    def __init__(
        self,
        store: EventStore,
        queue: WorkQueue | None,
        *,
        audit: Any | None = None,
        fleet: Any | None = None,
    ) -> None:
        self.store = store
        self.queue = queue
        self.audit = audit
        self.fleet = fleet

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
            project=ProjectSpec(
                project_id=project.project_id,
                name=project.name,
                repo=project.repo,
                work_dir=project.work_dir,
                base_branch=project.base_branch,
                checks=list(project.checks),
                fixes={k: list(v) for k, v in (project.fixes or {}).items()},
                durability=project.durability,
                max_item_seconds=project.max_item_seconds,
                max_item_spend_usd=project.max_item_spend_usd,
                plan_path=project.plan_path,
                roles=(
                    {name: RoleRoute(**route) for name, route in project.roles.items()}
                    if project.roles
                    else None
                ),
                max_workers=project.max_workers,
                max_attempts=project.max_attempts,
                min_free_disk_gb=project.min_free_disk_gb,
            ),
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

    def events(self, since_id: int = 0, limit: int = 200) -> EventPage:
        rows = self.store.since_id(since_id, limit=limit)
        return EventPage(
            events=[Event(**row) for row in rows],
            cursor=rows[-1]["id"] if rows else since_id,
        )

    def live_events(self, since_id: int = 0, limit: int = 200) -> EventPage:
        """The event source the running deployment actually writes.

        Under supervised `serve`, the audit store is the live sink. A plain
        ingest-and-serve deployment has only the legacy event store. The
        fallback is explicit and preserves each store's monotonic cursor.
        """
        if self.audit is None:
            return self.events(since_id, limit)
        rows = self.audit.since_id(since_id, limit=limit)
        return EventPage(
            events=[Event(**self._audit_event_fields(row)) for row in rows],
            cursor=rows[-1]["id"] if rows else since_id,
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
        return WorkEvidence(
            project_id=project_id,
            item_id=item_id,
            events=events,
            stages=stages,
            holds=holds,
        )

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
