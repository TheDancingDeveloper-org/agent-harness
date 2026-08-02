"""Claim tests. Claims are leases, and the properties that matter are what
happens when a worker dies, and what happens when two race."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.work import (
    CLAIMED,
    DONE,
    PENDING,
    WorkQueue,
    WorkRecord,
)


@pytest.fixture
def clock() -> list[float]:
    return [1000.0]


@pytest.fixture
def queue(tmp_path: Path, clock: list[float]) -> WorkQueue:
    return WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0, now=lambda: clock[0])


def rec(item_id: str, **kw: object) -> WorkRecord:
    kw.setdefault("title", f"do {item_id}")
    kw.setdefault("brief", "brief")
    return WorkRecord(item_id=item_id, **kw)  # type: ignore[arg-type]


def test_claiming_returns_an_item_and_marks_it(queue: WorkQueue) -> None:
    queue.add([rec("T1")])
    claimed = queue.claim("worker-a")
    assert claimed is not None
    assert claimed.item_id == "T1"
    assert queue.get("T1").state == CLAIMED  # type: ignore[union-attr]


def test_two_workers_cannot_hold_the_same_item(queue: WorkQueue) -> None:
    queue.add([rec("T1")])
    assert queue.claim("a") is not None
    assert queue.claim("b") is None


def test_a_dead_workers_item_is_reclaimed_when_its_lease_expires(
    queue: WorkQueue, clock: list[float]
) -> None:
    """The property the whole design turns on. A lock held by a dead process
    is a lock nobody can release."""
    queue.add([rec("T1")])
    assert queue.claim("dead-worker") is not None
    assert queue.claim("other") is None  # still leased
    clock[0] += 101  # ...lease expires
    reclaimed = queue.claim("other")
    assert reclaimed is not None
    assert reclaimed.owner == "other"


def test_a_heartbeat_keeps_a_slow_worker_from_being_evicted(
    queue: WorkQueue, clock: list[float]
) -> None:
    """'Slow' and 'dead' look identical from outside. Only a live process
    can keep stamping a heartbeat, which is what separates them."""
    queue.add([rec("T1")])
    queue.claim("a")
    clock[0] += 80
    assert queue.heartbeat("T1", "a") is True
    clock[0] += 80  # 160s in, lease is 100s
    assert queue.claim("b") is None  # still alive, still owned


def test_a_heartbeat_from_a_worker_that_lost_its_claim_fails(
    queue: WorkQueue, clock: list[float]
) -> None:
    """That False is the signal to stop working: someone else owns it now,
    and two workers finishing the same item is worse than neither."""
    queue.add([rec("T1")])
    queue.claim("a")
    clock[0] += 101
    queue.claim("b")
    assert queue.heartbeat("T1", "a") is False


def test_dependencies_gate_claiming(queue: WorkQueue) -> None:
    queue.add([rec("T1"), rec("T2", depends_on=["T1"])])
    first = queue.claim("a")
    assert first is not None and first.item_id == "T1"
    assert queue.claim("b") is None  # T2 blocked on T1
    queue.release("T1", DONE)
    second = queue.claim("b")
    assert second is not None and second.item_id == "T2"


def test_a_dependency_outside_the_queue_does_not_block(queue: WorkQueue) -> None:
    """Plans routinely reference work tracked elsewhere. Refusing to start
    would strand the item forever."""
    queue.add([rec("T1", depends_on=["EXTERNAL-9"])])
    assert queue.claim("a") is not None


def test_re_adding_a_synced_plan_does_not_reset_progress(queue: WorkQueue) -> None:
    """Re-syncing an edited plan must not un-finish completed work."""
    queue.add([rec("T1")])
    queue.claim("a")
    queue.release("T1", DONE)
    queue.add([rec("T1", title="renamed")])
    record = queue.get("T1")
    assert record is not None
    assert record.state == DONE  # progress preserved...
    assert record.title == "renamed"  # ...description refreshed
    assert queue.claim("b") is None  # still done, not re-queued


def test_re_adding_refreshes_the_brief(queue: WorkQueue) -> None:
    queue.add([rec("T1")])
    updated = rec("T1")
    updated.brief = "a much better brief"
    queue.add([updated])
    assert queue.get("T1").brief == "a much better brief"  # type: ignore[union-attr]


def test_release_to_pending_puts_it_back_for_another_attempt(queue: WorkQueue) -> None:
    queue.add([rec("T1")])
    queue.claim("a")
    queue.release("T1", PENDING, error="ran out of budget")
    again = queue.claim("b")
    assert again is not None
    assert again.attempts == 2


def test_attempts_are_counted_so_a_poisonous_item_is_visible(
    queue: WorkQueue, clock: list[float]
) -> None:
    queue.add([rec("T1")])
    for _ in range(3):
        queue.claim("a")
        queue.release("T1", PENDING)
    assert queue.get("T1").attempts == 3  # type: ignore[union-attr]


def test_the_least_attempted_item_is_claimed_first(queue: WorkQueue) -> None:
    """Otherwise one failing item is retried forever while untouched work
    waits behind it."""
    queue.add([rec("T1"), rec("T2")])
    queue.claim("a")
    queue.release("T1", PENDING)
    nxt = queue.claim("b")
    assert nxt is not None and nxt.item_id == "T2"


def test_stale_claims_are_reportable(queue: WorkQueue, clock: list[float]) -> None:
    queue.add([rec("T1")])
    queue.claim("a")
    assert queue.stale() == []
    clock[0] += 101
    assert [r.item_id for r in queue.stale()] == ["T1"]


def test_counts_projection(queue: WorkQueue) -> None:
    queue.add([rec("T1"), rec("T2"), rec("T3")])
    queue.claim("a")
    queue.release("T1", DONE)
    queue.claim("b")
    assert queue.counts() == {DONE: 1, CLAIMED: 1, PENDING: 1}


def test_state_survives_a_new_queue_object(tmp_path: Path) -> None:
    """Resume, in one assertion: claims are rows, not process memory."""
    path = str(tmp_path / "w.sqlite")
    first = WorkQueue(path, lease_seconds=100.0, now=lambda: 1000.0)
    first.add([rec("T1"), rec("T2")])
    first.claim("a")
    first.release("T1", DONE)

    resumed = WorkQueue(path, lease_seconds=100.0, now=lambda: 1000.0)
    assert resumed.get("T1").state == DONE  # type: ignore[union-attr]
    nxt = resumed.claim("b")
    assert nxt is not None and nxt.item_id == "T2"


def test_an_empty_queue_claims_nothing_rather_than_erroring(queue: WorkQueue) -> None:
    assert queue.claim("a") is None
