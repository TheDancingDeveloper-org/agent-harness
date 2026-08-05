"""Producing a plan for a project that already exists.

`inception` scopes a *new* project from a paragraph: describe it, argue with
the proposal, and on approval create the repository and the backlog. This is
the same idea pointed at a repository that is already there — the user states
an objective, and the first run of the harness works out what the items should
be rather than being handed them.

    "review and generate a plan to upgrade to Node v22"
        -> read the project
        -> propose a PLAN.md
        -> validate it with the harness's own parser
        -> a human approves, and `plan`/`adopt` execute it

**The deliverable is a `PLAN.md`, never queue rows.** Writing straight to the
queue would fork the pipeline in two, a generated path and a hand-written one,
diverging forever. A document means the existing parse/sync/queue machinery
runs unchanged, the scope is diffable and reviewable, and a human can edit it
at any point.

**The gate is the harness's own reader.** A generated plan is validated by
`parse_plan` — the same function that reads a hand-written one — so "the model
produced something the harness cannot consume" is caught here rather than
three commands later. That is a far better check than a model's opinion of its
own output, and it costs nothing.

**It reads the project rather than guessing.** This module exists because a
run against a real repository produced seven items that were all real, all
delivered, and none of them the work that mattered — the project kept its
roadmap in two documents that nothing in the harness ever opened (#181).

**Nothing external happens here.** No queue rows, no issues, no branches. The
output is a string and a report about it.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .inception import Proposal, parse_proposal, render_plan

log = logging.getLogger(__name__)

#: The role that surveys an existing project. Separate from `scoper`, which
#: scopes greenfield from prose, and from `planner`, which picks target files
#: inside one item: all three want different models, and naming a role rather
#: than a model keeps that a configuration change.
SURVEYOR = "surveyor"

#: Documents a project is likely to keep its direction in, tried in order when
#: the caller names none. Deliberately short: a wrong guess here costs context
#: budget and dilutes the evidence, and the caller can always be explicit.
ROADMAP_CANDIDATES = (
    "docs/current-state.md",
    "docs/roadmap.md",
    "docs/status.md",
    "ROADMAP.md",
    "STATUS.md",
    "PLAN.md",
    "README.md",
)

#: How much of any one document reaches the prompt. A roadmap is the most
#: valuable evidence there is here, so this is generous — but one enormous file
#: must not crowd out every other source.
DOC_LIMIT = 40_000

SURVEY_PROMPT = """\
You are producing a work plan for a project that already exists. You are not
writing code and not starting work.

## The objective

{objective}

## The project

{evidence}

## What to produce

Return JSON only, matching this shape exactly:

{{
  "goal": "one paragraph restating the objective in your words, in terms of
           this specific project",
  "assumptions": ["things you are taking as given"],
  "non_goals": ["what this explicitly does not cover"],
  "risks": ["what could make this fail"],
  "phases": [
    {{"id": "P0", "title": "...", "why": "...",
      "items": [{{"id": "T1", "title": "...", "brief": "what to do and how it
                  will be judged", "depends_on": []}}]}}
  ],
  "open_questions": [
    {{"id": "Q1", "question": "...", "severity": "blocking|deferrable",
      "why_it_matters": "what changes depending on the answer"}}
  ]
}}

## Rules

- **Ground every item in this repository.** Name the file, module, command or
  document you are working from. An item you cannot point at is a guess, and a
  guess is worse than an open question.
- **Order the work.** An unordered list of forty items is barely better than
  none. Use `depends_on`, and put the phases in the order they should happen.
- **Say what you could not determine.** If the project's own direction is
  unclear from what you were shown, that is an open question, not something to
  fill in. An invented constraint is indistinguishable from a decision the
  human made.
- **Item briefs are specifications an agent works from alone**, with no access
  to this conversation. State what is *out of scope* as explicitly as what is
  in it, and make the verb match the criteria — a brief that says "mirror the
  existing behaviour" licenses the opposite of criteria that say "change it".
- Ids must be unique across the whole plan.

## How to judge severity

`blocking` means the answer changes what gets built, so choosing wrong means
work is done and thrown away. `deferrable` means it is worth knowing but a
reasonable default holds.

Be sparing with `blocking`. If everything is blocking, nothing is, and the
human answers carelessly to get past the gate.
"""


@dataclass
class Evidence:
    """What the surveyor was shown, kept so a proposal can be argued with.

    Retained rather than discarded because the first question about any
    generated plan is "what did it actually read?" — and on the run that
    motivated this module the honest answer was "not the roadmap".
    """

    sources: list[str] = field(default_factory=list)
    text: str = ""

    def render(self) -> str:
        return self.text or "(nothing could be read from this repository)"


@dataclass
class SurveyReport:
    """A generated plan, and what the harness's own parser made of it."""

    markdown: str
    proposal: Proposal
    evidence: Evidence
    item_count: int = 0
    skipped: int = 0
    duplicate_ids: list[str] = field(default_factory=list)
    dependency_problems: list[str] = field(default_factory=list)
    blocking_questions: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Whether this plan can be executed as it stands.

        Unreadable or empty is fatal — there is nothing to run. Duplicate ids
        are fatal too, because each id becomes one issue and one queue row.
        A blocking question is *not* fatal here: it is a question for the
        human, and it is their answer that decides, which is the same rule
        `inception` applies at its approval gate.
        """
        return self.item_count > 0 and not self.duplicate_ids

    def lines(self) -> list[str]:
        out = [
            f"read {len(self.evidence.sources)} source(s): "
            + (", ".join(self.evidence.sources) or "none"),
            f"{self.item_count} work item(s), {self.skipped} heading(s) skipped as narrative",
        ]
        if self.duplicate_ids:
            out.append(
                "duplicate ids, which cannot become issues: " + ", ".join(self.duplicate_ids)
            )
        for problem in self.dependency_problems:
            out.append(f"dependency: {problem}")
        for question in self.blocking_questions:
            out.append(f"blocking question: {question}")
        return out


def _read(repo: Path, relative: str) -> str | None:
    path = repo / relative
    try:
        if not path.is_file():
            return None
        return path.read_text(errors="replace")[:DOC_LIMIT]
    except OSError:
        return None


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def gather(repo: Path, docs: list[str] | None = None) -> Evidence:
    """Read what the project says about itself.

    Named documents win over guessed ones, and a named document that does not
    exist is reported rather than skipped silently — asking for a roadmap and
    getting a plan built without it is the failure this whole module is for.
    """
    sources: list[str] = []
    parts: list[str] = []

    wanted = list(docs) if docs else list(ROADMAP_CANDIDATES)
    found = 0
    for relative in wanted:
        text = _read(repo, relative)
        if text is None:
            if docs:
                parts.append(f"### {relative}\n\n(named by the operator, and not present)\n")
                sources.append(f"{relative} (MISSING)")
            continue
        found += 1
        sources.append(relative)
        parts.append(f"### {relative}\n\n{text}\n")
        # Guessing stops at the first hit; being explicit does not. One
        # README is evidence, seven candidate files is noise.
        if not docs and found >= 2:
            break

    tree = _git(repo, "ls-files")
    if tree:
        paths = tree.splitlines()
        sources.append(f"{len(paths)} tracked path(s)")
        parts.append("### Tracked paths\n\n" + "\n".join(paths[:600]) + "\n")

    recent = _git(repo, "log", "--oneline", "-40")
    if recent:
        sources.append("recent history")
        parts.append(f"### Recent commits\n\n{recent}\n")

    return Evidence(sources=sources, text="\n".join(parts))


def survey(
    objective: str,
    repo: Path,
    *,
    ask: Callable[[str], str],
    docs: list[str] | None = None,
    name: str = "Plan",
    now: float = 0.0,
) -> SurveyReport:
    """Propose a plan for `objective` against `repo`, and check it can be read.

    Takes `ask` rather than a client so the surveyor's transport, routing and
    retry ladder stay the caller's business — the same shape `ModelAssessor`
    uses, and what makes this testable without a network.
    """
    from .plan import parse_plan

    if not objective.strip():
        raise ValueError("a survey needs an objective; there is nothing to plan towards")

    evidence = gather(repo, docs)
    prompt = SURVEY_PROMPT.format(objective=objective.strip(), evidence=evidence.render())
    proposal = parse_proposal(ask(prompt), 1, now)

    # Phase headings are containers here, not work. Their brief would be the
    # phase's rationale, and an agent that claims one is being asked to
    # implement a reason. `inception` keeps the other default; see there.
    markdown = render_plan(proposal, name, phases_as_items=False)
    # The harness's own reader is the gate. A generated plan that this cannot
    # consume is caught here rather than by `plan` three commands later, and
    # the parser reports what it could not read rather than dropping it.
    parsed = parse_plan(markdown)
    return SurveyReport(
        markdown=markdown,
        proposal=proposal,
        evidence=evidence,
        item_count=len(parsed.items),
        skipped=len(parsed.skipped),
        duplicate_ids=sorted(parsed.duplicate_ids()),
        dependency_problems=parsed.dependency_report().lines(),
        blocking_questions=[q.question for q in proposal.blocking_open()],
    )
