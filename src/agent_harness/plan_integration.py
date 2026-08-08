"""Local plan-branch integration for a project.

The coordinator owns no remote and knows nothing about a repository's file
layout. It receives a project checkout, an explicit integration ref and the
same configured checks used by the item executor. Item branches remain intact;
promotion replays their delta onto one serialized local branch and gates that
result again.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .executor import Checks, GitError
from .graph import LOCAL_WORK
from .work import WorkQueue, WorkRecord


class PromotionError(RuntimeError):
    """A promotion could not become the new plan head."""


class PromotionConflict(PromotionError):
    """The item delta does not apply cleanly to the current plan head."""


DEFAULT_PROMOTION_LEASE_SECONDS = 60.0
DEFAULT_PROMOTION_WAIT_SECONDS = 300.0


@dataclass(frozen=True)
class PlanState:
    project_id: str
    branch: str
    target_branch: str
    target_sha: str
    head_sha: str
    plan_digest: str


@dataclass(frozen=True)
class Promotion:
    item_id: str
    status: str
    old_head_sha: str
    new_head_sha: str | None
    detail: str = ""


_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _promotion_lock(project_id: str, repo: Path) -> threading.Lock:
    key = (project_id, str(repo))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


class _PromotionLeaseHeartbeat:
    """Keep a durable promotion lease alive while gates are running."""

    def __init__(self, queue: WorkQueue, project_id: str, owner: str, seconds: float) -> None:
        self.queue = queue
        self.project_id = project_id
        self.owner = owner
        self.seconds = seconds
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="harness-plan-lease-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.seconds))

    def _run(self) -> None:
        interval = max(0.01, self.seconds / 3.0)
        while not self._stop.wait(interval):
            if not self.queue.renew_plan_promotion_lease(self.project_id, self.owner, self.seconds):
                self.lost.set()
                return


class PlanCoordinator:
    """Durably create and advance one local integration branch."""

    def __init__(
        self,
        queue: WorkQueue,
        project_id: str,
        repo: Path,
        *,
        checks: Checks,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        promotion_lease_seconds: float = DEFAULT_PROMOTION_LEASE_SECONDS,
        promotion_wait_seconds: float = DEFAULT_PROMOTION_WAIT_SECONDS,
        promotion_sleep: Callable[[float], None] = time.sleep,
        promotion_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if promotion_lease_seconds <= 0:
            raise ValueError("promotion lease must be positive")
        if promotion_wait_seconds < 0:
            raise ValueError("promotion wait must not be negative")
        self.queue = queue
        self.project_id = project_id
        self.repo = Path(repo).resolve()
        self.checks = checks
        self.on_event = on_event
        self.promotion_lease_seconds = promotion_lease_seconds
        self.promotion_wait_seconds = promotion_wait_seconds
        self._promotion_sleep = promotion_sleep
        self._promotion_clock = promotion_clock
        self._lease_owner = uuid.uuid4().hex
        self._lease_heartbeat: _PromotionLeaseHeartbeat | None = None
        # Executors are built per worker, so an instance lock would leave
        # concurrent workers free to advance the same ref simultaneously.
        self._lock = _promotion_lock(project_id, self.repo)

    def ensure(self, *, target_branch: str, branch: str, plan_path: str | None = None) -> PlanState:
        """Create or restore the plan while serialising first-use races."""
        with self._promotion_guard():
            return self._ensure(
                target_branch=target_branch,
                branch=branch,
                plan_path=plan_path,
            )

    @contextmanager
    def _promotion_guard(self) -> Any:
        """Hold both local and durable serialization while integrating."""
        with self._lock:
            deadline = self._promotion_clock() + self.promotion_wait_seconds
            while True:
                acquired, lease_until = self.queue.acquire_plan_promotion_lease(
                    self.project_id,
                    self._lease_owner,
                    self.promotion_lease_seconds,
                )
                if acquired:
                    break
                remaining = deadline - self._promotion_clock()
                if remaining <= 0:
                    raise PromotionError(
                        f"plan promotion for {self.project_id!r} remained leased until "
                        f"{lease_until} after waiting {self.promotion_wait_seconds} seconds"
                    )
                self._promotion_sleep(
                    min(remaining, max(0.01, min(1.0, self.promotion_lease_seconds / 10)))
                )
            heartbeat = _PromotionLeaseHeartbeat(
                self.queue,
                self.project_id,
                self._lease_owner,
                self.promotion_lease_seconds,
            )
            self._lease_heartbeat = heartbeat
            heartbeat.start()
            try:
                yield
            finally:
                heartbeat.stop()
                self.queue.release_plan_promotion_lease(self.project_id, self._lease_owner)
                self._lease_heartbeat = None

    def _assert_lease(self) -> None:
        heartbeat = self._lease_heartbeat
        if heartbeat is None or heartbeat.lost.is_set():
            raise PromotionError("plan promotion lease was lost before publication")
        if not self.queue.renew_plan_promotion_lease(
            self.project_id, self._lease_owner, self.promotion_lease_seconds
        ):
            heartbeat.lost.set()
            raise PromotionError("plan promotion lease was lost before publication")

    def _ensure(
        self, *, target_branch: str, branch: str, plan_path: str | None = None
    ) -> PlanState:
        if not branch.strip():
            raise PromotionError("plan integration requires an explicit local plan branch")
        target_sha = self._git("rev-parse", target_branch).strip()
        digest = ""
        if plan_path:
            digest = hashlib.sha256(Path(plan_path).read_bytes()).hexdigest()
        current = self.queue.plan(self.project_id)
        if current is not None:
            state = PlanState(**dict(current))
            if (state.branch, state.target_branch) != (branch, target_branch):
                raise PromotionError(
                    "the durable plan identity changed; create a new project/plan before continuing"
                )
            self._recover_in_progress(state)
            self._recover_refreshes(self.state())
            state = self.state()
            if state.target_sha != target_sha:
                return self._refresh(state, target_sha)
            actual = self._git("rev-parse", state.branch).strip()
            if actual != state.head_sha:
                raise PromotionError(
                    f"plan branch {state.branch!r} moved outside the coordinator "
                    f"({state.head_sha} -> {actual})"
                )
            return state
        existing_ref = self._git(
            "rev-parse", "--verify", f"refs/heads/{branch}", check=False
        ).strip()
        if existing_ref and existing_ref != target_sha:
            raise PromotionError(
                f"plan branch {branch!r} already exists at {existing_ref}, not target {target_sha}"
            )
        if not existing_ref:
            self._git("branch", branch, target_sha)
        try:
            self.queue.create_plan(
                self.project_id,
                branch=branch,
                target_branch=target_branch,
                target_sha=target_sha,
                head_sha=target_sha,
                plan_digest=digest,
            )
        except Exception:
            current = self.queue.plan(self.project_id)
            if current is None:
                raise
            return PlanState(**dict(current))
        return PlanState(self.project_id, branch, target_branch, target_sha, target_sha, digest)

    def _recover_in_progress(self, state: PlanState) -> None:
        """Finish or abandon a promotion interrupted around ``update-ref``."""
        pending = self.queue.in_progress_promotions(self.project_id)
        if not pending:
            return
        actual = self._git("rev-parse", state.branch).strip()
        for row in pending:
            old_head = str(row["old_head_sha"])
            new_head = str(row["new_head_sha"])
            if actual == new_head and state.head_sha == old_head:
                self.queue.complete_promotion(
                    self.project_id,
                    str(row["item_id"]),
                    str(row["item_branch"]),
                    str(row["base_sha"]),
                    old_head,
                    new_head,
                    int(row["promotion_id"]),
                    "recovered after restart",
                )
                self._emit_promotion_row(
                    row,
                    status="promoted",
                    target_sha=state.target_sha,
                    detail="recovered after restart",
                )
                state = self.state()
                continue
            if actual == old_head and state.head_sha == old_head:
                detail = "Git ref was unchanged when the interrupted promotion was recovered"
                self.queue.finish_promotion(
                    int(row["promotion_id"]),
                    "abandoned",
                    detail,
                )
                self._emit_promotion_row(
                    row,
                    status="abandoned",
                    target_sha=state.target_sha,
                    detail=detail,
                )
                continue
            raise PromotionError(
                f"in-progress promotion {row['promotion_id']} has unexpected plan ref "
                f"{actual}; expected {old_head} or {new_head}"
            )

    def _recover_refreshes(self, state: PlanState) -> None:
        """Finish or abandon a refresh interrupted around ``update-ref``."""
        pending = self.queue.in_progress_refreshes(self.project_id)
        if not pending:
            return
        actual = self._git("rev-parse", state.branch).strip()
        for row in pending:
            old_target = str(row["old_target_sha"])
            new_target = str(row["new_target_sha"])
            old_head = str(row["old_head_sha"])
            new_head = str(row["new_head_sha"])
            if actual == new_head and state.target_sha == old_target and state.head_sha == old_head:
                self.queue.complete_refresh(
                    int(row["refresh_id"]),
                    self.project_id,
                    old_target,
                    new_target,
                    old_head,
                    new_head,
                    "recovered after restart",
                )
                state = self.state()
                continue
            if actual == old_head and state.target_sha == old_target and state.head_sha == old_head:
                self.queue.finish_refresh(
                    int(row["refresh_id"]),
                    "abandoned",
                    "Git ref was unchanged when the interrupted refresh was recovered",
                )
                continue
            raise PromotionError(
                f"in-progress refresh {row['refresh_id']} has unexpected plan ref "
                f"{actual}; expected {old_head} or {new_head}"
            )

    def _refresh(self, state: PlanState, target_sha: str) -> PlanState:
        """Rebuild the plan from a moved target and replay promoted items.

        The existing plan ref and projection are not touched until every
        recorded promotion has applied and passed the integration checks.
        """
        self._assert_lease()
        refresh_id = self.queue.begin_refresh(
            self.project_id,
            state.target_sha,
            target_sha,
            state.head_sha,
            "",
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix="harness-plan-refresh-", dir=self.repo.parent
            ) as temp:
                tree = Path(temp)
                self._git("worktree", "add", "--detach", str(tree), target_sha)
                try:
                    replayed = 0
                    for row in self.queue.successful_promotions(self.project_id):
                        item_sha = row["item_sha"]
                        if not item_sha:
                            item_sha = self._git(
                                "rev-parse", str(row["item_branch"]), check=False
                            ).strip()
                        if not item_sha:
                            raise PromotionError(
                                f"cannot refresh plan: item {row['item_id']!r} "
                                "has no durable commit"
                            )
                        try:
                            self._merge_item(tree, str(item_sha))
                        except PromotionConflict as exc:
                            raise PromotionConflict(
                                str(exc)
                                or f"promotion {row['item_id']!r} conflicts with moved target"
                            ) from exc
                        checked = self.checks.run(tree)
                        if not checked.ok:
                            raise PromotionError(
                                checked.detail
                                or f"integration gate failed while replaying {row['item_id']!r}"
                            )
                        message = self._git("show", "-s", "--format=%B", str(item_sha)).strip()
                        if not message:
                            message = f"Replay {row['item_id']}"
                        self._git_in(tree, "add", "-A")
                        self._git_in(tree, "commit", "-m", message)
                        replayed += 1
                    new_head = self._git_in(tree, "rev-parse", "HEAD").strip()
                finally:
                    self._git("worktree", "remove", "--force", str(tree), check=False)
                    self._git("worktree", "prune", check=False)
        except PromotionConflict as exc:
            self.queue.finish_refresh(refresh_id, "conflict", str(exc))
            raise
        except PromotionError as exc:
            self.queue.finish_refresh(refresh_id, "gates_failed", str(exc))
            raise
        except Exception as exc:
            self.queue.finish_refresh(refresh_id, "failed", str(exc))
            raise

        # The target is external state.  It may have advanced while replaying
        # and gating the promoted items, so do not publish a rebuild that was
        # based on an already stale target.  The durable plan still points at
        # the old target here; close this journal as superseded and rebuild
        # from the newer target instead.
        try:
            latest_target = self._git("rev-parse", state.target_branch).strip()
        except Exception as exc:
            self.queue.finish_refresh(refresh_id, "failed", str(exc))
            raise
        if latest_target != target_sha:
            self.queue.finish_refresh(
                refresh_id,
                "superseded",
                f"target advanced during replay ({target_sha} -> {latest_target})",
            )
            return self._refresh(self.state(), latest_target)

        self._assert_lease()
        self.queue.set_refresh_head(refresh_id, new_head)
        self._assert_lease()
        self._git(
            "update-ref",
            f"refs/heads/{state.branch}",
            new_head,
            state.head_sha,
        )
        self._assert_lease()
        self.queue.complete_refresh(
            refresh_id,
            self.project_id,
            state.target_sha,
            target_sha,
            state.head_sha,
            new_head,
            f"replayed {replayed} promoted item(s) after target moved",
        )
        return self.state()

    def state(self) -> PlanState:
        row = self.queue.plan(self.project_id)
        if row is None:
            raise PromotionError(f"project {self.project_id!r} has no initialized plan")
        return PlanState(**dict(row))

    def base_for(self, record: WorkRecord) -> tuple[str, str | None]:
        state = self.state()
        return state.branch, "local plan branch"

    def promote(self, record: WorkRecord, *, item_branch: str, base: str) -> Promotion:
        with self._promotion_guard():
            while True:
                self._assert_lease()
                state = self.state()
                self._recover_in_progress(state)
                self._recover_refreshes(self.state())
                state = self.state()
                live_target = self._git("rev-parse", state.target_branch).strip()
                if live_target != state.target_sha:
                    state = self._refresh(state, live_target)
                previous = self.queue.latest_promotion(self.project_id, record.item_id)
                if previous is not None:
                    return Promotion(
                        record.item_id,
                        "promoted",
                        str(previous["old_head_sha"]),
                        str(previous["new_head_sha"]),
                        "promotion already durable",
                    )
                for dependency in record.dependency_specs():
                    if not dependency.required or dependency.target_kind != LOCAL_WORK:
                        continue
                    if self.queue.latest_promotion(self.project_id, dependency.target_id) is None:
                        raise PromotionError(
                            f"cannot promote {record.item_id}: prerequisite "
                            f"{dependency.target_id} is not promoted"
                        )
                old_head = state.head_sha
                base_sha = self._git("rev-parse", base).strip()
                item_sha = self._git("rev-parse", item_branch).strip()
                retry_target: str | None = None
                promotion_id: int | None = None
                new_head = ""
                with tempfile.TemporaryDirectory(
                    prefix="harness-plan-", dir=self.repo.parent
                ) as temp:
                    tree = Path(temp)
                    self._git("worktree", "add", "--detach", str(tree), state.branch)
                    try:
                        try:
                            self._merge_item(tree, item_sha)
                        except PromotionConflict as exc:
                            detail = str(exc)
                            promotion_id = self.queue.record_promotion(
                                self.project_id,
                                record.item_id,
                                item_branch,
                                base_sha,
                                old_head,
                                None,
                                "conflict",
                                detail,
                                item_sha,
                            )
                            self._emit_promotion(
                                record,
                                status="conflict",
                                promotion_id=promotion_id,
                                base_sha=base_sha,
                                item_sha=item_sha,
                                old_head_sha=old_head,
                                detail=detail,
                            )
                            raise PromotionConflict(
                                f"plan branch {state.branch!r} is at {old_head}; "
                                "repair the item against that head: "
                                + (detail or "item delta conflicts with plan head")
                            ) from exc
                        checked = self.checks.run(tree)
                        if not checked.ok:
                            detail = checked.detail or "authoritative integration gate failed"
                            promotion_id = self.queue.record_promotion(
                                self.project_id,
                                record.item_id,
                                item_branch,
                                base_sha,
                                old_head,
                                None,
                                "gates_failed",
                                detail,
                                item_sha,
                            )
                            self._emit_promotion(
                                record,
                                status="gates_failed",
                                promotion_id=promotion_id,
                                base_sha=base_sha,
                                item_sha=item_sha,
                                old_head_sha=old_head,
                                detail=detail,
                            )
                            raise PromotionError(detail)

                        self._assert_lease()
                        latest_target = self._git("rev-parse", state.target_branch).strip()
                        if latest_target != state.target_sha:
                            retry_target = latest_target
                        else:
                            message = self._git("log", "-1", "--format=%B", item_branch).strip()
                            if not message:
                                message = f"Promote {record.item_id}"
                            self._git_in(tree, "add", "-A")
                            self._git_in(tree, "commit", "-m", message)
                            new_head = self._git_in(tree, "rev-parse", "HEAD").strip()
                            self._assert_lease()
                            promotion_id = self.queue.begin_promotion(
                                self.project_id,
                                record.item_id,
                                item_branch,
                                base_sha,
                                old_head,
                                new_head,
                                item_sha,
                            )
                            latest_target = self._git("rev-parse", state.target_branch).strip()
                            if latest_target != state.target_sha:
                                self.queue.finish_promotion(
                                    promotion_id,
                                    "superseded",
                                    f"target advanced during promotion ({state.target_sha} -> "
                                    f"{latest_target})",
                                )
                                self._emit_promotion(
                                    record,
                                    status="superseded",
                                    promotion_id=promotion_id,
                                    base_sha=base_sha,
                                    item_sha=item_sha,
                                    old_head_sha=old_head,
                                    new_head_sha=new_head,
                                    target_sha=latest_target,
                                    detail=(
                                        f"target advanced during promotion ({state.target_sha} -> "
                                        f"{latest_target})"
                                    ),
                                )
                                retry_target = latest_target
                                promotion_id = None
                            else:
                                self._assert_lease()
                                self._git(
                                    "update-ref",
                                    f"refs/heads/{state.branch}",
                                    new_head,
                                    old_head,
                                )
                    finally:
                        self._git("worktree", "remove", "--force", str(tree), check=False)
                        self._git("worktree", "prune", check=False)
                if retry_target is not None:
                    self._refresh(self.state(), retry_target)
                    continue
                if promotion_id is None:
                    raise PromotionError("promotion did not produce a durable journal entry")
                self._assert_lease()
                self.queue.complete_promotion(
                    self.project_id,
                    record.item_id,
                    item_branch,
                    base_sha,
                    old_head,
                    new_head,
                    promotion_id,
                    "authoritative integration gates passed",
                )
                self._emit_promotion(
                    record,
                    status="promoted",
                    promotion_id=promotion_id,
                    base_sha=base_sha,
                    item_sha=item_sha,
                    old_head_sha=old_head,
                    new_head_sha=new_head,
                    target_sha=state.target_sha,
                    detail="authoritative integration gates passed",
                )
                latest_target = self._git("rev-parse", state.target_branch).strip()
                if latest_target != state.target_sha:
                    refreshed = self._refresh(self.state(), latest_target)
                    return Promotion(
                        record.item_id,
                        "promoted",
                        old_head,
                        refreshed.head_sha,
                        "authoritative integration gates passed after target refresh",
                    )
                return Promotion(record.item_id, "promoted", old_head, new_head)

    def _emit_promotion(
        self,
        record: WorkRecord,
        *,
        promotion_id: int | None = None,
        status: str,
        base_sha: str,
        item_sha: str,
        old_head_sha: str,
        new_head_sha: str | None = None,
        target_sha: str | None = None,
        detail: str = "",
    ) -> None:
        """Publish a non-load-bearing, item-scoped promotion fact."""
        if self.on_event is None:
            return
        try:
            event = {
                "ts": time.time(),
                "kind": "work",
                "project_id": self.project_id,
                "item_id": record.item_id,
                "outcome": "plan_promotion",
                "promotion_id": promotion_id,
                "status": status,
                "plan_branch": self.state().branch,
                "base_sha": base_sha,
                "item_sha": item_sha,
                "old_head_sha": old_head_sha,
                "new_head_sha": new_head_sha,
                "target_sha": target_sha,
                "detail": detail,
            }
            self.on_event(event)
        except Exception:
            # Telemetry must never turn a durable promotion into a worker
            # failure. The queue and Git projections are authoritative.
            return

    def _emit_promotion_row(
        self,
        row: Any,
        *,
        status: str,
        detail: str,
        target_sha: str | None = None,
    ) -> None:
        """Emit a durable promotion row, including restart recovery facts."""
        if self.on_event is None:
            return
        item_id = str(row["item_id"])
        item_sha = str(row["item_sha"] or "")
        if not item_sha:
            item_sha = self._git("rev-parse", str(row["item_branch"]), check=False).strip()
        self._emit_promotion(
            WorkRecord(item_id, item_id),
            promotion_id=int(row["promotion_id"]),
            status=status,
            base_sha=str(row["base_sha"]),
            item_sha=item_sha,
            old_head_sha=str(row["old_head_sha"]),
            new_head_sha=(str(row["new_head_sha"]) if row["new_head_sha"] is not None else None),
            target_sha=target_sha,
            detail=detail,
        )

    def _git(self, *args: str, check: bool = True) -> str:
        return self._git_at(self.repo, *args, check=check)

    @staticmethod
    def _git_at(repo: Path, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout

    def _git_in(self, repo: Path, *args: str, check: bool = True) -> str:
        return self._git_at(repo, *args, check=check)

    def _merge_item(self, tree: Path, item_sha: str) -> None:
        """Stage an item commit as the second parent of the plan merge."""
        result = subprocess.run(
            ["git", "-C", str(tree), "merge", "--no-ff", "--no-commit", item_sha],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return
        self._git_in(tree, "merge", "--abort", check=False)
        detail = (result.stderr or result.stdout).strip()
        raise PromotionConflict(detail or f"item commit {item_sha} conflicts with plan head")
