"""Backlog sync tests. No network — the `gh` runner is injected."""

from __future__ import annotations

import json
from collections.abc import Sequence

from agent_harness.github import GitHub, Issue, body_for, sync
from agent_harness.plan import WorkItem


class FakeGh:
    """Records argv and replays canned `gh` output."""

    def __init__(
        self,
        issues: list[dict[str, object]] | None = None,
        labels: list[str] | None = None,
        milestones: list[str] | None = None,
    ) -> None:
        self.issues = issues or []
        # Default to "the repo already has everything the item asks for", so
        # tests about sync say nothing accidental about metadata creation.
        self.labels = labels
        self.milestones = milestones
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str:
        self.calls.append(list(args))
        if args[1:3] == ["issue", "list"]:
            return json.dumps(self.issues)
        if args[1:3] == ["label", "list"]:
            names = self.labels if self.labels is not None else self._all_wanted_labels()
            return json.dumps([{"name": n} for n in names])
        if args[1] == "api" and "milestones" in " ".join(args):
            if "-X" in args:
                return "{}"
            if self.milestones is not None:
                return json.dumps(self.milestones)
            return json.dumps(self._all_wanted_milestones())
        return "https://github.com/o/r/issues/1\n"

    def _all_wanted_labels(self) -> list[str]:
        names: set[str] = set()
        for issue in self.issues:
            labels = issue.get("labels")
            if isinstance(labels, list):
                names.update(label["name"] for label in labels if isinstance(label, dict))
        return sorted(names)

    def _all_wanted_milestones(self) -> list[str]:
        out: set[str] = set()
        for issue in self.issues:
            milestone = issue.get("milestone")
            if isinstance(milestone, dict) and milestone.get("title"):
                out.add(str(milestone["title"]))
        return sorted(out)

    def commands(self) -> list[str]:
        return [c[2] for c in self.calls if len(c) > 2]


def item(item_id: str = "T1", title: str = "Do the thing", **kw: object) -> WorkItem:
    return WorkItem(id=item_id, title=title, body=str(kw.pop("body", "the brief")), **kw)  # type: ignore[arg-type]


def as_issue(number: int, item_obj: WorkItem, **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "number": number,
        "title": item_obj.title,
        "body": body_for(item_obj),
        "state": "OPEN",
        "labels": [{"name": label} for label in item_obj.labels],
        "milestone": {"title": item_obj.milestone} if item_obj.milestone else None,
        "assignees": [],
        "url": f"https://github.com/o/r/issues/{number}",
    }
    base.update(kw)
    return base


def test_a_new_item_creates_an_issue() -> None:
    gh = FakeGh()
    report = sync(GitHub("o/r", gh), [item()])
    assert report.created == ["T1"]
    assert "create" in gh.commands()


def test_an_unchanged_item_is_not_rewritten() -> None:
    """The check that makes sync safe to run on every plan edit."""
    one = item()
    gh = FakeGh([as_issue(1, one)])
    report = sync(GitHub("o/r", gh), [one])
    assert report.unchanged == ["T1"]
    assert "edit" not in gh.commands()
    assert "create" not in gh.commands()


def test_an_edited_plan_updates_the_same_issue_rather_than_duplicating() -> None:
    """The whole point of the marker. Matching by title would fork the issue
    the moment you improved the wording."""
    gh = FakeGh([as_issue(7, item(title="Old wording"))])
    report = sync(GitHub("o/r", gh), [item(title="Much better wording")])
    assert report.updated == ["T1"]
    assert report.created == []
    edit = next(c for c in gh.calls if c[1:3] == ["issue", "edit"])
    assert edit[3] == "7"


def test_the_marker_survives_a_round_trip() -> None:
    body = body_for(item("T42"))
    assert "harness:id=T42" in body
    assert Issue(1, "t", body, "open").harness_id == "T42"


def test_an_issue_without_a_marker_is_ignored_not_adopted() -> None:
    """Someone else's issue in the same repo is not this plan's work."""
    gh = FakeGh([as_issue(1, item(), body="a hand-written issue")])
    report = sync(GitHub("o/r", gh), [item()])
    assert report.created == ["T1"]


def test_an_item_removed_from_the_plan_is_orphaned_never_closed() -> None:
    """An item vanishing from a plan is usually an edit, sometimes a
    mistake, and never grounds for the harness to close work by itself."""
    gh = FakeGh([as_issue(1, item("T1")), as_issue(2, item("T2"))])
    report = sync(GitHub("o/r", gh), [item("T1")])
    assert report.orphaned == ["T2"]
    assert "close" not in gh.commands()


def test_labels_added_by_a_human_are_not_stripped() -> None:
    """A sync that removed them would make the backlog hostile to use."""
    one = item(labels=["area:ci"])
    issue = as_issue(1, one)
    issue["labels"] = [{"name": "area:ci"}, {"name": "good-first-issue"}]
    gh = FakeGh([issue])
    assert sync(GitHub("o/r", gh), [one]).unchanged == ["T1"]


def test_a_missing_required_label_triggers_an_update() -> None:
    one = item(labels=["area:ci", "type:task"])
    issue = as_issue(1, one)
    issue["labels"] = [{"name": "area:ci"}]
    gh = FakeGh([issue])
    assert sync(GitHub("o/r", gh), [one]).updated == ["T1"]


def test_dry_run_reports_without_writing() -> None:
    gh = FakeGh()
    report = sync(GitHub("o/r", gh), [item()], dry_run=True)
    assert report.created == ["T1"]
    assert "create" not in gh.commands()


def test_dependencies_are_written_into_the_issue_body() -> None:
    body = body_for(item(depends_on=["T1", "T2"]))
    assert "Depends on:" in body
    assert "T1, T2" in body


def test_the_body_warns_that_edits_are_overwritten() -> None:
    # Otherwise someone writes a careful comment into the description and
    # loses it on the next sync without ever being told it would happen.
    assert "overwritten" in body_for(item())


def test_state_is_read_but_never_written_by_sync() -> None:
    gh = FakeGh([as_issue(1, item(), state="CLOSED")])
    sync(GitHub("o/r", gh), [item()])
    assert "reopen" not in gh.commands()
    assert "close" not in gh.commands()


def test_labels_the_plan_needs_are_created_before_syncing() -> None:
    """`gh issue create --label` fails outright on an unknown label, so
    without this the first sync of any plan dies on its first item."""
    gh = FakeGh(labels=["existing"])
    report = sync(GitHub("o/r", gh), [item(labels=["existing", "area:new"])])
    assert report.labels_created == ["area:new"]
    created = [c for c in gh.calls if c[1:3] == ["label", "create"]]
    assert [c[3] for c in created] == ["area:new"]


def test_milestones_the_plan_needs_are_created() -> None:
    gh = FakeGh(milestones=["P0"])
    report = sync(GitHub("o/r", gh), [item(milestone="P1")])
    assert report.milestones_created == ["P1"]


def test_nothing_is_created_when_the_repo_already_has_it() -> None:
    gh = FakeGh(labels=["area:ci"], milestones=["P0"])
    report = sync(GitHub("o/r", gh), [item(labels=["area:ci"], milestone="P0")])
    assert report.labels_created == []
    assert report.milestones_created == []


def test_creating_repository_metadata_is_reported_never_silent() -> None:
    """Creating labels and milestones changes the repository. A user who
    ran a sync should be told what it added, not discover it later."""
    gh = FakeGh()
    report = sync(GitHub("o/r", gh), [item(labels=["a:b"], milestone="M")])
    assert report.labels_created and report.milestones_created


def test_dry_run_does_not_create_metadata_either() -> None:
    gh = FakeGh()
    report = sync(GitHub("o/r", gh), [item(labels=["a:b"])], dry_run=True)
    assert report.labels_created == ["a:b"]  # reported...
    assert not [c for c in gh.calls if c[1:3] == ["label", "create"]]  # ...not made
