"""Scoping a project from a paragraph, and the gate before anything exists."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_harness.inception import (
    APPROVED,
    BLOCKING,
    DEFERRABLE,
    Inception,
    parse_proposal,
    render_plan,
)
from agent_harness.work import WorkQueue

PROPOSAL = {
    "goal": "A service that reconciles widgets.",
    "assumptions": ["Widgets already have stable ids"],
    "non_goals": ["A user interface"],
    "risks": ["The upstream feed is undocumented"],
    "phases": [
        {
            "id": "P0",
            "title": "Foundations",
            "why": "Nothing else can be tested without it",
            "items": [
                {
                    "id": "T1",
                    "title": "Schema",
                    "brief": "Define the widget table.",
                    "depends_on": [],
                },
                {"id": "T2", "title": "Importer", "brief": "Read the feed.", "depends_on": ["T1"]},
            ],
        }
    ],
    "open_questions": [
        {
            "id": "Q1",
            "question": "Which database?",
            "severity": "blocking",
            "why_it_matters": "The schema is written against it",
        },
        {
            "id": "Q2",
            "question": "Metric units?",
            "severity": "deferrable",
            "why_it_matters": "Cosmetic; can be converted later",
        },
    ],
}


class FakeModel:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.prompts: list[str] = []

    def call(self, role: str, messages: list[dict[str, Any]]) -> str:
        self.prompts.append(messages[0]["content"])
        return self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]


@pytest.fixture
def queue(tmp_path: Path) -> WorkQueue:
    return WorkQueue(str(tmp_path / "w.sqlite"))


def inception(queue: WorkQueue, replies: list[str] | None = None) -> Inception:
    model = FakeModel(replies or [json.dumps(PROPOSAL)])
    return Inception(queue, model_client=model, now=lambda: 1000.0)


# ------------------------------------------------------------- parsing


def test_a_fenced_reply_is_still_read() -> None:
    """Models fence JSON, prefix it with prose, or both. Failing on that is a
    worse outcome than tolerating it, because the content is usually right."""
    text = "Here you go:\n\n```json\n" + json.dumps(PROPOSAL) + "\n```\n\nHope that helps."
    proposal = parse_proposal(text, 1, 1000.0)
    assert proposal.goal.startswith("A service")
    assert proposal.item_count() == 2


def test_an_unknown_severity_is_deferrable_not_blocking() -> None:
    """A typo from the model must not wedge a project behind a gate nobody
    can satisfy."""
    payload = dict(PROPOSAL)
    payload["open_questions"] = [{"id": "Q1", "question": "?", "severity": "CRITICAL!!"}]
    proposal = parse_proposal(json.dumps(payload), 1, 1000.0)
    assert proposal.questions[0].severity == DEFERRABLE


def test_a_reply_with_no_json_is_an_error() -> None:
    with pytest.raises(ValueError, match="no JSON"):
        parse_proposal("I would rather not.", 1, 1000.0)


# ------------------------------------------------------------- questions


def test_approval_is_refused_while_a_blocking_question_is_open(queue: WorkQueue) -> None:
    inc = inception(queue)
    inc.start("w", "reconcile widgets")
    inc.scope("w")

    with pytest.raises(ValueError, match="blocking question"):
        inc.approve("w")


def test_a_deferrable_question_does_not_block(queue: WorkQueue) -> None:
    """A hard block on every question is worse than no gate: one cosmetic
    question stalls the project and people answer carelessly to get past it."""
    inc = inception(queue)
    inc.start("w", "reconcile widgets")
    inc.scope("w")
    inc.resolve("w", "Q1", answer="Postgres")

    proposal = inc.approve("w")
    assert proposal is not None
    # Q2 is still unanswered and that is fine.
    assert any(not q.resolved for q in proposal.questions)


def test_deferring_requires_a_reason_and_is_recorded(queue: WorkQueue) -> None:
    """'Not now' is a different answer from 'unasked', so it is recorded with
    who and when rather than silently cleared."""
    inc = inception(queue)
    inc.start("w", "reconcile widgets")
    inc.scope("w")
    proposal = inc.resolve("w", "Q2", defer_reason="cosmetic, revisit at P2", who="sprooty")

    q2 = [q for q in proposal.questions if q.id == "Q2"][0]
    assert q2.deferred_reason == "cosmetic, revisit at P2"
    assert q2.resolved_by == "sprooty"
    assert q2.resolved_at == 1000.0


def test_silence_never_resolves_a_question(queue: WorkQueue) -> None:
    inc = inception(queue)
    inc.start("w", "reconcile widgets")
    inc.scope("w")
    with pytest.raises(ValueError, match="supply an answer"):
        inc.resolve("w", "Q1")


def test_a_human_can_overrule_the_severity_either_way(queue: WorkQueue) -> None:
    """The model proposes severity so the human is not triaging a flat list,
    but it does not get the final say on what matters."""
    inc = inception(queue)
    inc.start("w", "reconcile widgets")
    inc.scope("w")

    # Demote the model's blocking question; approval now succeeds.
    inc.resolve("w", "Q1", severity=DEFERRABLE)
    inc.approve("w")

    # Promote the cosmetic one; approval is refused again.
    inc.resolve("w", "Q2", severity=BLOCKING)
    with pytest.raises(ValueError, match="blocking question"):
        inc.approve("w")


def test_resolving_an_unknown_question_is_an_error(queue: WorkQueue) -> None:
    inc = inception(queue)
    inc.start("w", "reconcile widgets")
    inc.scope("w")
    with pytest.raises(KeyError):
        inc.resolve("w", "Q99", answer="x")


# ------------------------------------------------------------- revisions


def test_feedback_revises_rather_than_restarts(queue: WorkQueue) -> None:
    """A scope from scratch loses whatever was already right, and the human
    re-argues points they had settled."""
    inc = inception(queue, [json.dumps(PROPOSAL), json.dumps(PROPOSAL)])
    inc.start("w", "reconcile widgets")
    inc.scope("w")
    inc.scope("w", feedback="drop the importer, we already have one")

    model = inc.model_client
    assert "previous proposal" in model.prompts[1]
    assert "drop the importer" in model.prompts[1]


def test_every_revision_is_kept(queue: WorkQueue) -> None:
    """Append-only, so drift between what was asked for and what got built
    stays visible rather than being overwritten."""
    inc = inception(queue, [json.dumps(PROPOSAL)] * 3)
    inc.start("w", "reconcile widgets")
    inc.scope("w")
    inc.scope("w", feedback="one")
    inc.scope("w", feedback="two")

    record = inc.load("w")
    assert len(record["revisions"]) == 3
    assert [r["feedback"] for r in record["revisions"]] == [None, "one", "two"]


# ------------------------------------------------------------- the plan


def test_the_proposal_becomes_a_plan_the_parser_can_read(queue: WorkQueue) -> None:
    """The load-bearing choice. Writing to the queue directly would fork the
    pipeline into a generated path and a hand-written path that diverge
    forever; a PLAN.md runs through the machinery that already exists.
    """
    from agent_harness.plan import parse_plan

    inc = inception(queue)
    inc.start("w", "reconcile widgets")
    inc.scope("w")
    markdown = inc.plan_markdown("w", name="Widgets")

    plan = parse_plan(markdown)
    # The phase heading is an item too. That is the parser working as
    # intended rather than a leak: real plans track phases as work (NGMS has
    # P0..P7 as issues), and a generated plan should behave the same way a
    # hand-written one does.
    assert [i.id for i in plan.items] == ["P0", "T1", "T2"]
    assert plan.duplicate_ids() == {}
    assert plan.unresolved_dependencies() == {}
    assert {i.id: i.depends_on for i in plan.items}["T2"] == ["T1"]


def test_a_deferred_question_survives_into_the_plan(queue: WorkQueue) -> None:
    """It stays visible rather than being cleared at the approval gate."""
    inc = inception(queue)
    inc.start("w", "reconcile widgets")
    inc.scope("w")
    inc.resolve("w", "Q2", defer_reason="cosmetic")

    markdown = inc.plan_markdown("w", name="Widgets")
    assert "deferred — cosmetic" in markdown


def test_the_plan_records_non_goals_and_risks(queue: WorkQueue) -> None:
    inc = inception(queue)
    inc.start("w", "reconcile widgets")
    inc.scope("w")
    markdown = inc.plan_markdown("w", name="Widgets")

    assert "A user interface" in markdown
    assert "undocumented" in markdown


def test_rendering_before_scoping_is_an_error(queue: WorkQueue) -> None:
    inc = inception(queue)
    inc.start("w", "reconcile widgets")
    with pytest.raises(ValueError, match="nothing has been scoped"):
        inc.plan_markdown("w")


def test_nothing_external_exists_before_approval(queue: WorkQueue) -> None:
    """The gate is only cheap enough to be honest because a further round of
    questions costs a conversation rather than a cleanup."""
    inc = inception(queue)
    inc.start("w", "reconcile widgets")
    inc.scope("w")

    assert queue.projects() == []
    assert queue.items() == []


def test_approval_records_that_it_happened(queue: WorkQueue) -> None:
    inc = inception(queue)
    inc.start("w", "reconcile widgets")
    inc.scope("w")
    inc.resolve("w", "Q1", answer="Postgres")
    inc.approve("w")

    record = inc.load("w")
    assert record["state"] == APPROVED
    assert record["approved_at"] == 1000.0


def test_render_plan_is_stable_for_an_empty_proposal() -> None:
    proposal = parse_proposal(json.dumps({"goal": "x", "phases": []}), 1, 1000.0)
    assert "# Thing" in render_plan(proposal, "Thing")
