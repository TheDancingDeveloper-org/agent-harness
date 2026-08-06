"""Claim tests. Claims are leases, and the properties that matter are what
happens when a worker dies, and what happens when two race."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.graph import ResolverOutcome
from agent_harness.outcomes import BLOCKED
from agent_harness.work import (
    CLAIMED,
    DONE,
    FAILED,
    PENDING,
    WorkQueue,
    WorkRecord,
)
from conftest import make_queue


@pytest.fixture
def clock() -> list[float]:
    return [1000.0]


@pytest.fixture
def queue(tmp_path: Path, clock: list[float]) -> WorkQueue:
    return make_queue(str(tmp_path / "w.sqlite"), lease_seconds=100.0, now=lambda: clock[0])


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


def test_a_dependency_the_graph_cannot_find_blocks_and_says_so(queue: WorkQueue) -> None:
    """This replaces the old rule, and the replacement is the point of Stage G.

    A dependency absent from the queue used to be treated as satisfied, on
    the grounds that plans reference work tracked elsewhere. That is true,
    and it made a typo, an omitted item and a genuine external reference
    indistinguishable -- all three ran immediately. An unresolvable required
    target is now a blocker that explains itself.
    """
    queue.add([rec("T1", depends_on=["EXTERNAL-9"])])

    assert queue.claim("a") is None
    state = queue.readiness("T1")
    assert state.ready is False
    assert [r.target_id for r in state.reasons] == ["EXTERNAL-9"]
    assert state.reasons[0].state == "unresolved"
    assert "no item 'EXTERNAL-9'" in state.reasons[0].explanation


def test_a_declared_external_target_is_claimable_once_a_resolver_answers(
    queue: WorkQueue,
) -> None:
    """The legitimate case still works -- it just has to say what it is.

    An external reference declares its kind and its resolver, and becomes
    satisfied when that resolver reports an outcome. Nothing is assumed on
    its behalf in the meantime.
    """
    queue.add([rec("T1", depends_on=["external:tracker:TICKET-9"])])

    assert queue.claim("a") is None, "an external target with no outcome is not satisfied"
    reason = queue.readiness("T1").reasons[0]
    assert reason.target_kind == "external_reference"
    assert reason.resolver == "tracker"
    assert "has not reported an outcome" in reason.explanation

    queue.graph.record_resolver_outcome(
        "default",
        "T1",
        "TICKET-9",
        ResolverOutcome("satisfied", "TICKET-9 was closed on 2026-08-01"),
    )
    claimed = queue.claim("a")
    assert claimed is not None and claimed.item_id == "T1"


def test_an_advisory_dependency_is_reported_but_never_blocks(queue: WorkQueue) -> None:
    """`?T9` says "related", not "wait for". Reported, because an advisory
    edge that vanished silently would be indistinguishable from one that was
    never declared."""
    queue.add([rec("T1", depends_on=["?T9"])])

    claimed = queue.claim("a")
    assert claimed is not None
    state = queue.readiness("T1")
    assert state.ready is True
    assert [r.target_id for r in state.advisory] == ["T9"]


def test_a_cycle_blocks_every_member_and_names_the_loop(queue: WorkQueue) -> None:
    """Two items that each wait for the other used to be reported as
    "waiting", forever, with nothing saying the wait could never end."""
    queue.add([rec("T1", depends_on=["T2"]), rec("T2", depends_on=["T1"])])

    assert queue.claim("a") is None
    state = queue.readiness("T1")
    cycle = [r for r in state.reasons if r.kind == "cycle"]
    assert cycle, state.reasons
    assert cycle[0].evidence == "T1 -> T2 -> T1"


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


def test_release_can_return_unattempted_work_without_consuming_an_attempt(
    queue: WorkQueue,
) -> None:
    queue.add([rec("T1")])
    queue.claim("a")

    queue.release("T1", PENDING, owner="a", consume_attempt=False)

    assert queue.get("T1").attempts == 0  # type: ignore[union-attr]


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
    first = make_queue(path, lease_seconds=100.0, now=lambda: 1000.0)
    first.add([rec("T1"), rec("T2")])
    first.claim("a")
    first.release("T1", DONE)

    resumed = make_queue(path, lease_seconds=100.0, now=lambda: 1000.0)
    assert resumed.get("T1").state == DONE  # type: ignore[union-attr]
    nxt = resumed.claim("b")
    assert nxt is not None and nxt.item_id == "T2"


def test_an_empty_queue_claims_nothing_rather_than_erroring(queue: WorkQueue) -> None:
    assert queue.claim("a") is None


# ------------------------------------------------------------- fleet control


def test_pausing_stops_new_claims_but_not_work_in_flight(queue: WorkQueue) -> None:
    """Killing an agent mid-item destroys the context that makes its work
    resumable. Stopping at the next boundary is strictly better."""
    queue.add([rec("T1"), rec("T2")])
    in_flight = queue.claim("a")
    assert in_flight is not None

    queue.set_control("paused", "deploying")
    assert queue.claim("b") is None
    # The item already claimed is untouched — its lease still stands.
    record = queue.get(in_flight.item_id)
    assert record is not None
    assert record.state == CLAIMED
    assert record.owner == "a"


def test_resuming_claims_again(queue: WorkQueue) -> None:
    queue.add([rec("T1")])
    queue.set_control("paused")
    assert queue.claim("a") is None
    queue.set_control("running")
    assert queue.claim("a") is not None


def test_draining_behaves_like_paused_but_records_the_intent(queue: WorkQueue) -> None:
    """The difference matters to whoever finds the fleet stopped and has to
    decide whether to resume it."""
    queue.add([rec("T1")])
    queue.set_control("draining", "rolling out a new image")
    assert queue.claim("a") is None
    assert queue.control() == ("draining", "rolling out a new image")


def test_control_defaults_to_running(queue: WorkQueue) -> None:
    assert queue.control() == ("running", None)


def test_an_unknown_control_state_is_refused(queue: WorkQueue) -> None:
    with pytest.raises(ValueError, match="unknown control state"):
        queue.set_control("halt")


def test_control_survives_a_restart(tmp_path: Path) -> None:
    """Deliberately not `make_queue`: that sets the project running on
    construction, which is exactly what this test needs NOT to happen."""
    path = str(tmp_path / "w.sqlite")
    WorkQueue(path).set_control("paused", "overnight")
    assert WorkQueue(path).control() == ("paused", "overnight")


# ---------------------------------------------------------------- settings


def test_settings_are_shared_across_processes(tmp_path: Path) -> None:
    """The API and the worker are different processes; an in-memory value
    could never be changed from outside the loop using it."""
    path = str(tmp_path / "w.sqlite")
    make_queue(path).set_setting("role_map", {"reviewer": {"model": "m"}})
    assert make_queue(path).get_setting("role_map") == {"reviewer": {"model": "m"}}


def test_an_unset_setting_is_none_not_an_error(queue: WorkQueue) -> None:
    assert queue.get_setting("nope") is None


def test_a_reviewed_setting_replacement_is_atomic(tmp_path: Path) -> None:
    queue = make_queue(str(tmp_path / "w.sqlite"))
    original = {"reviewer": {"model": "old"}}
    queue.set_setting("role_map", original)

    assert queue.compare_and_set_setting("role_map", original, {"reviewer": {"model": "reviewed"}})
    queue.set_setting("role_map", {"reviewer": {"model": "concurrent"}})

    assert not queue.compare_and_set_setting(
        "role_map", {"reviewer": {"model": "reviewed"}}, {"reviewer": {"model": "stale"}}
    )
    assert queue.get_setting("role_map") == {"reviewer": {"model": "concurrent"}}


# ------------------------------- a rewritten brief revives a stalled item


def _stalled(tmp_path: Path, state: str, *, brief: str = "the brief") -> WorkQueue:
    """An item the queue has stopped, ready for a plan re-sync to land on."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.add([WorkRecord(item_id="R7", title="t", brief=brief)])
    queue.set_control("running")
    queue.claim(owner="w")
    queue.release("R7", state, owner="w", error="cannot be done as specified")
    return queue


def test_a_rewritten_brief_returns_a_blocked_item_to_pending(tmp_path: Path) -> None:
    """The second half of the refusal loop (#178).

    An agent refuses an item as impossible, a human rewrites it in response,
    and until this the rewrite was inert: the brief updated, the item stayed
    put, and the next run said "nothing to do".
    """
    queue = _stalled(tmp_path, BLOCKED)

    queue.add([WorkRecord(item_id="R7", title="t", brief="an achievable thing instead")])

    item = queue.get("R7")
    assert item is not None
    assert item.state == PENDING
    assert item.brief == "an achievable thing instead"
    # Feeding the old refusal to the next attempt would be telling it not to
    # repeat a fault it can no longer commit.
    assert not item.last_error


def test_a_rewritten_brief_also_revives_a_failed_item(tmp_path: Path) -> None:
    queue = _stalled(tmp_path, FAILED)

    queue.add([WorkRecord(item_id="R7", title="t", brief="something else entirely")])

    item = queue.get("R7")
    assert item is not None
    assert item.state == PENDING


def test_an_unchanged_brief_leaves_a_failed_item_where_it_was(tmp_path: Path) -> None:
    """A routine re-sync must not un-fail work, and a better title is not an answer."""
    queue = _stalled(tmp_path, FAILED)

    queue.add([WorkRecord(item_id="R7", title="a better title", brief="the brief")])

    item = queue.get("R7")
    assert item is not None
    assert item.state == FAILED
    assert item.title == "a better title"
    assert item.last_error == "cannot be done as specified"


def test_whitespace_alone_is_not_a_rewrite(tmp_path: Path) -> None:
    """Re-parsing a plan must not revive an item because a newline moved."""
    queue = _stalled(tmp_path, FAILED)

    queue.add([WorkRecord(item_id="R7", title="t", brief="  the brief\n")])

    item = queue.get("R7")
    assert item is not None
    assert item.state == FAILED


def test_rewriting_the_brief_of_finished_work_does_not_unfinish_it(tmp_path: Path) -> None:
    """`done` is not stalled, and editing a description is not a reason to redo it."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.add([WorkRecord(item_id="R1", title="t", brief="the brief")])
    queue.set_control("running")
    queue.claim(owner="w")
    queue.release("R1", DONE, owner="w")

    queue.add([WorkRecord(item_id="R1", title="t", brief="a completely different brief")])

    item = queue.get("R1")
    assert item is not None
    assert item.state == DONE
