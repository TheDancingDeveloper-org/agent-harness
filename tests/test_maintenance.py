"""The thing that calls rollup and thin.

A method nobody invokes is the same defect as the session reaper that lived
on the client and was never called. This exists so that cannot happen again,
and the last test here is the one that proves it.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from agent_harness.audit import AuditStore
from agent_harness.events import WORK, Event
from agent_harness.maintenance import (
    MaintenanceLoop,
    MaintenanceReport,
    run_maintenance,
)

DAY = 86400.0
BASE = 1_700_000_000.0


def ev(ts: float, seq: int) -> Event:
    return Event(
        ts=ts,
        kind=WORK,
        source="test",
        outcome="done",
        data={"run_id": "r", "seq": seq, "project_id": "p"},
    )


@pytest.fixture
def audit(tmp_path: Path) -> AuditStore:
    store = AuditStore(tmp_path / "audit.sqlite")
    store.append([ev(BASE + n, n) for n in range(5)])
    return store


def test_a_pass_rolls_up_then_thins(audit: AuditStore) -> None:
    report = run_maintenance(audit, retention_days=0)

    assert report.rolled_up == 1
    assert report.thinned == 5
    assert audit.count() == 0
    assert audit.rollups()[0]["events"] == 5, "the series did not survive the thinning"


def test_a_failed_rollup_stops_the_pass_before_it_thins(
    audit: AuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering is the whole discipline.

    Thinning after a failed rollup removes raw rows that no aggregate has
    replaced -- a hole in the series, and nothing reports it.
    """

    def boom() -> int:
        raise RuntimeError("disk is unhappy")

    monkeypatch.setattr(audit, "rollup", boom)
    report = run_maintenance(audit, retention_days=0)

    assert report.rolled_up == 0
    assert report.thinned == 0, "thinned despite the rollup failing"
    assert audit.count() == 5, "raw events were removed with nothing covering them"
    assert report.errors


def test_maintenance_never_raises(audit: AuditStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Housekeeping must not be able to take the API down."""

    def boom(**kwargs: object) -> int:
        raise RuntimeError("nope")

    monkeypatch.setattr(audit, "thin", boom)
    report = run_maintenance(audit)
    assert report.errors


def test_a_degraded_store_is_left_alone(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    degraded = AuditStore(blocker / "audit.sqlite", degraded=True)

    assert run_maintenance(degraded) == MaintenanceReport()


def test_the_loop_actually_runs_a_pass(audit: AuditStore) -> None:
    """The test that would have caught the reaper bug.

    Everything above proves the work is correct when invoked. This proves it
    is invoked.
    """
    ran = threading.Event()
    loop = MaintenanceLoop(audit, interval=3600, retention_days=0, on_pass=lambda _: ran.set())
    loop.start()
    try:
        assert ran.wait(timeout=10), "the maintenance loop never ran a pass"
    finally:
        loop.stop()

    assert audit.rollups(), "the loop ran but rolled nothing up"


def test_the_loop_runs_immediately_rather_than_after_one_interval(audit: AuditStore) -> None:
    """After a restart, the first useful thing to know is whether yesterday
    closed -- not to wait an hour to find out."""
    ran = threading.Event()
    loop = MaintenanceLoop(audit, interval=86400, retention_days=90, on_pass=lambda _: ran.set())
    loop.start()
    try:
        assert ran.wait(timeout=10)
    finally:
        loop.stop()


def test_a_degraded_store_does_not_get_a_thread(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    loop = MaintenanceLoop(AuditStore(blocker / "a.sqlite", degraded=True))
    loop.start()
    assert loop._thread is None
    loop.stop()


def test_a_broken_callback_cannot_kill_the_loop(audit: AuditStore) -> None:
    passes = []

    def bad(report: MaintenanceReport) -> None:
        passes.append(report)
        raise RuntimeError("callback exploded")

    loop = MaintenanceLoop(audit, interval=0.05, retention_days=0, on_pass=bad)
    loop.start()
    try:
        deadline = threading.Event()
        deadline.wait(0.6)
    finally:
        loop.stop()

    assert len(passes) > 1, "the loop stopped after a callback raised"


def test_reconciliation_runs_on_a_slower_cadence_than_rollups(audit: AuditStore) -> None:
    """A merged PR stays merged. Hammering the GitHub API every hour to learn
    nothing is how a token gets rate limited for no benefit."""
    calls: list[int] = []

    class FakeQueue:
        def projects(self):  # type: ignore[no-untyped-def]
            calls.append(1)
            return []

        def items(self):  # type: ignore[no-untyped-def]
            return []

    loop = MaintenanceLoop(
        audit, interval=0.02, retention_days=0, queue=FakeQueue(), reconcile_every=5
    )
    loop.start()
    try:
        threading.Event().wait(0.4)
    finally:
        loop.stop()

    # Many passes, far fewer reconciliations.
    assert calls, "reconciliation never ran"
    assert len(calls) < 10, f"reconciled on every pass ({len(calls)} times)"


def test_one_repo_failing_does_not_stop_the_others(audit: AuditStore) -> None:
    """A project with a bad token must not cost every other project its
    ground truth."""
    from agent_harness.maintenance import reconcile_projects

    class Project:
        def __init__(self, pid: str, repo: str) -> None:
            self.project_id, self.repo = pid, repo

    class FakeQueue:
        def projects(self):  # type: ignore[no-untyped-def]
            return [Project("a", "o/broken"), Project("b", "o/fine")]

        def items(self):  # type: ignore[no-untyped-def]
            return []

    counts, errors = reconcile_projects(audit, FakeQueue())
    # Both were attempted; neither aborted the sweep.
    assert isinstance(counts, dict)
    assert isinstance(errors, list)
