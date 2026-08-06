"""Adopt a project that already exists, without guessing what was done.

Inception starts from nothing, so it may create freely. Adoption starts from
a repository, a plan and a backlog that a human has already been working in,
so almost everything it might do is destructive if it is wrong: re-queuing
finished work wastes money, dropping unfinished work loses it, and editing an
issue overwrites someone's prose.

The shape that follows from that:

**Inspection is read-only.** `inspect` reads the plan, the repository, the
queue and — if one is configured — the external adapter. It writes no queue
rows, opens no branches and edits nothing outside the harness. What it
produces is a *proposal*.

**Evidence is ranked, and the ranking is visible.** Explicit evidence (a
checked plan item, a closed issue or merged pull request carrying the item's
id) outranks a declared verification command, which outranks a model's
judgement. Every rung is retained in the report with its citations, so a
reader can see which one carried the conclusion.

**Nothing drops work on its own.** A proposal to treat an item as already
delivered is exactly that. `approve` takes the item ids a human named, and
nothing else is dropped. Uncertainty — competing candidates, a judgement
without citations, a verification that failed — biases towards `not_started`.

**Exit 0 is not proof, and this is the expensive direction.** A `verify:`
command that exits 0 has not failed; it has not necessarily *tested* anything.
Every test runner that takes a name filter passes when the filter matches
nothing — `cargo test <name>`, `pytest -k`, `go test -run`, `npm test -- -t` —
so the most natural `verify:` anyone writes, "run the test this item adds",
exits 0 on precisely the tree where the item has not been done (#149). Reading
a runner's output to count what it ran is an adapter's job and a guess besides.
So a passing verification no longer decides on its own: it proposes a drop only
when a second rung agrees — explicit evidence, or an assessor `done` with
citations. Alone, it is reported as a **candidate a human must confirm**, and
the item stays `pending` until they name it. Being wrong that way costs a
re-run; being wrong the other way deletes work from the backlog silently, and
the report reads as evidence that it was right to.

**Prior harness attempts are evidence, not authority.** An item the queue has
already failed keeps its attempts and its event history. Adoption reports the
prior failure so a human can decide; it never overrules it.

**Except when the question changed.** A refresh that **rewrites an item's
brief** returns it to `pending` from `failed` or `blocked`, and clears the
error with it. The attempt that failed was made against wording that no longer
exists, so keeping the verdict records a decision about a question nobody is
asking. This is the second half of the loop that lets an agent refuse an
impossible item: without it, a human who rewrites the item in response is told
"nothing to do" (#178). Nothing else moves state — a changed title, label or
dependency leaves it exactly where it was.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .executor import Checks
from .github import MARKER, GitHub
from .guard import CommandRefused
from .outcomes import ESCALATE, RETRY
from .plan import CODE, ParsedPlan, WorkItem
from .work import CLAIMED, DONE, PENDING, Project, WorkQueue, WorkRecord, revives

#: The lifecycle of one adoption (proposal §5.1). `rejected` and `revise` are
#: the two ways out of `proposed` that do not mutate anything.
DRAFT = "draft"
INSPECTING = "inspecting"
PROPOSED = "proposed"
APPROVED = "approved"
RECONCILED = "reconciled"
STOPPED = "stopped"
REJECTED = "rejected"
REVISE = "revise"

#: The role that judges whether an item's work is already present. Named as a
#: role rather than a model so routing it is a configuration change.
ASSESSOR = "assessor"

DONE_DISPOSITION = "done"
PARTIAL_DISPOSITION = "partial"
NOT_STARTED_DISPOSITION = "not_started"
ASSESSOR_DISPOSITIONS = frozenset({DONE_DISPOSITION, PARTIAL_DISPOSITION, NOT_STARTED_DISPOSITION})

#: Declared item verification runs under the project-check rules, and takes
#: that rule's timeout from the same place, so the two cannot drift apart.
DEFAULT_VERIFY_TIMEOUT: float = Checks.timeout

EXPLICIT = "explicit"
RUNNABLE = "runnable"
JUDGED = "judged"
PRIOR_ATTEMPT = "prior_attempt"

#: What a `verify:` command did, as the report says it. `passed` is the exit
#: code and nothing more — see the module docstring for why that is not the
#: same fact as "this item is done".
VERIFY_PASSED = "passed"
VERIFY_FAILED = "failed"
VERIFY_TIMEOUT = "timeout"
VERIFY_UNAVAILABLE = "unavailable"

#: The branch prefix both executors use. A branch that matches it is a lead,
#: never proof: a human can name a branch anything at all, including this.
BRANCH_PREFIX = "harness/"

ASSESS_PROMPT = """\
You are judging whether one unit of work is ALREADY PRESENT in a repository.
You are not implementing it and not planning it.

## The item

{brief}

## What to return

One JSON object and no commentary:

{{
  "disposition": "done|partial|not_started",
  "citations": ["path/to/file.py:symbol", "tests/test_x.py::test_y", "abc1234"],
  "rationale": "one or two sentences naming what you found"
}}

## Rules

- `done` means you can point at the code, test or commit that delivers it.
  A `done` without citations is not `done`.
- If you are unsure, say `not_started`. Being wrong in that direction costs a
  re-run; being wrong the other way silently deletes work from the backlog.
- Do not judge quality. The question is only whether it exists.
"""


@dataclass(frozen=True)
class ExternalCandidate:
    """One existing issue, branch or pull request that *may* be this item.

    A candidate is never a fact. `confidence` and `evidence` say why it is a
    candidate at all, and `harness_created` is the only thing that licenses
    the queue to claim the harness opened it.
    """

    kind: str
    identity: str
    state: str
    confidence: str
    evidence: str
    #: The item's harness marker is already in the body. Backfilling would be
    #: a no-op, and the branch or PR is genuinely the harness's own.
    marker_present: bool = False
    #: The candidate carries proof of harness authorship, not a similar name.
    harness_created: bool = False
    body: str = ""
    title: str = ""
    branch: str = ""
    url: str = ""
    repository: str | None = None
    #: For a pull request: whether its head branch lives in this repository.
    #: A fork's branch is not evidence that this repository produced it.
    same_repository: bool = True


@dataclass(frozen=True)
class InspectionSnapshot:
    """Read-only external state returned by a configured adapter."""

    candidates: dict[str, list[ExternalCandidate]] = field(default_factory=dict)


@dataclass(frozen=True)
class Judgement:
    """Structured assessor evidence. It proposes; it never changes work."""

    disposition: str
    citations: list[str]
    rationale: str


@dataclass(frozen=True)
class Evidence:
    """One rung of the ladder, retained whether or not it was decisive."""

    kind: str
    outcome: str
    detail: str
    citations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProposedMutation:
    """Exactly what reconciliation would change, said before it happens."""

    kind: str
    target: str
    detail: str
    #: Whether this specific change waits on a named `--approve-drop`. A queue
    #: row is created either way; an issue edit is not.
    requires_approval: bool = False


@dataclass
class AdoptionItem:
    item_id: str
    title: str
    brief: str
    depends_on: list[str]
    #: What the queue already says about this item, if anything. Adoption
    #: reports it rather than overwriting it.
    queue_state: str | None = None
    #: The brief the queue is holding, so the report can tell a re-sync that
    #: changes nothing from one that rewrites the item.
    queue_brief: str | None = None
    #: What the plan says this item produces. Carried through rather than
    #: defaulted, or a `deliverable: findings` item would be adopted as one
    #: that must produce a diff (#182).
    deliverable: str = CODE
    proposed_state: str = PENDING
    evidence: list[Evidence] = field(default_factory=list)
    candidates: list[ExternalCandidate] = field(default_factory=list)
    mutations: list[ProposedMutation] = field(default_factory=list)
    #: Why a human has to look at this item before anything happens.
    ambiguity: str | None = None
    #: The error a previous harness attempt left behind, if there was one.
    prior_failure: str | None = None
    requires_drop_approval: bool = False


@dataclass
class AdoptionReport:
    """One adoption proposal, storable as JSON and reviewable as text."""

    project_id: str
    state: str
    repository: str
    created_at: float
    dry_run: bool
    items: list[AdoptionItem]
    plan_path: str = ""
    configured_repo: str | None = None
    input_digest: str = ""
    inspect_remote: bool = False
    approved_drops: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=lambda: [DRAFT, INSPECTING, PROPOSED])
    #: Why a human rejected or asked for a revision. Never inferred.
    decision_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def content_digest(self) -> str:
        """Identity of the *findings*, independent of who has approved them.

        Two inspections of an unchanged repository must produce the same
        digest, which is what "repeated adoption produces the same report"
        means when the reader is a machine rather than a person.
        """
        payload = _inspection_signature(self)
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def proposed_drops(self) -> list[str]:
        return [item.item_id for item in self.items if item.requires_drop_approval]

    def unconfirmed_drops(self) -> list[str]:
        """Drops a human may approve that nothing in the report proposes.

        A passing `verify:` with no second rung behind it lands here: it is
        offered, it is not asserted, and if nobody names it the item stays
        `pending`.
        """
        return [
            item.item_id
            for item in self.items
            if item.requires_drop_approval and item.proposed_state != DONE
        ]

    def ambiguous(self) -> list[AdoptionItem]:
        return [item for item in self.items if item.ambiguity]

    def summary(self) -> str:
        """The report as a human reads it before deciding."""
        lines = [
            f"project {self.project_id}: {self.state}",
            f"repository {self.repository}",
            f"{len(self.items)} plan item(s); "
            f"{len(self.proposed_drops())} proposed as already delivered "
            f"({len(self.unconfirmed_drops())} unconfirmed); "
            f"{len(self.ambiguous())} needing a human decision",
        ]
        for item in self.items:
            marks = []
            if item.requires_drop_approval and item.proposed_state == DONE:
                marks.append("proposed done")
            elif item.requires_drop_approval:
                # A drop a human may name, that nothing here proposes. Said
                # differently from "proposed done" because the reader's next
                # action is different: this one has to be checked first.
                marks.append("possible drop, unconfirmed: droppable only if a human names it")
            if item.ambiguity:
                marks.append(f"ambiguous: {item.ambiguity}")
            if item.prior_failure:
                marks.append(f"prior failure: {item.prior_failure}")
            if item.queue_state:
                marks.append(f"already {item.queue_state} in the queue")
            suffix = f"  [{'; '.join(marks)}]" if marks else ""
            lines.append(f"  {item.item_id} -> {item.proposed_state}{suffix}")
            for evidence in item.evidence:
                lines.append(f"      {evidence.kind}/{evidence.outcome}: {evidence.detail}")
            for candidate in item.candidates:
                lines.append(
                    f"      candidate {candidate.kind} {candidate.identity} "
                    f"({candidate.state}, {candidate.confidence}): {candidate.evidence}"
                )
            for mutation in item.mutations:
                lines.append(
                    f"      {self._mutation_verb(item, mutation)} {mutation.kind} "
                    f"{mutation.target}: {mutation.detail}"
                )
        return "\n".join(lines)

    def _mutation_verb(self, item: AdoptionItem, mutation: ProposedMutation) -> str:
        """Past tense only for what actually happened.

        A report printed after reconciliation that still says "would" tells
        the reader the opposite of the truth, and one that says "applied"
        about an edit nobody approved is worse.
        """
        if self.state != STOPPED:
            return "would"
        if mutation.requires_approval and item.item_id not in self.approved_drops:
            return "did NOT (unapproved)"
        return "applied"


class ExternalInspector(Protocol):
    def inspect(self, items: list[WorkItem]) -> InspectionSnapshot: ...

    def backfill_marker(self, candidate: ExternalCandidate, item_id: str) -> None: ...


class Assessor(Protocol):
    def assess(self, item: WorkItem, repository: Path) -> Judgement: ...


def parse_judgement(text: str) -> Judgement:
    """Read an assessor reply, biasing every failure towards `not_started`.

    A reply this build cannot parse, a disposition it does not recognise, and
    a `done` with nothing cited are all the same thing from the backlog's
    point of view: no reason to believe the work exists. Raising instead
    would be worse — one malformed reply would abort an inspection that had
    already run real verification commands.
    """
    raw = text.strip()
    fenced = raw.split("```")
    if len(fenced) >= 3:
        raw = fenced[1].removeprefix("json").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return Judgement(NOT_STARTED_DISPOSITION, [], f"unparseable assessor reply: {text[:200]}")
    try:
        payload = json.loads(raw[start : end + 1])
    except ValueError:
        return Judgement(NOT_STARTED_DISPOSITION, [], f"unparseable assessor reply: {text[:200]}")
    if not isinstance(payload, dict):
        return Judgement(NOT_STARTED_DISPOSITION, [], f"unparseable assessor reply: {text[:200]}")

    disposition = str(payload.get("disposition") or "").strip().lower()
    citations = [str(c).strip() for c in payload.get("citations") or [] if str(c).strip()]
    rationale = str(payload.get("rationale") or "").strip()
    if disposition not in ASSESSOR_DISPOSITIONS:
        return Judgement(
            NOT_STARTED_DISPOSITION,
            citations,
            f"unrecognised disposition {disposition!r}; treated as not started",
        )
    if disposition == DONE_DISPOSITION and not citations:
        return Judgement(
            NOT_STARTED_DISPOSITION,
            [],
            "the assessor said done and cited nothing; treated as not started",
        )
    return Judgement(disposition, citations, rationale)


class ModelAssessor:
    """The `assessor` role, over any callable that turns a prompt into text.

    The transport is the caller's, exactly as it is for every other role: the
    harness does not own the HTTP, and adoption must be runnable in a test
    with no network at all.
    """

    def __init__(self, ask: Callable[[str], str]) -> None:
        self.ask = ask

    def assess(self, item: WorkItem, repository: Path) -> Judgement:
        try:
            reply = self.ask(ASSESS_PROMPT.format(brief=item.brief()))
        except Exception as exc:  # noqa: BLE001 - a dead role must not drop work
            return Judgement(NOT_STARTED_DISPOSITION, [], f"the assessor failed: {exc}")
        return parse_judgement(reply)


class GitHubAdoptionInspector:
    """Read GitHub candidates and make conservative, explainable matches.

    An exact harness marker is authoritative — the harness wrote it. An exact
    item id in the body or title is high confidence but still only a
    candidate. A similar title, or a branch that merely *looks* like the
    harness naming convention, is deliberately medium confidence and can
    never complete an item or be claimed as harness-created.
    """

    def __init__(self, github: GitHub) -> None:
        self.github = github

    def inspect(self, items: list[WorkItem]) -> InspectionSnapshot:
        issues = self.github.list_issues()
        pulls = self.github.list_prs()
        candidates: dict[str, list[ExternalCandidate]] = {}
        for item in items:
            found = [
                candidate
                for candidate in (
                    *(self._issue_candidate(item, issue) for issue in issues),
                    *(self._pr_candidate(item, pull) for pull in pulls),
                )
                if candidate is not None
            ]
            if found:
                candidates[item.id] = found
        return InspectionSnapshot(candidates=candidates)

    def _issue_candidate(self, item: WorkItem, issue: Any) -> ExternalCandidate | None:
        marker = issue.harness_id == item.id
        body_id = _mentions_id(issue.body, item.id)
        title_match = issue.title.strip().casefold() == item.title.strip().casefold()
        if not (marker or body_id or title_match):
            return None
        if marker:
            confidence, reason = "high", "the issue carries this item's harness marker"
        elif body_id:
            confidence, reason = "high", "the issue body names this item id exactly"
        else:
            confidence, reason = "medium", "the title matches and nothing names the item id"
        return ExternalCandidate(
            kind="issue",
            identity=str(issue.number),
            state=issue.state,
            confidence=confidence,
            evidence=reason,
            marker_present=marker,
            harness_created=marker,
            body=issue.body,
            title=issue.title,
            url=issue.url,
            repository=self.github.repo,
        )

    def _pr_candidate(self, item: WorkItem, pull: Any) -> ExternalCandidate | None:
        """A pull request is adopted only on explicit evidence.

        Explicit means all four of the things §5.3 names: an id, a head
        branch, a title or body reference, and a head branch in this
        repository. A fork's branch or a lookalike name gets a candidate row
        and a medium confidence, so a human sees it and nothing acts on it.
        """
        marker = pull.harness_id == item.id
        referenced = _mentions_id(pull.body, item.id) or _mentions_id(pull.title, item.id)
        looks_like = pull.head.casefold() == f"{BRANCH_PREFIX}{item.id}".casefold()
        if not (marker or referenced or looks_like):
            return None

        if marker and pull.same_repository:
            confidence = "high"
            reason = (
                f"pull request {pull.number} carries this item's harness marker, "
                f"head branch {pull.head} is in {self.github.repo}"
            )
        elif referenced and pull.same_repository:
            confidence = "high"
            reason = (
                f"pull request {pull.number} names this item id in its title or body, "
                f"head branch {pull.head} is in {self.github.repo}"
            )
        elif not pull.same_repository:
            confidence = "medium"
            reason = (
                f"pull request {pull.number} is from a head branch outside "
                f"{self.github.repo}; this repository did not produce it"
            )
        else:
            confidence = "medium"
            reason = (
                f"branch {pull.head} resembles the harness naming convention, and "
                "nothing in the title or body names the item id"
            )
        return ExternalCandidate(
            kind="pull_request",
            identity=str(pull.number),
            state=pull.state,
            confidence=confidence,
            evidence=reason,
            marker_present=marker,
            harness_created=marker and pull.same_repository,
            body=pull.body,
            title=pull.title,
            branch=pull.head,
            url=pull.url,
            repository=self.github.repo,
            same_repository=pull.same_repository,
        )

    def backfill_marker(self, candidate: ExternalCandidate, item_id: str) -> None:
        """Append only the marker, preserving every byte of human-authored body."""
        if candidate.kind != "issue" or candidate.marker_present:
            return
        body = candidate.body.rstrip()
        updated = f"{body}\n\n{MARKER.format(id=item_id)}\n"
        self.github.update_issue_body(int(candidate.identity), updated)


class Adoption:
    """The adoption lifecycle for one queue, project and repository.

    Storage is the queue's settings table, as it is for inception: the shape
    of a proposal is still moving, and a table of its own would freeze it.
    """

    def __init__(
        self,
        queue: WorkQueue,
        repository: Path | str,
        *,
        external: ExternalInspector | None = None,
        assessor: Assessor | None = None,
        branches: Callable[[Path], list[str]] = lambda repo: git_branches(repo),
        verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.queue = queue
        self.repository = Path(repository).resolve()
        self.external = external
        self.assessor = assessor
        self.branches = branches
        self.verify_timeout = verify_timeout
        self.on_event = on_event
        self.now = now

    # ------------------------------------------------------------- storage

    def _key(self, project_id: str) -> str:
        return f"adoption:{project_id}"

    def load(self, project_id: str) -> AdoptionReport:
        raw = self.queue.get_setting(self._key(project_id))
        if raw is None:
            raise ValueError(f"no adoption inspection for {project_id!r}")
        return report_from_dict(raw)

    def _save(self, report: AdoptionReport) -> None:
        self.queue.set_setting(self._key(report.project_id), report.to_dict())

    # ---------------------------------------------------------- inspection

    def inspect(
        self,
        project_id: str,
        plan: ParsedPlan,
        *,
        dry_run: bool = False,
        persist: bool = True,
        plan_path: str = "",
        configured_repo: str | None = None,
        input_digest: str = "",
        inspect_remote: bool = False,
        emit: bool = True,
    ) -> AdoptionReport:
        """Build a proposal. Reads everything; changes nothing outside the report.

        `persist` is what makes a dry run leave no trace at all: a proposal
        stored under a project id is harness state, and a command advertised
        as changing nothing should not change that either.
        """
        items = plan.deduplicated()
        snapshot = self.external.inspect(items) if self.external else InspectionSnapshot()
        branches = self.branches(self.repository)
        proposed = [
            self._inspect_item(
                project_id,
                item,
                [
                    *snapshot.candidates.get(item.id, []),
                    *_branch_candidates(item, branches),
                ],
            )
            for item in items
        ]
        report = AdoptionReport(
            project_id=project_id,
            state=PROPOSED,
            repository=str(self.repository),
            created_at=self.now(),
            dry_run=dry_run,
            items=proposed,
            plan_path=plan_path,
            configured_repo=configured_repo,
            input_digest=input_digest,
            inspect_remote=inspect_remote,
            history=[DRAFT, INSPECTING, PROPOSED],
        )
        previous = self.queue.get_setting(self._key(project_id))
        if previous is not None:
            earlier = report_from_dict(previous)
            if earlier.content_digest() == report.content_digest():
                # An unchanged repository yields the same report, not the same
                # findings wearing a fresh timestamp -- which would make two
                # identical inspections look like a change to anyone diffing
                # the stored proposals.
                report.created_at = earlier.created_at
        if persist:
            self._save(report)
        for item in report.items if emit else []:
            outcome = (
                "adoption_ambiguous"
                if item.ambiguity
                else f"adoption_proposed_{item.proposed_state}"
            )
            self._emit(
                project_id,
                item.item_id,
                outcome,
                detail=item.ambiguity or "; ".join(f"{e.kind}/{e.outcome}" for e in item.evidence),
            )
        return report

    def _inspect_item(
        self, project_id: str, item: WorkItem, candidates: list[ExternalCandidate]
    ) -> AdoptionItem:
        existing = self.queue.get(item.id, project_id=project_id)
        result = AdoptionItem(
            item_id=item.id,
            title=item.title,
            brief=item.brief(),
            depends_on=list(item.depends_on),
            queue_state=existing.state if existing else None,
            queue_brief=existing.brief if existing else None,
            deliverable=item.deliverable,
            candidates=list(candidates),
        )

        # Prior harness attempts are evidence about this item, and the only
        # place they live is the queue row and the event stream. Neither is
        # rewritten here; both are quoted.
        if existing is not None and existing.attempts:
            result.prior_failure = existing.last_error
            result.evidence.append(
                Evidence(
                    kind=PRIOR_ATTEMPT,
                    outcome=existing.state,
                    detail=(
                        f"{existing.attempts} prior harness attempt(s); "
                        f"last error: {existing.last_error or 'none recorded'}"
                    ),
                )
            )

        # An item the queue has already finished is not re-proposed as work.
        # Re-queuing it would be the "resets progress" failure, and proposing
        # to drop it would ask a human to approve something already true.
        if existing is not None and existing.state in (DONE, CLAIMED):
            result.proposed_state = existing.state
            result.evidence.append(
                Evidence(
                    kind=EXPLICIT,
                    outcome=existing.state,
                    detail=f"the queue already holds this item as {existing.state}",
                )
            )
            self._add_mutations(project_id, result)
            return result

        top = _top_candidates(candidates)
        if len(top) > 1:
            result.ambiguity = (
                f"{len(top)} competing {top[0].confidence}-confidence candidates: "
                + ", ".join(f"{c.kind} {c.identity}" for c in top)
            )

        explicit = _explicit_evidence(item, top, ambiguous=result.ambiguity is not None)
        if explicit is not None:
            result.evidence.append(explicit)
            _propose_done(result)
            self._add_mutations(project_id, result)
            return result

        verified: Evidence | None = None
        if item.verification:
            verified = self._run_verification(item.verification)
            result.evidence.append(verified)

        # The verification no longer short-circuits the ladder. It used to
        # return here on exit 0, which made one exit code the whole decision --
        # and a name-filtered test command exits 0 on a tree that does not
        # contain the test (#149). The assessor is asked anyway, because a
        # second rung is the only corroboration available that does not mean
        # reading another ecosystem's output and guessing what it meant.
        judgement: Judgement | None = None
        if self.assessor is not None:
            judgement = self._judge(item)
            result.evidence.append(
                Evidence(
                    kind=JUDGED,
                    outcome=judgement.disposition,
                    detail=judgement.rationale,
                    citations=list(judgement.citations),
                )
            )

        judged_done = judgement is not None and judgement.disposition == DONE_DISPOSITION
        proved = verified is not None and verified.outcome == VERIFY_PASSED

        if judged_done and verified is not None and not proved:
            # A declared verification that ran and did not pass is stronger
            # than a model saying the work is there. Believing the model here
            # is exactly the silent drop the stage exists to prevent.
            result.ambiguity = (
                f"the assessor says done but the item's own verification {verified.outcome}"
            )
        elif judged_done:
            # Either nothing was declared to run, or what was declared agrees
            # with a citation-carrying `done`. Two rungs, not one exit code.
            _propose_done(result)
        elif proved:
            _propose_unconfirmed_drop(result)
        self._add_mutations(project_id, result)
        return result

    def _judge(self, item: WorkItem) -> Judgement:
        assert self.assessor is not None
        try:
            judgement = self.assessor.assess(item, self.repository)
        except Exception as exc:  # noqa: BLE001 - a dead role must not drop work
            return Judgement(NOT_STARTED_DISPOSITION, [], f"the assessor failed: {exc}")
        if judgement.disposition not in ASSESSOR_DISPOSITIONS:
            return Judgement(
                NOT_STARTED_DISPOSITION,
                list(judgement.citations),
                f"unrecognised disposition {judgement.disposition!r}; treated as not started",
            )
        if judgement.disposition == DONE_DISPOSITION and not judgement.citations:
            return Judgement(
                NOT_STARTED_DISPOSITION,
                [],
                "the assessor said done and cited nothing; treated as not started",
            )
        return judgement

    def _add_mutations(self, project_id: str, result: AdoptionItem) -> None:
        """Say exactly what reconciliation would change, before it changes it."""
        if result.queue_state is None:
            result.mutations.append(
                ProposedMutation(
                    kind="create queue row",
                    target=f"item {result.item_id} in project {project_id}",
                    detail=(
                        "insert as done if this drop is approved, otherwise pending"
                        if result.requires_drop_approval
                        else f"insert as {result.proposed_state}"
                    ),
                )
            )
        else:
            result.mutations.append(
                ProposedMutation(
                    kind="refresh queue row",
                    target=f"item {result.item_id} in project {project_id}",
                    # The report used to say `R7 -> pending` in its heading and
                    # `state stays failed` in this line, which is the report
                    # contradicting itself two lines apart. Say what will
                    # happen: a rewritten brief revives a stalled item, and
                    # nothing else moves it.
                    detail=(
                        "update title, brief and dependencies; "
                        + (
                            f"the brief changed, so state returns to pending "
                            f"from {result.queue_state}"
                            if revives(result.queue_state or "", result.queue_brief, result.brief)
                            else f"state stays {result.queue_state}"
                        )
                    ),
                )
            )
        if not result.requires_drop_approval:
            return
        for candidate in result.candidates:
            if _can_backfill(candidate):
                result.mutations.append(
                    ProposedMutation(
                        kind="append issue marker",
                        target=(
                            f"issue {candidate.identity}"
                            + (f" in {candidate.repository}" if candidate.repository else "")
                        ),
                        detail=(
                            f"append {MARKER.format(id=result.item_id)} to the existing "
                            f"{len(candidate.body)}-character body; the title, labels, "
                            "milestone, assignees and prose are not touched"
                        ),
                        requires_approval=True,
                    )
                )
            if candidate.kind == "pull_request" and candidate.harness_created:
                result.mutations.append(
                    ProposedMutation(
                        kind="record pull request",
                        target=f"item {result.item_id}",
                        detail=(
                            f"attach {candidate.url or candidate.identity} and branch "
                            f"{candidate.branch}, which carries this item's harness marker"
                        ),
                        requires_approval=True,
                    )
                )

    def _run_verification(self, argv: Sequence[str]) -> Evidence:
        """Run a plan-declared verification under the project-check rules.

        The same `Checks` the executor runs before it pays a reviewer: a
        fixed argv, no shell, a timeout. Adoption gets no separate execution
        path, because a second one is a second thing to get wrong.
        """
        checks = Checks(commands=[list(argv)], timeout=self.verify_timeout)
        rendered = " ".join(argv)
        try:
            result = checks.run(self.repository)
        except CommandRefused as exc:
            # `verify:` is argv read out of a document, and a document is
            # something a model writes. The guard refuses it before it runs;
            # adoption reports that as `unavailable` — the verification did not
            # happen, so it is not evidence the work was done, and adoption's
            # existing rule that uncertainty resolves to "still to do" carries
            # it the rest of the way. Nothing is dropped on a refused check.
            return Evidence(kind=RUNNABLE, outcome="unavailable", detail=exc.refusal.detail)
        if result.ok:
            # Say what happened, not what it means. "succeeded" was read by
            # every reader -- human and report -- as "the work is there", and
            # the command may have run nothing at all.
            return Evidence(
                kind=RUNNABLE,
                outcome=VERIFY_PASSED,
                detail=(
                    f"`{rendered}` exited 0, which says the command did not fail; "
                    "it does not say the command tested anything"
                ),
            )
        # The four not-ok outcomes, mapped to the three adoption already
        # distinguishes. `Checks` classifies these itself now, so a timeout
        # and a missing interpreter no longer arrive here as exceptions this
        # module has to re-derive -- and, more to the point, no longer arrive
        # as the same thing as "the verification says this item is not done".
        #
        # An escalating check is `unavailable`, not `failed`, and the
        # distinction is the whole reason this stage exists: a verification
        # that could not run is not evidence that the work was not done, and
        # uncertainty here has always resolved to "still to do".
        if result.outcome == RETRY:
            return Evidence(
                kind=RUNNABLE,
                outcome=VERIFY_TIMEOUT,
                detail=f"`{rendered}` exceeded {self.verify_timeout:g}s",
            )
        if result.outcome == ESCALATE:
            return Evidence(kind=RUNNABLE, outcome=VERIFY_UNAVAILABLE, detail=result.detail)
        return Evidence(kind=RUNNABLE, outcome=VERIFY_FAILED, detail=result.detail)

    # ------------------------------------------------------------ decision

    def approve(
        self,
        project_id: str,
        *,
        approved_drops: Sequence[str] = (),
        reason: str = "",
    ) -> AdoptionReport:
        """Record the human's exact permission. Absence is never approval."""
        report = self.load(project_id)
        if report.state not in (PROPOSED, APPROVED):
            raise ValueError(f"an adoption in state {report.state!r} cannot be approved")
        proposed = set(report.proposed_drops())
        # An item the queue already holds as done is not re-proposed as a drop
        # -- it is already dropped -- but naming it again must not be an error,
        # or re-running the same approved command would start failing.
        settled = {item.item_id for item in report.items if item.proposed_state == DONE}
        unknown = sorted(set(approved_drops) - proposed - settled)
        if unknown:
            raise ValueError(f"cannot approve unproposed drops: {', '.join(unknown)}")
        report.state = APPROVED
        report.approved_drops = sorted(set(approved_drops))
        report.decision_reason = reason.strip()
        report.history = [*report.history, APPROVED]
        self._save(report)
        self._emit(
            project_id,
            None,
            "adoption_approved",
            detail=(
                f"approved drops: {', '.join(report.approved_drops) or 'none'}; "
                f"proposed: {', '.join(sorted(proposed)) or 'none'}"
                + (f"; reason: {report.decision_reason}" if report.decision_reason else "")
            ),
        )
        return report

    def reject(self, project_id: str, *, reason: str, revise: bool = False) -> AdoptionReport:
        """Refuse a proposal, or send it back for another inspection.

        Both are terminal for *this* proposal: reconciliation is refused
        afterwards, and a revision starts from a fresh `inspect`.
        """
        if not reason.strip():
            raise ValueError("a rejection needs a reason; silence is not a decision")
        report = self.load(project_id)
        report.state = REVISE if revise else REJECTED
        report.decision_reason = reason
        report.history = [*report.history, report.state]
        self._save(report)
        self._emit(project_id, None, f"adoption_{report.state}", detail=reason)
        return report

    # ------------------------------------------------------- reconciliation

    def reconcile(self, project_id: str, *, dry_run: bool = False) -> AdoptionReport:
        """Apply an approved proposal. The first step that changes anything."""
        report = self.load(project_id)
        if report.state != APPROVED:
            raise ValueError(
                f"adoption proposal for {project_id!r} is {report.state}, not approved"
            )
        if dry_run:
            report.dry_run = True
            return report

        # Adoption can start a project from nothing, but the HTTP/browser
        # workflow normally targets a project that is already configured.
        # Re-registering a minimal row in that case would erase its checks,
        # budgets, routes, repository and plan path at the moment a human
        # confirms adoption. Existing configuration is authoritative and is
        # therefore left byte-for-byte alone.
        if self.queue.get_project(project_id) is None:
            self.queue.add_project(
                Project(
                    project_id=project_id,
                    name=project_id,
                    work_dir=str(self.repository),
                    created_at=self.now(),
                )
            )
        approved = set(report.approved_drops)
        self.queue.add(
            [
                WorkRecord(
                    item_id=item.item_id,
                    title=item.title,
                    brief=item.brief,
                    depends_on=item.depends_on,
                    deliverable=item.deliverable,
                    state=DONE if item.item_id in approved else PENDING,
                )
                for item in report.items
            ],
            project_id=project_id,
        )

        for item in report.items:
            if item.item_id in approved:
                self._reconcile_external(project_id, item)
            # Read the state back rather than restating the intent: an item the
            # queue was already holding keeps whatever state it had, and an
            # event claiming otherwise would be the projection disagreeing with
            # the thing it projects.
            landed = self.queue.get(item.item_id, project_id=project_id)
            self._emit(
                project_id,
                item.item_id,
                "adoption_reconciled",
                detail=f"queue state {landed.state if landed else 'missing'}",
            )

        report.state = STOPPED
        report.history = [*report.history, RECONCILED, STOPPED]
        self._save(report)
        self._emit(
            project_id,
            None,
            "adoption_stopped",
            detail=f"{len(report.items)} item(s); {len(approved)} approved as already delivered",
        )
        return report

    def _reconcile_external(self, project_id: str, item: AdoptionItem) -> None:
        """Carry out the approved external mutations, and only those."""
        for candidate in item.candidates:
            if _can_backfill(candidate) and self.external is not None:
                self.external.backfill_marker(candidate, item.item_id)
                self._emit(
                    project_id,
                    item.item_id,
                    "adoption_marker_backfilled",
                    detail=f"issue {candidate.identity}",
                )
            # A pull request enters the queue only when it proves the harness
            # opened it. A similar branch name is a coincidence, and recording
            # it would make the queue assert something nobody verified.
            adopt_pr = (
                candidate.kind == "pull_request" and candidate.harness_created and candidate.url
            )
            if adopt_pr and self.queue.record_pr_url(
                item.item_id, candidate.url, project_id=project_id
            ):
                self._emit(
                    project_id,
                    item.item_id,
                    "adoption_pr_adopted",
                    detail=f"{candidate.url} on {candidate.branch}",
                )

    # -------------------------------------------------------------- events

    def _emit(self, project_id: str, item_id: str | None, outcome: str, detail: str = "") -> None:
        if self.on_event is None:
            return
        self.on_event(
            {
                "ts": self.now(),
                "kind": "work",
                "project_id": project_id,
                "item_id": item_id,
                "outcome": outcome,
                "detail": detail,
            }
        )


def git_branches(repo: Path) -> list[str]:
    """Local branch names, or nothing at all if this is not a git repository.

    Read-only and failure-tolerant on purpose: adoption inspecting a
    directory that turns out not to be a checkout is a report with one fewer
    kind of evidence in it, not an error that stops the inspection.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                "git",
                "-C",
                str(repo),
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _branch_candidates(item: WorkItem, branches: Sequence[str]) -> list[ExternalCandidate]:
    """Branches named for an item, reported and never believed.

    A branch carries no body, so there is nothing in it that could name the
    item other than its own name — which is why every one of these is medium
    confidence and none of them is ever `harness_created`.
    """
    wanted = f"{BRANCH_PREFIX}{item.id}".casefold()
    return [
        ExternalCandidate(
            kind="branch",
            identity=name,
            state="present",
            confidence="medium",
            evidence=(
                "a local branch is named for this item; a branch name is not "
                "proof that the harness created it, and carries no evidence "
                "that the work is finished"
            ),
            branch=name,
        )
        for name in branches
        if name.casefold() == wanted
    ]


def _mentions_id(text: str | None, item_id: str) -> bool:
    """Whether prose names this exact item id, as a word rather than a prefix.

    Substring matching would make `T1` a reference in every mention of `T10`,
    and the first thing that produces is a confident wrong match.
    """
    if not text:
        return False
    lowered = item_id.casefold()
    for token in str(text).casefold().replace("#", " ").split():
        if token.strip(".,;:!?()[]{}<>\"'`") == lowered:
            return True
    return False


def _top_candidates(candidates: Sequence[ExternalCandidate]) -> list[ExternalCandidate]:
    """The candidates at the best confidence available. Anything below the
    top is context for a human, not a competitor for the decision."""
    if not candidates:
        return []
    order = {"high": 0, "medium": 1, "low": 2}
    best = min(order.get(candidate.confidence, 3) for candidate in candidates)
    return [c for c in candidates if order.get(c.confidence, 3) == best]


def _explicit_evidence(
    item: WorkItem, candidates: Sequence[ExternalCandidate], *, ambiguous: bool
) -> Evidence | None:
    """Rung 1: evidence a human or the harness already recorded explicitly."""
    if item.done:
        return Evidence(kind=EXPLICIT, outcome=DONE, detail="the plan item is checked")
    if ambiguous or len(candidates) != 1:
        return None
    candidate = candidates[0]
    if candidate.confidence != "high":
        return None
    terminal = (candidate.kind == "issue" and candidate.state == "closed") or (
        candidate.kind == "pull_request" and candidate.state == "merged"
    )
    if not terminal:
        return None
    return Evidence(
        kind=EXPLICIT,
        outcome=DONE,
        detail=f"{candidate.kind} {candidate.identity} is {candidate.state}: {candidate.evidence}",
    )


def _can_backfill(candidate: ExternalCandidate) -> bool:
    """Whether appending this item's marker to this issue is defensible.

    High confidence for an issue means it already carries the marker or names
    the item id exactly in its body. The first needs nothing; the second is a
    statement by whoever wrote it that this issue is that item, and writing
    the marker only makes it machine-readable. A title that merely looks alike
    is not that statement, and gets no edit.

    The same predicate decides what the report promises and what
    reconciliation does, so the two cannot disagree.
    """
    return (
        candidate.kind == "issue"
        and candidate.confidence == "high"
        and not candidate.marker_present
    )


def _propose_done(result: AdoptionItem) -> None:
    """Propose — never decide. The flag is what a human is asked to approve."""
    result.proposed_state = DONE
    result.requires_drop_approval = True


def _propose_unconfirmed_drop(result: AdoptionItem) -> None:
    """A `verify:` exited 0 and nothing else agrees that the work is there.

    The item is still offered to `--approve-drop`, because the person reading
    the report may know the command is a real one — that is the whole shape of
    "a proposal is never a decision", and taking the option away would make
    adoption unable to record something true.

    What it does *not* do is propose `done`. `proposed_state` stays `pending`,
    so the outcome of approving nothing is that the work is still to do rather
    than silently gone (#149). The two flags say different things on purpose:
    one is what a human may confirm, the other is what happens if they say
    nothing, and the answer to silence is always "still to do".
    """
    result.requires_drop_approval = True


def report_from_dict(raw: Mapping[str, Any]) -> AdoptionReport:
    items = []
    for item_raw in raw.get("items") or []:
        data = dict(item_raw)
        data["evidence"] = [Evidence(**entry) for entry in data.get("evidence") or []]
        data["candidates"] = [ExternalCandidate(**entry) for entry in data.get("candidates") or []]
        data["mutations"] = [ProposedMutation(**entry) for entry in data.get("mutations") or []]
        items.append(AdoptionItem(**data))
    return AdoptionReport(
        project_id=str(raw["project_id"]),
        state=str(raw["state"]),
        repository=str(raw["repository"]),
        created_at=float(raw["created_at"]),
        dry_run=bool(raw.get("dry_run", False)),
        items=items,
        plan_path=str(raw.get("plan_path") or ""),
        configured_repo=(str(raw["configured_repo"]) if raw.get("configured_repo") else None),
        input_digest=str(raw.get("input_digest") or ""),
        inspect_remote=bool(raw.get("inspect_remote", False)),
        approved_drops=[str(value) for value in raw.get("approved_drops") or []],
        history=[str(value) for value in raw.get("history") or []]
        or [DRAFT, INSPECTING, str(raw["state"])],
        decision_reason=str(raw.get("decision_reason") or ""),
    )


def _inspection_signature(report: AdoptionReport) -> dict[str, Any]:
    """The findings, with everything a decision or a clock could change removed."""
    raw = report.to_dict()
    for key in ("created_at", "state", "history", "approved_drops", "dry_run", "decision_reason"):
        raw.pop(key, None)
    return raw
