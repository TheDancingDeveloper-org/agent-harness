"""Generating a plan for a project that already exists.

The model is faked; the repository, the git calls and the plan parser are all
real. That split is the point: what matters here is not what a model says, it
is whether the harness can *read back* what it produced, and that answer must
come from the same parser a hand-written plan goes through.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_harness.survey import DOC_LIMIT, gather, survey


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    (path / "docs").mkdir(parents=True)
    git(path.parent, "init", "-q", "-b", "main", str(path))
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    (path / "README.md").write_text("# Project\n\nA thing.\n")
    (path / "docs" / "current-state.md").write_text(
        "# Current state\n\n## Active forward roadmap\n\nUpgrade the runtime to Node 22.\n"
    )
    (path / "src.js").write_text("console.log(1)\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


def proposal_json(items: list[dict[str, object]], **extra: object) -> str:
    payload: dict[str, object] = {
        "goal": "Upgrade the runtime.",
        "assumptions": [],
        "non_goals": [],
        "risks": [],
        "phases": [{"id": "P0", "title": "Upgrade", "why": "because", "items": items}],
        "open_questions": [],
    }
    payload.update(extra)
    return json.dumps(payload)


def item(item_id: str, title: str = "Do the thing", **extra: object) -> dict[str, object]:
    out: dict[str, object] = {
        "id": item_id,
        "title": title,
        "brief": "Change the engines field in package.json to >=22, and keep CI green.",
        "depends_on": [],
    }
    out.update(extra)
    return out


def answering(text: str):  # type: ignore[no-untyped-def]
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return text

    ask.prompts = prompts  # type: ignore[attr-defined]
    return ask


# ------------------------------------------------- what the surveyor is shown


def test_the_project_s_own_roadmap_reaches_the_prompt(repo: Path) -> None:
    """The failure this module exists for: the roadmap was never opened."""
    ask = answering(proposal_json([item("T1")]))

    survey("upgrade to Node 22", repo, ask=ask, docs=["docs/current-state.md"])

    assert "Active forward roadmap" in ask.prompts[0]
    assert "Upgrade the runtime to Node 22" in ask.prompts[0]


def test_a_named_document_that_is_missing_is_reported_not_skipped(repo: Path) -> None:
    """Asking for the roadmap and silently planning without it is the bug."""
    evidence = gather(repo, ["docs/no-such-file.md"])

    assert any("MISSING" in source for source in evidence.sources)
    assert "not present" in evidence.text


def test_without_named_documents_it_guesses_and_stops_at_two(repo: Path) -> None:
    """One README is evidence. Seven candidate files is noise."""
    evidence = gather(repo, None)

    docs = [s for s in evidence.sources if s.endswith(".md")]
    assert docs == ["docs/current-state.md", "README.md"]


def test_the_tree_and_recent_history_are_shown(repo: Path) -> None:
    evidence = gather(repo, ["README.md"])

    assert "src.js" in evidence.text
    assert "initial" in evidence.text
    assert any("tracked path" in s for s in evidence.sources)


def test_an_enormous_document_cannot_crowd_out_everything_else(repo: Path) -> None:
    (repo / "docs" / "current-state.md").write_text("x" * (DOC_LIMIT * 3))

    evidence = gather(repo, ["docs/current-state.md"])

    assert len(evidence.text) < DOC_LIMIT * 2


def test_an_empty_objective_is_refused(repo: Path) -> None:
    with pytest.raises(ValueError, match="objective"):
        survey("   ", repo, ask=answering(proposal_json([item("T1")])))


# ------------------------------------- the harness's own parser is the gate


def test_a_generated_plan_is_read_back_by_the_real_parser(repo: Path) -> None:
    report = survey(
        "upgrade to Node 22",
        repo,
        ask=answering(proposal_json([item("T1"), item("T2", "And another")])),
    )

    assert report.item_count == 2
    assert report.usable
    assert "### T1 — Do the thing" in report.markdown


def test_a_plan_the_harness_cannot_read_is_not_usable(repo: Path) -> None:
    """A model that returns valid JSON with no items produces an empty plan.

    Nothing downstream would fail on this — `plan` would sync zero issues and
    `run` would say "nothing to do" — so it has to be caught where it happens.
    """
    report = survey("upgrade to Node 22", repo, ask=answering(proposal_json([])))

    assert report.item_count == 0
    assert not report.usable


def test_duplicate_ids_make_a_plan_unusable(repo: Path) -> None:
    """Each id becomes one issue and one queue row, so two T1s is not a plan."""
    report = survey(
        "upgrade to Node 22",
        repo,
        ask=answering(proposal_json([item("T1"), item("T1", "A different thing")])),
    )

    assert report.duplicate_ids == ["T1"]
    assert not report.usable


def test_a_blocking_question_is_reported_and_does_not_make_it_unusable(repo: Path) -> None:
    """It is a question for the human. Their answer decides, not this code."""
    report = survey(
        "upgrade to Node 22",
        repo,
        ask=answering(
            proposal_json(
                [item("T1")],
                open_questions=[
                    {
                        "id": "Q1",
                        "question": "Is the native addon still maintained?",
                        "severity": "blocking",
                        "why_it_matters": "it decides whether this is possible at all",
                    }
                ],
            )
        ),
    )

    assert report.blocking_questions == ["Is the native addon still maintained?"]
    assert report.usable
    assert "Q1" in report.markdown


def test_the_report_says_what_was_read(repo: Path) -> None:
    """The first question about any generated plan is what it actually read."""
    report = survey(
        "upgrade to Node 22", repo, ask=answering(proposal_json([item("T1")])), docs=["README.md"]
    )

    assert "README.md" in report.lines()[0]


def test_a_phase_heading_is_not_read_back_as_a_work_item(repo: Path) -> None:
    """`## P0 Upgrade` matches the parser's item pattern — `P0` is a valid id.

    Every generated plan therefore carried one phantom item per phase, whose
    brief was the phase's rationale. Found by counting items in a two-item
    plan and getting three; it applies to `inception`'s output equally.
    """
    report = survey(
        "upgrade to Node 22",
        repo,
        ask=answering(proposal_json([item("T1"), item("T2", "And another")])),
    )

    assert report.item_count == 2
    assert [i for i in report.markdown.splitlines() if i.startswith("## Phase")]
