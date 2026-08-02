"""Keeping a timed-out session alive is a decision; forgetting it is not."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.reaper import DEFAULT_MAX_AGE_SECONDS, reap_abandoned_sessions
from agent_harness.work import WorkQueue, WorkRecord


class FakeHost:
    """Records what was asked of it, and can be told to refuse."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.killed: list[str] = []
        self.deleted: list[str] = []
        self.fail = fail or set()

    def kill_session(self, session_id: str) -> None:
        if session_id in self.fail:
            raise RuntimeError("host says no")
        self.killed.append(session_id)

    def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)


@pytest.fixture
def clock() -> list[float]:
    return [1000.0]


@pytest.fixture
def queue(tmp_path: Path, clock: list[float]) -> WorkQueue:
    q = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0, now=lambda: clock[0])
    q.add([WorkRecord(item_id="T1", title="t", brief="b")])
    return q


def test_a_fresh_session_is_left_alone(queue: WorkQueue, clock: list[float]) -> None:
    """The whole reason sessions survive a timeout is that a human may come
    back to one. Reaping eagerly destroys exactly what was being preserved."""
    queue.record_abandoned_session("s-1", "T1", reason="timed out")
    host = FakeHost()

    report = reap_abandoned_sessions(queue, host, max_age=DEFAULT_MAX_AGE_SECONDS)

    assert host.killed == []
    assert report.kept == 1
    assert queue.abandoned_sessions()


def test_a_session_nobody_returned_to_is_reaped(queue: WorkQueue, clock: list[float]) -> None:
    queue.record_abandoned_session("s-1", "T1", reason="timed out")
    clock[0] += DEFAULT_MAX_AGE_SECONDS + 1
    host = FakeHost()

    report = reap_abandoned_sessions(queue, host)

    assert host.killed == ["s-1"]
    assert host.deleted == ["s-1"]
    assert report.reaped == ["s-1"]
    assert queue.abandoned_sessions() == [], "a reaped session was still being tracked"


def test_one_failure_does_not_strand_the_rest(queue: WorkQueue, clock: list[float]) -> None:
    """A host that restarted, or a session already gone, must not leave every
    other survivor running."""
    for n in range(4):
        queue.record_abandoned_session(f"s-{n}", "T1")
    clock[0] += DEFAULT_MAX_AGE_SECONDS + 1
    host = FakeHost(fail={"s-1"})

    report = reap_abandoned_sessions(queue, host)

    assert sorted(report.reaped) == ["s-0", "s-2", "s-3"]
    assert "s-1" in report.failed
    remaining = {row["session_id"] for row in queue.abandoned_sessions()}
    assert remaining == {"s-1"}, "a session we failed to kill must stay listed for a retry"


def test_a_failed_reap_is_retried_on_the_next_sweep(queue: WorkQueue, clock: list[float]) -> None:
    queue.record_abandoned_session("s-1", "T1")
    clock[0] += DEFAULT_MAX_AGE_SECONDS + 1

    first = reap_abandoned_sessions(queue, FakeHost(fail={"s-1"}))
    assert first.reaped == []

    second = reap_abandoned_sessions(queue, FakeHost())
    assert second.reaped == ["s-1"]


def test_recording_the_same_session_twice_is_not_two_sessions(queue: WorkQueue) -> None:
    """An item retried through the same session id must not double-count."""
    queue.record_abandoned_session("s-1", "T1", reason="first")
    queue.record_abandoned_session("s-1", "T1", reason="second")
    rows = queue.abandoned_sessions()
    assert len(rows) == 1
    assert rows[0]["reason"] == "second"


def test_the_count_is_visible_in_the_summary(tmp_path: Path) -> None:
    """A survivor nobody can see is indistinguishable from a leak."""
    from fastapi.testclient import TestClient

    from agent_harness.api import create_api
    from agent_harness.store import EventStore

    q = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    q.add([WorkRecord(item_id="T1", title="t", brief="b")])
    q.record_abandoned_session("s-1", "T1", reason="timed out")
    store = EventStore(tmp_path / "e.sqlite")

    with TestClient(create_api(store, queue=q, token="tok")) as client:  # noqa: S106
        payload = client.get("/api/summary", headers={"Authorization": "Bearer tok"}).json()

    assert payload["abandoned_sessions"] == 1


def test_the_executor_reaps_before_it_claims(tmp_path: Path) -> None:
    """A reaper nobody calls is the bug this fixes, not the fix.

    `kill_session` and `delete_session` existed on the client all along and
    were never called; adding a reaper and leaving it uninvoked would repeat
    exactly that.
    """
    from agent_harness.session_executor import SessionExecutor

    clock = [1000.0]
    q = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0, now=lambda: clock[0])
    q.record_abandoned_session("s-old", "T1")
    clock[0] += DEFAULT_MAX_AGE_SECONDS + 1

    host = FakeHost()
    executor = SessionExecutor(q, host, tmp_path)  # type: ignore[arg-type]
    executor.run(limit=0)

    assert host.killed == ["s-old"], "run() did not reap before claiming"


def test_a_host_that_cannot_reap_is_not_an_error(tmp_path: Path) -> None:
    """`SessionHost` deliberately excludes killing, so a create-and-wait host
    is a legitimate configuration rather than a broken one."""
    from agent_harness.session_executor import SessionExecutor

    class MinimalHost:
        pass

    q = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    executor = SessionExecutor(q, MinimalHost(), tmp_path)  # type: ignore[arg-type]

    assert executor.reap() is None
