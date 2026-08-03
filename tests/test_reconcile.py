"""Ground truth from GitHub: merged, closed unmerged, reverted.

Everything the harness records about quality is a proxy. Approval rate
measures whether a reviewer agreed; revert rate measures whether it should
have. They come apart exactly when it matters.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_harness.audit import AuditStore
from agent_harness.reconcile import (
    PR_CLOSED_UNMERGED,
    PR_MERGED,
    PR_REVERTED,
    GitHubReconciler,
    items_by_pr,
)


class FakeGh:
    """Answers `gh pr list` and `git log` from canned data."""

    def __init__(self, prs: list[dict[str, Any]], log_records: list[str] | None = None) -> None:
        self.prs = prs
        self.log_records = log_records or []
        self.calls: list[list[str]] = []

    def __call__(self, args, stdin=None):  # type: ignore[no-untyped-def]
        self.calls.append(list(args))
        if args[0] == "gh":
            return json.dumps(self.prs)
        if args[0] == "git":
            return "\x1e".join(self.log_records)
        raise AssertionError(f"unexpected command {args}")


@pytest.fixture
def audit(tmp_path: Path) -> AuditStore:
    return AuditStore(tmp_path / "audit.sqlite")


def outcomes(audit: AuditStore) -> list[str]:
    return [r["outcome"] for r in audit.recent(limit=50)]


def test_a_merged_pr_is_recorded_against_its_item(audit: AuditStore) -> None:
    gh = FakeGh(
        [
            {
                "number": 7,
                "state": "merged",
                "mergedAt": "2026-08-01T10:00:00Z",
                "mergeCommit": {"oid": "abc123"},
                "title": "feat: thing",
                "url": "u",
            }
        ]
    )
    report = GitHubReconciler("o/r", audit, runner=gh).reconcile(
        {7: {"project_id": "p", "item_id": "T1"}}
    )

    assert report.merged == 1
    row = audit.recent(limit=1)[0]
    assert row["outcome"] == PR_MERGED
    assert row["project_id"] == "p"
    assert row["item_id"] == "T1"


def test_a_closed_unmerged_pr_is_not_silence(audit: AuditStore) -> None:
    """A rejected PR is the clearest statement that the work was not wanted,
    and from inside the harness it looks identical to one still waiting."""
    gh = FakeGh(
        [
            {
                "number": 8,
                "state": "closed",
                "mergedAt": None,
                "mergeCommit": None,
                "title": "feat: no thanks",
                "url": "u",
            }
        ]
    )
    report = GitHubReconciler("o/r", audit, runner=gh).reconcile(
        {8: {"project_id": "p", "item_id": "T2"}}
    )

    assert report.closed_unmerged == 1
    assert outcomes(audit) == [PR_CLOSED_UNMERGED]


def test_an_open_pr_records_nothing_yet(audit: AuditStore) -> None:
    gh = FakeGh(
        [
            {
                "number": 9,
                "state": "open",
                "mergedAt": None,
                "mergeCommit": None,
                "title": "wip",
                "url": "u",
            }
        ]
    )
    report = GitHubReconciler("o/r", audit, runner=gh).reconcile(
        {9: {"project_id": "p", "item_id": "T3"}}
    )

    assert (report.merged, report.closed_unmerged, report.reverted) == (0, 0, 0)
    assert audit.count() == 0


def test_a_revert_is_recorded_as_a_second_fact_not_an_edit(audit: AuditStore) -> None:
    """Merged then reverted is two things that were each true when recorded.

    Rewriting the merge would make history depend on when you looked.
    """
    gh = FakeGh(
        [
            {
                "number": 7,
                "state": "merged",
                "mergedAt": "2026-08-01T10:00:00Z",
                "mergeCommit": {"oid": "abc123"},
                "title": "feat: thing",
                "url": "u",
            }
        ],
        log_records=['def456\x001700000000\x00Revert "feat: thing"\x00This reverts commit abc123.'],
    )
    report = GitHubReconciler("o/r", audit, runner=gh).reconcile(
        {7: {"project_id": "p", "item_id": "T1"}}
    )

    assert report.merged == 1
    assert report.reverted == 1
    assert set(outcomes(audit)) == {PR_MERGED, PR_REVERTED}


def test_a_revert_by_title_is_caught_too(audit: AuditStore) -> None:
    """The web UI's Revert button writes `Revert "<title>"` without always
    naming the commit, so matching only on sha misses real reverts."""
    gh = FakeGh(
        [
            {
                "number": 7,
                "state": "merged",
                "mergedAt": "2026-08-01T10:00:00Z",
                "mergeCommit": {"oid": "zzz"},
                "title": "feat: thing",
                "url": "u",
            }
        ],
        log_records=['def456\x001700000000\x00Revert "feat: thing"\x00'],
    )
    report = GitHubReconciler("o/r", audit, runner=gh).reconcile(
        {7: {"project_id": "p", "item_id": "T1"}}
    )
    assert report.reverted == 1


def test_an_ordinary_commit_is_not_a_revert(audit: AuditStore) -> None:
    gh = FakeGh(
        [
            {
                "number": 7,
                "state": "merged",
                "mergedAt": "2026-08-01T10:00:00Z",
                "mergeCommit": {"oid": "abc123"},
                "title": "feat: thing",
                "url": "u",
            }
        ],
        log_records=["def456\x001700000000\x00feat: something else\x00body"],
    )
    assert (
        GitHubReconciler("o/r", audit, runner=gh)
        .reconcile({7: {"project_id": "p", "item_id": "T1"}})
        .reverted
        == 0
    )


def test_an_unattributed_pr_is_skipped_not_counted(audit: AuditStore) -> None:
    """An outcome belonging to no item inflates every rate it appears in.

    Repos contain pull requests the harness never made -- dependabot, humans.
    """
    gh = FakeGh(
        [
            {
                "number": 99,
                "state": "merged",
                "mergedAt": "2026-08-01T10:00:00Z",
                "mergeCommit": {"oid": "x"},
                "title": "chore(deps): bump",
                "url": "u",
            }
        ]
    )
    report = GitHubReconciler("o/r", audit, runner=gh).reconcile({})

    assert report.skipped == 1
    assert report.merged == 0
    assert audit.count() == 0


def test_reconciling_twice_records_each_fact_once(audit: AuditStore) -> None:
    """It runs on a timer against a repository that mostly does not change."""
    gh = FakeGh(
        [
            {
                "number": 7,
                "state": "merged",
                "mergedAt": "2026-08-01T10:00:00Z",
                "mergeCommit": {"oid": "abc123"},
                "title": "feat: thing",
                "url": "u",
            }
        ],
        log_records=['def456\x001700000000\x00Revert "feat: thing"\x00This reverts commit abc123.'],
    )
    reconciler = GitHubReconciler("o/r", audit, runner=gh)
    reconciler.reconcile({7: {"project_id": "p", "item_id": "T1"}})
    reconciler.reconcile({7: {"project_id": "p", "item_id": "T1"}})

    assert audit.count() == 2, "reconciliation duplicated history on a second pass"


def test_github_being_unreachable_does_not_raise(audit: AuditStore) -> None:
    """Reconciliation is observation. It must not be able to stop the fleet."""

    def broken(args, stdin=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("gh: not logged in")

    report = GitHubReconciler("o/r", audit, runner=broken).reconcile(
        {7: {"project_id": "p", "item_id": "T1"}}
    )

    assert report.errors
    assert report.merged == 0


def test_missing_git_checkout_still_records_merge_state(audit: AuditStore) -> None:
    """A remote-only deployment has no working tree. Losing revert detection
    is not a reason to lose merge state as well."""

    def gh_only(args, stdin=None):  # type: ignore[no-untyped-def]
        if args[0] == "git":
            raise RuntimeError("not a git repository")
        return json.dumps(
            [
                {
                    "number": 7,
                    "state": "merged",
                    "mergedAt": "2026-08-01T10:00:00Z",
                    "mergeCommit": {"oid": "abc"},
                    "title": "t",
                    "url": "u",
                }
            ]
        )

    report = GitHubReconciler("o/r", audit, runner=gh_only).reconcile(
        {7: {"project_id": "p", "item_id": "T1"}}
    )

    assert report.merged == 1
    assert report.reverted == 0


def test_items_are_mapped_to_prs_from_the_queue(tmp_path: Path) -> None:
    from agent_harness.work import RUNNING, WorkQueue, WorkRecord

    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.set_control(RUNNING)
    queue.add([WorkRecord(item_id="T1", title="t", brief="b")])
    queue.claim("w")
    queue.release("T1", "done", pr_url="https://github.com/o/r/pull/42", owner="w")

    assert items_by_pr(queue) == {42: {"project_id": "default", "item_id": "T1"}}


def test_a_revert_borrows_the_commit_time_not_the_clock(audit: AuditStore) -> None:
    """Found by a flaky test, which is the honest way to describe it.

    Stamping a revert with `now` changes the event's identity on every pass,
    so each reconciliation records the same revert again -- wrong, and
    unbounded. It only looked intermittent because two calls to time.time()
    in one fast run can return the identical float.
    """
    gh = FakeGh(
        [
            {
                "number": 7,
                "state": "merged",
                "mergedAt": "2026-08-01T10:00:00Z",
                "mergeCommit": {"oid": "abc123"},
                "title": "feat: thing",
                "url": "u",
            }
        ],
        log_records=['def456\x001700000000\x00Revert "feat: thing"\x00This reverts commit abc123.'],
    )
    GitHubReconciler("o/r", audit, runner=gh).reconcile({7: {"project_id": "p", "item_id": "T1"}})

    revert = [r for r in audit.recent(limit=10) if r["outcome"] == PR_REVERTED][0]
    assert revert["ts"] == 1700000000.0, "the revert was stamped with wall-clock time"


def test_reconciling_many_times_never_grows_history(audit: AuditStore) -> None:
    """The property the flaky test was reaching for, stated so it cannot pass
    by luck: ten passes, still two facts."""
    gh = FakeGh(
        [
            {
                "number": 7,
                "state": "merged",
                "mergedAt": "2026-08-01T10:00:00Z",
                "mergeCommit": {"oid": "abc123"},
                "title": "feat: thing",
                "url": "u",
            }
        ],
        log_records=['def456\x001700000000\x00Revert "feat: thing"\x00This reverts commit abc123.'],
    )
    reconciler = GitHubReconciler("o/r", audit, runner=gh)
    for _ in range(10):
        reconciler.reconcile({7: {"project_id": "p", "item_id": "T1"}})

    assert audit.count() == 2


def test_a_closed_pr_borrows_its_closing_time(audit: AuditStore) -> None:
    """Same defect, same fix: a wall-clock stamp is a new event every pass."""
    gh = FakeGh(
        [
            {
                "number": 8,
                "state": "closed",
                "mergedAt": None,
                "closedAt": "2026-08-01T11:00:00Z",
                "mergeCommit": None,
                "title": "no",
                "url": "u",
            }
        ]
    )
    reconciler = GitHubReconciler("o/r", audit, runner=gh)
    for _ in range(5):
        reconciler.reconcile({8: {"project_id": "p", "item_id": "T2"}})

    assert audit.count() == 1
