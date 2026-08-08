from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent_harness.api import create_api
from agent_harness.audit import AuditStore
from agent_harness.events import WORK
from agent_harness.events import Event as HarnessEvent
from agent_harness.execution_environment import LocalExecutionEnvironment
from agent_harness.executor import Checks
from agent_harness.fleet import Fleet
from agent_harness.holds import Answer
from agent_harness.model_client import ModelClient, Response, Route
from agent_harness.notifications import NotificationOutbox
from agent_harness.plan_integration import PlanCoordinator
from agent_harness.query_service import HarnessQueries
from agent_harness.review_events import RemoteReviewEvent, ReviewEventProcessor
from agent_harness.role_runners import RoleRunResult
from agent_harness.runtime import direct_executor_factory
from agent_harness.store import EventStore
from agent_harness.work import DONE, HELD, PENDING, Project, WorkQueue, WorkRecord


def queue_for(tmp_path: Path) -> WorkQueue:
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project("p", "project"))
    queue.add([WorkRecord("T1", "original", brief="the original")], project_id="p")
    return queue


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "seed.txt").write_text("seed\n")
    git(repo, "add", "seed.txt")
    git(repo, "commit", "-qm", "base")
    return repo


class LocalBackend:
    name = "review-test-backend"
    api_version = 1
    version = "test"

    def check(self) -> tuple[bool, str]:
        return True, "available"

    def create(self, worktree: Path, **_: Any) -> LocalExecutionEnvironment:
        return LocalExecutionEnvironment(worktree)


class ReviewCorrectionRunner:
    name = "review-correction-runner"
    api_version = 1
    version = "test"

    def __init__(self) -> None:
        self.items: list[str] = []

    def run(self, request: Any) -> RoleRunResult:
        self.items.append(request.item_id)
        (request.repo / f"{request.item_id}.txt").write_text(request.item_id + "\n")
        return RoleRunResult(exit_status="completed", submission="done", calls=1)


def review_client() -> ModelClient:
    def transport(
        route: Route, messages: Sequence[Mapping[str, Any]], options: Mapping[str, Any]
    ) -> Response:
        del messages, options
        role = str(route.options.get("role") or "")
        if role == "planner":
            content = json.dumps(
                {
                    "plan": "write an item marker",
                    "targets": [{"path": "seed.txt", "reason": "item context"}],
                    "cannot_identify_target": None,
                }
            )
        else:
            content = "APPROVED\nlocal review"
        return Response(
            200,
            {},
            json.dumps({"choices": [{"message": {"content": content}}]}),
        )

    return ModelClient(
        roles={
            role: Route("review-test", "https://example.invalid", options={"role": role})
            for role in ("planner", "implementer", "reviewer")
        },
        transport=transport,
        sleep=lambda _: None,
    )


def wait_for(predicate: Any, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def event(disposition: str) -> RemoteReviewEvent:
    return RemoteReviewEvent(
        source="test-review",
        remote_id="comment-7",
        project_id="p",
        item_id="T1",
        disposition=disposition,  # type: ignore[arg-type]
        summary="Please handle the reported issue.",
    )


def test_actionable_review_is_deduplicated_and_waits_for_original(tmp_path: Path) -> None:
    queue = queue_for(tmp_path)
    processor = ReviewEventProcessor(queue)

    first = processor.process(event("actionable"))
    second = processor.process(event("actionable"))

    assert first.accepted and not first.duplicate
    assert second.duplicate and not second.accepted
    assert first.correction_item_id is not None
    correction = queue.get(first.correction_item_id, project_id="p")
    assert correction is not None
    assert correction.state == PENDING
    assert correction.depends_on == ["T1"]
    assert queue.items(project_id="p").count(correction) == 1


def test_ambiguous_review_never_becomes_agent_work(tmp_path: Path) -> None:
    queue = queue_for(tmp_path)
    result = ReviewEventProcessor(queue).process(event("ambiguous"))

    assert result.status == "needs_human"
    assert result.correction_item_id is not None
    correction = queue.get(result.correction_item_id, project_id="p")
    assert correction is not None
    assert correction.state == HELD
    hold = queue.holds.current("p", result.correction_item_id)
    assert hold is not None
    queue.answer_hold(
        result.correction_item_id,
        hold.resume_token,
        Answer(text="Please address the specific security issue.", who="operator"),
        project_id="p",
    )
    correction = queue.get(result.correction_item_id, project_id="p")
    assert correction is not None and correction.state == PENDING


def test_already_resolved_review_records_no_correction(tmp_path: Path) -> None:
    queue = queue_for(tmp_path)
    result = ReviewEventProcessor(queue).process(event("already_resolved"))

    assert result.status == "already_resolved"
    assert result.correction_item_id is None
    assert len(queue.items(project_id="p")) == 1


def test_review_event_api_is_typed_and_idempotent(tmp_path: Path) -> None:
    queue = queue_for(tmp_path)
    notifications = NotificationOutbox(tmp_path / "notifications.sqlite")
    client = TestClient(
        create_api(
            EventStore(tmp_path / "events.sqlite"),
            queue=queue,
            audit=AuditStore(tmp_path / "audit.sqlite"),
            notifications=notifications,
            token="token",
        )
    )
    payload = {
        "source": "test-review",
        "remote_id": "comment-api-1",
        "project_id": "p",
        "item_id": "T1",
        "disposition": "actionable",
        "summary": "Please address this API review.",
    }
    headers = {"Authorization": "Bearer token"}
    first = client.post("/api/review-events", json=payload, headers=headers)
    second = client.post("/api/review-events", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["accepted"] is True
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert len(notifications.rows()) == 1
    assert notifications.rows()[0].payload["outcome"] == "remote_review_received"
    notifications.close()


def test_item_evidence_projects_runner_gates_promotion_and_reviews(tmp_path: Path) -> None:
    queue = queue_for(tmp_path)
    audit = AuditStore(tmp_path / "audit.sqlite")
    audit.append(
        [
            HarnessEvent(
                ts=1.0,
                kind=WORK,
                source="runner",
                worker="worker-1",
                outcome="calling",
                data={"project_id": "p", "item_id": "T1", "attempt": 2, "detail": "running"},
            ),
            HarnessEvent(
                ts=2.0,
                kind=WORK,
                source="runner",
                worker="worker-1",
                outcome="checks_passed",
                data={
                    "project_id": "p",
                    "item_id": "T1",
                    "detail": "all gates passed",
                    "evidence": {
                        "outcome": "pass",
                        "command": ["tool", "check", "--strict"],
                        "commands": [["tool", "check", "--strict"], ["tool", "test"]],
                        "applied": [],
                    },
                },
            ),
            HarnessEvent(
                ts=3.0,
                kind=WORK,
                source="promotion",
                outcome="plan_promotion",
                data={
                    "project_id": "p",
                    "item_id": "T1",
                    "status": "promoted",
                    "plan_branch": "integration",
                    "base_sha": "base",
                    "item_sha": "item",
                    "old_head_sha": "old",
                    "new_head_sha": "new",
                    "target_sha": "target",
                    "detail": "promoted after authoritative checks",
                },
            ),
            HarnessEvent(
                ts=4.0,
                kind=WORK,
                source="review-event",
                outcome="remote_review_received",
                data={
                    "project_id": "p",
                    "item_id": "T1",
                    "source": "test-review",
                    "remote_id": "comment-7",
                    "disposition": "actionable",
                    "status": "queued",
                    "duplicate": False,
                    "correction_item_id": "review-123",
                    "detail": "queued correction work",
                },
            ),
        ]
    )

    evidence = HarnessQueries(EventStore(tmp_path / "events.sqlite"), queue, audit=audit).evidence(
        "p", "T1"
    )

    assert evidence is not None
    assert [(one.stage, one.attempt) for one in evidence.runner_progress] == [("calling", 2)]
    assert evidence.gates[0].command == ["tool", "check", "--strict"]
    assert evidence.gates[0].commands == [["tool", "check", "--strict"], ["tool", "test"]]
    assert evidence.gates[0].authoritative is True
    assert evidence.promotions[0].new_head_sha == "new"
    assert evidence.remote_reviews[0].remote_id == "comment-7"


def test_actionable_review_runs_once_on_the_existing_local_plan_and_fleet_continues(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    plan = tmp_path / "PLAN.md"
    plan.write_text("# local plan\n")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"), lease_seconds=100.0)
    queue.add_project(
        Project(
            project_id="p",
            name="p",
            work_dir=str(repo),
            plan_path=str(plan),
            plan_branch="integration/p",
            checks=["true"],
            max_workers=2,
        )
    )
    queue.add(
        [
            WorkRecord("T1", "original"),
            WorkRecord("B", "sibling", brief="Write the sibling marker."),
        ],
        project_id="p",
    )

    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks(commands=[["true"]]))
    coordinator.ensure(target_branch="main", branch="integration/p", plan_path=str(plan))
    target = git(repo, "rev-parse", "main")
    git(repo, "branch", "harness/t1", target)
    git(repo, "checkout", "harness/t1")
    (repo / "original.txt").write_text("original\n")
    git(repo, "add", "original.txt")
    git(repo, "commit", "-qm", "original")
    git(repo, "checkout", "main")
    original = queue.get("T1", project_id="p")
    assert original is not None
    original.state = DONE
    original.branch = "harness/t1"
    queue.release("T1", DONE, branch="harness/t1", project_id="p")
    coordinator.promote(original, item_branch="harness/t1", base=target)

    review = event("actionable")
    review = RemoteReviewEvent(
        source=review.source,
        remote_id=review.remote_id,
        project_id="p",
        item_id="T1",
        disposition=review.disposition,
        summary=review.summary,
    )
    processor = ReviewEventProcessor(queue)
    first = processor.process(review)
    duplicate = processor.process(review)
    assert first.accepted and not first.duplicate
    assert duplicate.duplicate and not duplicate.accepted
    assert first.correction_item_id is not None
    correction_id = first.correction_item_id
    correction = queue.get(correction_id, project_id="p")
    assert correction is not None and correction.state == PENDING
    assert coordinator.base_for(correction) == ("integration/p", "local plan branch")

    runner = ReviewCorrectionRunner()
    fleet = Fleet(
        queue,
        direct_executor_factory(
            queue,
            reviewer=review_client(),
            role_runner=runner,
            push=False,
            environment_factory=LocalBackend(),
            environment_image="test-image",
        ),
        poll_seconds=0.01,
    )
    try:
        fleet.start("p")
        assert wait_for(
            lambda: all(
                (record := queue.get(item, project_id="p")) is not None and record.state == DONE
                for item in (correction_id, "B")
            )
        )
    finally:
        fleet.stop_all()

    assert runner.items.count(correction_id) == 1
    assert runner.items.count("B") == 1
    correction_promotion = queue.latest_promotion("p", correction_id)
    sibling_promotion = queue.latest_promotion("p", "B")
    assert correction_promotion is not None
    assert correction_promotion["status"] == "promoted"
    assert sibling_promotion is not None and sibling_promotion["status"] == "promoted"
    assert git(repo, "show", f"integration/p:{correction_id}.txt") == correction_id
    assert git(repo, "show", "integration/p:B.txt") == "B"
