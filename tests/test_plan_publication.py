"""One plan branch, one pull request, and a correction that updates it.

The remote here is a local bare repository and a fake pull-request client, so
these tests exercise the real push and the real record without contacting
GitHub. What is under test is the property the product requires: publishing
repeatedly never produces a second pull request, and a plan head that did not
move never touches the remote at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_harness.plan_integration import PlanState
from agent_harness.plan_publication import PlanPublisher, PublicationError
from agent_harness.work import FAILED, Project, WorkQueue, WorkRecord


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def make_repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "value.txt").write_text("base\n")
    git(repo, "add", "value.txt")
    git(repo, "commit", "-m", "base")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "main")
    git(repo, "branch", "plan/one")
    return repo, remote


class FakePRs:
    """A pull-request client that records what it was asked to do."""

    def __init__(self, open_pr: str | None = None) -> None:
        self.open_pr = open_pr
        self.created: list[dict[str, object]] = []
        self.comments: list[tuple[str, str]] = []

    def create_pr(self, *, title: str, body: str, head: str, base: str, draft: bool = False) -> str:
        self.created.append({"title": title, "head": head, "base": base, "draft": draft})
        self.open_pr = f"https://example.invalid/pr/{len(self.created)}"
        return self.open_pr

    def find_open_pr(self, head: str) -> str | None:
        return self.open_pr

    def comment_pr(self, pr: str, body: str) -> None:
        self.comments.append((pr, body))


def advance(repo: Path, message: str) -> str:
    """Promote something onto the plan branch, as the coordinator would."""
    git(repo, "checkout", "plan/one")
    (repo / "value.txt").write_text(message + "\n")
    git(repo, "add", "value.txt")
    git(repo, "commit", "-m", message)
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    return head


def state_for(repo: Path) -> PlanState:
    return PlanState(
        project_id="p",
        branch="plan/one",
        target_branch="main",
        target_sha=git(repo, "rev-parse", "main"),
        head_sha=git(repo, "rev-parse", "plan/one"),
        plan_digest="digest",
    )


def publisher(tmp_path: Path, repo: Path, github: FakePRs, **kwargs: object) -> PlanPublisher:
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project("p", "project"))
    return PlanPublisher(queue, "p", repo, github, **kwargs)  # type: ignore[arg-type]


def test_the_first_publication_pushes_the_branch_and_opens_one_pr(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path)
    head = advance(repo, "first")
    github = FakePRs()
    api = publisher(tmp_path, repo, github)

    result = api.publish(state_for(repo), title="Plan one", body="items")

    assert result.status == "created"
    assert github.created == [
        {"title": "Plan one", "head": "plan/one", "base": "main", "draft": False}
    ]
    assert git(remote, "rev-parse", "plan/one") == head
    assert api.record.pr_url == result.pr_url and api.record.head_sha == head


def test_republishing_an_unchanged_head_touches_nothing(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path)
    advance(repo, "first")
    github = FakePRs()
    api = publisher(tmp_path, repo, github)
    first = api.publish(state_for(repo), title="Plan one", body="items")

    again = api.publish(state_for(repo), title="Plan one", body="items", summary="note")

    assert again.status == "unchanged"
    assert again.pr_url == first.pr_url
    assert github.created == [github.created[0]]  # still exactly one
    assert github.comments == []


def test_a_correction_updates_the_same_pr_rather_than_opening_another(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path)
    advance(repo, "first")
    github = FakePRs()
    api = publisher(tmp_path, repo, github)
    first = api.publish(state_for(repo), title="Plan one", body="items")
    corrected = advance(repo, "correction")

    result = api.publish(
        state_for(repo),
        title="Plan one",
        body="items",
        summary="correction for T1 promoted",
    )

    assert result.status == "updated"
    assert result.pr_url == first.pr_url
    assert len(github.created) == 1
    assert github.comments == [(first.pr_url, "correction for T1 promoted")]
    assert git(remote, "rev-parse", "plan/one") == corrected
    assert api.record.head_sha == corrected


def test_an_existing_remote_pr_is_adopted_instead_of_duplicated(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    advance(repo, "first")
    github = FakePRs(open_pr="https://example.invalid/pr/existing")
    api = publisher(tmp_path, repo, github)

    result = api.publish(state_for(repo), title="Plan one", body="items")

    assert (result.status, result.pr_url) == ("updated", "https://example.invalid/pr/existing")
    assert github.created == []


def test_an_adopted_branch_this_plan_already_contains_is_published(tmp_path: Path) -> None:
    """A lost publication record must not strand an existing pull request."""
    repo, remote = make_repo(tmp_path)
    published = advance(repo, "first")
    git(repo, "push", "origin", "plan/one")
    github = FakePRs(open_pr="https://example.invalid/pr/existing")
    api = publisher(tmp_path, repo, github)
    assert api.record.head_sha is None  # nothing durable explains the remote
    corrected = advance(repo, "correction")

    result = api.publish(state_for(repo), title="Plan one", body="items")

    assert result.status == "updated"
    assert git(remote, "rev-parse", "plan/one") == corrected
    assert published != corrected


def test_an_unexplained_remote_branch_is_not_discarded(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(remote), str(other)], check=True, capture_output=True)
    git(other, "config", "user.email", "other@example.invalid")
    git(other, "config", "user.name", "Other")
    git(other, "checkout", "-b", "plan/one")
    (other / "value.txt").write_text("theirs\n")
    git(other, "add", "value.txt")
    git(other, "commit", "-m", "theirs")
    theirs = git(other, "rev-parse", "HEAD")
    git(other, "push", "origin", "plan/one")
    advance(repo, "ours")
    github = FakePRs(open_pr="https://example.invalid/pr/existing")
    api = publisher(tmp_path, repo, github)

    with pytest.raises(PublicationError, match="does not contain"):
        api.publish(state_for(repo), title="Plan one", body="items")
    assert git(remote, "rev-parse", "plan/one") == theirs


def test_a_rebuilt_plan_branch_still_publishes_under_the_lease(tmp_path: Path) -> None:
    """A moved target rebuilds the plan, so its history is rewritten."""
    repo, remote = make_repo(tmp_path)
    advance(repo, "first")
    github = FakePRs()
    api = publisher(tmp_path, repo, github)
    api.publish(state_for(repo), title="Plan one", body="items")

    git(repo, "checkout", "plan/one")
    git(repo, "reset", "--hard", "main")
    rebuilt = advance(repo, "replayed")

    result = api.publish(state_for(repo), title="Plan one", body="items")

    assert result.status == "updated"
    assert git(remote, "rev-parse", "plan/one") == rebuilt


def test_a_branch_moved_by_somebody_else_is_refused(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path)
    advance(repo, "first")
    github = FakePRs()
    api = publisher(tmp_path, repo, github)
    api.publish(state_for(repo), title="Plan one", body="items")

    # Another clone advances the published branch behind this harness's back.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(remote), str(other)], check=True, capture_output=True)
    git(other, "config", "user.email", "other@example.invalid")
    git(other, "config", "user.name", "Other")
    git(other, "checkout", "plan/one")
    (other / "value.txt").write_text("theirs\n")
    git(other, "add", "value.txt")
    git(other, "commit", "-m", "theirs")
    theirs = git(other, "rev-parse", "HEAD")
    git(other, "push", "origin", "plan/one")
    advance(repo, "ours")

    with pytest.raises(PublicationError, match="could not publish"):
        api.publish(state_for(repo), title="Plan one", body="items")
    assert git(remote, "rev-parse", "plan/one") == theirs


def test_publishing_a_second_branch_for_one_plan_is_refused(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    advance(repo, "first")
    github = FakePRs()
    api = publisher(tmp_path, repo, github)
    api.publish(state_for(repo), title="Plan one", body="items")

    other = PlanState("p", "plan/two", "main", "sha", "sha", "digest")
    with pytest.raises(PublicationError, match="second"):
        api.publish(other, title="Plan two", body="items")
    assert github.created == [github.created[0]]


def test_an_unreadable_remote_listing_never_creates_a_duplicate(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    advance(repo, "first")

    class Broken(FakePRs):
        def find_open_pr(self, head: str) -> str | None:
            raise RuntimeError("gh unreachable")

    github = Broken()
    api = publisher(tmp_path, repo, github)

    with pytest.raises(PublicationError, match="already has a pull"):
        api.publish(state_for(repo), title="Plan one", body="items")
    assert github.created == []


def test_a_failed_comment_does_not_lose_the_published_head(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path)
    advance(repo, "first")

    class Mute(FakePRs):
        def comment_pr(self, pr: str, body: str) -> None:
            raise RuntimeError("comment rejected")

    github = Mute()
    api = publisher(tmp_path, repo, github)
    api.publish(state_for(repo), title="Plan one", body="items")
    corrected = advance(repo, "correction")

    result = api.publish(state_for(repo), title="Plan one", body="items", summary="note")

    assert result.status == "updated"
    assert api.record.head_sha == corrected
    assert git(remote, "rev-parse", "plan/one") == corrected


def test_publication_is_reported_as_one_event(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    advance(repo, "first")
    seen: list[dict[str, object]] = []
    api = publisher(tmp_path, repo, FakePRs(), on_event=seen.append)

    api.publish(state_for(repo), title="Plan one", body="items")

    assert [event["outcome"] for event in seen] == ["plan_published"]
    assert seen[0]["status"] == "created" and seen[0]["branch"] == "plan/one"


def test_an_unreadable_record_is_reported_rather_than_overwritten(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    advance(repo, "first")
    github = FakePRs()
    api = publisher(tmp_path, repo, github)
    api.queue.set_setting(api.setting_key, "{not json")

    with pytest.raises(PublicationError, match="unreadable"):
        api.publish(state_for(repo), title="Plan one", body="items")
    assert github.created == []


def test_readiness_waits_for_work_that_could_still_change_the_tree(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    advance(repo, "first")
    api = publisher(tmp_path, repo, FakePRs())
    api.queue.add([WorkRecord("A", "A"), WorkRecord("B", "B")], project_id="p")

    assert api.readiness().ready is False
    assert "in flight" in api.readiness().detail
    assert api.publish_if_ready(state_for(repo), title="Plan one") is None


def test_readiness_withholds_publication_when_an_item_did_not_deliver(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    advance(repo, "first")
    github = FakePRs()
    api = publisher(tmp_path, repo, github)
    api.queue.add([WorkRecord("A", "A")], project_id="p")
    api.queue.set_control("running", project_id="p")
    claimed = api.queue.claim("worker", project_id="p")
    assert claimed is not None
    api.queue.release("A", FAILED, owner="worker", project_id="p")

    readiness = api.readiness()

    assert readiness.ready is False and readiness.unresolved == 1
    assert "need a person" in readiness.detail
    assert api.publish_if_ready(state_for(repo), title="Plan one") is None
    assert github.created == []


def test_the_promoting_item_does_not_block_its_own_plan(tmp_path: Path) -> None:
    """The last item is still claimed while it promotes; its work is in already."""
    repo, _ = make_repo(tmp_path)
    advance(repo, "first")
    github = FakePRs()
    api = publisher(tmp_path, repo, github)
    api.queue.add([WorkRecord("A", "A")], project_id="p")
    api.queue.set_control("running", project_id="p")
    assert api.queue.claim("worker", project_id="p") is not None

    assert api.readiness().ready is False
    assert api.readiness(excluding="A").ready is True

    result = api.publish_if_ready(state_for(repo), title="Plan one", excluding="A")

    assert result is not None and result.status == "created"


def test_an_empty_plan_is_not_something_to_publish(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    advance(repo, "first")
    github = FakePRs()
    api = publisher(tmp_path, repo, github)

    assert api.readiness().detail == "the plan has no items"
    assert api.publish_if_ready(state_for(repo), title="Plan one") is None
    assert github.created == []
