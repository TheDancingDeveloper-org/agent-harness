"""Per-project worker pools, and the daemon loop underneath them."""

from __future__ import annotations

import os
import socket
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
    worker_identity,
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


# ------------------------------------------------- a worker that dies


class DyingExecutor:
    """Claims an item, gets as far as a session, then dies.

    Modelled on the real failure: the worker thread went away while the
    AIDevEnv session it had created was still running.
    """

    def __init__(self, queue: WorkQueue, project_id: str, sessions: list[str]) -> None:
        self.queue, self.project_id = queue, project_id
        self.owner = f"dying-{project_id}"
        self.sessions = sessions

    def serve(self, *, poll_seconds: float, stop: threading.Event) -> None:
        record = self.queue.claim(self.owner, project_id=self.project_id)
        assert record is not None
        self.sessions.append(f"pty-{record.item_id}")
        raise RuntimeError("the session host went away")


def dying_fleet(queue: WorkQueue, sessions: list[str], events: list[Any]) -> Fleet:
    return Fleet(
        queue,
        lambda pid: DyingExecutor(queue, pid, sessions),
        poll_seconds=0.01,
        on_event=events.append,
    )


def test_a_dead_worker_does_not_strand_its_claim(queue: WorkQueue) -> None:
    """The bug: the item stayed `claimed` by a dead owner with no completion
    or failure recorded, so it was unavailable to everyone -- including a
    human -- until the lease expired."""
    queue.add([rec("T1")], project_id="b")
    fleet = dying_fleet(queue, [], [])
    fleet.start("b")

    assert wait_for(lambda: (queue.get("T1", project_id="b") or rec("x")).state == "failed")
    record = queue.get("T1", project_id="b")
    assert record is not None
    assert record.owner is None
    assert "the session host went away" in (record.last_error or "")


def test_the_death_is_visible_rather_than_only_logged(queue: WorkQueue) -> None:
    queue.add([rec("T1")], project_id="b")
    events: list[Any] = []
    fleet = dying_fleet(queue, [], events)
    fleet.start("b")

    assert wait_for(lambda: bool(fleet.failures("b")))
    failure = fleet.failures("b")[0]
    assert failure.worker == "dying-b"
    assert "T1" in failure.released
    assert any(e["outcome"] == "worker_died" for e in events)


def test_a_project_whose_workers_all_died_is_not_running(queue: WorkQueue) -> None:
    """Otherwise it reports `running` with zero workers -- exactly the state
    the start preflight refuses to create, reached the slow way."""
    queue.add([rec("T1")], project_id="b")
    fleet = dying_fleet(queue, [], [])
    fleet.start("b")

    assert wait_for(lambda: queue.control(project_id="b")[0] == STOPPED)
    state, reason = queue.control(project_id="b")
    assert state == STOPPED
    assert "died" in (reason or "")
    assert fleet.running().get("b", 0) == 0


def test_one_project_s_dying_workers_do_not_touch_another(queue: WorkQueue) -> None:
    queue.add([rec("T1")], project_id="b")
    queue.add([rec("A1")], project_id="a")
    seen: list[str] = []
    healthy = fleet_for(queue, seen)
    dying = dying_fleet(queue, [], [])
    try:
        healthy.start("a")
        dying.start("b")
        assert wait_for(lambda: "a:A1" in seen)
        assert queue.control(project_id="a")[0] == RUNNING
    finally:
        healthy.stop_all()


def test_a_factory_that_cannot_build_an_executor_is_recorded(queue: WorkQueue) -> None:
    """Previously a log line and nothing else: the project sat `running` with
    a pool of workers that had all returned immediately."""

    def explode(_pid: str) -> Any:
        raise RuntimeError("no checkout at /gone")

    fleet = Fleet(queue, explode, poll_seconds=0.01)
    fleet.start("b")
    assert wait_for(lambda: bool(fleet.failures("b")))
    assert "no checkout" in fleet.failures("b")[0].error
    assert wait_for(lambda: queue.control(project_id="b")[0] == STOPPED)


# ---------------------------------------------- resizing a running project


def test_a_running_project_can_grow_and_shrink_without_stopping(queue: WorkQueue) -> None:
    """Capacity used to cost a drain and restart cycle -- lifecycle risk
    during live work, bought purely to change a number."""
    fleet = fleet_for(queue, [])
    try:
        assert fleet.start("a") == 2
        assert wait_for(lambda: fleet.running().get("a") == 2)

        assert fleet.resize("a", 4) == 4
        assert wait_for(lambda: fleet.running().get("a") == 4)

        fleet.resize("a", 1)
        assert wait_for(lambda: fleet.running().get("a") == 1), fleet.running()
        # Never stopped: the project is still running throughout.
        assert queue.control("a")[0] == RUNNING
    finally:
        fleet.stop_all()


def test_shrinking_lets_in_flight_work_finish(queue: WorkQueue) -> None:
    """The reason the excess is signalled and not joined or killed: an agent
    interrupted mid-item loses the context that makes its work resumable."""
    seen: list[str] = []
    started = threading.Event()
    release = threading.Event()

    class SlowExecutor:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id
            self.owner = f"w-{project_id}"

        def serve(self, *, poll_seconds: float, stop: threading.Event) -> None:
            while not stop.is_set():
                record = queue.claim(self.owner, project_id=self.project_id)
                if record is None:
                    stop.wait(0.01)
                    continue
                started.set()
                # Held past the resize, deliberately. `stop` is ignored here,
                # exactly as a real agent mid-item ignores it.
                release.wait(5)
                seen.append(record.item_id)
                queue.release(record.item_id, "done", owner=self.owner, project_id=self.project_id)

    fleet = Fleet(queue, lambda pid: SlowExecutor(pid), poll_seconds=0.01)
    queue.add([rec("T1")], project_id="a")
    try:
        fleet.start("a")
        assert started.wait(5), "no worker ever claimed the item"

        fleet.resize("a", 1)
        # The item is still claimed and still being worked on, not abandoned.
        assert queue.get("T1", project_id="a").state == "claimed"  # type: ignore[union-attr]

        release.set()
        assert wait_for(lambda: queue.get("T1", project_id="a").state == "done")  # type: ignore[union-attr]
        assert seen == ["T1"], "the in-flight item did not finish"
    finally:
        release.set()
        fleet.stop_all()


def test_resizing_to_the_same_number_does_nothing(queue: WorkQueue) -> None:
    fleet = fleet_for(queue, [])
    try:
        fleet.start("a")
        assert wait_for(lambda: fleet.running().get("a") == 2)
        assert fleet.resize("a", 2) == 2
        assert fleet.resize("a", 2) == 2
        assert fleet.running().get("a") == 2
    finally:
        fleet.stop_all()


def test_resizing_reads_the_projects_limit_when_none_is_given(queue: WorkQueue) -> None:
    """What a project update calls: change the row, reconcile the pool."""
    fleet = fleet_for(queue, [])
    try:
        fleet.start("a")
        assert wait_for(lambda: fleet.running().get("a") == 2)
        queue.add_project(Project(project_id="a", name="A", max_workers=3))
        assert fleet.resize("a") == 3
        assert wait_for(lambda: fleet.running().get("a") == 3)
    finally:
        fleet.stop_all()


def test_resizing_a_project_that_is_not_running_starts_nothing(queue: WorkQueue) -> None:
    """A registration must never be what begins spending money."""
    fleet = fleet_for(queue, [])
    try:
        assert fleet.resize("a", 4) == 0
        assert fleet.running() == {}
        assert queue.control("a")[0] != RUNNING
    finally:
        fleet.stop_all()


def test_a_resize_never_drops_below_one_worker(queue: WorkQueue) -> None:
    """Zero workers on a running project is the false-running state the start
    gate exists to refuse. Reaching it by resize would be the same bug."""
    fleet = fleet_for(queue, [])
    try:
        fleet.start("a")
        assert fleet.resize("a", 0) == 1
        assert wait_for(lambda: fleet.running().get("a") == 1)
    finally:
        fleet.stop_all()


def test_concurrent_resizes_do_not_both_act_on_one_count(queue: WorkQueue) -> None:
    """Two requests reading the same live count and both adding to it is how
    a pool ends up at twice its budget."""
    fleet = fleet_for(queue, [])
    try:
        fleet.start("a")
        assert wait_for(lambda: fleet.running().get("a") == 2)

        barrier = threading.Barrier(6)

        def resize() -> None:
            barrier.wait()
            fleet.resize("a", 5)

        threads = [threading.Thread(target=resize) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert wait_for(lambda: fleet.running().get("a") == 5), fleet.running()
    finally:
        fleet.stop_all()


def test_a_project_update_resizes_a_running_pool(queue: WorkQueue, tmp_path: Path) -> None:
    """End to end: the operator changes max_workers and it takes effect."""
    from fastapi.testclient import TestClient

    from agent_harness.api import create_api
    from agent_harness.store import EventStore

    fleet = fleet_for(queue, [])
    app = create_api(EventStore(tmp_path / "e.sqlite"), queue=queue, token="tok", fleet=fleet)
    try:
        fleet.start("a")
        assert wait_for(lambda: fleet.running().get("a") == 2)
        with TestClient(app) as client:
            response = client.post(
                "/api/projects",
                headers={"Authorization": "Bearer tok"},
                json={"project_id": "a", "name": "A", "max_workers": 4},
            )
            assert response.status_code == 200, response.text
            assert response.json()["project"]["max_workers"] == 4
        assert wait_for(lambda: fleet.running().get("a") == 4), fleet.running()
    finally:
        fleet.stop_all()


# --------------------------------------- claims a dead process left behind


def test_a_claim_from_a_dead_process_is_reclaimed_at_the_next_start(
    queue: WorkQueue,
) -> None:
    """The restart case. An item claimed by a process that is gone stayed
    `claimed` with a live lease, so the project reported work in progress
    that nothing was doing and the item was unavailable to everyone --
    including a human -- until the lease ran out."""
    queue.add([rec("T1")], project_id="a")
    # A pid that cannot exist, on this host: what a previous process's claim
    # looks like after a deploy.
    dead = f"{socket.gethostname()}:{_never_a_pid()}"
    queue.set_control(RUNNING, project_id="a")
    queue.claim(dead, project_id="a")
    assert queue.get("T1", project_id="a").state == "claimed"  # type: ignore[union-attr]

    events: list[dict[str, Any]] = []
    fleet = Fleet(
        queue,
        lambda pid: FakeExecutor(queue, pid, []),
        poll_seconds=0.01,
        on_event=events.append,
    )
    try:
        fleet.start("a")
        assert wait_for(lambda: queue.get("T1", project_id="a").state == "done")  # type: ignore[union-attr]
        # And the recovery is observable, not silent.
        assert [e for e in events if e["outcome"] == "claim_reclaimed"]
    finally:
        fleet.stop_all()


def test_reclaiming_puts_it_back_to_pending_not_failed(queue: WorkQueue) -> None:
    """The process is gone; nothing is known to be wrong with the ITEM. The
    attempt is already counted, so a genuinely poisonous item still reaches
    the exhaustion ceiling rather than looping forever."""
    queue.add([rec("T1")], project_id="a")
    queue.set_control(RUNNING, project_id="a")
    queue.claim(f"{socket.gethostname()}:{_never_a_pid()}", project_id="a")

    fleet = Fleet(queue, lambda pid: _Idle(), poll_seconds=0.01)
    try:
        fleet.start("a")
        assert wait_for(lambda: queue.get("T1", project_id="a").state == PENDING)
        item = queue.get("T1", project_id="a")
        assert item is not None
        assert item.attempts == 1, "the attempt must still count"
        assert "no longer exists" in (item.last_error or "")
    finally:
        fleet.stop_all()


def test_a_live_claim_is_never_reclaimed(queue: WorkQueue) -> None:
    """This process is alive and holding the item. Reclaiming it would give
    one item to two workers, which is worse than one stuck item."""
    queue.add([rec("T1")], project_id="a")
    queue.set_control(RUNNING, project_id="a")
    queue.claim(worker_identity(), project_id="a")

    fleet = Fleet(queue, lambda pid: _Idle(), poll_seconds=0.01)
    try:
        fleet.start("a")
        time.sleep(0.1)
        assert queue.get("T1", project_id="a").state == "claimed"  # type: ignore[union-attr]
        assert queue.orphaned("a") == []
    finally:
        fleet.stop_all()


def test_a_claim_from_another_host_is_left_to_its_lease(queue: WorkQueue) -> None:
    """Unknowable from here. Releasing it could take an item away from a
    worker on another machine that is alive and working on it."""
    queue.add([rec("T1")], project_id="a")
    queue.set_control(RUNNING, project_id="a")
    queue.claim("some-other-machine:1", project_id="a")
    assert queue.orphaned("a") == []


def test_orphaned_and_stale_are_different_questions(queue: WorkQueue, tmp_path: Path) -> None:
    """Stale means a lease ran out, which is a timeout and a guess. Orphaned
    means the pid is gone, which is a fact."""
    clock = [1000.0]
    q = WorkQueue(str(tmp_path / "o.sqlite"), lease_seconds=100.0, now=lambda: clock[0])
    q.add_project(Project(project_id="a", name="A"))
    q.add([rec("T1"), rec("T2")], project_id="a")
    q.set_control(RUNNING, project_id="a")
    q.claim("some-other-machine:1", project_id="a")

    # Leased to a host we cannot ask about: not orphaned...
    assert q.orphaned("a") == []
    assert q.stale("a") == []
    # ...and stale only once the lease actually runs out.
    clock[0] += 101
    assert [r.item_id for r in q.stale("a")] == ["T1"]
    assert q.orphaned("a") == []


class _Idle:
    """A worker that claims nothing, so a test can observe the reclaim alone."""

    def serve(self, *, poll_seconds: float, stop: threading.Event) -> None:
        stop.wait(5)


def _never_a_pid() -> int:
    """A pid that is not running. Searched rather than assumed, because a
    hardcoded one is a flaky test waiting for the wrong process to exist."""
    for candidate in range(4_000_000, 4_000_500):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    raise AssertionError("could not find an unused pid")
