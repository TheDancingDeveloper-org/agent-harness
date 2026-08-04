"""Stage A: public-observable, deterministic end-to-end safety scenarios.

The slice crosses plan/queue input, the real SQLite claim path, real git,
declared checks, model routing, review, an external side-effect boundary, and
the HTTP projections over the append-only event stream.  Existing focused
tests remain useful; these scenarios prove the seams compose.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_harness import providers as P
from agent_harness.api import create_api
from agent_harness.audit import AuditStore
from agent_harness.executor import Checks, Executor
from agent_harness.fleet import Fleet
from agent_harness.model_client import (
    CapExhausted,
    ModelClient,
    RequestRefused,
    RetryPolicy,
    Route,
)
from agent_harness.session_executor import AgentSpec, SessionExecutor
from agent_harness.session_host import IDLE, RUNNING, Session
from agent_harness.store import EventStore
from agent_harness.work import CLAIMED, DONE, FAILED, PENDING, Project, WorkQueue, WorkRecord
from stage_a_support import (
    MATRIX_PROVIDER,
    DeterministicTransport,
    FixtureRepository,
    Raise,
    RecordingGitHub,
    Reply,
    event_sink,
    failure,
    generated_repository,
    git,
)

TOKEN = "stage-a-token"  # noqa: S105 - deterministic test credential
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def fixture_repo(tmp_path: Path) -> FixtureRepository:
    return generated_repository(tmp_path / "project")


def queue_at(tmp_path: Path, *, lease_seconds: float = 100.0) -> WorkQueue:
    queue = WorkQueue(str(tmp_path / "queue.sqlite"), lease_seconds=lease_seconds)
    queue.add_project(Project(project_id="fixture", name="Fixture", max_workers=2))
    queue.set_control("running", project_id="fixture")
    return queue


def checks_for(fixture: FixtureRepository) -> Checks:
    return Checks(commands=[list(command) for command in fixture.checks])


def client_for(
    transport: DeterministicTransport,
    *,
    events: Callable[[dict[str, Any]], None] | None = None,
    roles: Mapping[str, Route | Sequence[Route]] | None = None,
    attempts: int = 2,
    sleeps: list[float] | None = None,
    now: Callable[[], float] = lambda: 1000.0,
) -> ModelClient:
    if roles is None:
        roles = {
            role: Route(
                f"fixture-{role}",
                f"fixture://{role}",
                MATRIX_PROVIDER,
                options={"role": role},
            )
            for role in ("planner", "implementer", "reviewer")
        }
    return ModelClient(
        roles=roles,
        transport=transport,
        policy=RetryPolicy(max_attempts=attempts, backoff_seconds=0.01),
        on_event=events,
        sleep=(sleeps.append if sleeps is not None else lambda _seconds: None),
        jitter=lambda: 0.0,
        now=now,
        run_id="stage-a",
    )


def add_item(queue: WorkQueue, item_id: str = "A1", *, depends_on: list[str] | None = None) -> None:
    queue.add(
        [
            WorkRecord(
                item_id=item_id,
                title="Add multiplication",
                brief=(
                    "Add multiply to src/mathkit/operations.py, cover it in "
                    "tests/test_operations.py, create CHANGELOG.md, archive docs/OLD.md, "
                    "and remove src/mathkit/deprecated.py."
                ),
                depends_on=depends_on or [],
                issue=17,
            )
        ],
        project_id="fixture",
    )


def public_api(
    tmp_path: Path, queue: WorkQueue, audit: AuditStore
) -> tuple[TestClient, EventStore]:
    store = EventStore(tmp_path / "events.sqlite")
    return TestClient(create_api(store, queue=queue, audit=audit, token=TOKEN)), store


def execute(
    tmp_path: Path,
    fixture: FixtureRepository,
    *,
    transport: DeterministicTransport,
    queue: WorkQueue | None = None,
    audit: AuditStore | None = None,
    github: RecordingGitHub | None = None,
    client: ModelClient | None = None,
    checks: Checks | None = None,
) -> tuple[Any, WorkQueue, AuditStore, ModelClient]:
    queue = queue or queue_at(tmp_path)
    audit = audit or AuditStore(tmp_path / "audit.sqlite")
    sink = event_sink(audit)
    client = client or client_for(transport, events=sink)
    executor = Executor(
        queue,
        client,
        fixture.root,
        checks=checks or checks_for(fixture),
        github=github,
        push=False,
        on_event=sink,
        project_id="fixture",
    )
    return executor.run_once(), queue, audit, client


def outcomes_from_http(client: TestClient) -> list[str]:
    events = client.get("/api/audit/events", headers=AUTH).json()["events"]
    return [event["outcome"] for event in events]


def test_01_happy_api_path_is_visible_in_queue_git_external_effects_and_events(
    fixture_repo: FixtureRepository, tmp_path: Path
) -> None:
    queue = queue_at(tmp_path)
    add_item(queue)
    github = RecordingGitHub()
    transport = DeterministicTransport(
        {
            "planner": Reply("Modify the named files and run declared checks."),
            "implementer": Reply(fixture_repo.canonical_change()),
            "reviewer": Reply("APPROVED\nVerified the requested tree and tests."),
        }
    )

    outcome, queue, audit, _client = execute(
        tmp_path, fixture_repo, queue=queue, transport=transport, github=github
    )

    assert outcome is not None and outcome.state == DONE
    assert "multiply" in git(fixture_repo.root, "show", "harness/a1:src/mathkit/operations.py")
    assert git(fixture_repo.root, "cat-file", "-e", "harness/a1:CHANGELOG.md") == ""
    assert git(fixture_repo.root, "cat-file", "-e", "harness/a1:docs/ARCHIVE.md") == ""
    assert (
        subprocess.run(
            ["git", "-C", str(fixture_repo.root), "cat-file", "-e", "harness/a1:docs/OLD.md"],
            capture_output=True,
        ).returncode
        != 0
    )
    assert github.created[0]["draft"] is True
    assert github.ready == [outcome.pr_url]

    with public_api(tmp_path, queue, audit)[0] as api:
        item = api.get("/api/work/A1?project_id=fixture", headers=AUTH).json()
        assert item["state"] == DONE and item["pr_url"] == outcome.pr_url
        event_rows = api.get("/api/audit/events", headers=AUTH).json()["events"]
    assert any(
        row["role"] == "implementer"
        and row["model"] == "fixture-implementer"
        and row["endpoint"] == "fixture://implementer"
        and row["outcome"] == "ok"
        for row in event_rows
    )
    assert {"checks_passed", "checkpointed", "review_approved", "done"} <= {
        row["outcome"] for row in event_rows
    }


def test_02_dependency_is_not_claimed_before_required_work_is_done(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    add_item(queue, "A1", depends_on=["A0"])
    queue.add([WorkRecord(item_id="A0", title="foundation")], project_id="fixture")

    first = queue.claim("worker-one", project_id="fixture")
    assert first is not None and first.item_id == "A0"
    assert queue.claim("worker-two", project_id="fixture") is None
    queue.release("A0", DONE, owner="worker-one", project_id="fixture")
    second = queue.claim("worker-two", project_id="fixture")
    assert second is not None and second.item_id == "A1"


def test_03_midflight_dependency_correction_waits_for_the_durable_boundary(
    fixture_repo: FixtureRepository, tmp_path: Path
) -> None:
    queue = queue_at(tmp_path)
    add_item(queue)

    def correct_graph() -> None:
        queue.add([WorkRecord(item_id="A0", title="foundation")], project_id="fixture")
        add_item(queue, depends_on=["A0"])
        assert queue.get("A1", project_id="fixture").state == CLAIMED  # type: ignore[union-attr]

    transport = DeterministicTransport(
        {
            "planner": Reply("plan"),
            "implementer": Reply(fixture_repo.canonical_change(), before=correct_graph),
            "reviewer": Reply("APPROVED"),
        }
    )
    outcome, queue, audit, _client = execute(
        tmp_path, fixture_repo, queue=queue, transport=transport
    )

    assert outcome is not None and outcome.state == PENDING
    assert [call["role"] for call in transport.calls] == ["planner", "implementer"]
    assert git(fixture_repo.root, "branch", "--list", "harness/a1").strip() == ""
    with public_api(tmp_path, queue, audit)[0] as api:
        assert "dependency_invalidated" in outcomes_from_http(api)


def test_04_reingesting_identical_work_does_not_duplicate_or_reset_progress(
    tmp_path: Path,
) -> None:
    queue = queue_at(tmp_path)
    add_item(queue)
    claimed = queue.claim("worker", project_id="fixture")
    assert claimed is not None
    queue.release("A1", DONE, branch="harness/a1", owner="worker", project_id="fixture")

    add_item(queue)

    rows = [row for row in queue.items(project_id="fixture") if row.item_id == "A1"]
    assert len(rows) == 1
    assert rows[0].state == DONE and rows[0].attempts == 1 and rows[0].branch == "harness/a1"


def test_05_fallback_answers_before_backoff_and_is_publicly_recorded(tmp_path: Path) -> None:
    audit = AuditStore(tmp_path / "audit.sqlite")
    sink = event_sink(audit)
    transport = DeterministicTransport(
        {"preferred": failure(P.TRANSIENT, status=503), "fallback": Reply("answer")}
    )
    sleeps: list[float] = []
    roles = {
        "implementer": (
            Route("preferred", "fixture://one", MATRIX_PROVIDER, options={"role": "implementer"}),
            Route("fallback", "fixture://two", MATRIX_PROVIDER, options={"role": "implementer"}),
        )
    }
    client = client_for(transport, roles=roles, events=sink, sleeps=sleeps)

    assert client.call("implementer", [{"role": "user", "content": "work"}]).status == 200
    assert sleeps == []
    rows = audit.since_id(0)
    assert any(
        row["model"] == "fallback" and "fell back" in json.loads(row["data"])["detail"]
        for row in rows
    )


def test_06_all_routes_unavailable_is_an_explicit_cheap_refusal(
    fixture_repo: FixtureRepository, tmp_path: Path
) -> None:
    queue = queue_at(tmp_path)
    add_item(queue)
    audit = AuditStore(tmp_path / "audit.sqlite")
    sink = event_sink(audit)
    transport = DeterministicTransport(
        {"planner-one": failure(P.NON_RETRYABLE), "planner-two": failure(P.NON_RETRYABLE)}
    )
    client = client_for(
        transport,
        events=sink,
        roles={
            "planner": (
                Route("planner-one", "fixture://one", MATRIX_PROVIDER, options={"role": "planner"}),
                Route("planner-two", "fixture://two", MATRIX_PROVIDER, options={"role": "planner"}),
            )
        },
    )

    outcome, queue, audit, _client = execute(
        tmp_path, fixture_repo, queue=queue, audit=audit, transport=transport, client=client
    )

    assert outcome is not None and outcome.state == FAILED
    assert len(transport.calls) == 2
    assert "planner refused" in outcome.reason
    assert queue.get("A1", project_id="fixture").attempts == 1  # type: ignore[union-attr]


def test_07_spend_caps_do_not_consume_item_attempts_and_refusals_do_not_park(
    fixture_repo: FixtureRepository, tmp_path: Path
) -> None:
    queue = queue_at(tmp_path)
    add_item(queue)
    cap_transport = DeterministicTransport({"planner": failure(P.TERMINAL_CAP)})
    cap_client = client_for(
        cap_transport,
        roles={
            "planner": Route(
                "planner", "fixture://cap", MATRIX_PROVIDER, options={"role": "planner"}
            )
        },
    )
    with pytest.raises(CapExhausted):
        execute(
            tmp_path,
            fixture_repo,
            queue=queue,
            transport=cap_transport,
            client=cap_client,
        )
    row = queue.get("A1", project_id="fixture")
    assert row is not None and row.state == PENDING and row.attempts == 0

    refusal = DeterministicTransport({"one": failure(P.NON_RETRYABLE)})
    client = client_for(
        refusal,
        roles={
            "implementer": Route(
                "one", "fixture://healthy", MATRIX_PROVIDER, options={"role": "implementer"}
            )
        },
    )
    with pytest.raises(RequestRefused):
        client.call("implementer", [{"role": "user", "content": "request"}])
    assert client.parks.remaining("fixture://healthy", 1000.0, "implementer") == 0


def test_08_worker_death_releases_only_its_claim_and_leaves_sibling_project_running(
    tmp_path: Path,
) -> None:
    queue = WorkQueue(str(tmp_path / "queue.sqlite"), lease_seconds=100.0)
    for project in ("a", "b"):
        queue.add_project(Project(project_id=project, name=project.upper(), max_workers=1))
        queue.add([WorkRecord(item_id="A1", title="work")], project_id=project)

    hold_b = threading.Event()

    class DyingExecutor:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id
            self.owner = f"owner-{project_id}"

        def serve(self, *, poll_seconds: float, stop: threading.Event) -> None:
            assert queue.claim(self.owner, project_id=self.project_id) is not None
            if self.project_id == "a":
                raise RuntimeError("worker died")
            hold_b.wait(2)

    fleet = Fleet(queue, DyingExecutor, poll_seconds=0.01)
    try:
        fleet.start("a")
        fleet.start("b")
        assert wait_until(lambda: queue.get("A1", project_id="a").state == FAILED)  # type: ignore[union-attr]
        assert wait_until(lambda: queue.get("A1", project_id="b").state == CLAIMED)  # type: ignore[union-attr]
        sibling = queue.get("A1", project_id="b")
        assert sibling is not None and sibling.state == CLAIMED and sibling.owner == "owner-b"
        assert queue.control(project_id="b")[0] == "running"
    finally:
        hold_b.set()
        fleet.stop_all()


def test_09_heartbeat_keeps_a_slow_healthy_item_claimed_past_lease(tmp_path: Path) -> None:
    clock = [1000.0]
    queue = WorkQueue(str(tmp_path / "queue.sqlite"), lease_seconds=0.2, now=lambda: clock[0])
    queue.set_control("running")
    queue.add([WorkRecord(item_id="A1", title="slow")])
    claimed = queue.claim("slow-worker")
    assert claimed is not None

    from agent_harness.work import LeaseHeartbeat

    heartbeat = LeaseHeartbeat(queue, "A1", "slow-worker", interval=0.01)
    with heartbeat:
        clock[0] += 1.0
        assert wait_until(lambda: queue.get("A1").lease_until > clock[0])  # type: ignore[union-attr]
        assert queue.claim("racer") is None
    assert not heartbeat.lost


def test_10_concurrent_claims_are_unique_and_resize_does_not_kill_inflight(
    tmp_path: Path,
) -> None:
    queue = queue_at(tmp_path)
    add_item(queue)
    barrier = threading.Barrier(3)
    won: list[str] = []

    def race(owner: str) -> None:
        barrier.wait()
        if record := queue.claim(owner, project_id="fixture"):
            won.append(record.item_id)

    threads = [threading.Thread(target=race, args=(f"racer-{number}",)) for number in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert won == ["A1"]

    # Return the claim for the fleet half of the same public scenario.
    queue.release("A1", PENDING, project_id="fixture")
    entered = threading.Event()
    finish = threading.Event()

    class HoldingExecutor:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id

        def serve(self, *, poll_seconds: float, stop: threading.Event) -> None:
            owner = threading.current_thread().name
            record = queue.claim(owner, project_id=self.project_id)
            if record is None:
                stop.wait(1)
                return
            entered.set()
            finish.wait(2)
            queue.release(record.item_id, DONE, owner=owner, project_id=self.project_id)

    fleet = Fleet(queue, HoldingExecutor, poll_seconds=0.01)
    try:
        fleet.start("fixture")
        assert entered.wait(1)
        assert fleet.resize("fixture", 1) >= 1
        assert queue.get("A1", project_id="fixture").state == CLAIMED  # type: ignore[union-attr]
        finish.set()
        assert wait_until(lambda: queue.get("A1", project_id="fixture").state == DONE)  # type: ignore[union-attr]
    finally:
        finish.set()
        fleet.stop_all()


def test_11_failed_checks_prevent_reviewer_spend(
    fixture_repo: FixtureRepository, tmp_path: Path
) -> None:
    queue = queue_at(tmp_path)
    add_item(queue)
    transport = DeterministicTransport(
        {
            "planner": Reply("plan"),
            "implementer": Reply(fixture_repo.canonical_change()),
            "reviewer": Reply("APPROVED"),
        }
    )
    outcome, _queue, audit, _client = execute(
        tmp_path,
        fixture_repo,
        queue=queue,
        transport=transport,
        checks=Checks(commands=[["python", "-c", "raise SystemExit(7)"]]),
    )

    assert outcome is not None and outcome.state == FAILED
    assert [call["role"] for call in transport.calls] == ["planner", "implementer"]
    assert not any(row["role"] == "reviewer" for row in audit.since_id(0))
    assert any(row["outcome"] == "checks_failed" for row in audit.since_id(0))


@pytest.mark.parametrize(
    ("name", "patch_factory", "expected"),
    [
        ("valid", lambda fixture: fixture.canonical_change(), DONE),
        (
            "over-counted",
            lambda fixture: fixture.canonical_change().replace(
                "@@ -1,8 +1,12 @@", "@@ -1,9 +1,13 @@"
            ),
            DONE,
        ),
        (
            "truncated",
            lambda fixture: (
                "diff --git a/src/mathkit/operations.py "
                "b/src/mathkit/operations.py\n"
                "--- a/src/mathkit/operations.py\n"
                "+++ b/src/mathkit/operations.py\n"
                "@@ -4,2 +4,3 @@\n"
                " def add(left: int, right: int) -> int:\n"
                "+def multiply(left, right):\n"
            ),
            FAILED,
        ),
        ("zero-context", lambda fixture: fixture.existing_file_zero_context(), FAILED),
        ("prose", lambda fixture: "I cannot provide a patch.", FAILED),
    ],
)
def test_12_malformed_patch_matrix_repairs_only_derivable_damage_and_never_misplaces(
    fixture_repo: FixtureRepository,
    tmp_path: Path,
    name: str,
    patch_factory: Callable[[FixtureRepository], str],
    expected: str,
) -> None:
    queue = queue_at(tmp_path)
    add_item(queue)
    transport = DeterministicTransport(
        {
            "planner": Reply("plan"),
            "implementer": Reply(patch_factory(fixture_repo)),
            "reviewer": Reply("APPROVED"),
        }
    )
    outcome, _queue, _audit, _client = execute(
        tmp_path, fixture_repo, queue=queue, transport=transport
    )

    assert outcome is not None and outcome.state == expected, (name, outcome.reason)
    if expected == FAILED:
        assert (
            (fixture_repo.root / "src/mathkit/operations.py")
            .read_text()
            .startswith('"""Arithmetic operations')
        )


class EditingSessionHost:
    def __init__(self, edit: Callable[[Path], None]) -> None:
        self.edit = edit

    def create_session(
        self,
        name: str,
        command: Sequence[str],
        cwd: str,
        env: Mapping[str, str] | None = None,
        scrollback_bytes: int | None = None,
    ) -> Session:
        self.edit(Path(cwd))
        return Session(id="fixture-session", name=name, activity=RUNNING, cwd=cwd)

    def wait_for_exit(self, session_id: str, **_kwargs: Any) -> Session:
        return Session(id=session_id, name="fixture", activity=IDLE, exit_code=0)


@pytest.mark.parametrize("mode", ["api", "session"])
def test_13_both_executors_checkpoint_before_review(
    fixture_repo: FixtureRepository, tmp_path: Path, mode: str
) -> None:
    queue = queue_at(tmp_path)
    add_item(queue)
    audit = AuditStore(tmp_path / "audit.sqlite")
    sink = event_sink(audit)

    if mode == "api":
        transport = DeterministicTransport(
            {
                "planner": Reply("plan"),
                "implementer": Reply(fixture_repo.canonical_change()),
                "reviewer": Raise(TimeoutError("reviewer died")),
            }
        )
        executor: Any = Executor(
            queue,
            client_for(transport, events=sink),
            fixture_repo.root,
            checks=checks_for(fixture_repo),
            push=False,
            on_event=sink,
            project_id="fixture",
        )
    else:

        def edit(tree: Path) -> None:
            operations = tree / "src/mathkit/operations.py"
            operations.write_text(
                operations.read_text()
                + "\n\ndef multiply(left: int, right: int) -> int:\n    return left * right\n"
            )

        reviewer_transport = DeterministicTransport(
            {"reviewer": Raise(TimeoutError("reviewer died"))}
        )
        executor = SessionExecutor(
            queue,
            EditingSessionHost(edit),
            fixture_repo.root,
            agent=AgentSpec(command=("fixture-agent", "{prompt_file}"), poll_seconds=0),
            checks=checks_for(fixture_repo),
            reviewer=client_for(
                reviewer_transport,
                events=sink,
                roles={
                    "reviewer": Route(
                        "reviewer",
                        "fixture://reviewer",
                        MATRIX_PROVIDER,
                        options={"role": "reviewer"},
                    )
                },
            ),
            worktrees=tmp_path / "worktrees",
            push=False,
            on_event=sink,
            project_id="fixture",
        )

    outcome = executor.run_once()
    assert outcome is not None and outcome.state == PENDING
    assert "commit" in outcome.stages
    assert "Reviewed: not yet" in git(fixture_repo.root, "log", "-1", "--format=%B", "harness/a1")
    rows = audit.since_id(0)
    checkpoint = next(index for index, row in enumerate(rows) if row["outcome"] == "checkpointed")
    review_error = next(
        index
        for index, row in enumerate(rows)
        if row["role"] == "reviewer" and row["outcome"] == "error"
    )
    assert checkpoint < review_error


@pytest.mark.parametrize(
    ("step", "expected_class"),
    [
        (failure(P.RPM), P.RPM),
        (failure(P.WINDOW_CAP), P.WINDOW_CAP),
        (failure(P.TERMINAL_CAP), P.TERMINAL_CAP),
        (failure(P.NON_RETRYABLE), P.NON_RETRYABLE),
        (failure(P.TRANSIENT, status=503), P.TRANSIENT),
        (Raise(TimeoutError("slow network")), P.TRANSIENT),
    ],
)
def test_transport_failure_matrix_reaches_the_existing_classifier_contract(
    tmp_path: Path, step: Reply | Raise, expected_class: str
) -> None:
    audit = AuditStore(tmp_path / "audit.sqlite")
    sink = event_sink(audit)
    transport = DeterministicTransport({"route": [step, Reply("ok")]})
    client = client_for(
        transport,
        events=sink,
        roles={
            "implementer": Route(
                "route", "fixture://matrix", MATRIX_PROVIDER, options={"role": "implementer"}
            )
        },
        attempts=2,
    )
    if expected_class in P.CAPS:
        with pytest.raises(CapExhausted):
            client.call("implementer", [{"role": "user", "content": "matrix"}])
    elif expected_class == P.NON_RETRYABLE:
        with pytest.raises(RequestRefused):
            client.call("implementer", [{"role": "user", "content": "matrix"}])
    else:
        assert client.call("implementer", [{"role": "user", "content": "matrix"}]).status == 200
    assert any(row["error_class"] == expected_class for row in audit.since_id(0))


def test_transport_covers_malformed_and_slow_success_without_network() -> None:
    called: list[str] = []
    transport = DeterministicTransport(
        {
            "malformed": Reply(body="not-json"),
            "slow": Reply("healthy", before=lambda: called.append("slow")),
        }
    )
    malformed = transport(Route("malformed", "fixture://one"), [], {})
    slow = transport(Route("slow", "fixture://two"), [], {})
    assert malformed.body == "not-json"
    assert json.loads(slow.body)["choices"][0]["message"]["content"] == "healthy"  # type: ignore[arg-type]
    assert called == ["slow"]


def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False
