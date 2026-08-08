"""The installed GitHub review source, exercised without a network or a token.

`gh` is injected, as it is everywhere else a GitHub path is tested. What is
under test is the two things this adapter decides — immutable identity and
explicit disposition — not GitHub's own behaviour.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_harness.adapters.github_pr_review import GitHubPullRequestReviewSource, source
from agent_harness.review_sources import API_VERSION, ReviewPoller, names, resolve
from agent_harness.work import HELD, PENDING, Project, WorkQueue, WorkRecord


class Gh:
    """A `gh api` stand-in returning one payload per endpoint."""

    def __init__(
        self,
        reviews: list[dict[str, object]] | None = None,
        comments: list[dict[str, object]] | None = None,
    ) -> None:
        self.reviews = reviews or []
        self.comments = comments or []
        self.calls: list[str] = []

    def __call__(self, args: Sequence[str]) -> str:
        path = args[-1]
        self.calls.append(path)
        return json.dumps(self.reviews if "/reviews" in path else self.comments)


def make(gh: Gh, **kwargs: object) -> GitHubPullRequestReviewSource:
    config: dict[str, object] = {
        "repo": "acme/widgets",
        "pr": 7,
        "project_id": "p",
        "default_item_id": "T1",
        "runner": gh,
    }
    config.update(kwargs)
    return GitHubPullRequestReviewSource(**config)  # type: ignore[arg-type]


def test_unmarked_prose_is_ambiguous_rather_than_guessed_at() -> None:
    gh = Gh(
        comments=[
            {"id": 5, "body": "Not sure this is right?", "updated_at": "2026-08-07T00:00:00Z"}
        ]
    )

    batch = make(gh).poll(None)

    assert [event.disposition for event in batch.events] == ["ambiguous"]


def test_explicit_markers_decide_the_disposition() -> None:
    gh = Gh(
        comments=[
            {
                "id": 1,
                "body": "harness: fix — rename the field",
                "updated_at": "2026-08-07T00:00:01Z",
            },
            {
                "id": 2,
                "body": "harness: hold — is this in scope?",
                "updated_at": "2026-08-07T00:00:02Z",
            },
            {
                "id": 3,
                "body": "harness: resolved, done upstream",
                "updated_at": "2026-08-07T00:00:03Z",
            },
        ]
    )

    batch = make(gh).poll(None)

    assert [event.disposition for event in batch.events] == [
        "actionable",
        "ambiguous",
        "already_resolved",
    ]


def test_review_state_decides_when_no_marker_is_present() -> None:
    gh = Gh(
        reviews=[
            {
                "id": 10,
                "state": "CHANGES_REQUESTED",
                "body": "Fix the lock",
                "submitted_at": "2026-08-07T01:00:00Z",
            },
            {"id": 11, "state": "APPROVED", "body": "", "submitted_at": "2026-08-07T01:00:01Z"},
        ]
    )

    batch = make(gh).poll(None)

    assert [event.disposition for event in batch.events] == ["actionable", "already_resolved"]
    assert "approved with no comment" in batch.events[1].summary


def test_an_unsubmitted_draft_review_is_not_feedback_yet() -> None:
    gh = Gh(
        reviews=[
            {
                "id": 12,
                "state": "PENDING",
                "body": "harness: fix half a thought",
                "submitted_at": "2026-08-07T01:00:02Z",
            }
        ]
    )

    assert make(gh).poll(None).events == ()


def test_identity_separates_reviews_from_review_comments_sharing_a_number() -> None:
    gh = Gh(
        reviews=[
            {
                "id": 42,
                "state": "CHANGES_REQUESTED",
                "body": "a",
                "submitted_at": "2026-08-07T02:00:00Z",
            }
        ],
        comments=[{"id": 42, "body": "harness: fix b", "updated_at": "2026-08-07T02:00:01Z"}],
    )

    remote_ids = {event.remote_id for event in make(gh).poll(None).events}

    assert remote_ids == {
        "acme/widgets#7/reviews/42",
        "acme/widgets#7/comments/42",
    }


def test_an_explicit_item_marker_overrides_the_default_item() -> None:
    gh = Gh(
        comments=[
            {
                "id": 8,
                "body": "harness: fix\nharness-item: T4",
                "updated_at": "2026-08-07T03:00:00Z",
            }
        ]
    )

    batch = make(gh).poll(None)

    assert batch.events[0].item_id == "T4"


def test_the_cursor_is_the_latest_stamp_and_filters_the_comments_endpoint() -> None:
    gh = Gh(
        reviews=[
            {
                "id": 1,
                "state": "COMMENTED",
                "body": "harness: fix a",
                "submitted_at": "2026-08-07T04:00:00Z",
            }
        ],
        comments=[{"id": 2, "body": "harness: fix b", "updated_at": "2026-08-07T05:00:00Z"}],
    )
    api = make(gh)

    first = api.poll(None)
    api.poll(first.next_cursor)

    assert first.next_cursor == "2026-08-07T05:00:00Z"
    assert "since=2026-08-07T05:00:00Z" in gh.calls[-1]
    assert "since=" not in gh.calls[-2]  # the reviews endpoint has no `since`


def test_a_long_body_is_truncated_into_a_bounded_summary() -> None:
    gh = Gh(
        comments=[
            {"id": 9, "body": "harness: fix " + "x" * 9000, "updated_at": "2026-08-07T06:00:00Z"}
        ]
    )

    assert len(make(gh).poll(None).events[0].summary) <= 2000


def test_unreadable_output_fails_loudly_rather_than_polling_empty() -> None:
    class Broken:
        def __call__(self, args: Sequence[str]) -> str:
            return "not json"

    with pytest.raises(RuntimeError, match="unreadable"):
        make(Broken()).poll(None)  # type: ignore[arg-type]


def test_the_factory_names_what_it_needs_and_rejects_what_it_does_not_know() -> None:
    with pytest.raises(ValueError, match="default_item_id"):
        source({"repo": "acme/widgets", "pr": 1, "project_id": "p"})
    with pytest.raises(ValueError, match="does not accept token"):
        source(
            {
                "repo": "acme/widgets",
                "pr": 1,
                "project_id": "p",
                "default_item_id": "T1",
                "token": "secret",
            }
        )


def test_the_source_is_installed_and_resolves_by_name() -> None:
    assert "github-pr-review" in names()

    resolved = resolve(
        "github-pr-review",
        {"repo": "acme/widgets", "pr": 3, "project_id": "p", "default_item_id": "T1"},
    )

    assert resolved.api_version == API_VERSION
    assert resolved.name == "github-pr-review"


def test_polling_the_installed_source_creates_correction_work_once(tmp_path: Path) -> None:
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project("p", "project"))
    queue.add([WorkRecord("T1", "original")], project_id="p")
    gh = Gh(
        comments=[
            {"id": 1, "body": "harness: fix the lock", "updated_at": "2026-08-07T07:00:00Z"},
            {"id": 2, "body": "should this move?", "updated_at": "2026-08-07T07:00:01Z"},
        ]
    )
    poller = ReviewPoller(queue, make(gh))

    first = poller.poll_once()
    gh.calls.clear()
    second = poller.poll_once()

    assert (first.accepted, first.duplicates) == (2, 0)
    assert (second.accepted, second.duplicates) == (0, 2)
    states = sorted(item.state for item in queue.items(project_id="p"))
    assert states.count(PENDING) == 2  # the original plus the actionable correction
    assert states.count(HELD) == 1  # the unmarked comment is a person's decision
