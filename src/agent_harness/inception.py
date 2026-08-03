"""Scoping a project from a paragraph.

The pipeline used to start at "you already wrote a PLAN.md". This is the front
of it: describe a project in prose, have a model propose a scope, argue with
the proposal, and on approval let it create the repository and the backlog.

Three things this deliberately does.

**The proposal becomes a real PLAN.md**, not queue rows. Writing straight to
the queue would fork the pipeline in two -- a generated path and a
hand-written path, diverging forever. A plan document means the existing
parse/sync/queue machinery runs unchanged, the scope is diffable and
reviewable in a PR, and a human can edit it by hand at any point. It also
subjects the generated plan to the same parser that reports what it could
*not* read, so a proposal the harness cannot consume is caught before it
creates a single issue.

**Open questions carry a severity.** A scoping model that quietly invents a
constraint produces something indistinguishable from a decision the human
made -- and you would only find out after a repo and ninety issues exist. But
blocking on *every* question is worse than no gate: one cosmetic question
stalls the project, and the predictable adaptation is answering carelessly to
get past it, which converts a real signal into noise while looking like
diligence. So blocking questions refuse approval and deferrable ones do not.

**Nothing external happens before approval.** No repo, no issues, no branches,
no queue rows exist while questions are being resolved, so another round of
questions costs a conversation rather than a cleanup.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: The role that scopes a project. Deliberately separate from `planner`, which
#: plans a single work item: the two want very different models, and naming a
#: role rather than a model is what makes that a data change.
SCOPER = "scoper"

DRAFT = "draft"
SCOPING = "scoping"
PROPOSED = "proposed"
APPROVED = "approved"
INITIALISED = "initialised"

BLOCKING = "blocking"
DEFERRABLE = "deferrable"

SCOPE_PROMPT = """\
You are scoping a software project from a short description. You are not
writing code and not starting work.

## What the human said they want

{overview}

{feedback_section}

## What to produce

Return JSON only, matching this shape exactly:

{{
  "goal": "one paragraph restating what this is for, in your words",
  "assumptions": ["things you are taking as given"],
  "non_goals": ["things this explicitly does not do"],
  "risks": ["what could make this fail"],
  "phases": [
    {{"id": "P0", "title": "...", "why": "...",
      "items": [{{"id": "T1", "title": "...", "brief": "2-4 sentences: what to
                  do and how it will be judged", "depends_on": []}}]}}
  ],
  "open_questions": [
    {{"id": "Q1", "question": "...", "severity": "blocking|deferrable",
      "why_it_matters": "what changes depending on the answer"}}
  ]
}}

## How to judge severity

`blocking` means the answer changes what gets built -- choosing wrong means
work is done and then thrown away. `deferrable` means it is worth knowing but
a reasonable default holds and can be revisited.

Be sparing with `blocking`. If everything is blocking, nothing is, and the
human will answer carelessly to get past the gate.

## Rules

- Do not invent constraints. If you do not know, ask -- an invented constraint
  is indistinguishable from a decision the human made.
- Item briefs are specifications an agent will work from alone. "Add caching"
  is not a brief; say what, where and how it will be checked.
- Ids must be unique across the whole plan.
"""


@dataclass
class Question:
    id: str
    question: str
    severity: str = DEFERRABLE
    why_it_matters: str = ""
    answer: str | None = None
    deferred_reason: str | None = None
    resolved_at: float | None = None
    resolved_by: str | None = None

    @property
    def resolved(self) -> bool:
        return self.answer is not None or self.deferred_reason is not None

    @property
    def blocks_approval(self) -> bool:
        return self.severity == BLOCKING and not self.resolved


@dataclass
class Proposal:
    """One revision of a scope. Append-only: revisions are kept, never edited,
    so drift between what was asked for and what got built stays visible."""

    revision: int
    created_at: float
    goal: str = ""
    assumptions: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    phases: list[dict[str, Any]] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    feedback: str | None = None
    raw: str = ""

    def item_count(self) -> int:
        return sum(len(p.get("items") or []) for p in self.phases)

    def blocking_open(self) -> list[Question]:
        return [q for q in self.questions if q.blocks_approval]


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the JSON object out of a model reply.

    Models fence it, prefix it with prose, or both. Failing loudly on that is
    a worse outcome than tolerating it, because the content is usually right.
    """
    fenced = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("the scoper returned no JSON object")
    return json.loads(candidate[start : end + 1])  # type: ignore[no-any-return]


def parse_proposal(text: str, revision: int, now: float, feedback: str | None = None) -> Proposal:
    data = _extract_json(text)
    questions: list[Question] = []
    for raw in data.get("open_questions") or []:
        severity = str(raw.get("severity") or DEFERRABLE).lower()
        if severity not in (BLOCKING, DEFERRABLE):
            # An unrecognised severity is treated as deferrable rather than
            # blocking: a typo from the model must not wedge a project behind
            # a gate nobody can satisfy.
            severity = DEFERRABLE
        questions.append(
            Question(
                id=str(raw.get("id") or f"Q{len(questions) + 1}"),
                question=str(raw.get("question") or "").strip(),
                severity=severity,
                why_it_matters=str(raw.get("why_it_matters") or "").strip(),
            )
        )
    return Proposal(
        revision=revision,
        created_at=now,
        goal=str(data.get("goal") or "").strip(),
        assumptions=[str(a) for a in data.get("assumptions") or []],
        non_goals=[str(a) for a in data.get("non_goals") or []],
        risks=[str(a) for a in data.get("risks") or []],
        phases=list(data.get("phases") or []),
        questions=questions,
        feedback=feedback,
        raw=text,
    )


def render_plan(proposal: Proposal, name: str) -> str:
    """A proposal as a PLAN.md the existing parser can read.

    Headings use the `### T1 — Title` shape the parser recognises, so the
    generated plan goes through exactly the same path as a hand-written one.
    """
    out: list[str] = [f"# {name}", ""]
    if proposal.goal:
        out += ["## Goal", "", proposal.goal, ""]
    if proposal.non_goals:
        out += ["## Not doing", ""]
        out += [f"- {g}" for g in proposal.non_goals]
        out.append("")
    if proposal.assumptions:
        out += ["## Assumptions", ""]
        out += [f"- {a}" for a in proposal.assumptions]
        out.append("")
    if proposal.risks:
        out += ["## Risks", ""]
        out += [f"- {r}" for r in proposal.risks]
        out.append("")

    unresolved = [q for q in proposal.questions if not q.resolved]
    deferred = [q for q in proposal.questions if q.deferred_reason]
    if unresolved or deferred:
        out += ["## Open questions", ""]
        for q in unresolved:
            out.append(f"- **{q.id}** ({q.severity}) {q.question}")
        for q in deferred:
            # Deferred is answered "not now", which is different from unasked.
            # It survives into the plan so it stays visible rather than being
            # cleared at the approval gate.
            out.append(f"- **{q.id}** deferred — {q.deferred_reason}: {q.question}")
        out.append("")

    out += ["## Work", ""]
    for phase in proposal.phases:
        title = phase.get("title") or phase.get("id") or "Phase"
        out += [f"## {phase.get('id', '')} {title}".strip(), ""]
        if phase.get("why"):
            out += [str(phase["why"]), ""]
        for item in phase.get("items") or []:
            out.append(f"### {item.get('id')} — {item.get('title')}")
            out.append("")
            out.append(str(item.get("brief") or "").strip())
            depends = item.get("depends_on") or []
            if depends:
                out.append("")
                out.append(f"depends on: {', '.join(depends)}")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


class Inception:
    """The scoping conversation for one project.

    Storage is injected as a settings-style get/set pair so this does not need
    a table of its own before the shape has settled.
    """

    def __init__(self, queue: Any, model_client: Any = None, now: Any = time.time) -> None:
        self.queue = queue
        self.model_client = model_client
        self.now = now

    # ------------------------------------------------------------- state

    def _key(self, project_id: str) -> str:
        return f"inception:{project_id}"

    def load(self, project_id: str) -> dict[str, Any]:
        return self.queue.get_setting(self._key(project_id)) or {
            "project_id": project_id,
            "state": DRAFT,
            "overview": "",
            "revisions": [],
        }

    def _save(self, project_id: str, record: dict[str, Any]) -> None:
        self.queue.set_setting(self._key(project_id), record)

    def start(self, project_id: str, overview: str) -> dict[str, Any]:
        """Begin. Nothing external exists yet, and will not until approval."""
        record = {
            "project_id": project_id,
            "state": DRAFT,
            "overview": overview,
            "revisions": [],
            "created_at": self.now(),
        }
        self._save(project_id, record)
        return record

    # ------------------------------------------------------------ scoping

    def scope(self, project_id: str, feedback: str | None = None) -> Proposal:
        """Produce a proposal, or revise the previous one.

        Feedback revises rather than restarts: a fresh scope from scratch
        loses whatever was already right, and the human ends up re-arguing
        points they had settled.
        """
        if self.model_client is None:
            raise RuntimeError("no model client configured for the scoper role")
        record = self.load(project_id)
        revisions = record.get("revisions") or []

        previous = ""
        if revisions:
            last = revisions[-1]
            previous = (
                "## Your previous proposal\n\n"
                f"```json\n{last.get('raw', '')}\n```\n\n"
                "## What the human said about it\n\n"
                f"{feedback or '(no comment)'}\n\n"
                "Revise it. Keep what they did not object to."
            )

        prompt = SCOPE_PROMPT.format(
            overview=record.get("overview", ""),
            feedback_section=previous,
        )
        reply = self.model_client.call(SCOPER, [{"role": "user", "content": prompt}])
        text = reply if isinstance(reply, str) else str(reply)

        proposal = parse_proposal(text, len(revisions) + 1, self.now(), feedback)
        revisions.append(asdict(proposal))
        record["revisions"] = revisions
        record["state"] = PROPOSED
        self._save(project_id, record)
        return proposal

    def current(self, project_id: str) -> Proposal | None:
        record = self.load(project_id)
        revisions = record.get("revisions") or []
        if not revisions:
            return None
        raw = dict(revisions[-1])
        raw["questions"] = [Question(**q) for q in raw.get("questions") or []]
        return Proposal(**raw)

    # ---------------------------------------------------------- questions

    def resolve(
        self,
        project_id: str,
        question_id: str,
        *,
        answer: str | None = None,
        defer_reason: str | None = None,
        who: str = "operator",
        severity: str | None = None,
    ) -> Proposal:
        """Answer a question, defer it, or change what it is worth.

        `severity` lets a human overrule the model in either direction. The
        model proposes severity so the human is not triaging a flat list, but
        it does not get the final say on what matters.

        Silence is never a resolution: a question closes by an answer or by an
        explicit deferral, and both are recorded with who and when.
        """
        if answer is None and defer_reason is None and severity is None:
            raise ValueError("supply an answer, a deferral reason, or a severity")
        record = self.load(project_id)
        revisions = record.get("revisions") or []
        if not revisions:
            raise ValueError("nothing has been scoped yet")
        latest = revisions[-1]
        found = False
        for q in latest.get("questions") or []:
            if q["id"] != question_id:
                continue
            found = True
            if severity is not None:
                if severity not in (BLOCKING, DEFERRABLE):
                    raise ValueError(f"unknown severity {severity!r}")
                q["severity"] = severity
            if answer is not None:
                q["answer"] = answer
            if defer_reason is not None:
                q["deferred_reason"] = defer_reason
            if answer is not None or defer_reason is not None:
                q["resolved_at"] = self.now()
                q["resolved_by"] = who
        if not found:
            raise KeyError(f"no question {question_id!r}")
        self._save(project_id, record)
        return self.current(project_id)  # type: ignore[return-value]

    # ----------------------------------------------------------- approval

    def approve(self, project_id: str) -> Proposal:
        """The human gate. Refused while a blocking question is unanswered."""
        proposal = self.current(project_id)
        if proposal is None:
            raise ValueError("nothing has been scoped yet")
        blocking = proposal.blocking_open()
        if blocking:
            names = ", ".join(f"{q.id}: {q.question}" for q in blocking)
            raise ValueError(
                f"{len(blocking)} blocking question(s) unanswered — {names}. "
                "Answer them, or downgrade them to deferrable if they turn out "
                "not to change what gets built."
            )
        record = self.load(project_id)
        record["state"] = APPROVED
        record["approved_at"] = self.now()
        self._save(project_id, record)
        return proposal

    def plan_markdown(self, project_id: str, name: str | None = None) -> str:
        proposal = self.current(project_id)
        if proposal is None:
            raise ValueError("nothing has been scoped yet")
        return render_plan(proposal, name or project_id)


def new_project_id(name: str) -> str:
    """A stable, readable id from a name, with a short suffix for collisions."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    return slug or f"project-{uuid.uuid4().hex[:8]}"
