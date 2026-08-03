"""Per-project worker pools, and the daemon loop underneath them."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_harness.fleet import Fleet
from agent_harness.work import (
    EXHAUSTED,
    PENDING,
    RUNNING,
    STOPPED,
    Project,
    WorkQueue,
    WorkRecord,
)


def rec(item_id: str) -> WorkRecord:
    return WorkRecord(item_id=item_id, title=f"do {item_id}", brief="b")


@pytest.fixture
def queue(tmp_path: Path) -> WorkQueue:
    q = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    q.add_project(Project(project_id="a", name="A", max_workers=2))
    q.add_project(Project(project_id="b", name="B", max_workers=1))
    return q


class FakeExecutor:
    """Claims through the real queue, so control state is genuinely honoured."""

    def __init__(
        self, queue: WorkQueue, project_id: str, seen: list[str], delay: float = 0.0
    ) -> None:
        self.queue, self.project_id, self.seen = queue, project_id, seen
        self.delay = delay

    def serve(self, *, poll_seconds: float, stop: threading.Event) -> None:
        while not stop.is_set():
            record = self.queue.claim(f"w-{self.project_id}", project_id=self.project_id)
            if record is None:
                stop.wait(0.01)
                continue
            if self.delay:
                time.sleep(self.delay)
            self.seen.append(f"{self.project_id}:{record.item_id}")
            self.queue.release(
                record.item_id, "done", owner=f"w-{self.project_id}", project_id=self.project_id
            )


def fleet_for(queue: WorkQueue, seen: list[str], delay: float = 0.0) -> Fleet:
    return Fleet(queue, lambda pid: FakeExecutor(queue, pid, seen, delay), poll_seconds=0.01)


def wait_for(predicate, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------- starting


def test_starting_a_project_honours_its_worker_budget(queue: WorkQueue) -> None:
    fleet = fleet_for(queue, [])
    try:
        assert fleet.start("a") == 2, "max_workers was not honoured"
        assert fleet.start("b") == 1
    finally:
        fleet.stop_all()


def test_starting_twice_does_not_double_the_budget(queue: WorkQueue) -> None:
    """Two pools on one project silently double its concurrency, which is
    worse than an error because nothing reports it."""
    fleet = fleet_for(queue, [])
    try:
        fleet.start("a")
        assert fleet.start("a") == 2
        assert fleet.running()["a"] == 2
    finally:
        fleet.stop_all()


def test_starting_an_unknown_project_raises(queue: WorkQueue) -> None:
    """A typo must not create a pool for a project that does not exist."""
    fleet = fleet_for(queue, [])
    with pytest.raises(KeyError):
        fleet.start("nope")


def test_work_is_only_claimed_for_the_project_that_was_started(queue: WorkQueue) -> None:
    """The point of the whole design: separate streams."""
    queue.add([rec("T1"), rec("T2")], project_id="a")
    queue.add([rec("T1")], project_id="b")
    seen: list[str] = []
    fleet = fleet_for(queue, seen)
    try:
        fleet.start("a")
        assert wait_for(lambda: len([s for s in seen if s.startswith("a:")]) == 2)
        time.sleep(0.1)
        assert not [s for s in seen if s.startswith("b:")], "a stopped project was worked"
    finally:
        fleet.stop_all()

    assert queue.get("T1", project_id="b").state == PENDING  # type: ignore[union-attr]


def test_a_project_starts_only_when_asked(queue: WorkQueue) -> None:
    """Nothing resumes on its own, including a project with work waiting."""
    queue.add([rec("T1")], project_id="a")
    seen: list[str] = []
    fleet = fleet_for(queue, seen)

    assert fleet.running() == {}
    assert queue.control(project_id="a")[0] == STOPPED
    time.sleep(0.1)
    assert seen == []


def test_stopping_leaves_the_other_projects_running(queue: WorkQueue) -> None:
    fleet = fleet_for(queue, [])
    try:
        fleet.start("a")
        fleet.start("b")
        fleet.stop("a", reason="deploying")

        assert "a" not in fleet.running()
        assert fleet.running().get("b") == 1
        assert queue.control(project_id="a") == (STOPPED, "deploying")
        assert queue.control(project_id="b")[0] == RUNNING
    finally:
        fleet.stop_all()


def test_a_worker_that_dies_does_not_take_the_fleet_with_it(queue: WorkQueue) -> None:
    """Its claim is a lease, so whatever it held comes back on its own."""

    class Exploding:
        def serve(self, *, poll_seconds: float, stop: threading.Event) -> None:
            raise RuntimeError("worker exploded")

    fleet = Fleet(queue, lambda pid: Exploding(), poll_seconds=0.01)
    fleet.start("a")
    time.sleep(0.1)

    # The fleet is still usable; the pool simply has no live workers.
    assert fleet.running().get("a") in (None, 0)
    fleet.stop_all()


def test_an_executor_that_cannot_be_built_is_not_fatal(queue: WorkQueue) -> None:
    def broken(project_id: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("no repo checked out")

    fleet = Fleet(queue, broken, poll_seconds=0.01)
    fleet.start("a")
    time.sleep(0.1)
    fleet.stop_all()


# ------------------------------------------------------------- max attempts


def test_an_item_that_keeps_killing_its_worker_is_given_up_on(tmp_path: Path) -> None:
    """The seven-day failure mode.

    An item whose worker dies is never released, so its lease expires and it
    is re-claimed forever -- spending real money each cycle while looking
    exactly like an item that is merely busy.
    """
    clock = [1000.0]
    q = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=10.0, now=lambda: clock[0])
    q.add_project(Project(project_id="p", name="P", max_attempts=3))
    q.set_control(RUNNING, project_id="p")
    q.add([rec("T1")], project_id="p")

    for _ in range(3):
        assert q.claim("w", project_id="p") is not None
        clock[0] += 11.0  # the worker died; the lease lapses

    assert q.claim("w", project_id="p") is None, "a poison item was claimed forever"
    record = q.get("T1", project_id="p")
    assert record is not None
    assert record.state == EXHAUSTED
    assert "gave up after" in (record.last_error or "")


def test_raising_the_limit_rescues_exhausted_work(tmp_path: Path) -> None:
    """Giving up must be recoverable without editing the database by hand."""
    clock = [1000.0]
    q = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=10.0, now=lambda: clock[0])
    q.add_project(Project(project_id="p", name="P", max_attempts=1))
    q.set_control(RUNNING, project_id="p")
    q.add([rec("T1")], project_id="p")

    q.claim("w", project_id="p")
    clock[0] += 11.0
    assert q.claim("w", project_id="p") is None

    q.add_project(Project(project_id="p", name="P", max_attempts=10))
    q.release("T1", PENDING, project_id="p")
    assert q.claim("w", project_id="p") is not None


def test_zero_disables_giving_up(tmp_path: Path) -> None:
    clock = [1000.0]
    q = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=10.0, now=lambda: clock[0])
    q.add_project(Project(project_id="p", name="P", max_attempts=0))
    q.set_control(RUNNING, project_id="p")
    q.add([rec("T1")], project_id="p")

    for _ in range(8):
        assert q.claim("w", project_id="p") is not None
        clock[0] += 11.0


# ------------------------------------------------------------- daemon loop


def test_serve_waits_for_work_instead_of_exiting(tmp_path: Path) -> None:
    """The defect this closes: run() drained the backlog and returned, so an
    item added an hour later was never claimed."""
    from agent_harness.session_executor import SessionExecutor

    q = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    q.set_control(RUNNING)

    class Host:
        pass

    executor = SessionExecutor(q, Host(), tmp_path)  # type: ignore[arg-type]
    stop = threading.Event()
    result: list[list[Any]] = []

    def run() -> None:
        result.append(executor.serve(poll_seconds=0.01, stop=stop))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        # An empty queue must not end the loop.
        time.sleep(0.15)
        assert thread.is_alive(), "serve() exited on an empty queue"
    finally:
        stop.set()
        thread.join(timeout=5)

    assert not thread.is_alive()


def test_serve_gives_up_after_a_bounded_idle_when_asked(tmp_path: Path) -> None:
    """`max_idle_polls` exists so a test can assert the loop terminates
    without waiting on wall-clock time."""
    from agent_harness.session_executor import SessionExecutor

    q = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    q.set_control(RUNNING)

    class Host:
        pass

    executor = SessionExecutor(q, Host(), tmp_path)  # type: ignore[arg-type]
    assert executor.serve(poll_seconds=0.001, max_idle_polls=3) == []


def test_pausing_a_project_stops_claiming_without_a_restart(tmp_path: Path) -> None:
    """Control is re-read every pass, so a pause takes effect at the next item
    boundary and resuming needs no restart either."""
    q = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    q.add_project(Project(project_id="a", name="A", max_workers=1))
    # Enough items, worked slowly enough, that a pause lands mid-backlog.
    # With instant work the queue drains before the pause and the test proves
    # nothing -- resuming then has nothing left to claim.
    q.add([rec(f"T{n}") for n in range(200)], project_id="a")

    seen: list[str] = []
    fleet = fleet_for(q, seen, delay=0.005)
    try:
        fleet.start("a")
        assert wait_for(lambda: len(seen) >= 1)
        q.set_control("paused", reason="testing", project_id="a")
        time.sleep(0.1)
        settled = len(seen)
        time.sleep(0.15)
        assert len(seen) == settled, "work continued after the project was paused"

        q.set_control(RUNNING, project_id="a")
        assert wait_for(lambda: len(seen) > settled), "resuming needed a restart"
    finally:
        fleet.stop_all()
