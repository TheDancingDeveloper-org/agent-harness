"""Projects: separate streams, and the migration that gets there.

The defect this fixes is not cosmetic. `work.item_id` was a global primary
key, so two plans that both name `T1` were the same row -- and NGMS has a
`T1`. Loading a second project did not fail to isolate; it overwrote.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agent_harness.work import (
    CLAIMED,
    DEFAULT_PROJECT,
    DONE,
    PENDING,
    RUNNING,
    STOPPED,
    Project,
    WorkQueue,
    WorkRecord,
)


def rec(item_id: str, **kw: object) -> WorkRecord:
    kw.setdefault("title", f"do {item_id}")
    kw.setdefault("brief", "brief")
    return WorkRecord(item_id=item_id, **kw)  # type: ignore[arg-type]


@pytest.fixture
def queue(tmp_path: Path) -> WorkQueue:
    return WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)


# ------------------------------------------------------------- the defect


def test_two_projects_can_both_have_a_t1(queue: WorkQueue) -> None:
    """The whole point. Before this, the second T1 overwrote the first."""
    queue.add_project(Project(project_id="ngms", name="NGMS"))
    queue.add_project(Project(project_id="other", name="Other"))

    queue.add([rec("T1", title="NGMS first item")], project_id="ngms")
    queue.add([rec("T1", title="Other first item")], project_id="other")

    ngms = queue.get("T1", project_id="ngms")
    other = queue.get("T1", project_id="other")
    assert ngms is not None and other is not None
    assert ngms.title == "NGMS first item"
    assert other.title == "Other first item"
    assert len(queue.items()) == 2, "two distinct items collapsed into one row"


def test_a_worker_claims_only_within_its_project(queue: WorkQueue) -> None:
    """Separate streams at the queue, not merely in the UI."""
    queue.add_project(Project(project_id="a", name="A"))
    queue.add_project(Project(project_id="b", name="B"))
    queue.add([rec("T1")], project_id="a")
    queue.add([rec("T1")], project_id="b")
    # Both running: a project starts stopped, so without this the test would
    # pass for the wrong reason -- no claim rather than no cross-project claim.
    queue.set_control(RUNNING, project_id="a")
    queue.set_control(RUNNING, project_id="b")

    claimed = queue.claim("worker-a", project_id="a")
    assert claimed is not None
    assert claimed.project_id == "a"

    # Project A is now empty; the worker must not wander into B.
    assert queue.claim("worker-a", project_id="a") is None
    assert queue.get("T1", project_id="b").state == PENDING  # type: ignore[union-attr]


def test_dependencies_do_not_cross_projects(queue: WorkQueue) -> None:
    """An id that means one thing here and another there must not resolve
    across the boundary -- that is how a project unblocks on someone else's
    work."""
    queue.add_project(Project(project_id="a", name="A"))
    queue.add_project(Project(project_id="b", name="B"))
    queue.add([rec("T1")], project_id="a")
    queue.add([rec("T2", depends_on=["T1"])], project_id="b")

    # A's T1 is done; B's T2 depends on a T1 that does not exist in B.
    queue.claim("w", project_id="a")
    queue.release("T1", DONE, project_id="a")

    blocked = queue.get("T2", project_id="b")
    assert blocked is not None
    assert blocked.depends_on == ["T1"]


# ------------------------------------------------------------- control


def test_each_project_has_its_own_control_state(queue: WorkQueue) -> None:
    """Draining one project must not stop another."""
    queue.add_project(Project(project_id="a", name="A"))
    queue.add_project(Project(project_id="b", name="B"))
    queue.add([rec("T1")], project_id="a")
    queue.add([rec("T1")], project_id="b")

    queue.set_control(RUNNING, project_id="a")
    queue.set_control(RUNNING, project_id="b")
    queue.set_control("draining", reason="deploying", project_id="a")

    assert queue.claim("w", project_id="a") is None
    assert queue.claim("w", project_id="b") is not None
    assert queue.control(project_id="b")[0] == RUNNING


def test_a_new_project_starts_stopped(queue: WorkQueue) -> None:
    """Registering a project must not start spending money on it."""
    queue.add_project(Project(project_id="a", name="A"))
    queue.add([rec("T1")], project_id="a")

    assert queue.control(project_id="a")[0] == STOPPED
    assert queue.claim("w", project_id="a") is None


def test_boot_stops_every_project_and_remembers_what_it_was(queue: WorkQueue) -> None:
    """After a restart nothing resumes on its own -- but a project that was
    deliberately drained must not come back looking like one that was running
    happily, or the operator's intent is lost."""
    queue.add_project(Project(project_id="a", name="A"))
    queue.add_project(Project(project_id="b", name="B"))
    queue.set_control(RUNNING, project_id="a")
    queue.set_control("draining", reason="deploying", project_id="b")

    queue.stop_all_on_boot(reason="process started")

    state_a, reason_a, previous_a = queue.control_detail(project_id="a")
    state_b, reason_b, previous_b = queue.control_detail(project_id="b")
    assert state_a == STOPPED and previous_a == RUNNING
    assert state_b == STOPPED and previous_b == "draining"
    assert "deploying" in (reason_b or ""), "why it was drained must survive the restart"


def test_stopping_never_starts_a_worker(queue: WorkQueue) -> None:
    queue.add_project(Project(project_id="a", name="A"))
    queue.add([rec("T1")], project_id="a")
    queue.set_control(RUNNING, project_id="a")
    queue.stop_all_on_boot(reason="restart")

    assert queue.claim("w", project_id="a") is None


# ------------------------------------------------------------- migration


def test_an_existing_database_migrates_without_losing_work(tmp_path: Path) -> None:
    """The dangerous path. A live queue mid-flight must survive the schema
    change, keeping state, ownership, attempts and history."""
    path = str(tmp_path / "w.sqlite")

    # Build the pre-project schema by hand -- the shape a deployed harness has.
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE work (
            item_id TEXT PRIMARY KEY, issue INTEGER, title TEXT NOT NULL,
            brief TEXT NOT NULL DEFAULT '', depends_on TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL DEFAULT 'pending', owner TEXT,
            lease_until REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, branch TEXT, pr_url TEXT,
            updated_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE control (
            id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT NOT NULL DEFAULT 'running',
            reason TEXT, changed_at REAL NOT NULL DEFAULT 0
        );
        INSERT INTO control (id, state) VALUES (1, 'running');
        INSERT INTO work (item_id, title, brief, state, owner, attempts, branch)
            VALUES ('T1', 'in flight', 'b', 'claimed', 'host:42', 3, 'harness/t1');
        INSERT INTO work (item_id, title, brief, state) VALUES ('T2', 'done', 'b', 'done');
    """)
    conn.commit()
    conn.close()

    queue = WorkQueue(path, lease_seconds=100.0)

    items = {i.item_id: i for i in queue.items()}
    assert set(items) == {"T1", "T2"}, "migration lost work"
    assert items["T1"].state == CLAIMED
    assert items["T1"].owner == "host:42", "a live claim lost its owner"
    assert items["T1"].attempts == 3, "attempt history was reset"
    assert items["T1"].branch == "harness/t1"
    assert items["T2"].state == DONE
    assert all(i.project_id == DEFAULT_PROJECT for i in items.values())


def test_migration_creates_the_project_the_rows_were_moved_into(tmp_path: Path) -> None:
    """Rows pointing at a project that does not exist would be invisible to
    every project-scoped query -- present in the table, gone from the UI."""
    path = str(tmp_path / "w.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE work (
            item_id TEXT PRIMARY KEY, issue INTEGER, title TEXT NOT NULL,
            brief TEXT NOT NULL DEFAULT '', depends_on TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL DEFAULT 'pending', owner TEXT,
            lease_until REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, branch TEXT, pr_url TEXT, updated_at REAL NOT NULL DEFAULT 0
        );
        INSERT INTO work (item_id, title, brief) VALUES ('T1', 'x', 'b');
    """)
    conn.commit()
    conn.close()

    queue = WorkQueue(path, lease_seconds=100.0)
    projects = {p.project_id for p in queue.projects()}
    assert DEFAULT_PROJECT in projects
    assert queue.items(project_id=DEFAULT_PROJECT)


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """It runs on every open. Running it twice must not duplicate or destroy."""
    path = str(tmp_path / "w.sqlite")
    first = WorkQueue(path, lease_seconds=100.0)
    first.add_project(Project(project_id="a", name="A"))
    first.add([rec("T1")], project_id="a")

    second = WorkQueue(path, lease_seconds=100.0)
    assert len(second.items()) == 1
    assert {p.project_id for p in second.projects()} >= {"a"}


# ------------------------------------------------------------- registration


def test_a_project_round_trips_its_configuration(queue: WorkQueue) -> None:
    """The point of persisting it: nothing has to be re-supplied after a
    restart, which is why CLI flags were not enough."""
    queue.add_project(
        Project(
            project_id="ngms",
            name="NGMS",
            repo="TheDancingDeveloper-org/NGMS",
            work_dir="/work/ngms",
            base_branch="feat/mariadb-baseline",
            checks=["cargo test", "cargo clippy"],
            max_workers=3,
            min_free_disk_gb=48,
        )
    )
    loaded = queue.get_project("ngms")
    assert loaded is not None
    assert loaded.repo == "TheDancingDeveloper-org/NGMS"
    assert loaded.checks == ["cargo test", "cargo clippy"]
    assert loaded.min_free_disk_gb == 48
    assert loaded.base_branch == "feat/mariadb-baseline"
    assert loaded.max_workers == 3


def test_re_registering_a_project_updates_rather_than_duplicates(queue: WorkQueue) -> None:
    queue.add_project(Project(project_id="a", name="A", max_workers=1))
    queue.add_project(Project(project_id="a", name="A renamed", max_workers=4))

    assert len([p for p in queue.projects() if p.project_id == "a"]) == 1
    loaded = queue.get_project("a")
    assert loaded is not None and loaded.max_workers == 4


def test_counts_are_per_project(queue: WorkQueue) -> None:
    queue.add_project(Project(project_id="a", name="A"))
    queue.add_project(Project(project_id="b", name="B"))
    queue.add([rec("T1"), rec("T2")], project_id="a")
    queue.add([rec("T1")], project_id="b")

    assert queue.counts(project_id="a") == {PENDING: 2}
    assert queue.counts(project_id="b") == {PENDING: 1}
    assert queue.counts() == {PENDING: 3}, "the cross-project rollup should still work"


# ------------------------------------------------------------------- API


@pytest.fixture
def client(tmp_path: Path):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from agent_harness.api import create_api
    from agent_harness.store import EventStore

    q = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    store = EventStore(tmp_path / "e.sqlite")

    class ReadyFleet:
        """Stands in for a worker pool.

        `start` refuses without one on purpose: marking a project RUNNING with
        no pool attached is the false-running state the preflight gate exists
        to prevent. These tests are about control semantics rather than
        workers, so the pool is a stand-in and readiness is covered in
        test_preflight.py.
        """

        def __init__(self) -> None:
            self.started: list[str] = []

        def start(self, project_id: str) -> int:
            self.started.append(project_id)
            q.set_control(RUNNING, project_id=project_id)
            return 1

        def stop(self, project_id: str, *, reason: str | None = None) -> None:
            if project_id in self.started:
                self.started.remove(project_id)
            q.set_control(STOPPED, reason=reason, project_id=project_id)

        def running(self) -> dict[str, int]:
            return {p: 1 for p in self.started}

    with TestClient(create_api(store, queue=q, token="tok", fleet=ReadyFleet())) as c:  # noqa: S106
        holder: Any = c
        holder.queue = q
        yield c


def hdr() -> dict[str, str]:
    return {"Authorization": "Bearer tok"}


def test_registering_a_project_leaves_it_stopped(client) -> None:  # type: ignore[no-untyped-def]
    """Registering must never begin spending money."""
    response = client.post(
        "/api/projects",
        headers=hdr(),
        json={"project_id": "ngms", "name": "NGMS", "repo": "org/NGMS", "max_workers": 2},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["control"]["state"] == STOPPED
    assert body["project"]["max_workers"] == 2


def test_start_is_the_only_thing_that_lets_a_project_claim(client) -> None:  # type: ignore[no-untyped-def]
    client.post("/api/projects", headers=hdr(), json={"project_id": "a", "name": "A"})
    client.queue.add([rec("T1")], project_id="a")

    assert client.queue.claim("w", project_id="a") is None

    # force: these fixtures have no checkout, repo or reviewer, and this test
    # is about control semantics rather than readiness. Preflight is covered
    # in test_preflight.py.
    response = client.post("/api/projects/a/start?force=true", headers=hdr())
    assert response.status_code == 200
    assert response.json()["control"]["state"] == RUNNING
    assert client.queue.claim("w", project_id="a") is not None


def test_stopping_one_project_leaves_the_others_running(client) -> None:  # type: ignore[no-untyped-def]
    for pid in ("a", "b"):
        client.post("/api/projects", headers=hdr(), json={"project_id": pid, "name": pid})
        client.post(f"/api/projects/{pid}/start?force=true", headers=hdr())
        client.queue.add([rec("T1")], project_id=pid)

    client.post("/api/projects/a/stop", headers=hdr(), json={"reason": "deploying"})

    assert client.queue.claim("w", project_id="a") is None
    assert client.queue.claim("w", project_id="b") is not None


@pytest.mark.parametrize(
    ("send_body", "payload"),
    [(False, None), (True, None), (True, {}), (True, {"reason": "deploying"})],
)
def test_stop_accepts_every_optional_body_shape(  # type: ignore[no-untyped-def]
    client, send_body: bool, payload: object
) -> None:
    client.post("/api/projects", headers=hdr(), json={"project_id": "a", "name": "A"})
    kwargs = {"json": payload} if send_body else {}

    response = client.post("/api/projects/a/stop", headers=hdr(), **kwargs)

    assert response.status_code == 200
    if payload == {"reason": "deploying"}:
        assert response.json()["control"]["reason"] == "deploying"


def test_stop_rejects_the_fleet_control_shape(client) -> None:  # type: ignore[no-untyped-def]
    client.post("/api/projects", headers=hdr(), json={"project_id": "a", "name": "A"})

    response = client.post(
        "/api/projects/a/stop",
        headers=hdr(),
        json={"state": "stopped", "reason": "old contract"},
    )

    assert response.status_code == 422


def test_starting_an_unknown_project_is_a_404_not_a_silent_no_op(client) -> None:  # type: ignore[no-untyped-def]
    """A typo in a project id must not look like success."""
    assert client.post("/api/projects/nope/start?force=true", headers=hdr()).status_code == 404


def test_the_overview_is_one_call(client) -> None:  # type: ignore[no-untyped-def]
    """The first screen a user sees must not depend on N successful requests."""
    for pid in ("a", "b"):
        client.post("/api/projects", headers=hdr(), json={"project_id": pid, "name": pid})
        client.queue.add([rec("T1"), rec("T2")], project_id=pid)

    payload = client.get("/api/projects", headers=hdr()).json()
    assert {p["project"]["project_id"] for p in payload["projects"]} == {"a", "b"}
    assert all(p["counts"] == {PENDING: 2} for p in payload["projects"])
    assert all(p["control"]["state"] == STOPPED for p in payload["projects"])


def test_the_overview_says_what_a_project_was_doing_before_it_stopped(client) -> None:  # type: ignore[no-untyped-def]
    """'Was running' and 'was drained for a deploy' must stay distinguishable,
    or the restart destroys the operator's intent."""
    client.post("/api/projects", headers=hdr(), json={"project_id": "a", "name": "A"})
    client.post("/api/projects/a/start?force=true", headers=hdr())
    client.queue.stop_all_on_boot(reason="process started")

    payload = client.get("/api/projects/a", headers=hdr()).json()
    assert payload["control"]["state"] == STOPPED
    assert payload["previous_state"] == RUNNING
    assert "process started" in payload["control"]["reason"]


def test_adding_work_through_the_api_respects_the_project(client) -> None:  # type: ignore[no-untyped-def]
    """Phase 1's definition of done, and the gap it caught.

    The queue was project-scoped while POST /api/work was not, so both
    projects' items landed in `default` and the second overwrote the first --
    the original defect, intact, one layer up. Scoping the storage is not the
    same as scoping the door into it.
    """
    for pid in ("ngms", "harness"):
        client.post("/api/projects", headers=hdr(), json={"project_id": pid, "name": pid})
        client.post(
            "/api/work",
            headers=hdr(),
            json={
                "project_id": pid,
                "items": [
                    {"item_id": "T1", "title": f"{pid} first"},
                    {"item_id": "T2", "title": f"{pid} second"},
                ],
            },
        )

    rows = client.queue.items()
    assert len(rows) == 4, "items from two projects collapsed onto each other"
    assert {(r.project_id, r.item_id) for r in rows} == {
        ("ngms", "T1"),
        ("ngms", "T2"),
        ("harness", "T1"),
        ("harness", "T2"),
    }
    assert client.queue.get("T1", project_id="ngms").title == "ngms first"


def test_the_backlog_can_be_filtered_to_one_project(client) -> None:  # type: ignore[no-untyped-def]
    for pid in ("a", "b"):
        client.post("/api/projects", headers=hdr(), json={"project_id": pid, "name": pid})
        client.post(
            "/api/work",
            headers=hdr(),
            json={"project_id": pid, "items": [{"item_id": "T1", "title": pid}]},
        )

    scoped = client.get("/api/work?project_id=a", headers=hdr()).json()
    assert [i["item_id"] for i in scoped["items"]] == ["T1"]
    assert scoped["counts"] == {PENDING: 1}

    everything = client.get("/api/work", headers=hdr()).json()
    assert len(everything["items"]) == 2, "the cross-project view should still work"


def test_retry_cannot_reach_into_another_project(client) -> None:  # type: ignore[no-untyped-def]
    """Same id, two projects: retrying one must not touch the other."""
    for pid in ("a", "b"):
        client.post("/api/projects", headers=hdr(), json={"project_id": pid, "name": pid})
        client.queue.add([rec("T1")], project_id=pid)
        client.queue.set_control(RUNNING, project_id=pid)
        client.queue.claim(f"w-{pid}", project_id=pid)
        client.queue.release("T1", DONE, owner=f"w-{pid}", project_id=pid)

    client.post("/api/work/T1/retry?project_id=a", headers=hdr())

    assert client.queue.get("T1", project_id="a").state == PENDING
    assert client.queue.get("T1", project_id="b").state == DONE


def test_a_dead_worker_shows_up_in_the_project_summary(tmp_path: Path) -> None:
    """`workers: 0` alone cannot distinguish "nothing to do" from "everything
    that could do it died"."""
    from fastapi.testclient import TestClient

    from agent_harness.api import create_api
    from agent_harness.fleet import WorkerFailure
    from agent_harness.store import EventStore

    q = WorkQueue(str(tmp_path / "w.sqlite"))
    q.add_project(Project(project_id="a", name="A"))
    store = EventStore(tmp_path / "e.sqlite")

    class FleetWithACasualty:
        def running(self) -> dict[str, int]:
            return {}

        def failures(self, project_id: str | None = None) -> list[WorkerFailure]:
            return [
                WorkerFailure(
                    project_id="a",
                    worker="host:1",
                    error="worker exited: the session host went away",
                    at=1.0,
                    released=("T1",),
                )
            ]

    with TestClient(
        create_api(store, queue=q, token="tok", fleet=FleetWithACasualty())  # noqa: S106
    ) as c:
        body = c.get("/api/projects/a", headers=hdr()).json()
    assert body["workers"] == 0
    assert body["worker_failures"] == 1
    assert "session host went away" in body["last_worker_error"]
