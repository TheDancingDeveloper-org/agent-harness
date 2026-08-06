"""The operational layer, end to end, across a multi-item run.

The queue, the leases, the dependency graph, the holds, the audit store and
the reaper have each been tested on their own. What had never been exercised
is all of them at once, over one real SQLite file, with dependencies, a
worker killed mid-item, retries, a question waiting on a person and two
threads competing for the same backlog.

Written in the shape of `test_agent_loop_e2e.py`: nothing is mocked that has
a real cheap equivalent. Real SQLite files on disk, real threads where
concurrency is the point, a real git repository where a worktree is, and the
clock injected -- the one thing that must not be real, because `AGENTS.md`
forbids sleeping in tests and a lease measured against a sleep is a lease
measured against the machine's load.

Five defects were found by writing it, plus the leak recorded as #206. Each
has a test here that fails against the code as it was:

1. `claim` read only the first `CLAIM_SCAN_LIMIT` eligible rows, so a project
   whose first page was entirely dependency-blocked was handed *nothing*,
   permanently, while ready work sat one row past the limit. A stalled fleet
   with a full queue and nothing saying why.
2. `POST /api/work/{id}/retry` called `release`, not `requeue`. Retrying an
   `exhausted` item therefore reported `ok`, put it back to `pending`, and
   watched the very next claim scan retire it again before any worker saw it
   -- which is the exact failure `WorkQueue.requeue` was written for, in the
   one place an operator can reach.
3. Nothing ever cancelled a hold. `holds.CANCELLED` was a state the whole
   codebase declared and never wrote, so a question stayed `open` after its
   item was retried out from under it: it sat in the operator's inbox where
   answering could no longer affect anything, it stopped the item's **new**
   owner from asking a question of its own, and it later expired as "nobody
   answered", which is not what happened.
4. `AuditStore.record_baseline` wrote a human's free text straight into the
   one store that has no way to remove anything, going around the redaction
   every other write path goes through.
5. `GET /api/holds` -- the inbox, the whole answer to #103 -- returned
   entries with **no item id**. `Hold.as_dict` supplies one; `HoldView` had
   no field for it, so pydantic dropped it silently. An operator reading the
   inbox could see the questions and had no way to tell which item any of
   them was about, while the route that answers one needs exactly that id.
6. #206: `with self._connect() as conn` reads like a closing block and is
   not one -- a sqlite3 connection's context manager manages a transaction.
   Every `WorkQueue`, `SQLiteCommandJournal` and `AuthorityStore` call left a
   live handle behind, and the queue holds a reference cycle, so the
   collector only found them on a gc pass.

Two things it deliberately does **not** change, and neither is silent:

* `POST /api/work/{id}/retry` and `/block` compare a stored `lease_until`
  against `time.time()` rather than against the queue's own clock, unlike
  `/api/holds`, which uses `queue.now()` and says why. Identical in
  production, so the `api` fixture starts its clock at the wall clock rather
  than paper over it.
* `holds` is keyed on `(project, item, attempt, asked_at)`, and `asked_at` is
  a float, so two questions asked by one attempt within the same clock tick
  raise a bare `sqlite3.IntegrityError` out of `WorkQueue.hold` rather than a
  `HoldError`. Reachable with an injected clock; effectively unreachable
  against a real one.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_harness.api import create_api
from agent_harness.audit import AuditStore
from agent_harness.events import Event
from agent_harness.graph import REASON_CYCLE
from agent_harness.holds import ANSWERED, CANCELLED, EXPIRED, OPEN, Answer, HoldError
from agent_harness.reaper import reap_abandoned_sessions
from agent_harness.redaction import Redactor
from agent_harness.store import EventStore
from agent_harness.work import (
    BLOCKED,
    CLAIM_SCAN_LIMIT,
    CLAIMED,
    DONE,
    EXHAUSTED,
    FAILED,
    HELD,
    PENDING,
    RUNNING,
    Project,
    WorkQueue,
    WorkRecord,
)

TOKEN = "operational-token"  # noqa: S105 - a fixture, not a credential

#: Long enough that nothing expires by accident, short enough that a test can
#: step past it in one move.
LEASE = 100.0


class Clock:
    """An injected clock. Real time is the one thing a lease test must not
    use: `AGENTS.md` forbids sleeping in tests, and a lease measured against
    a sleep is really a measurement of how loaded the machine is."""

    def __init__(self, at: float = 1_000_000.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at

    def advance(self, by: float) -> None:
        self.at += by


def queue_at(path: Path, clock: Clock | None = None, **kwargs: Any) -> WorkQueue:
    """A running queue on a real file. Projects start `stopped` on purpose,
    so anything that claims has to say so."""
    queue = WorkQueue(str(path), now=clock or Clock(), lease_seconds=LEASE, **kwargs)
    queue.set_control(RUNNING)
    return queue


# =====================================================================
# A claim is a lease
# =====================================================================


def test_a_killed_worker_returns_its_item_by_doing_nothing(tmp_path: Path) -> None:
    """The whole point of a lease over a lock: nobody has to clean up.

    The worker is killed the only way a test honestly can -- it stops
    existing, releases nothing, beats nothing -- and the item comes back.
    """
    clock = Clock()
    queue = queue_at(tmp_path / "q.sqlite", clock)
    queue.add([WorkRecord(item_id="T1", title="one")])

    first = queue.claim("host:111")
    assert first is not None
    assert first.attempts == 1

    # The worker dies here. No release, no heartbeat, nothing.
    assert queue.claim("host:222") is None, "a live lease must not be stolen"

    clock.advance(LEASE + 1)
    assert [r.item_id for r in queue.stale()] == ["T1"]

    second = queue.claim("host:222")
    assert second is not None
    assert second.item_id == "T1"
    assert second.owner == "host:222"
    assert second.attempts == 2, "a crash that decided nothing still costs an attempt"


def test_the_late_report_of_a_worker_that_was_only_slow_is_discarded(tmp_path: Path) -> None:
    """A worker past its lease is not dead, only slow, and it will surface.

    Its result is about an attempt somebody else now owns, so it is refused
    -- otherwise the item is marked finished from work the new owner never
    did, and the new owner is left with nothing to release.
    """
    clock = Clock()
    queue = queue_at(tmp_path / "q.sqlite", clock)
    queue.add([WorkRecord(item_id="T1", title="one")])
    queue.claim("host:111")
    clock.advance(LEASE + 1)
    new_owner = queue.claim("host:222")
    assert new_owner is not None

    assert queue.release("T1", DONE, owner="host:111") is False
    assert queue.get("T1") is not None
    assert queue.get("T1").state == CLAIMED  # type: ignore[union-attr]
    assert queue.heartbeat("T1", "host:111") is False, "the old owner cannot renew either"
    assert queue.heartbeat("T1", "host:222") is True


def test_an_item_that_kills_every_worker_ends_exhausted_rather_than_cycling(
    tmp_path: Path,
) -> None:
    """`attempts` is what stops an unattended fleet burning money on a loop.

    An item that reliably kills its worker is never *released*, so nothing
    but the attempt ceiling can retire it: it would otherwise be re-claimed
    every lease, for ever, looking exactly like an item that is merely busy.
    """
    clock = Clock()
    queue = queue_at(tmp_path / "q.sqlite", clock)
    queue.add_project(Project(project_id="default", name="d", max_attempts=3))
    queue.add([WorkRecord(item_id="T1", title="kills its worker")])

    owners = []
    for cycle in range(8):
        record = queue.claim(f"host:{cycle}")
        if record is not None:
            owners.append(record.owner)
        clock.advance(LEASE + 1)

    assert len(owners) == 3, f"claimed {len(owners)} times against a ceiling of 3"
    final = queue.get("T1")
    assert final is not None
    assert final.state == EXHAUSTED
    assert final.attempts == 3
    assert "gave up after 3 attempts" in (final.last_error or "")
    assert queue.claim("host:later") is None


def test_a_heartbeat_keeps_a_long_item_from_being_retired_underneath_it(
    tmp_path: Path,
) -> None:
    """Slow and dead have to stay distinguishable, and only a live process
    can keep stamping. Beaten by hand rather than by the heartbeat thread:
    the property under test is the renewal, not the threading."""
    clock = Clock()
    queue = queue_at(tmp_path / "q.sqlite", clock)
    queue.add([WorkRecord(item_id="T1", title="a long one")])
    queue.claim("host:111")

    for _ in range(10):
        clock.advance(LEASE / 2)
        assert queue.heartbeat("T1", "host:111") is True
        assert queue.claim("host:222") is None

    assert queue.stale() == []
    assert queue.release("T1", DONE, owner="host:111") is True


# =====================================================================
# Ordering, and the claim scan
# =====================================================================


def test_a_ready_item_beyond_the_first_scan_page_is_still_claimed(tmp_path: Path) -> None:
    """**Bug.** The claim scan read one page and stopped.

    `ORDER BY attempts, item_id LIMIT 200` made "does the queue have ready
    work" and "does the *first page* have ready work" the same question. They
    are not: here, 250 items depend on `zzz`, and `zzz` sorts last. Every
    candidate on the first page is blocked, so the scan yielded nothing --
    permanently, for every worker, while the one item that would have
    unblocked all 250 sat one row past the limit.

    A stalled fleet with a full queue and nothing in the queue saying why,
    which is precisely the unattended-operation failure this layer exists to
    prevent.
    """
    queue = queue_at(tmp_path / "q.sqlite")
    blocked = [
        WorkRecord(item_id=f"a{i:04d}", title="waits", depends_on=["zzz"])
        for i in range(CLAIM_SCAN_LIMIT + 50)
    ]
    queue.add([*blocked, WorkRecord(item_id="zzz", title="unblocks everything")])

    first = queue.claim("host:1")
    assert first is not None, "the queue has one ready item and handed out nothing"
    assert first.item_id == "zzz"

    queue.release("zzz", DONE, owner="host:1")
    assert queue.claim("host:1") is not None, "everything is ready once zzz is done"


def test_an_item_with_a_prior_attempt_is_not_starved_behind_fresh_work(
    tmp_path: Path,
) -> None:
    """`ORDER BY attempts, item_id` puts a retried item behind fresh ones --
    deliberately, so a poison item cannot monopolise the fleet. It must still
    be reached once the fresh work is taken, rather than sinking for ever."""
    clock = Clock()
    queue = queue_at(tmp_path / "q.sqlite", clock)
    queue.add([WorkRecord(item_id=f"T{i}", title="fresh") for i in range(5)])

    # T0 is attempted and its worker dies, so it carries one attempt.
    queue.claim("host:1")
    clock.advance(LEASE + 1)
    assert queue.get("T0") is not None
    assert queue.get("T0").attempts == 1  # type: ignore[union-attr]

    seen = []
    while (record := queue.claim("host:2")) is not None:
        seen.append(record.item_id)
        queue.release(record.item_id, DONE, owner="host:2")

    assert sorted(seen) == ["T0", "T1", "T2", "T3", "T4"]
    assert seen[-1] == "T0", "the retried item goes last, but it does go"


def test_a_dependency_is_never_claimed_before_its_target(tmp_path: Path) -> None:
    """A three-item chain declared in the order that would break it.

    `T1` depends on `T2` depends on `T3`, so id order and dependency order
    disagree. Driven to completion one claim at a time, the only possible
    order is the dependency order.
    """
    queue = queue_at(tmp_path / "q.sqlite")
    queue.add(
        [
            WorkRecord(item_id="T1", title="last", depends_on=["T2"]),
            WorkRecord(item_id="T2", title="middle", depends_on=["T3"]),
            WorkRecord(item_id="T3", title="first"),
        ]
    )

    order = []
    while (record := queue.claim("host:1")) is not None:
        order.append(record.item_id)
        queue.release(record.item_id, DONE, owner="host:1")

    assert order == ["T3", "T2", "T1"]


def test_a_failed_dependency_holds_back_everything_that_needed_it(tmp_path: Path) -> None:
    """`done` is the only state that satisfies an edge. A target that failed
    is not a target that finished, and the work behind it must wait for a
    person rather than run against a half-built thing."""
    queue = queue_at(tmp_path / "q.sqlite")
    queue.add(
        [
            WorkRecord(item_id="T1", title="needs T2", depends_on=["T2"]),
            WorkRecord(item_id="T2", title="the foundation"),
        ]
    )
    record = queue.claim("host:1")
    assert record is not None
    assert record.item_id == "T2"
    queue.release("T2", FAILED, error="the build broke", owner="host:1")

    assert queue.claim("host:1") is None
    readiness = queue.readiness("T1")
    assert readiness.ready is False
    assert "T2 is failed, not done" in readiness.explain()


# =====================================================================
# The graph says WHY
# =====================================================================


def test_a_cycle_is_named_as_a_path_rather_than_waited_on_for_ever(tmp_path: Path) -> None:
    queue = queue_at(tmp_path / "q.sqlite")
    queue.add(
        [
            WorkRecord(item_id="A", title="a", depends_on=["B"]),
            WorkRecord(item_id="B", title="b", depends_on=["C"]),
            WorkRecord(item_id="C", title="c", depends_on=["A"]),
        ]
    )

    assert queue.claim("host:1") is None
    readiness = queue.readiness("B")
    assert readiness.ready is False
    kinds = {reason.kind for reason in readiness.reasons}
    assert REASON_CYCLE in kinds
    cycle = next(r for r in readiness.reasons if r.kind == REASON_CYCLE)
    assert "can never all become ready" in cycle.explanation
    assert "A -> B -> C -> A" in cycle.explanation


def test_an_unresolvable_required_target_names_the_id_it_could_not_find(
    tmp_path: Path,
) -> None:
    """A typo, an omitted item and a genuine external reference used to be
    indistinguishable, and all three ran immediately."""
    queue = queue_at(tmp_path / "q.sqlite")
    queue.add([WorkRecord(item_id="T1", title="one", depends_on=["T99"])])

    assert queue.claim("host:1") is None
    readiness = queue.readiness("T1")
    assert readiness.ready is False
    assert "no item 'T99'" in readiness.explain()
    assert queue.unmet_dependencies("T1") == ["T99"]


def test_an_advisory_edge_is_reported_and_never_blocks(tmp_path: Path) -> None:
    queue = queue_at(tmp_path / "q.sqlite")
    queue.add(
        [
            WorkRecord(item_id="T1", title="one", depends_on=["?T99"]),
            WorkRecord(item_id="T99", title="never done"),
        ]
    )
    readiness = queue.readiness("T1")
    assert readiness.ready is True
    assert [reason.target_id for reason in readiness.advisory] == ["T99"]
    claimed = queue.claim("host:1")
    assert claimed is not None
    assert claimed.item_id == "T1"


def test_a_cross_project_dependency_waits_for_the_other_project(tmp_path: Path) -> None:
    """Ids are only unique within a project, so `project:other/X` is a
    different kind of edge from `X` and has to be resolved against the other
    project's rows."""
    queue = queue_at(tmp_path / "q.sqlite")
    queue.add([WorkRecord(item_id="X", title="the other project's item")], project_id="other")
    queue.set_control(RUNNING, project_id="other")
    queue.add([WorkRecord(item_id="T1", title="waits", depends_on=["project:other/X"])])

    assert queue.claim("host:1") is None
    assert "no item 'X' in project 'other'" not in queue.readiness("T1").explain()
    assert "X is pending, not done" in queue.readiness("T1").explain()

    other = queue.claim("host:1", project_id="other")
    assert other is not None
    queue.release("X", DONE, owner="host:1", project_id="other")

    assert queue.readiness("T1").ready is True
    claimed = queue.claim("host:1")
    assert claimed is not None
    assert claimed.item_id == "T1"


def test_a_cross_project_target_in_a_project_that_does_not_exist_blocks(
    tmp_path: Path,
) -> None:
    queue = queue_at(tmp_path / "q.sqlite")
    queue.add([WorkRecord(item_id="T1", title="waits", depends_on=["project:ghost/X"])])
    assert queue.claim("host:1") is None
    assert "no item 'X' in project 'ghost'" in queue.readiness("T1").explain()


def test_the_api_says_why_a_blocked_item_is_blocked(tmp_path: Path) -> None:
    """The item sits in `pending` -- correctly, it has not been attempted --
    so `pending` alone cannot be the whole answer. The readiness route is
    where the reason lives, and it must carry the target and a kind a client
    can branch on, not only English."""
    store = EventStore(tmp_path / "e.sqlite")
    queue = queue_at(tmp_path / "q.sqlite")
    queue.add(
        [
            WorkRecord(item_id="T1", title="waits", depends_on=["T2"]),
            WorkRecord(item_id="T2", title="the foundation"),
        ]
    )
    with TestClient(create_api(store, queue=queue, token=TOKEN)) as client:
        response = client.get(
            "/api/work/T1/readiness", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is False
        assert body["reasons"], "a blocked item that lists no reasons is silent"
        assert body["reasons"][0]["target_id"] == "T2"
        assert body["reasons"][0]["kind"] == "dependency"


# =====================================================================
# Holds -- D12: a hold suspends the lease and KEEPS the claim
# =====================================================================


def test_a_hold_keeps_the_claim_and_survives_the_worker_dying(tmp_path: Path) -> None:
    """D12. The lease goes to zero because a held item is neither slow nor
    dead, and the owner stays on the row so answering hands the item back to
    the worker that asked -- with its worktree and its context.

    The worker is then killed. Nothing may take the item: no lease can lapse
    because there is no lease, and a person is still thinking about it.
    """
    clock = Clock()
    queue = queue_at(tmp_path / "q.sqlite", clock)
    queue.add([WorkRecord(item_id="T1", title="one")])
    queue.claim("host:111")

    hold = queue.hold("T1", question="which database?", owner="host:111", max_seconds=10_000)
    held = queue.get("T1")
    assert held is not None
    assert held.state == HELD
    assert held.owner == "host:111", "D12: the claim is kept"
    assert held.lease_until == 0.0, "D12: the lease is suspended"

    # The worker dies. Ten lease-lengths pass.
    clock.advance(LEASE * 10)
    assert queue.claim("host:222") is None
    assert queue.get("T1") is not None
    assert queue.get("T1").state == HELD  # type: ignore[union-attr]
    assert queue.stale() == [], "a held item is not a stale claim"
    assert queue.reclaim_dead_workers() == [], "nor is it a dead worker's claim"

    answered = queue.answer_hold("T1", hold.resume_token, Answer(text="postgres", who="ops"))
    assert answered.state == ANSWERED
    back = queue.get("T1")
    assert back is not None
    assert back.state == CLAIMED
    assert back.owner == "host:111", "answering hands it back to the worker that asked"
    assert back.lease_until > clock.at, "with a fresh lease"
    assert back.held_until == 0.0


def test_a_hold_refuses_an_answer_that_does_not_carry_its_token(tmp_path: Path) -> None:
    queue = queue_at(tmp_path / "q.sqlite")
    queue.add([WorkRecord(item_id="T1", title="one")])
    queue.claim("host:111")
    queue.hold("T1", question="which database?", owner="host:111")

    with pytest.raises(HoldError):
        queue.answer_hold("T1", "not-the-token", Answer(text="postgres"))
    assert queue.get("T1") is not None
    assert queue.get("T1").state == HELD  # type: ignore[union-attr]


def test_an_unanswered_hold_returns_the_item_to_blocked_and_never_to_ready(
    tmp_path: Path,
) -> None:
    """Being held is not approval. A hold that times out has not been agreed
    to, so it goes to a state a person has to act on -- with the question
    preserved, because the hold is closed and the question would otherwise go
    with it."""
    clock = Clock()
    queue = queue_at(tmp_path / "q.sqlite", clock)
    queue.add([WorkRecord(item_id="T1", title="one")])
    queue.claim("host:111")
    queue.hold("T1", question="may I drop the table?", owner="host:111", max_seconds=60)

    clock.advance(61)
    # Nothing schedules this: the claim scan sweeps, so a broken cron cannot
    # strand a held item.
    assert queue.claim("host:222") is None
    item = queue.get("T1")
    assert item is not None
    assert item.state == BLOCKED, "never to pending, and never past a gate"
    assert "may I drop the table?" in (item.last_error or "")
    assert item.reason_kind == "hold_expired"
    assert [h.state for h in queue.holds.history("default", "T1")] == [EXPIRED]


def test_a_hold_is_cancelled_when_its_item_is_retried_out_from_under_it(
    tmp_path: Path,
) -> None:
    """**Bug.** `holds.CANCELLED` was declared, documented as "the item was
    blocked, retried or requeued out from under the hold", and written by
    nothing at all.

    So the question stayed `open` after the item moved on. Three consequences,
    all asserted here: the operator's inbox showed a question that answering
    could no longer affect; the item's **new** owner was refused a question of
    its own -- "T1 already has an unanswered question", about an attempt that
    no longer exists; and the abandoned hold went on to expire and be recorded
    as "nobody answered", which is a different fact from "somebody cancelled
    it" and is not the one that happened.
    """
    clock = Clock()
    queue = queue_at(tmp_path / "q.sqlite", clock)
    queue.add([WorkRecord(item_id="T1", title="one")])
    queue.claim("host:111")
    queue.hold("T1", question="which database?", owner="host:111", max_seconds=10_000)

    # An operator retries it. The attempt that asked the question is over.
    assert queue.requeue("T1") is True
    assert queue.holds.open_holds() == [], "the inbox must not hold a dead question"
    assert queue.holds.current("default", "T1") is None
    assert [h.state for h in queue.holds.history("default", "T1")] == [CANCELLED]

    # The new owner can ask its own question.
    new_owner = queue.claim("host:222")
    assert new_owner is not None
    clock.advance(1)
    fresh = queue.hold("T1", question="and now?", owner="host:222", max_seconds=10_000)
    assert fresh.owner == "host:222"

    # And a release cancels one too, rather than leaving it to "expire".
    assert queue.release("T1", BLOCKED, error="parked", owner=None) is True
    assert queue.holds.open_holds() == []
    assert [h.state for h in queue.holds.history("default", "T1")] == [CANCELLED, CANCELLED]
    clock.advance(100_000)
    assert queue.holds.due() == [], "a cancelled question can never expire later"


def test_holding_an_item_nobody_is_working_on_is_refused(tmp_path: Path) -> None:
    """A hold is a suspended attempt. Holding an unclaimed item would be an
    operator parking it, which is what `blocked` already is."""
    queue = queue_at(tmp_path / "q.sqlite")
    queue.add([WorkRecord(item_id="T1", title="one")])
    with pytest.raises(HoldError, match="not claimed"):
        queue.hold("T1", question="anything?")


def test_a_question_reaches_a_consumer_the_moment_it_is_opened(tmp_path: Path) -> None:
    """A hold that only a poll can discover is a hold nobody answers (#188).
    The notice carries no resume token: it is read by strictly more things
    than the API is."""
    notices: list[dict[str, Any]] = []
    clock = Clock()
    queue = WorkQueue(
        str(tmp_path / "q.sqlite"),
        now=clock,
        lease_seconds=LEASE,
        on_hold=notices.append,
    )
    queue.set_control(RUNNING)
    queue.add([WorkRecord(item_id="T1", title="one")])
    queue.claim("host:111")
    hold = queue.hold("T1", question="which database?", owner="host:111")

    assert len(notices) == 1
    assert notices[0]["item_id"] == "T1"
    assert notices[0]["question"] == "which database?"
    assert hold.resume_token not in str(notices[0])


def test_a_broken_notice_hook_cannot_reach_the_item(tmp_path: Path) -> None:
    def explode(_notice: dict[str, Any]) -> None:
        raise RuntimeError("the webhook is down")

    queue = WorkQueue(str(tmp_path / "q.sqlite"), lease_seconds=LEASE, on_hold=explode)
    queue.set_control(RUNNING)
    queue.add([WorkRecord(item_id="T1", title="one")])
    queue.claim("host:111")
    hold = queue.hold("T1", question="which database?", owner="host:111")
    assert hold.state == OPEN
    assert queue.get("T1") is not None
    assert queue.get("T1").state == HELD  # type: ignore[union-attr]


# =====================================================================
# Concurrency: two workers, one queue, fifty items
# =====================================================================


def test_fifty_items_two_workers_and_no_item_claimed_twice(tmp_path: Path) -> None:
    """Real threads and a real SQLite file, because the property under test
    is what SQLite does when two `BEGIN IMMEDIATE` transactions race. A fake
    connection would prove only that the test's own lock works.

    Each worker opens its own `WorkQueue`, as a real worker process does.
    """
    path = tmp_path / "q.sqlite"
    queue = queue_at(path)
    queue.add([WorkRecord(item_id=f"T{i:03d}", title="work") for i in range(50)])

    claims: list[tuple[str, str]] = []
    lock = threading.Lock()
    start = threading.Barrier(4)

    def worker(name: str) -> None:
        own = WorkQueue(str(path), lease_seconds=LEASE)
        start.wait(timeout=30)
        while True:
            record = own.claim(name)
            if record is None:
                return
            with lock:
                claims.append((record.item_id, name))
            own.release(record.item_id, DONE, owner=name)

    threads = [threading.Thread(target=worker, args=(f"host:{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
        assert not thread.is_alive()

    ids = [item for item, _ in claims]
    assert len(ids) == 50
    assert len(set(ids)) == 50, "an item was handed to two workers at once"
    assert queue.counts() == {DONE: 50}


def test_two_workers_racing_a_single_expired_lease_produce_one_owner(
    tmp_path: Path,
) -> None:
    """The narrow race the IMMEDIATE transaction exists for: one item, one
    expired lease, several workers scanning at the same instant."""
    clock = Clock()
    path = tmp_path / "q.sqlite"
    queue = queue_at(path, clock)
    queue.add([WorkRecord(item_id="T1", title="one")])
    queue.claim("host:original")
    clock.advance(LEASE + 1)

    winners: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(6)

    def worker(name: str) -> None:
        own = WorkQueue(str(path), now=clock, lease_seconds=LEASE)
        start.wait(timeout=30)
        record = own.claim(name)
        if record is not None:
            with lock:
                winners.append(name)

    threads = [threading.Thread(target=worker, args=(f"host:{i}",)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert len(winners) == 1, f"{len(winners)} workers all believe they own T1"
    assert queue.get("T1") is not None
    assert queue.get("T1").owner == winners[0]  # type: ignore[union-attr]


# =====================================================================
# A whole multi-item run, over a real git repository
# =====================================================================


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repository. The worktree an item works in is a real
    directory in a real repository, so anything that reasons about a branch
    is reasoning about one that exists."""
    where = tmp_path / "repo"
    where.mkdir()
    (where / "README.md").write_text("start\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=where, check=True)
    subprocess.run(["git", "add", "-A"], cwd=where, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=where,
        check=True,
    )
    return where


def test_a_multi_item_run_with_a_dependency_a_crash_a_hold_and_a_retry(
    tmp_path: Path, repo: Path
) -> None:
    """Everything at once, over one file and one real repository.

    Four items: `T1` is fine, `T2` crashes its worker once and then succeeds,
    `T3` waits on a person, `T4` depends on `T2`. What is asserted is the
    observable end state -- every item done, one branch per item in a real
    repository, and the queue's own counts -- not which internal method ran.
    """
    clock = Clock()
    queue = queue_at(tmp_path / "q.sqlite", clock)
    queue.add(
        [
            WorkRecord(item_id="T1", title="independent"),
            WorkRecord(item_id="T2", title="crashes once"),
            WorkRecord(item_id="T3", title="asks a person"),
            WorkRecord(item_id="T4", title="needs T2", depends_on=["T2"]),
        ]
    )

    def branch_for(item_id: str) -> str:
        name = f"work/{item_id}"
        subprocess.run(["git", "checkout", "-q", "-b", name], cwd=repo, check=True)
        (repo / f"{item_id}.txt").write_text(f"{item_id} did something\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", item_id],
            cwd=repo,
            check=True,
        )
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
        return name

    crashed_once = False
    holds_asked = False
    order: list[str] = []

    for _ in range(30):
        record = queue.claim("host:worker")
        if record is None:
            if queue.holds.open_holds():
                # A person answers, and D12 hands the item straight back to
                # the worker that asked -- it is `claimed` again, by that
                # worker, so nothing re-claims it and the worker finishes it.
                hold = queue.holds.open_holds()[0]
                queue.answer_hold(hold.item_id, hold.resume_token, Answer(text="yes", who="ops"))
                queue.release(
                    hold.item_id,
                    DONE,
                    branch=branch_for(hold.item_id),
                    owner="host:worker",
                )
                continue
            break
        order.append(record.item_id)

        if record.item_id == "T2" and not crashed_once:
            crashed_once = True
            clock.advance(LEASE + 1)  # the worker is killed; the lease lapses
            continue
        if record.item_id == "T3" and not holds_asked:
            holds_asked = True
            queue.hold("T3", question="ship it?", owner="host:worker", max_seconds=10_000)
            continue
        queue.release(record.item_id, DONE, branch=branch_for(record.item_id), owner="host:worker")

    assert queue.counts() == {DONE: 4}
    assert order.index("T2") < order.index("T4"), "T4 must never precede its target"
    assert crashed_once and holds_asked

    branches = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert sorted(branches) == ["main", "work/T1", "work/T2", "work/T3", "work/T4"]
    for item in ("T1", "T2", "T3", "T4"):
        record = queue.get(item)
        assert record is not None
        assert record.branch == f"work/{item}"
    assert queue.get("T2") is not None
    assert queue.get("T2").attempts == 2, "the crash cost an attempt"  # type: ignore[union-attr]


# =====================================================================
# The API is the operator's only lever
# =====================================================================


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, WorkQueue, Clock]]:
    # Started from the wall clock rather than an arbitrary epoch. Two routes
    # (`retry` and `block`) compare a stored `lease_until` against
    # `time.time()` rather than against the queue's own clock, so a queue
    # driven from a fictional epoch would exercise a code path production
    # never takes. Advancing this clock still advances only the queue's, which
    # is what the tests need.
    clock = Clock(time.time())
    store = EventStore(tmp_path / "e.sqlite")
    queue = WorkQueue(str(tmp_path / "q.sqlite"), now=clock, lease_seconds=LEASE)
    queue.add_project(Project(project_id="default", name="d", max_attempts=3))
    queue.set_control(RUNNING)
    with TestClient(create_api(store, queue=queue, token=TOKEN)) as client:
        yield client, queue, clock


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_retrying_an_exhausted_item_actually_makes_it_claimable_again(
    api: tuple[TestClient, WorkQueue, Clock],
) -> None:
    """**Bug.** The route reported success and changed nothing that mattered.

    `WorkQueue.requeue` exists because of exactly this: "a retry that left
    the count alone put the item back to `pending` and watched it return to
    `exhausted` before any worker saw it, while reporting success". The fix
    went into the queue and never reached the API -- which is the *only*
    lever an operator has over a wedged row.

    Driven entirely through HTTP, because that is where the defect was.
    """
    client, queue, clock = api
    queue.add([WorkRecord(item_id="T1", title="kills its worker")])

    for cycle in range(4):
        queue.claim(f"host:{cycle}")  # every worker dies holding it
        clock.advance(LEASE + 1)

    assert client.get("/api/work/T1", headers=auth()).json()["state"] == EXHAUSTED

    response = client.post("/api/work/T1/retry", headers=auth())
    assert response.status_code == 200
    assert response.json() == {"ok": True, "item_id": "T1", "state": "pending"}

    revived = queue.claim("host:after-the-retry")
    assert revived is not None, "the retry reported ok and the item was never claimable"
    assert revived.item_id == "T1"
    assert revived.attempts == 1, "a retry means from the start"
    assert client.get("/api/work/T1", headers=auth()).json()["state"] == CLAIMED


def test_a_retry_keeps_the_only_record_of_why_the_item_stopped(
    api: tuple[TestClient, WorkQueue, Clock],
) -> None:
    """`last_error` is what the person retrying the item needs to read.
    Clearing it turns a diagnosable failure into `gave up after N attempts`
    with no cause."""
    client, queue, clock = api
    queue.add([WorkRecord(item_id="T1", title="one")])
    queue.claim("host:1")
    queue.release("T1", FAILED, error="the reviewer said no", owner="host:1")

    assert client.post("/api/work/T1/retry", headers=auth()).status_code == 200
    record = queue.get("T1")
    assert record is not None
    assert record.state == PENDING
    assert record.last_error == "the reviewer said no"


def test_a_retry_is_refused_while_the_claim_is_live(
    api: tuple[TestClient, WorkQueue, Clock],
) -> None:
    """Yanking an item out from under a running agent produces two workers on
    one item, which is worse than one stuck item."""
    client, queue, clock = api
    queue.add([WorkRecord(item_id="T1", title="one")])
    queue.claim("host:1")
    response = client.post("/api/work/T1/retry", headers=auth())
    assert response.status_code == 409
    assert queue.get("T1") is not None
    assert queue.get("T1").state == CLAIMED  # type: ignore[union-attr]


def test_a_question_can_be_answered_from_anywhere_over_http(
    api: tuple[TestClient, WorkQueue, Clock],
) -> None:
    """The answer arrives from a phone, not from the terminal that asked.

    **Bug, in the first assertion.** The inbox listed its questions without
    saying which item each one was about: `Hold.as_dict` supplies `item_id`,
    `HoldView` had no field for it, and pydantic dropped it without a word.
    An operator could read every question and answer none of them --
    `POST /api/work/{item_id}/answer` needs exactly the id the list withheld.
    """
    client, queue, clock = api
    queue.add([WorkRecord(item_id="T1", title="one")])
    queue.claim("host:111")
    hold = queue.hold("T1", question="ship it?", owner="host:111", max_seconds=10_000)

    inbox = client.get("/api/holds", headers=auth()).json()["open"]
    assert [h["item_id"] for h in inbox] == ["T1"]
    assert inbox[0]["attempt"] == 1
    assert "resume_token" not in str(inbox), "a token in the inbox is a token anyone may spend"

    refused = client.post(
        "/api/work/T1/answer", headers=auth(), json={"resume_token": "wrong", "text": "yes"}
    )
    assert refused.status_code == 409

    accepted = client.post(
        "/api/work/T1/answer",
        headers=auth(),
        json={"resume_token": hold.resume_token, "text": "yes", "who": "ops"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["state"] == "claimed"
    assert client.get("/api/holds", headers=auth()).json()["open"] == []
    record = queue.get("T1")
    assert record is not None
    assert record.state == CLAIMED
    assert record.owner == "host:111"


# =====================================================================
# The audit store outlives the queue
# =====================================================================


def test_deleting_the_queue_database_changes_no_audited_answer(tmp_path: Path) -> None:
    """A stated v1 goal, and the reason `audit.py` is a separate file.

    The queue is mutable, migrated in place, and a reasonable thing to delete
    and rebuild from the plan. Anything sharing that file shares that fate --
    so the history is asked its questions, the queue file is *actually
    deleted* from disk, and every answer has to be identical.
    """
    audit = AuditStore(tmp_path / "audit.sqlite")
    queue_path = tmp_path / "q.sqlite"
    clock = Clock()
    queue = queue_at(queue_path, clock)
    queue.add([WorkRecord(item_id=f"T{i}", title="work") for i in range(4)])

    for i in range(4):
        record = queue.claim("host:1")
        assert record is not None
        audit.append(
            [
                Event(
                    ts=clock.at,
                    kind="model_call",
                    source="run",
                    worker="host:1",
                    role="implementer",
                    model="m",
                    outcome="ok",
                    latency_s=1.5,
                    data={
                        "project_id": "default",
                        "item_id": record.item_id,
                        "tokens_in": 100 * (i + 1),
                        "tokens_out": 10,
                        "price_in_per_mtok": 1.0,
                        "price_out_per_mtok": 2.0,
                    },
                ),
                Event(
                    ts=clock.at + 1,
                    kind="work",
                    source="run",
                    worker="host:1",
                    outcome=DONE,
                    data={"project_id": "default", "item_id": record.item_id},
                ),
            ]
        )
        queue.release(record.item_id, DONE, owner="host:1")
        clock.advance(10)

    before = {
        "count": audit.count(),
        "cost": audit.cost(),
        "delivery": audit.delivery(),
        "by_item": audit.latest_by_item(),
        "span": audit.span(),
    }
    assert before["count"] == 8
    assert before["cost"], "the run has to have produced something to lose"

    queue.checkpoint()
    del queue
    for suffix in ("", "-wal", "-shm"):
        Path(str(queue_path) + suffix).unlink(missing_ok=True)
    assert not queue_path.exists()

    reopened = AuditStore(tmp_path / "audit.sqlite")
    after = {
        "count": reopened.count(),
        "cost": reopened.cost(),
        "delivery": reopened.delivery(),
        "by_item": reopened.latest_by_item(),
        "span": reopened.span(),
    }
    assert after == before


def test_the_audit_store_offers_no_way_to_change_or_remove_an_answer(
    tmp_path: Path,
) -> None:
    """A method that exists will eventually be called during an incident, by
    someone with a good reason, at the worst possible time. `thin` is the one
    deletion and it is fenced: nothing may be removed until a rollup covers
    it."""
    audit = AuditStore(tmp_path / "audit.sqlite")
    for name in ("update", "delete", "purge", "clear", "amend"):
        assert not hasattr(audit, name), f"AuditStore grew a {name}()"

    audit.append(
        [
            Event(
                ts=1.0,
                kind="work",
                source="run",
                outcome=DONE,
                data={"project_id": "p", "item_id": "T1"},
            )
        ]
    )
    assert audit.thin(older_than_days=0, now=1_000_000.0) == 0, "nothing is rolled up yet"
    assert audit.count() == 1


def test_a_repeated_event_is_a_duplicate_and_never_an_amendment(tmp_path: Path) -> None:
    audit = AuditStore(tmp_path / "audit.sqlite")
    event = Event(
        ts=1.0,
        kind="work",
        source="run",
        outcome=DONE,
        data={"project_id": "p", "item_id": "T1"},
    )
    assert audit.append([event]) == 1
    assert audit.append([event]) == 0
    assert audit.count() == 1


def test_every_write_path_into_the_audit_store_redacts(tmp_path: Path) -> None:
    """**Bug.** `record_baseline` was a second door, and it was unlocked.

    The module's own reasoning is that the filter sits on the only way in
    "for the same reason it does in `store`: nothing added later can route
    around it" -- and `record_baseline` writes a human's free text, into the
    store that is *retained* after `maintenance` thins the primary, with no
    way to remove anything afterwards.
    """
    secret = "sk-live-abcdefghijklmnop"  # noqa: S105 - a fixture, not a credential
    audit = AuditStore(tmp_path / "audit.sqlite", redact=Redactor([secret]))

    audit.append(
        [
            Event(
                ts=1.0,
                kind="model_call",
                source="run",
                endpoint=f"https://gw.example/v1?key={secret}",
                data={"project_id": "p", "item_id": "T1", "prompt": f"use {secret}"},
            )
        ]
    )
    row = audit.recent()[0]
    assert secret not in str(row)
    assert row["data"].count("[redacted]") == 1

    audit.record_baseline(
        "b1",
        "p",
        label=f"before rotating {secret}",
        window_days=7,
        recorded_at=1.0,
        notes=f"measured with {secret}",
    )
    baseline = audit.baselines()[0]
    assert secret not in str(baseline), "a credential in a baseline outlives every other copy"
    assert "[redacted]" in baseline["notes"]
    assert "[redacted]" in baseline["label"]


def test_a_baseline_is_immutable_once_recorded(tmp_path: Path) -> None:
    audit = AuditStore(tmp_path / "audit.sqlite")
    assert audit.record_baseline("b1", "p", label="first", window_days=7, recorded_at=1.0)
    assert not audit.record_baseline("b1", "p", label="second", window_days=7, recorded_at=2.0)
    assert audit.baselines()[0]["label"] == "first"


def test_the_event_store_has_no_update_and_no_delete(tmp_path: Path) -> None:
    """Risk R3, asserted against the source rather than against behaviour: a
    change that needs to mutate an event has to delete this first."""
    source = Path("src/agent_harness/store.py").read_text()
    body = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    ).lower()
    assert "update events" not in body
    assert "delete from events" not in body


# =====================================================================
# The reaper
# =====================================================================


class FakeHost:
    """A session host that records what it was asked to kill, and can be
    told to refuse one. Not a stand-in for a PTY: the property under test is
    which sessions the reaper selects, and that is decided here."""

    def __init__(self, refuse: set[str] | None = None) -> None:
        self.killed: list[str] = []
        self.deleted: list[str] = []
        self.refuse = refuse or set()

    def kill_session(self, session_id: str) -> None:
        if session_id in self.refuse:
            raise RuntimeError("the host is gone")
        self.killed.append(session_id)

    def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)


def test_the_reaper_takes_only_what_nobody_came_back_to(tmp_path: Path) -> None:
    """A human returning after lunch must still find their terminal, so the
    only thing that makes a session reapable is age."""
    clock = Clock()
    queue = queue_at(tmp_path / "q.sqlite", clock)
    queue.add([WorkRecord(item_id="T1", title="one"), WorkRecord(item_id="T2", title="two")])
    queue.record_abandoned_session("old-session", "T1", reason="timed out")
    clock.advance(3600)
    queue.record_abandoned_session("fresh-session", "T2", reason="timed out")

    host = FakeHost()
    report = reap_abandoned_sessions(queue, host, max_age=1800)

    assert report.reaped == ["old-session"]
    assert report.kept == 1
    assert host.killed == ["old-session"]
    assert host.deleted == ["old-session"]
    assert [row["session_id"] for row in queue.abandoned_sessions()] == ["fresh-session"]


def test_the_reaper_never_touches_the_session_of_a_live_claim(tmp_path: Path) -> None:
    """The one thing it must not be able to do. A session is reapable
    because it was *recorded abandoned*, never because its item looks idle --
    an item claimed and heartbeating has nothing on the list, however long it
    has been running."""
    clock = Clock()
    queue = queue_at(tmp_path / "q.sqlite", clock)
    queue.add([WorkRecord(item_id="T1", title="a long one")])
    queue.claim("host:111")
    clock.advance(100_000)
    assert queue.heartbeat("T1", "host:111") is True

    host = FakeHost()
    report = reap_abandoned_sessions(queue, host, max_age=1.0)
    assert report.reaped == []
    assert host.killed == []
    assert queue.get("T1") is not None
    assert queue.get("T1").state == CLAIMED  # type: ignore[union-attr]


def test_one_session_the_reaper_cannot_kill_does_not_stop_the_sweep(
    tmp_path: Path,
) -> None:
    """And it stays on the list, so the next sweep retries it. Forgetting a
    session that was never killed is how an agent goes on spending tokens
    with nothing recording that it exists."""
    clock = Clock()
    queue = queue_at(tmp_path / "q.sqlite", clock)
    queue.add([WorkRecord(item_id="T1", title="one")])
    for name in ("s1", "s2", "s3"):
        queue.record_abandoned_session(name, "T1")
    clock.advance(10_000)

    host = FakeHost(refuse={"s2"})
    report = reap_abandoned_sessions(queue, host, max_age=1.0)

    assert sorted(report.reaped) == ["s1", "s3"]
    assert list(report.failed) == ["s2"]
    assert [row["session_id"] for row in queue.abandoned_sessions()] == ["s2"]


# =====================================================================
# #206: connections that were opened and never closed
# =====================================================================


def _open_handles(path: Path) -> int:
    """How many file descriptors in this process point at `path`.

    Real, and observable from outside the code under test -- which is what
    makes it evidence rather than an assertion about an implementation.
    """
    total = 0
    for entry in Path("/proc/self/fd").iterdir():
        try:
            if os.readlink(entry) == str(path):
                total += 1
        except OSError:  # pragma: no cover - the fd closed under us
            continue
    return total


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc")
def test_opening_a_queue_leaves_no_handle_behind(tmp_path: Path) -> None:
    """#206. `with self._connect() as conn` reads like a closing block and is
    not one: a sqlite3 connection's context manager manages a *transaction*.
    Every `WorkQueue` therefore left a live handle, and because the object
    holds a reference cycle -- its graph, attempt log and holds all carry its
    bound `_connect` -- the collector only found it on a gc pass, in a
    process meant to run for weeks.
    """
    path = tmp_path / "q.sqlite"
    queue_at(path).add([WorkRecord(item_id="T1", title="one")])
    baseline = _open_handles(path)

    for _ in range(20):
        WorkQueue(str(path), lease_seconds=LEASE)

    assert _open_handles(path) == baseline, "twenty queues, twenty leaked handles"


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc")
def test_the_command_journal_and_the_oversight_authority_close_what_they_open(
    tmp_path: Path,
) -> None:
    """The same mistake, in the same shape, in two more places (#206)."""
    from agent_harness.command_service import SQLiteCommandJournal
    from agent_harness.oversight import AuthorityStore

    journal_path = tmp_path / "journal.sqlite"
    SQLiteCommandJournal(journal_path)
    baseline = _open_handles(journal_path)
    for _ in range(20):
        SQLiteCommandJournal(journal_path)
    assert _open_handles(journal_path) == baseline

    authority_path = tmp_path / "authority.sqlite"
    authority = AuthorityStore(authority_path)
    baseline = _open_handles(authority_path)
    for index in range(20):
        assert authority.acquire("p", f"holder-{index}") is None or index == 0
        authority.current("p")
    assert _open_handles(authority_path) == baseline

    # And it still does what it did: authority is exclusive while the lease
    # is live, which is the whole reason it opens a transaction at all.
    assert authority.current("p") is not None
    assert authority.current("p").holder_id == "holder-0"  # type: ignore[union-attr]
    assert authority.release("p", "holder-0") is True
    assert authority.current("p") is None
