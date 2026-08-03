"""The lease must outlive the stage, not the other way round.

A claim is a lease, and the module that defines it says the lease "is kept
alive by a heartbeat while work is genuinely in progress". That was true only
between stages. The two stages that matter — an agent thinking, and a full
check suite — are both routinely longer than the lease, so a worker doing
exactly what it was asked lost its item mid-attempt and every later write was
silently discarded.

These tests are about the gap, not the boundaries: each one holds a single
stage open for longer than the lease.
"""

from __future__ import annotations

import threading
from pathlib import Path

from agent_harness.work import (
    CLAIMED,
    EXHAUSTED,
    PENDING,
    LeaseHeartbeat,
    Project,
    WorkQueue,
    WorkRecord,
)
from conftest import make_queue


def rec(item_id: str = "T1") -> WorkRecord:
    return WorkRecord(item_id=item_id, title=f"do {item_id}", brief="b")


def test_a_stage_longer_than_the_lease_keeps_its_claim(tmp_path: Path) -> None:
    """The regression. 915s of agent against a 900s lease used to lose the item."""
    clock = [1000.0]
    q = make_queue(str(tmp_path / "w.sqlite"), lease_seconds=900.0, now=lambda: clock[0])
    q.add([rec()])
    claimed = q.claim("worker-a")
    assert claimed is not None

    beat = LeaseHeartbeat(q, "T1", "worker-a", interval=0.01)
    with beat:
        # One stage, no boundaries, longer than the lease.
        clock[0] += 915.0
        _wait_until(lambda: (q.get("T1") or claimed).lease_until > clock[0])

    record = q.get("T1")
    assert record is not None
    assert record.state == CLAIMED
    assert record.owner == "worker-a"
    # Still ours, so a second worker cannot take it.
    assert q.claim("worker-b") is None
    assert not beat.lost


def test_a_heartbeat_that_is_refused_reports_the_claim_lost(tmp_path: Path) -> None:
    clock = [1000.0]
    q = make_queue(str(tmp_path / "w.sqlite"), lease_seconds=10.0, now=lambda: clock[0])
    q.add([rec()])
    assert q.claim("worker-a") is not None
    # Someone else takes the row out from under it.
    q.release("T1", PENDING)
    assert q.claim("worker-b") is not None

    beat = LeaseHeartbeat(q, "T1", "worker-a", interval=0.01)
    with beat:
        assert _wait_until(lambda: beat.lost)


def test_a_worker_that_lost_its_claim_cannot_report_a_result(tmp_path: Path) -> None:
    """The other half of the same bug: the discarded write was silent."""
    q = make_queue(str(tmp_path / "w.sqlite"), lease_seconds=10.0)
    q.add([rec()])
    assert q.claim("worker-a") is not None
    q.release("T1", PENDING)
    assert q.claim("worker-b") is not None

    # worker-a surfaces late and tries to report.
    assert q.release("T1", "done", owner="worker-a") is False
    record = q.get("T1")
    assert record is not None
    assert record.state == CLAIMED
    assert record.owner == "worker-b"


def test_the_heartbeat_stops_when_it_is_stopped(tmp_path: Path) -> None:
    """A daemon that outlives its attempt would keep a dead worker's claim
    alive, which is the failure the lease exists to prevent."""
    q = make_queue(str(tmp_path / "w.sqlite"), lease_seconds=10.0)
    q.add([rec()])
    assert q.claim("worker-a") is not None

    beat = LeaseHeartbeat(q, "T1", "worker-a", interval=0.01)
    beat.start()
    beat.stop()
    assert all(t.name != "harness-lease-T1" for t in threading.enumerate())


def test_giving_up_still_names_the_cause(tmp_path: Path) -> None:
    """`gave up after N attempts` with nothing after it is what an item looks
    like when its last attempt never got to say why."""
    q = WorkQueue(str(tmp_path / "w.sqlite"))
    q.add_project(Project(project_id="p", name="P", max_attempts=1))
    q.set_control("running", project_id="p")
    q.add([rec()], project_id="p")
    assert q.claim("w", project_id="p") is not None
    q.release("T1", PENDING, error="checks failed: cargo clippy", project_id="p")

    assert q.claim("w", project_id="p") is None
    record = q.get("T1", project_id="p")
    assert record is not None and record.state == EXHAUSTED
    assert "cargo clippy" in (record.last_error or "")


def _wait_until(predicate: object, timeout: float = 5.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.01)
    return False
