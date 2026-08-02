"""The claim invariants, exercised with real threads.

`test_work.py` says its subject is "what happens when two race", but every
race in it is simulated by calling methods in a chosen order on one thread.
That proves the logic given an interleaving; it cannot prove the interleaving
is impossible. Only contention can, and the invariant here -- an item is
claimed by exactly one worker -- is the one whose failure silently duplicates
or destroys work rather than raising.

Each thread gets its own connection: `WorkQueue._connect` opens per call, so
this is the real access pattern, not a shortcut for testing.
"""

from __future__ import annotations

import threading
from collections import Counter
from pathlib import Path

import pytest

from agent_harness.work import (
    CLAIMED,
    DONE,
    FAILED,
    PENDING,
    ClaimLost,
    WorkQueue,
    WorkRecord,
)


def rec(item_id: str) -> WorkRecord:
    return WorkRecord(item_id=item_id, title=f"do {item_id}", brief="brief")


def drain(queue: WorkQueue, owner: str, out: list[str], barrier: threading.Barrier) -> None:
    """Claim until the queue is dry, recording what this worker won."""
    barrier.wait()
    while True:
        record = queue.claim(owner)
        if record is None:
            return
        out.append(record.item_id)


def test_racing_workers_never_claim_the_same_item_twice(tmp_path: Path) -> None:
    """The whole queue rests on this. Two workers must not both win a row.

    A barrier starts every thread at once, so they contend on the same
    BEGIN IMMEDIATE rather than politely following each other.
    """
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=1000.0)
    items = [f"T{i}" for i in range(200)]
    queue.add(rec(i) for i in items)

    workers = 8
    barrier = threading.Barrier(workers)
    claimed: list[list[str]] = [[] for _ in range(workers)]
    threads = [
        threading.Thread(target=drain, args=(queue, f"worker-{n}", claimed[n], barrier))
        for n in range(workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), "a worker deadlocked on the claim transaction"

    won = [item for batch in claimed for item in batch]
    duplicates = [item for item, n in Counter(won).items() if n > 1]
    assert not duplicates, f"claimed by more than one worker: {duplicates}"
    assert sorted(won) == sorted(items), "some items were never claimed"


def test_an_expired_lease_is_re_claimed_by_exactly_one_racer(tmp_path: Path) -> None:
    """A dead worker's item returns to the pool -- to one successor, not many.

    This is the recovery path, so getting it wrong means a crash quietly
    becomes duplicated work rather than resumed work.
    """
    clock = [1000.0]
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0, now=lambda: clock[0])
    queue.add([rec("T1")])

    first = queue.claim("worker-dead")
    assert first is not None
    clock[0] += 101.0  # the lease expires; the worker never came back

    racers = 6
    barrier = threading.Barrier(racers)
    results: list[WorkRecord | None] = [None] * racers

    def race(n: int) -> None:
        barrier.wait()
        results[n] = queue.claim(f"worker-{n}")

    threads = [threading.Thread(target=race, args=(n,)) for n in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"{len(winners)} workers re-claimed the same expired lease"


def test_a_heartbeat_reports_that_the_claim_was_lost(tmp_path: Path) -> None:
    """The signal a slow worker needs to stop before it does damage."""
    clock = [1000.0]
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0, now=lambda: clock[0])
    queue.add([rec("T1")])

    queue.claim("worker-slow")
    assert queue.heartbeat("T1", "worker-slow") is True

    clock[0] += 101.0
    assert queue.claim("worker-new") is not None

    assert queue.heartbeat("T1", "worker-slow") is False, (
        "a worker whose claim was taken was told its lease is still good"
    )


def test_a_zombie_worker_cannot_finish_an_item_someone_else_now_owns(tmp_path: Path) -> None:
    """The other half of the lease contract, and the one that was missing.

    A worker that stalled past its lease is not dead -- it is slow, and it
    will eventually finish and report. By then the item belongs to someone
    else. If that late report is accepted, it overwrites a live claim: the
    item is marked done from work the new owner never did, and the new owner
    is still running with nothing to release.

    `heartbeat` guards on owner. `release` must too, or the guard only covers
    the half of the race that asks politely.
    """
    clock = [1000.0]
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0, now=lambda: clock[0])
    queue.add([rec("T1")])

    queue.claim("worker-stalled")
    clock[0] += 101.0
    live = queue.claim("worker-live")
    assert live is not None and live.owner == "worker-live"

    # The zombie surfaces and reports success on work it no longer owns.
    queue.release("T1", DONE, error=None, owner="worker-stalled")

    record = queue.get("T1")
    assert record is not None
    assert record.state == CLAIMED, "a zombie's late report overwrote a live claim"
    assert record.owner == "worker-live", "the live owner lost its claim to a zombie"


def test_the_live_owner_can_still_finish_normally(tmp_path: Path) -> None:
    """The guard must not break the ordinary path it is protecting."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    queue.add([rec("T1")])

    claimed = queue.claim("worker-1")
    assert claimed is not None
    queue.release("T1", DONE, owner="worker-1")

    record = queue.get("T1")
    assert record is not None and record.state == DONE


def test_an_unowned_release_is_an_administrative_override(tmp_path: Path) -> None:
    """Retry from the API has no worker identity and must still work.

    Omitting the owner means "I am not a worker racing for this row" -- the
    operator, deliberately. Guarding that would break the one control a human
    has over a stuck item.
    """
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    queue.add([rec("T1")])
    queue.claim("worker-1")

    queue.release("T1", PENDING)

    record = queue.get("T1")
    assert record is not None
    assert record.state == PENDING
    assert record.owner is None


def test_concurrent_releases_do_not_resurrect_finished_work(tmp_path: Path) -> None:
    """Whatever the interleaving, an item ends in exactly one terminal state."""
    clock = [1000.0]
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0, now=lambda: clock[0])
    queue.add([rec(f"T{i}") for i in range(50)])

    # claim() hands out the next available item, which is not necessarily the
    # one whose number matches the worker -- so pair them by what was actually
    # won rather than by assuming.
    held: list[tuple[str, str]] = []
    for i in range(50):
        owner = f"worker-{i}"
        claimed = queue.claim(owner)
        assert claimed is not None
        held.append((claimed.item_id, owner))

    barrier = threading.Barrier(50)

    def finish(n: int) -> None:
        item_id, owner = held[n]
        barrier.wait()
        queue.release(item_id, DONE if n % 2 == 0 else FAILED, owner=owner)

    threads = [threading.Thread(target=finish, args=(n,)) for n in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()

    counts = queue.counts()
    assert counts.get(DONE, 0) == 25
    assert counts.get(FAILED, 0) == 25
    assert counts.get(CLAIMED, 0) == 0


@pytest.mark.parametrize("workers", [2, 16])
def test_a_paused_fleet_grants_no_claims_under_contention(tmp_path: Path, workers: int) -> None:
    """Pausing must hold when many workers ask at once, not just one."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=1000.0)
    queue.add([rec(f"T{i}") for i in range(20)])
    queue.set_control("paused", reason="test")

    barrier = threading.Barrier(workers)
    got: list[WorkRecord | None] = [None] * workers

    def ask(n: int) -> None:
        barrier.wait()
        got[n] = queue.claim(f"worker-{n}")

    threads = [threading.Thread(target=ask, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()

    assert all(g is None for g in got), "a paused fleet handed out work"


def test_a_worker_that_lost_its_claim_stops_instead_of_reporting(tmp_path: Path) -> None:
    """The guard is only worth having if the worker actually consults it.

    `heartbeat` has always returned whether the claim survived, and every
    call site discarded it. A guard nobody reads and a lease nobody enforces
    fail the same way: both look correct in isolation.
    """
    clock = [1000.0]
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0, now=lambda: clock[0])
    queue.add([rec("T1")])

    stalled = queue.claim("worker-stalled")
    assert stalled is not None

    # While it works, the lease lapses and a successor takes the item.
    clock[0] += 101.0
    assert queue.claim("worker-live") is not None

    with pytest.raises(ClaimLost):
        _keepalive_of(queue, "worker-stalled", stalled)

    record = queue.get("T1")
    assert record is not None
    assert record.owner == "worker-live"


def _keepalive_of(queue: WorkQueue, owner: str, record: WorkRecord) -> None:
    """The executors' `_keepalive`, exercised without a model or a git tree.

    Both executors share this behaviour verbatim; testing it through either
    one would require standing up a worktree, a session host and a provider
    to reach a heartbeat three stages deep.
    """
    if not queue.heartbeat(record.item_id, owner):
        raise ClaimLost(f"{record.item_id} is no longer owned by {owner}")
