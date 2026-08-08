from __future__ import annotations

import json
import multiprocessing
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from agent_harness.execution_environment import LocalExecutionEnvironment
from agent_harness.executor import Checks, Executor
from agent_harness.fleet import Fleet
from agent_harness.model_client import ModelClient, Response, Route
from agent_harness.outcomes import PASS, CheckResult
from agent_harness.plan_integration import PlanCoordinator, PromotionConflict, PromotionError
from agent_harness.role_runners import RoleRunResult
from agent_harness.work import DONE, RUNNING, Project, WorkQueue, WorkRecord


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def commit(repo: Path, message: str, content: str, name: str = "value.txt") -> str:
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    commit(repo, "base", "base\n")
    return repo


class _ProcessPromotionChecks:
    def __init__(
        self,
        item_id: str,
        first_entered: Any,
        second_entered: Any,
        release_first: Any,
    ) -> None:
        self.item_id = item_id
        self.first_entered = first_entered
        self.second_entered = second_entered
        self.release_first = release_first

    def run(self, tree: Path) -> CheckResult:
        del tree
        if self.item_id == "A":
            self.first_entered.set()
            if not self.release_first.wait(timeout=10):
                raise AssertionError("first promotion gate was not released")
        else:
            self.second_entered.set()
        return CheckResult(PASS)


def _promote_from_process(
    queue_path: str,
    repo: str,
    item_id: str,
    item_branch: str,
    first_entered: Any,
    second_entered: Any,
    release_first: Any,
    second_blocked: Any,
    allow_second_wait: Any,
) -> None:
    queue = WorkQueue(queue_path, lease_seconds=100.0)

    def promotion_sleep(_: float) -> None:
        second_blocked.set()
        if not allow_second_wait.wait(timeout=10):
            raise AssertionError("second promotion was not released from lease wait")

    coordinator = PlanCoordinator(
        queue,
        "p",
        Path(repo),
        checks=cast(
            Checks,
            _ProcessPromotionChecks(item_id, first_entered, second_entered, release_first),
        ),
        promotion_lease_seconds=10.0,
        promotion_wait_seconds=10.0,
        promotion_sleep=promotion_sleep if item_id == "B" else (lambda _: None),
    )
    record = queue.get(item_id, project_id="p")
    assert record is not None
    coordinator.promote(record, item_branch=item_branch, base="main")


def test_plan_branch_is_created_from_exact_target_and_survives_restart(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    plan_file = tmp_path / "PLAN.md"
    plan_file.write_text("# plan\n")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(
        Project(
            project_id="p",
            name="p",
            work_dir=str(repo),
            plan_path=str(plan_file),
            plan_branch="integration/p",
        )
    )
    target = git(repo, "rev-parse", "main")
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    state = coordinator.ensure(
        target_branch="main", branch="integration/p", plan_path=str(plan_file)
    )

    assert state.target_sha == target
    assert state.head_sha == target
    assert git(repo, "rev-parse", "integration/p") == target
    restored = PlanCoordinator(
        WorkQueue(str(tmp_path / "queue.sqlite")), "p", repo, checks=Checks()
    )
    assert restored.state() == state


def test_independent_promotions_then_dependent_sees_both(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    coordinator.ensure(target_branch="main", branch="integration/p")
    target = git(repo, "rev-parse", "main")

    a_branch = "harness/a"
    git(repo, "branch", a_branch, "integration/p")
    git(repo, "checkout", a_branch)
    commit(repo, "A", "A\n", "a.txt")
    git(repo, "checkout", "main")
    b_branch = "harness/b"
    git(repo, "branch", b_branch, "integration/p")
    git(repo, "checkout", b_branch)
    commit(repo, "B", "B\n", "b.txt")
    git(repo, "checkout", "main")

    queue.add(
        [
            WorkRecord("A", "A", state=DONE, branch=a_branch),
            WorkRecord("B", "B", state=DONE, branch=b_branch),
            WorkRecord("C", "C", depends_on=["A", "B"]),
        ],
        project_id="p",
    )
    a = queue.get("A", project_id="p")
    b = queue.get("B", project_id="p")
    assert a is not None and b is not None
    coordinator.promote(a, item_branch=a_branch, base=target)
    coordinator.promote(b, item_branch=b_branch, base=target)
    c = queue.get("C", project_id="p")
    assert c is not None
    assert coordinator.base_for(c) == ("integration/p", "local plan branch")
    assert git(repo, "show", "integration/p:a.txt") == "A"
    assert git(repo, "show", "integration/p:b.txt") == "B"
    c_branch = "harness/c"
    git(repo, "branch", c_branch, "integration/p")
    git(repo, "checkout", c_branch)
    commit(repo, "C", "C\n", "c.txt")
    git(repo, "checkout", "main")
    c.branch = c_branch
    c.state = DONE
    coordinator.promote(c, item_branch=c_branch, base="integration/p")
    assert git(repo, "show", "integration/p:c.txt") == "C"


def test_promotion_replays_item_created_from_older_plan_head(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    coordinator.ensure(target_branch="main", branch="integration/p")
    target = git(repo, "rev-parse", "main")

    # Both workers started from the original plan head.  B reaches promotion
    # first, so A must be replayed onto a plan branch that has since moved.
    a_branch = "harness/older-a"
    git(repo, "branch", a_branch, target)
    git(repo, "checkout", a_branch)
    commit(repo, "A", "A\n", "a.txt")
    git(repo, "checkout", "main")
    b_branch = "harness/first-b"
    git(repo, "branch", b_branch, target)
    git(repo, "checkout", b_branch)
    commit(repo, "B", "B\n", "b.txt")
    git(repo, "checkout", "main")

    queue.add(
        [
            WorkRecord("A", "A", state=DONE, branch=a_branch),
            WorkRecord("B", "B", state=DONE, branch=b_branch),
        ],
        project_id="p",
    )
    a = queue.get("A", project_id="p")
    b = queue.get("B", project_id="p")
    assert a is not None and b is not None
    coordinator.promote(b, item_branch=b_branch, base=target)
    before_a = coordinator.state()
    coordinator.promote(a, item_branch=a_branch, base=target)

    after_a = coordinator.state()
    assert after_a.head_sha != before_a.head_sha
    assert git(repo, "show", "integration/p:a.txt") == "A"
    assert git(repo, "show", "integration/p:b.txt") == "B"
    assert (
        git(repo, "merge-base", "--is-ancestor", git(repo, "rev-parse", b_branch), after_a.head_sha)
        == ""
    )
    assert (
        git(repo, "merge-base", "--is-ancestor", git(repo, "rev-parse", a_branch), after_a.head_sha)
        == ""
    )
    assert git(repo, "rev-parse", f"{after_a.head_sha}^2") == git(repo, "rev-parse", a_branch)
    promotion = queue.latest_promotion("p", "A")
    assert promotion is not None
    assert promotion["base_sha"] == target
    assert promotion["old_head_sha"] == before_a.head_sha
    assert promotion["new_head_sha"] == after_a.head_sha


def test_dependent_admission_waits_for_every_prerequisite_promotion(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    coordinator.ensure(target_branch="main", branch="integration/p")
    target = git(repo, "rev-parse", "main")

    branches: dict[str, str] = {}
    for item, filename in (("A", "a.txt"), ("B", "b.txt")):
        branch = f"harness/{item.lower()}-admission"
        git(repo, "branch", branch, target)
        git(repo, "checkout", branch)
        commit(repo, item, item + "\n", filename)
        git(repo, "checkout", "main")
        branches[item] = branch

    queue.add(
        [
            WorkRecord("A", "A", state=DONE, branch=branches["A"]),
            WorkRecord("B", "B", state=DONE, branch=branches["B"]),
            WorkRecord("C", "C", depends_on=["A", "B"]),
        ],
        project_id="p",
    )
    queue.set_control("running", project_id="p")

    assert queue.claim("worker", project_id="p") is None
    blocked = queue.readiness("C", project_id="p")
    assert blocked.ready is False
    assert {reason.target_id for reason in blocked.reasons} == {"A", "B"}

    a = queue.get("A", project_id="p")
    b = queue.get("B", project_id="p")
    assert a is not None and b is not None
    coordinator.promote(a, item_branch=branches["A"], base=target)
    assert queue.claim("worker", project_id="p") is None
    coordinator.promote(b, item_branch=branches["B"], base=target)
    claimed = queue.claim("worker", project_id="p")
    assert claimed is not None and claimed.item_id == "C"


def test_advisory_local_dependency_does_not_block_promotion(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    coordinator.ensure(target_branch="main", branch="integration/p")
    target = git(repo, "rev-parse", "main")

    branch = "harness/advisory"
    git(repo, "branch", branch, target)
    git(repo, "checkout", branch)
    commit(repo, "work", "work\n", "work.txt")
    git(repo, "checkout", "main")
    record = WorkRecord("A", "A", state=DONE, branch=branch, depends_on=["?MISSING"])
    queue.add([record], project_id="p")

    coordinator.promote(record, item_branch=branch, base=target)
    assert queue.latest_promotion("p", "A") is not None


def test_restart_recovers_promotion_after_git_ref_advanced(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    state = coordinator.ensure(target_branch="main", branch="integration/p")
    target = state.head_sha
    branch = "harness/recover-a"
    git(repo, "branch", branch, target)
    git(repo, "checkout", branch)
    item_sha = commit(repo, "A", "A\n", "a.txt")
    git(repo, "checkout", "main")
    queue.add([WorkRecord("A", "A", state=DONE, branch=branch)], project_id="p")

    old_head = state.head_sha
    new_head = item_sha
    promotion_id = queue.begin_promotion("p", "A", branch, target, old_head, new_head)
    git(repo, "update-ref", "refs/heads/integration/p", new_head, old_head)
    events: list[dict[str, Any]] = []
    restarted = PlanCoordinator(
        WorkQueue(str(tmp_path / "queue.sqlite")),
        "p",
        repo,
        checks=Checks(),
        on_event=events.append,
    )
    recovered = restarted.ensure(target_branch="main", branch="integration/p")
    assert recovered.head_sha == new_head
    promotion = queue.latest_promotion("p", "A")
    assert promotion is not None
    assert promotion["promotion_id"] == promotion_id
    assert promotion["status"] == "promoted"
    assert len(events) == 1
    assert events[0]["outcome"] == "plan_promotion"
    assert events[0]["promotion_id"] == promotion_id
    assert events[0]["status"] == "promoted"
    assert events[0]["item_sha"] == item_sha
    assert events[0]["new_head_sha"] == new_head
    assert events[0]["detail"] == "recovered after restart"


def test_restart_abandons_promotion_when_git_ref_did_not_move(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    state = coordinator.ensure(target_branch="main", branch="integration/p")
    events: list[dict[str, Any]] = []
    promotion_id = queue.begin_promotion(
        "p", "A", "harness/a", state.head_sha, state.head_sha, "not-a-real-sha"
    )

    restarted = PlanCoordinator(
        WorkQueue(str(tmp_path / "queue.sqlite")),
        "p",
        repo,
        checks=Checks(),
        on_event=events.append,
    )
    recovered = restarted.ensure(target_branch="main", branch="integration/p")
    assert recovered.head_sha == state.head_sha
    conn = queue._connect()
    try:
        row = conn.execute(
            "SELECT status, detail FROM plan_promotions WHERE promotion_id = ?",
            (promotion_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["status"] == "abandoned"
    assert len(events) == 1
    assert events[0]["outcome"] == "plan_promotion"
    assert events[0]["promotion_id"] == promotion_id
    assert events[0]["status"] == "abandoned"
    assert events[0]["detail"] == row["detail"]


def test_conflicting_promotion_is_returned_for_repair_and_head_is_unchanged(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    events: list[dict[str, Any]] = []
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks(), on_event=events.append)
    coordinator.ensure(target_branch="main", branch="integration/p")
    target = git(repo, "rev-parse", "main")
    branch = "harness/conflict"
    git(repo, "branch", branch, "integration/p")
    git(repo, "checkout", branch)
    commit(repo, "conflict", "base\nitem\n")
    git(repo, "checkout", "main")
    other = "harness/other"
    git(repo, "branch", other, "integration/p")
    git(repo, "checkout", other)
    commit(repo, "other", "base\nother\n")
    git(repo, "checkout", "main")
    record = WorkRecord("X", "X", state=DONE, branch=branch)
    queue.add([record], project_id="p")
    other_record = WorkRecord("Y", "Y", state=DONE, branch=other)
    queue.add([other_record], project_id="p")
    coordinator.promote(other_record, item_branch=other, base="integration/p")
    before = coordinator.state().head_sha
    with pytest.raises(PromotionConflict):
        coordinator.promote(record, item_branch=branch, base=target)
    assert coordinator.state().head_sha == before
    assert queue.latest_promotion("p", "X") is None
    conn = queue._connect()
    try:
        promotion = conn.execute(
            "SELECT promotion_id, status, item_sha FROM plan_promotions "
            "WHERE project_id = ? AND item_id = ? ORDER BY promotion_id DESC LIMIT 1",
            ("p", "X"),
        ).fetchone()
    finally:
        conn.close()
    assert promotion is not None and promotion["status"] == "conflict"
    event = next(event for event in events if event["item_id"] == "X")
    assert event["promotion_id"] == promotion["promotion_id"]
    assert event["status"] == "conflict"
    assert event["item_sha"] == promotion["item_sha"]


def test_target_move_rebuilds_plan_and_replays_promoted_items(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    initial = coordinator.ensure(target_branch="main", branch="integration/p")

    item_branch = "harness/refresh-item"
    git(repo, "branch", item_branch, initial.target_sha)
    git(repo, "checkout", item_branch)
    item_sha = commit(repo, "item", "item\n", "item.txt")
    git(repo, "checkout", "main")
    queue.add(
        [WorkRecord("A", "A", state=DONE, branch=item_branch)],
        project_id="p",
    )
    item = queue.get("A", project_id="p")
    assert item is not None
    coordinator.promote(item, item_branch=item_branch, base=initial.target_sha)

    target_sha = commit(repo, "target moved", "target\n", "target.txt")
    refreshed = coordinator.ensure(target_branch="main", branch="integration/p")

    assert refreshed.target_sha == target_sha
    assert refreshed.head_sha == git(repo, "rev-parse", "integration/p")
    assert git(repo, "show", "integration/p:item.txt") == "item"
    assert git(repo, "show", "integration/p:target.txt") == "target"
    assert git(repo, "merge-base", "--is-ancestor", item_sha, refreshed.head_sha) == ""
    assert git(repo, "rev-parse", f"{refreshed.head_sha}^2") == item_sha
    promotion = queue.latest_promotion("p", "A")
    assert promotion is not None and promotion["item_sha"] == item_sha
    refreshes = queue.in_progress_refreshes("p")
    assert refreshes == []
    conn = queue._connect()
    try:
        row = conn.execute(
            "SELECT status, old_target_sha, new_target_sha FROM plan_refreshes "
            "WHERE project_id = ? ORDER BY refresh_id DESC LIMIT 1",
            ("p",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["status"] == "refreshed"
    assert row["old_target_sha"] == initial.target_sha
    assert row["new_target_sha"] == target_sha


def test_target_move_replays_dependent_item_created_from_promoted_plan_head(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    initial = coordinator.ensure(target_branch="main", branch="integration/p")

    a_branch = "harness/refresh-dependent-a"
    git(repo, "branch", a_branch, initial.target_sha)
    git(repo, "checkout", a_branch)
    a_sha = commit(repo, "A", "A\n", "a.txt")
    git(repo, "checkout", "main")
    queue.add([WorkRecord("A", "A", state=DONE, branch=a_branch)], project_id="p")
    a = queue.get("A", project_id="p")
    assert a is not None
    coordinator.promote(a, item_branch=a_branch, base=initial.target_sha)
    after_a = coordinator.state()

    # B is authored from the already-promoted plan head, as a real dependent
    # worker is. Refresh must replay its own item commit after A, not assume
    # every item was based directly on the target branch.
    b_branch = "harness/refresh-dependent-b"
    git(repo, "branch", b_branch, after_a.head_sha)
    git(repo, "checkout", b_branch)
    b_sha = commit(repo, "B", "B\n", "b.txt")
    git(repo, "checkout", "main")
    queue.add(
        [WorkRecord("B", "B", state=DONE, branch=b_branch, depends_on=["A"])],
        project_id="p",
    )
    b = queue.get("B", project_id="p")
    assert b is not None
    coordinator.promote(b, item_branch=b_branch, base=after_a.branch)

    target_sha = commit(repo, "target moved", "target\n", "target.txt")
    refreshed = coordinator.ensure(target_branch="main", branch="integration/p")

    assert refreshed.target_sha == target_sha
    assert git(repo, "show", "integration/p:a.txt") == "A"
    assert git(repo, "show", "integration/p:b.txt") == "B"
    assert git(repo, "show", "integration/p:target.txt") == "target"
    assert git(repo, "merge-base", "--is-ancestor", a_sha, refreshed.head_sha) == ""
    assert git(repo, "merge-base", "--is-ancestor", b_sha, refreshed.head_sha) == ""
    assert git(repo, "rev-parse", f"{refreshed.head_sha}^2") == b_sha


def test_promotion_refreshes_plan_when_target_moves_during_long_lived_run(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    initial = coordinator.ensure(target_branch="main", branch="integration/p")

    item_branch = "harness/live-target-item"
    git(repo, "branch", item_branch, initial.target_sha)
    git(repo, "checkout", item_branch)
    item_sha = commit(repo, "item", "item\n", "item.txt")
    git(repo, "checkout", "main")
    queue.add(
        [WorkRecord("A", "A", state=DONE, branch=item_branch)],
        project_id="p",
    )
    item = queue.get("A", project_id="p")
    assert item is not None

    target_sha = commit(repo, "target moved before promotion", "target\n", "target.txt")
    promoted = coordinator.promote(item, item_branch=item_branch, base=initial.target_sha)

    state = coordinator.state()
    assert promoted.status == "promoted"
    assert state.target_sha == target_sha
    assert git(repo, "show", "integration/p:item.txt") == "item"
    assert git(repo, "show", "integration/p:target.txt") == "target"
    promotion = queue.latest_promotion("p", "A")
    assert promotion is not None and promotion["item_sha"] == item_sha


def test_refresh_restarts_if_target_moves_while_replaying_promotions(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    initial = coordinator.ensure(target_branch="main", branch="integration/p")

    item_branch = "harness/refresh-target-race-item"
    git(repo, "branch", item_branch, initial.target_sha)
    git(repo, "checkout", item_branch)
    item_sha = commit(repo, "item", "item\n", "item.txt")
    git(repo, "checkout", "main")
    queue.add(
        [WorkRecord("A", "A", state=DONE, branch=item_branch)],
        project_id="p",
    )
    item = queue.get("A", project_id="p")
    assert item is not None
    coordinator.promote(item, item_branch=item_branch, base=initial.target_sha)

    first_target = commit(repo, "target moved once", "one\n", "target.txt")
    moved = False

    class TargetMovingChecks:
        def run(self, tree: Path) -> CheckResult:
            nonlocal moved
            if not moved:
                moved = True
                commit(repo, "target moved twice", "two\n", "target.txt")
            return CheckResult(PASS)

    coordinator.checks = TargetMovingChecks()  # type: ignore[assignment]
    refreshed = coordinator.ensure(target_branch="main", branch="integration/p")
    second_target = git(repo, "rev-parse", "main")

    assert first_target != second_target
    assert refreshed.target_sha == second_target
    assert git(repo, "show", "integration/p:target.txt") == "two"
    assert git(repo, "show", "integration/p:item.txt") == "item"
    promotion = queue.latest_promotion("p", "A")
    assert promotion is not None and promotion["item_sha"] == item_sha
    conn = queue._connect()
    try:
        rows = conn.execute(
            "SELECT status, detail FROM plan_refreshes WHERE project_id = ? ORDER BY refresh_id",
            ("p",),
        ).fetchall()
    finally:
        conn.close()
    assert [row["status"] for row in rows] == ["superseded", "refreshed"]
    assert "target advanced during replay" in rows[0]["detail"]


def test_promotion_rechecks_target_after_integration_gates(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    initial = coordinator.ensure(target_branch="main", branch="integration/p")

    item_branch = "harness/promotion-target-race-item"
    git(repo, "branch", item_branch, initial.target_sha)
    git(repo, "checkout", item_branch)
    item_sha = commit(repo, "item", "item\n", "item.txt")
    git(repo, "checkout", "main")
    queue.add(
        [WorkRecord("A", "A", state=DONE, branch=item_branch)],
        project_id="p",
    )
    item = queue.get("A", project_id="p")
    assert item is not None
    moved = False

    class TargetMovingChecks:
        def run(self, tree: Path) -> CheckResult:
            nonlocal moved
            if not moved:
                moved = True
                commit(repo, "target moved during promotion", "target\n", "target.txt")
            return CheckResult(PASS)

    coordinator.checks = TargetMovingChecks()  # type: ignore[assignment]
    promoted = coordinator.promote(item, item_branch=item_branch, base=initial.target_sha)
    state = coordinator.state()

    assert promoted.status == "promoted"
    assert state.target_sha == git(repo, "rev-parse", "main")
    assert git(repo, "show", "integration/p:target.txt") == "target"
    assert git(repo, "show", "integration/p:item.txt") == "item"
    promotion = queue.latest_promotion("p", "A")
    assert promotion is not None and promotion["item_sha"] == item_sha
    assert queue.in_progress_refreshes("p") == []


def test_promotion_event_retains_item_and_plan_commit_identity(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    events: list[dict[str, Any]] = []

    def record_then_fail(event: dict[str, Any]) -> None:
        events.append(event)
        raise RuntimeError("audit sink unavailable")

    coordinator = PlanCoordinator(
        queue,
        "p",
        repo,
        checks=Checks(),
        on_event=record_then_fail,
    )
    initial = coordinator.ensure(target_branch="main", branch="integration/p")
    item_branch = "harness/promotion-event-item"
    git(repo, "branch", item_branch, initial.target_sha)
    git(repo, "checkout", item_branch)
    item_sha = commit(repo, "item", "item\n", "item.txt")
    git(repo, "checkout", "main")
    queue.add(
        [WorkRecord("A", "A", state=DONE, branch=item_branch)],
        project_id="p",
    )
    item = queue.get("A", project_id="p")
    assert item is not None

    promoted = coordinator.promote(item, item_branch=item_branch, base=initial.target_sha)
    promotion = queue.latest_promotion("p", "A")
    assert promotion is not None
    plan_head = git(repo, "rev-parse", "integration/p")

    assert promoted.status == "promoted"
    assert events == [
        {
            "ts": events[0]["ts"],
            "kind": "work",
            "project_id": "p",
            "item_id": "A",
            "outcome": "plan_promotion",
            "promotion_id": promotion["promotion_id"],
            "status": "promoted",
            "plan_branch": "integration/p",
            "base_sha": initial.target_sha,
            "item_sha": item_sha,
            "old_head_sha": initial.head_sha,
            "new_head_sha": plan_head,
            "target_sha": initial.target_sha,
            "detail": "authoritative integration gates passed",
        }
    ]
    assert promotion["item_sha"] == events[0]["item_sha"]
    assert promotion["new_head_sha"] == events[0]["new_head_sha"]
    assert git(repo, "rev-parse", item_branch) == item_sha
    assert git(repo, "show", f"{item_branch}:item.txt") == "item"


def test_separate_coordinators_serialize_integration_gates(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    initial = PlanCoordinator(queue, "p", repo, checks=Checks()).ensure(
        target_branch="main", branch="integration/p"
    )
    branches: list[tuple[str, str]] = []
    for item_id, filename in (("A", "a.txt"), ("B", "b.txt")):
        branch = f"harness/serialized-{item_id.lower()}"
        git(repo, "branch", branch, initial.target_sha)
        git(repo, "checkout", branch)
        commit(repo, item_id, item_id + "\n", filename)
        branches.append((item_id, branch))
    git(repo, "checkout", "main")
    queue.add(
        [WorkRecord(item_id, item_id, state=DONE, branch=branch) for item_id, branch in branches],
        project_id="p",
    )

    entered = threading.Event()
    release = threading.Event()
    active = 0
    maximum_active = 0
    state_lock = threading.Lock()

    class SerialChecks:
        def run(self, tree: Path) -> CheckResult:
            del tree
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            entered.set()
            assert release.wait(timeout=5)
            with state_lock:
                active -= 1
            return CheckResult(PASS)

    errors: list[BaseException] = []
    coordinator_a = PlanCoordinator(queue, "p", repo, checks=cast(Checks, SerialChecks()))
    coordinator_b = PlanCoordinator(queue, "p", repo, checks=cast(Checks, SerialChecks()))

    def promote(item_id: str, branch: str, coordinator: PlanCoordinator) -> None:
        try:
            record = queue.get(item_id, project_id="p")
            assert record is not None
            coordinator.promote(record, item_branch=branch, base=initial.target_sha)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=promote, args=(*branches[0], coordinator_a))
    second = threading.Thread(target=promote, args=(*branches[1], coordinator_b))
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    assert not release.is_set()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert maximum_active == 1
    assert git(repo, "show", "integration/p:a.txt") == "A"
    assert git(repo, "show", "integration/p:b.txt") == "B"


def test_plan_promotion_lease_blocks_live_owner_and_allows_expiry_takeover(
    tmp_path: Path,
) -> None:
    clock = [100.0]

    def now() -> float:
        return clock[0]

    path = str(tmp_path / "queue.sqlite")
    first = WorkQueue(path, now=now)
    second = WorkQueue(path, now=now)

    acquired, until = first.acquire_plan_promotion_lease("p", "first", 10.0)
    assert acquired
    assert until == 110.0
    blocked, current_until = second.acquire_plan_promotion_lease("p", "second", 10.0)
    assert not blocked
    assert current_until == until
    assert first.renew_plan_promotion_lease("p", "first", 10.0)

    clock[0] = 111.0
    acquired, until = second.acquire_plan_promotion_lease("p", "second", 10.0)
    assert acquired
    assert until == 121.0
    assert not first.release_plan_promotion_lease("p", "first")
    assert second.release_plan_promotion_lease("p", "second")


def test_coordinator_waits_for_a_live_cross_process_promotion_owner(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    path = str(tmp_path / "queue.sqlite")
    queue = WorkQueue(path)
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    external = WorkQueue(path)
    acquired, _ = external.acquire_plan_promotion_lease("p", "other-process", 10.0)
    assert acquired

    sleep_calls: list[float] = []

    def release_on_wait(seconds: float) -> None:
        sleep_calls.append(seconds)
        assert external.release_plan_promotion_lease("p", "other-process")

    coordinator = PlanCoordinator(
        queue,
        "p",
        repo,
        checks=Checks(),
        promotion_wait_seconds=1.0,
        promotion_sleep=release_on_wait,
    )
    state = coordinator.ensure(target_branch="main", branch="integration/p")

    assert state.target_sha == git(repo, "rev-parse", "main")
    assert sleep_calls


def test_dependent_admission_waits_for_plan_initialization(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(
        Project(
            project_id="p",
            name="p",
            work_dir=str(repo),
            plan_path=str(tmp_path / "PLAN.md"),
            plan_branch="integration/p",
        )
    )
    queue.add(
        [
            WorkRecord("A", "A", state=DONE),
            WorkRecord("B", "B", depends_on=["A"]),
        ],
        project_id="p",
    )
    queue.set_control(RUNNING, project_id="p")

    assert queue.claim("worker", project_id="p") is None
    readiness = queue.readiness("B", project_id="p")
    assert readiness.ready is False
    assert len(readiness.reasons) == 1
    assert readiness.reasons[0].kind == "plan_promotion"
    assert readiness.reasons[0].evidence == "durable plan identity is absent"


def test_separate_processes_serialize_authoritative_promotion_gates(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue_path = str(tmp_path / "queue.sqlite")
    queue = WorkQueue(queue_path)
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    initial = PlanCoordinator(queue, "p", repo, checks=Checks()).ensure(
        target_branch="main", branch="integration/p"
    )
    branches: list[tuple[str, str]] = []
    for item_id, filename in (("A", "process-a.txt"), ("B", "process-b.txt")):
        branch = f"harness/process-{item_id.lower()}"
        git(repo, "branch", branch, initial.target_sha)
        git(repo, "checkout", branch)
        commit(repo, item_id, item_id + "\n", filename)
        branches.append((item_id, branch))
    git(repo, "checkout", "main")
    queue.add(
        [WorkRecord(item_id, item_id, state=DONE, branch=branch) for item_id, branch in branches],
        project_id="p",
    )

    context = multiprocessing.get_context()
    first_entered = context.Event()
    second_entered = context.Event()
    release_first = context.Event()
    second_blocked = context.Event()
    allow_second_wait = context.Event()

    first = context.Process(
        target=_promote_from_process,
        args=(
            queue_path,
            str(repo),
            branches[0][0],
            branches[0][1],
            first_entered,
            second_entered,
            release_first,
            second_blocked,
            allow_second_wait,
        ),
    )
    second = context.Process(
        target=_promote_from_process,
        args=(
            queue_path,
            str(repo),
            branches[1][0],
            branches[1][1],
            first_entered,
            second_entered,
            release_first,
            second_blocked,
            allow_second_wait,
        ),
    )
    first.start()
    try:
        assert first_entered.wait(timeout=10)
        second.start()
        assert second_blocked.wait(timeout=10)
        assert not second_entered.is_set()
        release_first.set()
        first.join(timeout=10)
        assert first.exitcode == 0
        allow_second_wait.set()
        second.join(timeout=10)
        assert second.exitcode == 0
    finally:
        release_first.set()
        allow_second_wait.set()
        first.join(timeout=10)
        second.join(timeout=10)
        if first.is_alive():
            first.terminate()
        if second.is_alive():
            second.terminate()

    assert second_entered.is_set()
    assert git(repo, "show", "integration/p:process-a.txt") == "A"
    assert git(repo, "show", "integration/p:process-b.txt") == "B"
    assert queue.latest_promotion("p", "A") is not None
    assert queue.latest_promotion("p", "B") is not None


def test_target_move_conflict_leaves_existing_plan_and_projection_unchanged(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    initial = coordinator.ensure(target_branch="main", branch="integration/p")

    item_branch = "harness/refresh-conflict-item"
    git(repo, "branch", item_branch, initial.target_sha)
    git(repo, "checkout", item_branch)
    commit(repo, "item", "item\n")
    git(repo, "checkout", "main")
    queue.add(
        [WorkRecord("A", "A", state=DONE, branch=item_branch)],
        project_id="p",
    )
    item = queue.get("A", project_id="p")
    assert item is not None
    coordinator.promote(item, item_branch=item_branch, base=initial.target_sha)
    before = coordinator.state()
    plan_file_before = git(repo, "show", "integration/p:value.txt")

    commit(repo, "target conflict", "target\n")
    with pytest.raises(PromotionConflict):
        coordinator.ensure(target_branch="main", branch="integration/p")

    after = coordinator.state()
    assert after == before
    assert git(repo, "rev-parse", "integration/p") == before.head_sha
    assert git(repo, "show", "integration/p:value.txt") == plan_file_before
    conn = queue._connect()
    try:
        row = conn.execute(
            "SELECT status, detail FROM plan_refreshes "
            "WHERE project_id = ? ORDER BY refresh_id DESC LIMIT 1",
            ("p",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["status"] == "conflict"
    assert row["detail"]


def test_restart_recovers_refresh_after_git_ref_advanced(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    initial = coordinator.ensure(target_branch="main", branch="integration/p")
    target_sha = commit(repo, "target moved", "target\n", "target.txt")

    with tempfile.TemporaryDirectory(prefix="harness-test-refresh-", dir=repo.parent) as temp:
        tree = Path(temp)
        git(repo, "worktree", "add", "--detach", str(tree), target_sha)
        try:
            new_head = git(repo, "rev-parse", "HEAD")
        finally:
            git(repo, "worktree", "remove", "--force", str(tree))
            git(repo, "worktree", "prune")
    refresh_id = queue.begin_refresh(
        "p", initial.target_sha, target_sha, initial.head_sha, new_head
    )
    git(repo, "update-ref", "refs/heads/integration/p", new_head, initial.head_sha)
    restarted = PlanCoordinator(
        WorkQueue(str(tmp_path / "queue.sqlite")), "p", repo, checks=Checks()
    )
    recovered = restarted.ensure(target_branch="main", branch="integration/p")

    assert recovered.target_sha == target_sha
    assert recovered.head_sha == new_head
    conn = queue._connect()
    try:
        row = conn.execute(
            "SELECT status FROM plan_refreshes WHERE refresh_id = ?", (refresh_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["status"] == "refreshed"


def test_restart_abandons_refresh_when_git_ref_did_not_move(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    initial = coordinator.ensure(target_branch="main", branch="integration/p")
    git(repo, "branch", "future", initial.target_sha)
    git(repo, "checkout", "future")
    target_sha = commit(repo, "future target", "future\n", "future.txt")
    git(repo, "checkout", "main")
    refresh_id = queue.begin_refresh("p", initial.target_sha, target_sha, initial.head_sha, "")

    restarted = PlanCoordinator(
        WorkQueue(str(tmp_path / "queue.sqlite")), "p", repo, checks=Checks()
    )
    recovered = restarted.ensure(target_branch="main", branch="integration/p")

    assert recovered == initial
    conn = queue._connect()
    try:
        row = conn.execute(
            "SELECT status FROM plan_refreshes WHERE refresh_id = ?", (refresh_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["status"] == "abandoned"


def test_promotion_recovers_pending_refresh_before_continuing(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    initial = coordinator.ensure(target_branch="main", branch="integration/p")
    target_sha = commit(repo, "target moved", "target\n", "target.txt")

    refresh_id = queue.begin_refresh(
        "p", initial.target_sha, target_sha, initial.head_sha, target_sha
    )
    git(repo, "update-ref", "refs/heads/integration/p", target_sha, initial.head_sha)
    item_branch = "harness/recover-before-promote"
    git(repo, "branch", item_branch, target_sha)
    git(repo, "checkout", item_branch)
    commit(repo, "item", "item\n", "item.txt")
    git(repo, "checkout", "main")
    queue.add([WorkRecord("A", "A", state=DONE, branch=item_branch)], project_id="p")
    item = queue.get("A", project_id="p")
    assert item is not None

    coordinator.promote(item, item_branch=item_branch, base=target_sha)

    conn = queue._connect()
    try:
        refresh = conn.execute(
            "SELECT status FROM plan_refreshes WHERE refresh_id = ?", (refresh_id,)
        ).fetchone()
    finally:
        conn.close()
    assert refresh is not None and refresh["status"] == "refreshed"
    assert git(repo, "show", "integration/p:item.txt") == "item"


def test_refresh_gate_failure_closes_refresh_journal(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    coordinator = PlanCoordinator(queue, "p", repo, checks=Checks())
    initial = coordinator.ensure(target_branch="main", branch="integration/p")
    item_branch = "harness/refresh-gate-failure"
    git(repo, "branch", item_branch, initial.target_sha)
    git(repo, "checkout", item_branch)
    commit(repo, "item", "item\n", "item.txt")
    git(repo, "checkout", "main")
    queue.add([WorkRecord("A", "A", state=DONE, branch=item_branch)], project_id="p")
    item = queue.get("A", project_id="p")
    assert item is not None
    coordinator.promote(item, item_branch=item_branch, base=initial.target_sha)

    commit(repo, "target moved", "target\n", "target.txt")
    coordinator.checks = Checks(commands=[["false"]])
    with pytest.raises(PromotionError, match="failed"):
        coordinator.ensure(target_branch="main", branch="integration/p")

    refreshes = queue.in_progress_refreshes("p")
    assert refreshes == []
    conn = queue._connect()
    try:
        row = conn.execute(
            "SELECT status FROM plan_refreshes "
            "WHERE project_id = ? ORDER BY refresh_id DESC LIMIT 1",
            ("p",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["status"] == "gates_failed"


def test_promotion_conflict_returns_item_to_work_without_consuming_attempt(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p", work_dir=str(repo)))
    queue.add([WorkRecord("A", "A", brief="change the repository")], project_id="p")
    queue.set_control("running", project_id="p")
    runner = FixtureRunner()
    statuses: list[tuple[str, str, str]] = []

    def plan_base_for(_: WorkRecord) -> tuple[str, str | None]:
        return "main", "fixture plan head"

    def plan_promote(record: WorkRecord, branch: str, base: str) -> tuple[str, str]:
        statuses.append((record.item_id, branch, base))
        return "conflict", "plan branch 'integration/p' is at exact-head; repair against it"

    executor = Executor(
        queue,
        fixture_client(),
        repo,
        checks=Checks(),
        role_runner=runner,
        push=False,
        project_id="p",
        plan_base_for=plan_base_for,
        plan_promote=plan_promote,
    )

    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == "pending"
    assert outcome.stop is not None
    assert outcome.stop.disposition == "withheld"
    assert outcome.stop.reason_kind == "plan_promotion_conflict"
    assert "exact-head" in outcome.reason
    record = queue.get("A", project_id="p")
    assert record is not None
    assert record.state == "pending"
    assert record.attempts == 0
    assert statuses == [("A", "harness/a", git(repo, "rev-parse", "main"))]


class FixtureEnvironmentFactory:
    name = "fixture-host"
    api_version = 1
    version = "test"

    def check(self) -> tuple[bool, str]:
        return True, "fixture environment available"

    def create(self, worktree: Path, **_: Any) -> LocalExecutionEnvironment:
        return LocalExecutionEnvironment(worktree)


class FixtureRunner:
    name = "fixture-runner"
    api_version = 1
    version = "test"

    def __init__(self) -> None:
        self.seen: dict[str, tuple[str, ...]] = {}

    def run(self, request: Any) -> RoleRunResult:
        visible = tuple(sorted(path.name for path in request.repo.iterdir()))
        self.seen[request.item_id] = visible
        if request.item_id in {"A", "B"}:
            (request.repo / f"{request.item_id.lower()}.txt").write_text(request.item_id + "\n")
        else:
            (request.repo / "dependent.txt").write_text("\n".join(visible) + "\n")
        return RoleRunResult(exit_status="completed", submission="fixture", calls=1)


def fixture_client() -> ModelClient:
    def transport(
        route: Route, messages: Sequence[Mapping[str, Any]], options: Mapping[str, Any]
    ) -> Response:
        del messages, options
        role = str(route.options.get("role") or "")
        if role == "planner":
            content = json.dumps(
                {
                    "plan": "change the fixture repository",
                    "targets": [{"path": "base.txt", "reason": "fixture context"}],
                    "cannot_identify_target": None,
                }
            )
        else:
            content = "APPROVED\nfixture review"
        return Response(200, {}, json.dumps({"choices": [{"message": {"content": content}}]}))

    return ModelClient(
        roles={
            role: Route("fixture", "https://example.invalid", options={"role": role})
            for role in ("planner", "implementer", "reviewer")
        },
        transport=transport,
        sleep=lambda _seconds: None,
    )


def test_fleet_promotes_two_independent_items_before_the_dependent_item(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    plan_file = tmp_path / "PLAN.md"
    plan_file.write_text("# fixture plan\n")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"), lease_seconds=100.0)
    queue.add_project(
        Project(
            project_id="p",
            name="p",
            work_dir=str(repo),
            base_branch="main",
            checks=["true"],
            plan_path=str(plan_file),
            plan_branch="integration/p",
            max_workers=2,
        )
    )
    queue.add(
        [
            WorkRecord("A", "A", brief="add A"),
            WorkRecord("B", "B", brief="add B"),
            WorkRecord("C", "C", brief="use A and B", depends_on=["A", "B"]),
        ],
        project_id="p",
    )
    runner = FixtureRunner()
    from agent_harness.runtime import direct_executor_factory

    fleet = Fleet(
        queue,
        direct_executor_factory(
            queue,
            reviewer=fixture_client(),
            role_runner=runner,
            push=False,
            environment_factory=FixtureEnvironmentFactory(),
            environment_image="fixture-image",
        ),
        poll_seconds=0.01,
    )
    try:
        fleet.start("p")

        deadline = time.time() + 15
        while time.time() < deadline:
            records = [queue.get(item, project_id="p") for item in ("A", "B", "C")]
            if all(record is not None and record.state == DONE for record in records):
                break
            time.sleep(0.02)
        else:
            states = {
                item: (
                    (record.state, record.last_error)
                    if (record := queue.get(item, project_id="p"))
                    else "missing"
                )
                for item in ("A", "B", "C")
            }
            raise AssertionError(
                f"fixture plan did not finish: {states}; failures={fleet.failures()}"
            )
    finally:
        fleet.stop_all()

    assert {"a.txt", "b.txt"} <= set(runner.seen["C"])
    assert git(repo, "show", "integration/p:a.txt") == "A"
    assert git(repo, "show", "integration/p:b.txt") == "B"
    dependent = git(repo, "show", "integration/p:dependent.txt")
    assert "a.txt" in dependent and "b.txt" in dependent
    promotions = [queue.latest_promotion("p", item) for item in ("A", "B", "C")]
    assert all(promotion is not None for promotion in promotions)


class FakePullRequests:
    """A pull-request client that records what a real one would have done."""

    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self.url: str | None = None

    def create_pr(self, *, title: str, body: str, head: str, base: str, draft: bool = False) -> str:
        self.created.append({"title": title, "head": head, "base": base, "body": body})
        self.url = f"https://example.invalid/pr/{len(self.created)}"
        return self.url

    def find_open_pr(self, head: str) -> str | None:
        del head
        return self.url

    def comment_pr(self, pr: str, body: str) -> None:
        self.comments.append((pr, body))


def _await(condition: Any, fleet: Any, what: str, seconds: float = 20.0) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"{what} did not happen; failures={fleet.failures()}")


def test_fleet_publishes_one_plan_pr_only_when_the_plan_is_finished(tmp_path: Path) -> None:
    """The whole point of P7/P8, exercised through the executor factory.

    Nothing is published while an item is still in flight; the plan branch —
    never an item branch — reaches the remote once; and a correction added
    afterwards updates that same pull request instead of opening a second.
    """
    repo = make_repo(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "main")
    plan_file = tmp_path / "PLAN.md"
    plan_file.write_text("# fixture plan\n")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"), lease_seconds=100.0)
    queue.add_project(
        Project(
            project_id="p",
            name="fixture",
            repo="acme/widgets",
            work_dir=str(repo),
            base_branch="main",
            checks=["true"],
            plan_path=str(plan_file),
            plan_branch="integration/p",
            max_workers=1,
        )
    )
    queue.add(
        [WorkRecord("A", "A", brief="add A"), WorkRecord("B", "B", brief="add B")],
        project_id="p",
    )
    github = FakePullRequests()
    from agent_harness.runtime import direct_executor_factory

    fleet = Fleet(
        queue,
        direct_executor_factory(
            queue,
            reviewer=fixture_client(),
            role_runner=FixtureRunner(),
            push=True,
            github_for=lambda _repo: github,
            environment_factory=FixtureEnvironmentFactory(),
            environment_image="fixture-image",
        ),
        poll_seconds=0.01,
    )
    try:
        fleet.start("p")
        _await(
            lambda: all(
                (record := queue.get(item, project_id="p")) is not None and record.state == DONE
                for item in ("A", "B")
            ),
            fleet,
            "the fixture plan finished",
        )
        _await(lambda: bool(github.created), fleet, "the plan was published")

        assert len(github.created) == 1
        assert github.created[0]["head"] == "integration/p"
        assert github.created[0]["base"] == "main"
        # P8: the one pull request carries the item evidence.
        assert "`A`" in github.created[0]["body"] and "`B`" in github.created[0]["body"]
        published = git(remote, "rev-parse", "integration/p")
        assert published == git(repo, "rev-parse", "integration/p")
        # No item branch was ever pushed.
        assert "harness/" not in git(remote, "branch", "--list")

        queue.add([WorkRecord("R", "R", brief="correction")], project_id="p")
        _await(
            lambda: (record := queue.get("R", project_id="p")) is not None and record.state == DONE,
            fleet,
            "the correction finished",
        )
        _await(lambda: bool(github.comments), fleet, "the existing pull request was updated")
    finally:
        fleet.stop_all()

    assert len(github.created) == 1  # still exactly one, after the correction
    assert git(remote, "rev-parse", "integration/p") == git(repo, "rev-parse", "integration/p")
    assert git(remote, "rev-parse", "integration/p") != published
