"""Getting an item back, and letting go of one.

Three ways the queue held on to work it should not have, or handed back work
it had not really released:

* retrying an exhausted item put it back and watched it be retired again,
  while reporting success and erasing the reason;
* a claim held by a process that no longer exists waited out its full lease
  alongside newly dispatched work;
* an item kept going through durable gates after the graph said it was no
  longer eligible.
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_harness.work import (
    CLAIMED,
    DONE,
    EXHAUSTED,
    PENDING,
    Project,
    WorkQueue,
    WorkRecord,
)


def rec(item_id: str, **kw: object) -> WorkRecord:
    return WorkRecord(item_id=item_id, title=f"do {item_id}", brief="b", **kw)  # type: ignore[arg-type]


def queue_at_the_ceiling(tmp_path: Path) -> WorkQueue:
    """An item that has been given up on, the way the queue gives up."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.add_project(Project(project_id="p", name="P", max_attempts=1))
    queue.set_control("running", project_id="p")
    queue.add([rec("T1")], project_id="p")
    assert queue.claim("w", project_id="p") is not None
    queue.release("T1", PENDING, error="checks failed: cargo clippy", project_id="p")
    assert queue.claim("w", project_id="p") is None  # retires it
    record = queue.get("T1", project_id="p")
    assert record is not None and record.state == EXHAUSTED
    return queue


def test_retrying_an_exhausted_item_makes_it_claimable(tmp_path: Path) -> None:
    """The regression for #126.

    `release(..., PENDING)` left `attempts` at the ceiling, so the next claim
    scan retired the item again before any worker saw it.
    """
    queue = queue_at_the_ceiling(tmp_path)

    assert queue.requeue("T1", project_id="p")

    claimed = queue.claim("worker", project_id="p")
    assert claimed is not None, "a retried item must actually be claimable"
    assert claimed.item_id == "T1"


def test_retrying_keeps_the_reason_it_failed(tmp_path: Path) -> None:
    """`error=None` erased the only record of why, which is what the operator
    doing the retry most needs to read."""
    queue = queue_at_the_ceiling(tmp_path)

    queue.requeue("T1", project_id="p")

    record = queue.get("T1", project_id="p")
    assert record is not None
    assert "cargo clippy" in (record.last_error or "")


def test_a_claim_held_by_a_dead_process_is_reclaimed(tmp_path: Path) -> None:
    """The regression for #104.

    The lease is deliberately slow, so that a *slow* worker is not evicted.
    A worker whose pid is gone is not slow, and waiting it out strands the
    item alongside newly dispatched work with nothing saying why.
    """
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=9999.0)
    queue.add_project(Project(project_id="p", name="P"))
    queue.set_control("running", project_id="p")
    queue.add([rec("T1")], project_id="p")

    import socket

    dead = f"{socket.gethostname()}:999999"  # a pid that does not exist
    assert queue.claim(dead, project_id="p") is not None
    assert queue.get("T1", project_id="p").state == CLAIMED  # type: ignore[union-attr]

    assert queue.reclaim_dead_workers(project_id="p") == ["T1"]

    record = queue.get("T1", project_id="p")
    assert record is not None
    assert record.state == PENDING
    assert "is gone" in (record.last_error or "")


def test_a_live_worker_keeps_its_claim(tmp_path: Path) -> None:
    """The other half, and the one that matters: reclaiming from a healthy
    worker would hand its item to a second agent."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=9999.0)
    queue.add_project(Project(project_id="p", name="P"))
    queue.set_control("running", project_id="p")
    queue.add([rec("T1")], project_id="p")

    import socket

    alive = f"{socket.gethostname()}:{os.getpid()}"
    assert queue.claim(alive, project_id="p") is not None

    assert queue.reclaim_dead_workers(project_id="p") == []
    assert queue.get("T1", project_id="p").state == CLAIMED  # type: ignore[union-attr]


def test_a_claim_on_another_host_is_never_reclaimed(tmp_path: Path) -> None:
    """A pid on another machine says nothing about whether it is running."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=9999.0)
    queue.add_project(Project(project_id="p", name="P"))
    queue.set_control("running", project_id="p")
    queue.add([rec("T1")], project_id="p")
    assert queue.claim("some-other-host:999999", project_id="p") is not None

    assert queue.reclaim_dead_workers(project_id="p") == []
    assert queue.get("T1", project_id="p").state == CLAIMED  # type: ignore[union-attr]


def test_unmet_dependencies_are_reported_for_work_in_flight(tmp_path: Path) -> None:
    """The regression for #107.

    `claim` checks the graph once. Correcting a plan while work is in flight
    is normal, and the item must not pass a durable gate on a check made
    minutes earlier.
    """
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.add_project(Project(project_id="p", name="P"))
    queue.set_control("running", project_id="p")
    queue.add([rec("A"), rec("B")], project_id="p")
    assert queue.claim("w", project_id="p") is not None

    # B is claimed and running when the operator corrects the graph.
    queue.add([rec("B", depends_on=["A"])], project_id="p")

    assert queue.unmet_dependencies("B", project_id="p") == ["A"]

    queue.release("A", DONE, project_id="p")
    assert queue.unmet_dependencies("B", project_id="p") == []


def test_a_dependency_tracked_elsewhere_is_not_unmet(tmp_path: Path) -> None:
    """Plans routinely reference work this queue does not hold; treating that
    as a blocker would strand the item forever. Same rule `claim` uses."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.add_project(Project(project_id="p", name="P"))
    queue.add([rec("B", depends_on=["SOMEWHERE-ELSE"])], project_id="p")

    assert queue.unmet_dependencies("B", project_id="p") == []
