"""Recovering a pull request whose URL the queue dropped.

Reconciliation was driven entirely by the URLs the queue had recorded, so the
one kind of pull request most in need of being found — the one whose URL was
lost — was the one kind it could never see. The head branch is the link that
was always there: the queue records the branch it pushed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.audit import AuditStore
from agent_harness.maintenance import adopt_lost_pull_requests
from agent_harness.reconcile import PullRequest
from agent_harness.work import Project, WorkQueue, WorkRecord


class FakeReconciler:
    def __init__(self, pulls: list[PullRequest]) -> None:
        self.pulls = pulls
        self.calls = 0

    def pull_requests(self, limit: int = 200) -> list[PullRequest]:
        self.calls += 1
        return self.pulls


def pull(number: int, head: str) -> PullRequest:
    return PullRequest(
        number=number,
        state="open",
        merged=False,
        merge_commit=None,
        title=f"pr {number}",
        url=f"https://github.com/o/r/pull/{number}",
        head=head,
    )


def queue_with(tmp_path: Path, item_id: str, branch: str, pr_url: str | None = None) -> WorkQueue:
    """A queue holding one item that has already pushed a branch.

    `add` deliberately does not carry a branch — a branch is something an
    attempt produces, not something a plan declares — so it is recorded the
    way a real attempt records it.
    """
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.add_project(Project(project_id="default", name="D", repo="o/r"))
    queue.add([WorkRecord(item_id=item_id, title=f"do {item_id}", brief="b")])
    queue.release(item_id, "failed", branch=branch, pr_url=pr_url)
    return queue


def test_an_item_with_a_branch_and_no_url_adopts_its_pull_request(tmp_path: Path) -> None:
    queue = queue_with(tmp_path, "T27", "harness/t27")
    reconciler = FakeReconciler([pull(110, "harness/t27")])

    assert adopt_lost_pull_requests(queue, reconciler, "default") == 1

    record = queue.get("T27")
    assert record is not None
    assert record.pr_url == "https://github.com/o/r/pull/110"


def test_a_url_that_was_recorded_correctly_is_never_overwritten(tmp_path: Path) -> None:
    queue = queue_with(tmp_path, "T27", "harness/t27", "https://github.com/o/r/pull/9")
    reconciler = FakeReconciler([pull(110, "harness/t27")])

    assert adopt_lost_pull_requests(queue, reconciler, "default") == 0

    record = queue.get("T27")
    assert record is not None
    assert record.pr_url == "https://github.com/o/r/pull/9"


def test_a_branch_the_queue_never_recorded_is_not_adopted(tmp_path: Path) -> None:
    """The bound on this: only branches an item actually pushed can match, so
    a human's pull request cannot be attributed to the harness."""
    queue = queue_with(tmp_path, "T27", "harness/t27")
    reconciler = FakeReconciler([pull(200, "someone/their-feature")])

    assert adopt_lost_pull_requests(queue, reconciler, "default") == 0
    record = queue.get("T27")
    assert record is not None and record.pr_url is None


def test_nothing_to_recover_costs_no_github_call(tmp_path: Path) -> None:
    queue = queue_with(tmp_path, "T27", "harness/t27", "https://github.com/o/r/pull/9")
    reconciler = FakeReconciler([pull(110, "harness/t27")])

    assert adopt_lost_pull_requests(queue, reconciler, "default") == 0
    assert reconciler.calls == 0


def test_a_recovered_url_is_reconciled_on_the_same_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Left for the next sweep, the recovery would be an hour late for no
    reason: the mapping is rebuilt after adoption precisely so it is not."""
    from agent_harness import maintenance
    from agent_harness import reconcile as reconcile_mod

    queue = queue_with(tmp_path, "T27", "harness/t27")
    audit = AuditStore(tmp_path / "audit.sqlite")
    seen: list[dict[int, dict[str, str]]] = []

    class Recorder(FakeReconciler):
        def reconcile(self, mapping: dict[int, dict[str, str]]) -> object:
            seen.append(dict(mapping))

            class Report:
                merged = closed_unmerged = reverted = 0
                errors: list[str] = []

            return Report()

    recorder = Recorder([pull(110, "harness/t27")])
    monkeypatch.setattr(reconcile_mod, "GitHubReconciler", lambda repo, audit_: recorder)
    maintenance.reconcile_projects(audit, queue)

    assert seen == [{110: {"project_id": "default", "item_id": "T27"}}]
