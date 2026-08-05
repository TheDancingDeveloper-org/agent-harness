"""Stage C: adoption is read-only inspection followed by approved reconciliation.

Every assertion here is on something a user or an operator can see: the
report, the queue rows, the `gh` argv the harness would run, and the
append-only event stream. Nothing reaches into a private attribute to decide
whether adoption worked.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_harness.adoption import (
    APPROVED,
    DRAFT,
    INSPECTING,
    PROPOSED,
    RECONCILED,
    REJECTED,
    REVISE,
    STOPPED,
    Adoption,
    GitHubAdoptionInspector,
    Judgement,
    ModelAssessor,
    parse_judgement,
)
from agent_harness.audit import AuditStore
from agent_harness.github import MARKER, GitHub
from agent_harness.plan import parse_plan
from agent_harness.work import DONE, FAILED, PENDING, WorkQueue, WorkRecord
from stage_a_support import event_sink

PLAN = """\
# Existing project

### T1 — Explicitly complete

A human closed the issue for this and the issue names the item.

### T2 — Runnable evidence

The repository already contains the implementation.

verify: ["python", "-c", "import pathlib; assert pathlib.Path('feature.txt').exists()"]

### T3 — Judged evidence

Needs semantic inspection; no marker and nothing to run.

### T4 — Ambiguous external work

Two equally good candidates. Do not guess between them.

### T5 — Never started

Nothing anywhere refers to this.
"""


# --------------------------------------------------------------- fixtures


class FakeGh:
    """A stateful `gh` that records argv and applies the edits it is given.

    Stateful on purpose: "re-running adoption does not duplicate an issue
    edit" is only observable if the second listing shows the first edit.
    """

    MUTATING = {
        ("issue", "create"),
        ("issue", "edit"),
        ("issue", "close"),
        ("issue", "comment"),
        ("pr", "create"),
        ("pr", "ready"),
        ("pr", "comment"),
        ("label", "create"),
    }

    def __init__(
        self,
        issues: list[dict[str, Any]] | None = None,
        prs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.issues = issues if issues is not None else []
        self.prs = prs if prs is not None else []
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str:
        self.calls.append(list(args))
        if args[1:3] == ["issue", "list"]:
            return json.dumps(self.issues)
        if args[1:3] == ["pr", "list"]:
            return json.dumps(self.prs)
        if args[1:3] == ["issue", "edit"]:
            number = int(args[3])
            for issue in self.issues:
                if issue["number"] == number:
                    issue["body"] = stdin or ""
            return ""
        return ""

    def mutations(self) -> list[list[str]]:
        return [call for call in self.calls if tuple(call[1:3]) in self.MUTATING]


def issue(
    number: int,
    title: str,
    body: str,
    state: str = "CLOSED",
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "labels": [{"name": "human"}],
        "milestone": None,
        "assignees": [{"login": "alice"}],
        "url": f"https://example.invalid/o/r/issues/{number}",
    }


def pull(
    number: int,
    title: str,
    body: str,
    head: str,
    state: str = "MERGED",
    cross_repository: bool = False,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "headRefName": head,
        "url": f"https://example.invalid/o/r/pull/{number}",
        "isCrossRepository": cross_repository,
    }


class AssessorFixture:
    """Deterministic stand-in for the assessor role."""

    def __init__(self, judgements: dict[str, Judgement] | None = None) -> None:
        self.judgements = judgements or {
            "T3": Judgement(
                disposition="done",
                citations=["src/service.py:reconcile", "tests/test_service.py::test_reconcile"],
                rationale="implementation and focused test are present",
            )
        }
        self.asked: list[str] = []

    def assess(self, item: Any, _repo: Path) -> Judgement:
        self.asked.append(item.id)
        return self.judgements.get(item.id, Judgement("not_started", [], "no evidence found"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    (path / "feature.txt").write_text("present\n")
    return path


@pytest.fixture
def queue(tmp_path: Path) -> WorkQueue:
    return WorkQueue(str(tmp_path / "queue.sqlite"), now=lambda: 1000.0)


def default_gh() -> FakeGh:
    return FakeGh(
        issues=[
            issue(17, "Explicitly complete", "Human-authored prose mentioning T1 exactly."),
            issue(18, "Ambiguous external work", "Prose naming T4 exactly.", state="OPEN"),
        ],
        prs=[
            pull(21, "Ambiguous external work", "This finishes T4.", head="feature/t4"),
        ],
    )


def adoption(
    queue: WorkQueue,
    repo: Path,
    *,
    gh: FakeGh | None = None,
    assessor: Any | None = None,
    on_event: Any = None,
    branches: Sequence[str] = (),
    verify_timeout: float = 60.0,
) -> Adoption:
    return Adoption(
        queue,
        repo,
        external=GitHubAdoptionInspector(GitHub("o/r", gh)) if gh is not None else None,
        assessor=AssessorFixture() if assessor is None else assessor,
        branches=lambda _repo: list(branches),
        verify_timeout=verify_timeout,
        on_event=on_event,
        now=lambda: 1000.0,
    )


def by_id(report: Any) -> dict[str, Any]:
    return {item.item_id: item for item in report.items}


# ------------------------------------------------ §5.2 plan verify syntax


def test_item_verification_is_json_argv_and_never_shell_text() -> None:
    item = parse_plan(PLAN).items[1]
    assert item.verification is not None
    assert item.verification[0] == "python"

    for bad in (
        "### B1 — Unsafe\n\nverify: pytest && rm -rf elsewhere\n",
        "### B2 — Empty\n\nverify: []\n",
        '### B3 — Not argv\n\nverify: "pytest -q"\n',
        '### B4 — Blank part\n\nverify: ["pytest", ""]\n',
    ):
        with pytest.raises(ValueError, match="JSON argv"):
            parse_plan(bad)


def test_verification_runs_under_the_project_check_rules(queue: WorkQueue, repo: Path) -> None:
    """No shell, the repository as cwd, and a timeout that actually fires."""
    plan = parse_plan(
        "### S1 — Shell metacharacters are arguments\n\n"
        'verify: ["python", "-c", "import sys; sys.exit(1)", ";", "touch", "pwned"]\n\n'
        "### S2 — Slow\n\n"
        'verify: ["python", "-c", "import time; time.sleep(30)"]\n\n'
        "### S3 — Reads the repository, not the caller\n\n"
        'verify: ["python", "-c", "import pathlib, sys; '
        "sys.exit(0 if pathlib.Path('feature.txt').exists() else 1)\"]\n"
    )
    report = adoption(queue, repo, assessor=None, verify_timeout=0.5).inspect("existing", plan)

    items = by_id(report)
    assert not (repo / "pwned").exists(), "argv was interpreted by a shell"
    assert items["S1"].evidence[0].outcome == "failed"
    assert items["S2"].evidence[0].outcome == "timeout"
    assert "0.5s" in items["S2"].evidence[0].detail
    assert items["S3"].evidence[0].outcome == "passed"
    # It ran in the repository and it exited 0 -- which is what this test is
    # about. What that exit code is worth on its own is the test below.
    assert items["S3"].proposed_state == PENDING
    assert items["S3"].requires_drop_approval is True


# ---------------------------------------------- §5.1 read-only inspection


def test_inspection_is_read_only_and_ranks_its_evidence(queue: WorkQueue, repo: Path) -> None:
    gh = default_gh()
    report = adoption(queue, repo, gh=gh).inspect("existing", parse_plan(PLAN))

    assert queue.projects() == []
    assert queue.items() == []
    assert gh.mutations() == []
    assert report.state == PROPOSED
    assert report.history == [DRAFT, INSPECTING, PROPOSED]

    items = by_id(report)
    assert [e.kind for e in items["T1"].evidence] == ["explicit"]
    assert items["T1"].proposed_state == DONE
    assert items["T1"].requires_drop_approval is True

    # The verification passed, so the ladder does not stop there: the assessor
    # is asked anyway, and its answer is what decides. Both rungs are kept.
    assert [e.kind for e in items["T2"].evidence] == ["runnable", "judged"]
    assert items["T2"].evidence[0].outcome == "passed"
    assert items["T2"].proposed_state == PENDING

    assert [e.kind for e in items["T3"].evidence] == ["judged"]
    assert items["T3"].evidence[0].citations == [
        "src/service.py:reconcile",
        "tests/test_service.py::test_reconcile",
    ]

    assert items["T5"].proposed_state == PENDING
    assert items["T5"].requires_drop_approval is False
    assert report.proposed_drops() == ["T1", "T2", "T3"]


def test_a_dry_run_stores_nothing_at_all(queue: WorkQueue, repo: Path) -> None:
    gh = default_gh()
    adopter = adoption(queue, repo, gh=gh)
    adopter.inspect("existing", parse_plan(PLAN), dry_run=True, persist=False)

    assert gh.mutations() == []
    assert queue.items() == []
    assert queue.projects() == []
    with pytest.raises(ValueError, match="no adoption inspection"):
        adopter.load("existing")


def test_dry_run_reconciliation_performs_no_mutation(queue: WorkQueue, repo: Path) -> None:
    gh = default_gh()
    adopter = adoption(queue, repo, gh=gh)
    adopter.inspect("existing", parse_plan(PLAN))
    adopter.approve("existing", approved_drops=["T1"])

    report = adopter.reconcile("existing", dry_run=True)

    assert report.state == APPROVED
    assert queue.items() == []
    assert queue.projects() == []
    assert gh.mutations() == []


# ------------------------------------------------------- §5.2 the ladder


def test_a_judgement_alone_cannot_drop_work(queue: WorkQueue, repo: Path) -> None:
    adopter = adoption(queue, repo)
    adopter.inspect("existing", parse_plan(PLAN))

    with pytest.raises(ValueError, match="not approved"):
        adopter.reconcile("existing")

    adopter.approve("existing", approved_drops=[])
    adopter.reconcile("existing")

    records = {r.item_id: r for r in queue.items("existing")}
    assert records["T3"].state == PENDING
    assert {r.state for r in records.values()} == {PENDING}


@pytest.mark.parametrize(
    ("judgement", "reason"),
    [
        (Judgement("done", [], "I am confident"), "cited nothing"),
        (Judgement("probably", ["src/x.py"], "hedging"), "unrecognised disposition"),
        (Judgement("partial", ["src/x.py"], "half of it exists"), None),
    ],
)
def test_uncertainty_biases_towards_not_started(
    queue: WorkQueue, repo: Path, judgement: Judgement, reason: str | None
) -> None:
    adopter = adoption(queue, repo, assessor=AssessorFixture({"T3": judgement}))
    report = adopter.inspect("existing", parse_plan(PLAN))

    item = by_id(report)["T3"]
    assert item.proposed_state == PENDING
    assert item.requires_drop_approval is False
    if reason is not None:
        assert reason in item.evidence[0].detail


def test_an_assessor_that_fails_does_not_drop_or_abort(queue: WorkQueue, repo: Path) -> None:
    class Broken:
        def assess(self, item: Any, _repo: Path) -> Judgement:
            raise RuntimeError("route unavailable")

    report = adoption(queue, repo, assessor=Broken()).inspect("existing", parse_plan(PLAN))

    item = by_id(report)["T3"]
    assert item.proposed_state == PENDING
    assert "route unavailable" in item.evidence[0].detail


def test_a_failed_verification_outranks_a_judged_done(queue: WorkQueue, repo: Path) -> None:
    plan = parse_plan(
        "### V1 — Declared verification fails\n\n"
        'verify: ["python", "-c", "import sys; sys.exit(3)"]\n'
    )
    adopter = adoption(
        queue,
        repo,
        assessor=AssessorFixture({"V1": Judgement("done", ["src/v1.py"], "looks present")}),
    )
    report = adopter.inspect("existing", plan)

    item = by_id(report)["V1"]
    assert [e.kind for e in item.evidence] == ["runnable", "judged"]
    assert item.proposed_state == PENDING
    assert item.requires_drop_approval is False
    assert item.ambiguity is not None and "verification failed" in item.ambiguity


#: A test runner asked for a name that does not exist in this tree. Every
#: runner that takes a name filter behaves this way -- `cargo test <name>`,
#: `pytest -k`, `go test -run`, `npm test -- -t`: it reports that it ran
#: nothing, and it exits 0, because not-failing is what it was asked about.
MATCHED_NOTHING = [
    "python",
    "-c",
    "print('running 0 tests'); print('test result: ok. 0 passed; 0 failed')",
]


def unassessed(queue: WorkQueue, repo: Path) -> Adoption:
    """Adoption with no assessor role at all, which is the default `adopt`.

    The `adoption` helper above substitutes the fixture for `assessor=None`,
    so this is the only way to ask what one rung on its own is worth.
    """
    return Adoption(queue, repo, assessor=None, branches=lambda _repo: [], now=lambda: 1000.0)


FILTERED_PLAN = (
    "### R2 — Refuse an insecure-cookie start behind an HTTPS origin\n\n"
    f"verify: {json.dumps(MATCHED_NOTHING)}\n"
)


def test_a_verification_that_matched_nothing_never_drops_the_work(
    queue: WorkQueue, repo: Path
) -> None:
    """The #149 reproduction: exit 0 from a filter that matched no tests.

    The item's work does not exist and its named test was never written, so
    the runner ran zero tests and exited 0. Adoption used to stop the ladder
    right there and report `R2 -> done`, and the sentence under it read as
    evidence -- which is how a real import came within one approval of
    deleting work that then nobody would ever do.

    The command's exit code is still reported, because it is a fact. What it
    no longer does is decide: with nothing corroborating it the item is
    proposed `pending`, and approving the report as it stands leaves the work
    in the queue to be done.
    """
    adopter = unassessed(queue, repo)
    report = adopter.inspect("existing", parse_plan(FILTERED_PLAN))

    item = by_id(report)["R2"]
    assert [e.kind for e in item.evidence] == ["runnable"]
    assert item.evidence[0].outcome == "passed"
    assert "does not say the command tested anything" in item.evidence[0].detail
    assert item.proposed_state == PENDING

    adopter.approve("existing", approved_drops=[])
    adopter.reconcile("existing")
    record = queue.get("R2", project_id="existing")
    assert record is not None and record.state == PENDING


def test_a_passing_verification_alone_is_offered_and_never_asserted(
    queue: WorkQueue, repo: Path
) -> None:
    """It is still a drop a human may name — the report just does not claim it.

    Taking the option away would be the opposite error: the person reading the
    report may know the command is a real one, and adoption would then be
    unable to record something true. So the item stays in `proposed_drops`,
    the summary says out loud that nothing confirmed it, and the state that
    follows from saying nothing is `pending`.
    """
    adopter = unassessed(queue, repo)
    report = adopter.inspect("existing", parse_plan(FILTERED_PLAN))

    assert report.proposed_drops() == ["R2"]
    assert report.unconfirmed_drops() == ["R2"]
    summary = report.summary()
    assert "1 proposed as already delivered (1 unconfirmed)" in summary
    assert "R2 -> pending" in summary
    assert "possible drop, unconfirmed" in summary
    assert "proposed done" not in summary

    adopter.approve("existing", approved_drops=["R2"])
    adopter.reconcile("existing")
    record = queue.get("R2", project_id="existing")
    assert record is not None and record.state == DONE


@pytest.mark.parametrize(
    ("judgement", "expected"),
    [
        (Judgement("done", ["src/gateway/cookies.rs:secure"], "the guard is there"), DONE),
        (Judgement("done", [], "I am sure"), PENDING),
        (Judgement("partial", ["src/gateway/cookies.rs"], "half of it"), PENDING),
        (Judgement("not_started", [], "nothing found"), PENDING),
    ],
)
def test_a_passing_verification_proposes_done_only_with_a_second_rung(
    queue: WorkQueue, repo: Path, judgement: Judgement, expected: str
) -> None:
    """Corroboration is what turns exit 0 into a proposal, not the exit code.

    The harness cannot tell a runner that proved something from one that ran
    nothing without reading that ecosystem's output and guessing at it, which
    is an adapter's job and a wrong guess besides. What it can do is decline
    to decide on one rung: a `done` that cites the code agrees with the
    command, and two rungs are a proposal. A `done` that cites nothing is not
    a rung at all, and anything short of `done` leaves the work to do.
    """
    adopter = adoption(queue, repo, assessor=AssessorFixture({"R2": judgement}))
    report = adopter.inspect("existing", parse_plan(FILTERED_PLAN))

    item = by_id(report)["R2"]
    assert [e.kind for e in item.evidence] == ["runnable", "judged"]
    assert item.proposed_state == expected
    # Either way it is a candidate a human may name; only the default differs.
    assert item.requires_drop_approval is True
    assert item.ambiguity is None


def test_a_model_assessor_parses_conservatively() -> None:
    replies: list[str] = []
    scripted = [
        '```json\n{"disposition": "done", "citations": ["a.py:1"], "rationale": "there"}\n```'
    ]

    def ask(prompt: str) -> str:
        replies.append(prompt)
        return scripted[len(replies) - 1]

    assessor = ModelAssessor(ask)
    item = parse_plan(PLAN).items[2]
    assert assessor.assess(item, Path(".")) == Judgement("done", ["a.py:1"], "there")
    assert "You are judging whether one unit of work" in replies[0]
    assert item.brief() in replies[0]

    scripted.append("I could not tell.")
    assert assessor.assess(item, Path(".")).disposition == "not_started"

    assert parse_judgement('{"disposition": "done", "citations": []}').disposition == "not_started"
    assert parse_judgement("").disposition == "not_started"


# ---------------------------------------- §5.3 issues, branches and PRs


def test_the_report_shows_candidates_confidence_state_and_the_exact_mutation(
    queue: WorkQueue, repo: Path
) -> None:
    gh = default_gh()
    report = adoption(queue, repo, gh=gh).inspect("existing", parse_plan(PLAN))

    t1 = by_id(report)["T1"]
    candidate = t1.candidates[0]
    assert (candidate.kind, candidate.identity, candidate.state) == ("issue", "17", "closed")
    assert candidate.confidence == "high"
    assert "names this item id exactly" in candidate.evidence
    assert candidate.repository == "o/r"

    kinds = {m.kind: m for m in t1.mutations}
    assert set(kinds) == {"create queue row", "append issue marker"}
    marker = kinds["append issue marker"]
    assert marker.target == "issue 17 in o/r"
    assert MARKER.format(id="T1") in marker.detail
    assert "the title, labels, milestone, assignees and prose are not touched" in marker.detail


def test_competing_candidates_are_reported_and_never_guessed(queue: WorkQueue, repo: Path) -> None:
    gh = default_gh()
    report = adoption(queue, repo, gh=gh).inspect("existing", parse_plan(PLAN))

    t4 = by_id(report)["T4"]
    assert {(c.kind, c.identity, c.state) for c in t4.candidates} == {
        ("issue", "18", "open"),
        ("pull_request", "21", "merged"),
    }
    assert t4.ambiguity is not None
    assert "2 competing high-confidence candidates" in t4.ambiguity
    assert t4.proposed_state == PENDING
    assert t4.requires_drop_approval is False
    assert report.ambiguous() == [t4]

    with pytest.raises(ValueError, match="cannot approve unproposed drops: T4"):
        adoption(queue, repo, gh=gh).approve("existing", approved_drops=["T4"])


def test_a_lookalike_branch_is_never_claimed_as_harness_work(queue: WorkQueue, repo: Path) -> None:
    gh = FakeGh(
        issues=[],
        prs=[
            pull(30, "Some human's work", "No item id here.", head="harness/T5", state="OPEN"),
            pull(31, "Forked work", "Closes T1.", head="harness/T1", cross_repository=True),
        ],
    )
    adopter = adoption(queue, repo, gh=gh, assessor=AssessorFixture({}))
    report = adopter.inspect("existing", parse_plan(PLAN))

    lookalike = by_id(report)["T5"].candidates[0]
    assert lookalike.confidence == "medium"
    assert lookalike.harness_created is False
    assert "resembles the harness naming convention" in lookalike.evidence

    forked = by_id(report)["T1"].candidates[0]
    assert forked.confidence == "medium"
    assert forked.harness_created is False
    assert "outside o/r" in forked.evidence
    assert by_id(report)["T1"].proposed_state == PENDING

    adopter.approve("existing", approved_drops=[])
    adopter.reconcile("existing")
    for item_id in ("T1", "T5"):
        record = queue.get(item_id, project_id="existing")
        assert record is not None
        assert record.branch is None and record.pr_url is None


def test_an_existing_branch_is_a_lead_and_never_a_completion(
    tmp_path: Path, queue: WorkQueue
) -> None:
    """Read from a real repository: a branch name is all the evidence there is."""
    work = tmp_path / "checkout"
    work.mkdir()
    (work / "README.md").write_text("fixture\n")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "fixture@example.invalid"),
        ("config", "user.name", "Fixture"),
        ("add", "-A"),
        ("commit", "-q", "-m", "baseline"),
        ("branch", "harness/T5"),
        ("branch", "unrelated/work"),
    ):
        subprocess.run(["git", "-C", str(work), *args], check=True, capture_output=True)

    adopter = Adoption(queue, work, assessor=AssessorFixture({}), now=lambda: 1000.0)
    report = adopter.inspect("existing", parse_plan(PLAN))

    t5 = by_id(report)["T5"]
    assert [(c.kind, c.identity, c.confidence) for c in t5.candidates] == [
        ("branch", "harness/T5", "medium")
    ]
    assert t5.candidates[0].harness_created is False
    assert "not proof that the harness created it" in t5.candidates[0].evidence
    assert t5.proposed_state == PENDING
    assert not any("unrelated/work" in c.identity for item in report.items for c in item.candidates)

    adopter.approve("existing", approved_drops=[])
    adopter.reconcile("existing")
    record = queue.get("T5", project_id="existing")
    assert record is not None
    assert record.branch is None and record.pr_url is None


def test_a_marked_pull_request_is_adopted_with_its_branch(queue: WorkQueue, repo: Path) -> None:
    gh = FakeGh(
        issues=[],
        prs=[
            pull(
                40,
                "Runnable evidence",
                f"Implements it.\n\n{MARKER.format(id='T2')}\n",
                head="harness/t2",
            )
        ],
    )
    adopter = adoption(queue, repo, gh=gh, assessor=AssessorFixture({}))
    report = adopter.inspect("existing", parse_plan(PLAN))

    candidate = by_id(report)["T2"].candidates[0]
    assert candidate.confidence == "high"
    assert candidate.harness_created is True
    assert candidate.branch == "harness/t2"
    assert "record pull request" in {m.kind for m in by_id(report)["T2"].mutations}

    adopter.approve("existing", approved_drops=["T2"])
    adopter.reconcile("existing")

    record = queue.get("T2", project_id="existing")
    assert record is not None
    assert record.pr_url == "https://example.invalid/o/r/pull/40"


def test_marker_backfill_preserves_human_issue_content(queue: WorkQueue, repo: Path) -> None:
    gh = default_gh()
    adopter = adoption(queue, repo, gh=gh)
    adopter.inspect("existing", parse_plan(PLAN))
    adopter.approve("existing", approved_drops=["T1"])
    adopter.reconcile("existing")

    edits = [call for call in gh.mutations() if call[1:3] == ["issue", "edit"]]
    assert len(edits) == 1
    assert edits[0][:4] == ["gh", "issue", "edit", "17"]
    assert "--body-file" in edits[0]
    assert "--title" not in edits[0] and "--add-label" not in edits[0]
    assert gh.issues[0]["body"] == (
        "Human-authored prose mentioning T1 exactly.\n\n<!-- harness:id=T1 -->\n"
    )


def test_a_title_lookalike_issue_is_never_edited(queue: WorkQueue, repo: Path) -> None:
    """The report promises no edit, and reconciliation makes none."""
    gh = FakeGh(
        issues=[issue(50, "Runnable evidence", "Nothing here names any item.", state="OPEN")]
    )
    adopter = adoption(queue, repo, gh=gh, assessor=AssessorFixture({}))
    report = adopter.inspect("existing", parse_plan(PLAN))

    t2 = by_id(report)["T2"]
    assert t2.candidates[0].confidence == "medium"
    assert t2.proposed_state == PENDING
    assert t2.requires_drop_approval is True
    assert {m.kind for m in t2.mutations} == {"create queue row"}

    adopter.approve("existing", approved_drops=["T2"])
    adopter.reconcile("existing")
    assert gh.mutations() == []
    assert gh.issues[0]["body"] == "Nothing here names any item."


def test_prior_harness_attempts_are_evidence_and_history_is_retained(
    tmp_path: Path, queue: WorkQueue, repo: Path
) -> None:
    audit = AuditStore(tmp_path / "audit.sqlite")
    sink = event_sink(audit, source="stage-c")
    # Seeded with the brief the plan carries, so this is a re-sync that changes
    # nothing. A refresh that *rewrites* the brief deliberately does move the
    # state, which the test below this one covers.
    queue.add(
        [
            WorkRecord(
                item_id="T5",
                title="Never started",
                brief="Never started\n\nNothing anywhere refers to this.",
            )
        ],
        project_id="existing",
    )
    queue.set_control("running", project_id="existing")
    claimed = queue.claim("worker-1", project_id="existing")
    assert claimed is not None and claimed.item_id == "T5"
    queue.release(
        "T5", FAILED, error="checks failed twice", owner="worker-1", project_id="existing"
    )
    sink(
        {
            "ts": 900.0,
            "kind": "work",
            "project_id": "existing",
            "item_id": "T5",
            "outcome": "checks_failed",
            "detail": "the original attempt",
        }
    )
    before = audit.since_id(0)

    adopter = adoption(queue, repo, on_event=sink)
    report = adopter.inspect("existing", parse_plan(PLAN))

    t5 = by_id(report)["T5"]
    assert t5.queue_state == FAILED
    assert t5.prior_failure == "checks failed twice"
    assert t5.evidence[0].kind == "prior_attempt"
    assert "1 prior harness attempt(s)" in t5.evidence[0].detail

    adopter.approve("existing", approved_drops=[])
    adopter.reconcile("existing")

    record = queue.get("T5", project_id="existing")
    assert record is not None
    assert record.attempts == 1
    assert record.last_error == "checks failed twice"
    after = audit.since_id(0)
    assert after[: len(before)] == before, "adoption rewrote earlier history"


# ------------------------------------------------------- §5.1 decisions


def test_approval_is_exact_and_reconciliation_needs_it(queue: WorkQueue, repo: Path) -> None:
    adopter = adoption(queue, repo, gh=default_gh())
    adopter.inspect("existing", parse_plan(PLAN))

    with pytest.raises(ValueError, match="cannot approve unproposed drops: T5"):
        adopter.approve("existing", approved_drops=["T5"])

    report = adopter.approve("existing", approved_drops=["T1", "T3"])
    assert report.state == APPROVED
    assert report.approved_drops == ["T1", "T3"]
    assert report.history == [DRAFT, INSPECTING, PROPOSED, APPROVED]

    final = adopter.reconcile("existing")
    assert final.history == [DRAFT, INSPECTING, PROPOSED, APPROVED, RECONCILED, STOPPED]
    states = {r.item_id: r.state for r in queue.items("existing")}
    assert states == {"T1": DONE, "T2": PENDING, "T3": DONE, "T4": PENDING, "T5": PENDING}


@pytest.mark.parametrize(("revise", "expected"), [(False, REJECTED), (True, REVISE)])
def test_a_rejected_proposal_cannot_reconcile(
    queue: WorkQueue, repo: Path, revise: bool, expected: str
) -> None:
    gh = default_gh()
    adopter = adoption(queue, repo, gh=gh)
    adopter.inspect("existing", parse_plan(PLAN))

    report = adopter.reject("existing", reason="T3 is not done", revise=revise)
    assert report.state == expected
    assert report.decision_reason == "T3 is not done"
    assert report.history[-1] == expected

    with pytest.raises(ValueError, match=f"is {expected}, not approved"):
        adopter.reconcile("existing")
    with pytest.raises(ValueError, match="cannot be approved"):
        adopter.approve("existing", approved_drops=[])
    with pytest.raises(ValueError, match="needs a reason"):
        adopter.reject("existing", reason="  ")
    assert queue.items() == []
    assert gh.mutations() == []


def test_a_reconciled_report_says_what_happened(queue: WorkQueue, repo: Path) -> None:
    adopter = adoption(queue, repo, gh=default_gh())
    adopter.inspect("existing", parse_plan(PLAN))
    adopter.approve("existing", approved_drops=["T1"])
    text = adopter.reconcile("existing").summary()

    assert "applied append issue marker issue 17 in o/r" in text
    assert "did NOT (unapproved) create queue row" not in text
    t3_marker = [line for line in text.splitlines() if "record pull request" in line]
    assert t3_marker == []
    assert "would " not in text


# ------------------------------------------------------- §5.4 idempotence


def test_repeated_inspection_produces_the_same_report(queue: WorkQueue, repo: Path) -> None:
    adopter = adoption(queue, repo, gh=default_gh())
    first = adopter.inspect("existing", parse_plan(PLAN))
    second = adopter.inspect("existing", parse_plan(PLAN))

    assert first.to_dict() == second.to_dict()
    assert first.content_digest() == second.content_digest()


def test_repeated_adoption_reaches_a_fixed_point_without_losing_progress(
    queue: WorkQueue, repo: Path
) -> None:
    gh = default_gh()
    plan = parse_plan(PLAN)

    def cycle() -> Any:
        adopter = adoption(queue, repo, gh=gh)
        report = adopter.inspect("existing", plan)
        adopter.approve("existing", approved_drops=["T1", "T3"])
        adopter.reconcile("existing")
        return report

    first = cycle()
    # Real progress made by the fleet between adoptions.
    queue.release("T4", DONE, project_id="existing")
    second = cycle()
    third = cycle()

    # The findings settle: from the second adoption onwards the report is
    # byte-identical, which is what "repeated adoption produces the same
    # report" means once the queue exists.
    assert second.to_dict() == third.to_dict()
    assert second.content_digest() == third.content_digest()
    assert first.proposed_drops() == ["T1", "T2", "T3"]

    # No duplicated issues, and the one marker backfill was not repeated.
    assert [call[1:3] for call in gh.mutations()] == [["issue", "edit"]]
    assert not any(call[1:3] == ["issue", "create"] for call in gh.calls)

    # No reset progress and no dropped completed work.
    states = {r.item_id: r.state for r in queue.items("existing")}
    assert states == {"T1": DONE, "T2": PENDING, "T3": DONE, "T4": DONE, "T5": PENDING}
    assert len(queue.items("existing")) == 5
    settled = by_id(third)
    assert settled["T4"].queue_state == DONE
    assert settled["T4"].proposed_state == DONE
    assert settled["T4"].requires_drop_approval is False


def test_adoption_appends_events_a_projection_can_read(
    tmp_path: Path, queue: WorkQueue, repo: Path
) -> None:
    audit = AuditStore(tmp_path / "audit.sqlite")
    adopter = adoption(queue, repo, gh=default_gh(), on_event=event_sink(audit, source="stage-c"))

    adopter.inspect("existing", parse_plan(PLAN))
    adopter.approve("existing", approved_drops=["T1"])
    adopter.reconcile("existing")

    rows = audit.since_id(0)
    outcomes = [row["outcome"] for row in rows]
    # T1 (explicit) and T3 (judged) are proposed done; T2's verification
    # passed and nothing corroborated it, so it is proposed pending like T5.
    assert outcomes.count("adoption_proposed_done") == 2
    assert outcomes.count("adoption_proposed_pending") == 2
    assert outcomes.count("adoption_ambiguous") == 1
    assert outcomes.index("adoption_approved") < outcomes.index("adoption_marker_backfilled")
    assert outcomes[-1] == "adoption_stopped"
    assert all(row["project_id"] == "existing" for row in rows)
    marker_row = next(row for row in rows if row["outcome"] == "adoption_marker_backfilled")
    assert marker_row["item_id"] == "T1"
    reconciled = {
        row["item_id"]: json.loads(row["data"])["detail"]
        for row in rows
        if row["outcome"] == "adoption_reconciled"
    }
    assert reconciled == {
        "T1": "queue state done",
        "T2": "queue state pending",
        "T3": "queue state pending",
        "T4": "queue state pending",
        "T5": "queue state pending",
    }


def test_report_is_storable_and_reviewable(queue: WorkQueue, repo: Path) -> None:
    report = adoption(queue, repo, gh=default_gh()).inspect("existing", parse_plan(PLAN))

    restored = json.loads(json.dumps(report.to_dict()))
    assert restored["project_id"] == "existing"
    assert restored["items"][0]["candidates"][0]["identity"] == "17"

    text = report.summary()
    assert (
        "5 plan item(s); 3 proposed as already delivered (1 unconfirmed); "
        "1 needing a human decision"
    ) in text
    assert "would append issue marker issue 17 in o/r" in text


# --------------------------------------------------------------- the CLI


def write_plan(tmp_path: Path) -> Path:
    path = tmp_path / "PLAN.md"
    path.write_text(PLAN)
    return path


def test_adopt_cli_defaults_to_read_only_inspection(
    tmp_path: Path, repo: Path, capsys: Any
) -> None:
    from agent_harness.__main__ import main

    db = tmp_path / "queue.sqlite"
    code = main(
        [
            "--db",
            str(db),
            "adopt",
            str(write_plan(tmp_path)),
            "--project",
            "existing",
            "--work",
            str(repo),
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "inspection only: no queue rows, issue edits" in out
    assert "proposed as already delivered and NOT dropped: T2" in out
    assert WorkQueue(str(db)).items() == []


def test_adopt_cli_writes_a_report_and_reconciles_only_what_was_approved(
    tmp_path: Path, repo: Path
) -> None:
    from agent_harness.__main__ import main

    db = tmp_path / "queue.sqlite"
    report_path = tmp_path / "reports" / "adoption.json"
    plan = write_plan(tmp_path)
    base = ["--db", str(db), "adopt", str(plan), "--project", "existing", "--work", str(repo)]

    assert main([*base, "--report", str(report_path)]) == 0
    assert json.loads(report_path.read_text())["state"] == "proposed"

    assert main([*base, "--approve", "--approve-drop", "T2", "--reconcile"]) == 0
    states = {r.item_id: r.state for r in WorkQueue(str(db)).items("existing")}
    assert states == {"T1": PENDING, "T2": DONE, "T3": PENDING, "T4": PENDING, "T5": PENDING}


def test_adopt_cli_refuses_to_mutate_without_an_explicit_decision(
    tmp_path: Path, repo: Path, capsys: Any
) -> None:
    from agent_harness.__main__ import main

    plan = write_plan(tmp_path)
    base = [
        "--db",
        str(tmp_path / "q.sqlite"),
        "adopt",
        str(plan),
        "--project",
        "existing",
        "--work",
        str(repo),
    ]

    assert main([*base, "--reconcile"]) == 2
    assert "requires --approve" in capsys.readouterr().err

    assert main([*base, "--approve-drop", "T2"]) == 2
    assert "--approve-drop requires --approve" in capsys.readouterr().err

    assert main([*base, "--approve", "--reject", "no"]) == 2
    assert "opposite decisions" in capsys.readouterr().err

    assert main([*base, "--dry-run", "--approve"]) == 2
    assert "stores nothing" in capsys.readouterr().err

    assert main([*base, "--assessor-model", "some-model"]) == 2
    assert "needs --endpoint" in capsys.readouterr().err


def test_adopt_cli_dry_run_leaves_no_trace(tmp_path: Path, repo: Path, capsys: Any) -> None:
    from agent_harness.__main__ import main

    db = tmp_path / "queue.sqlite"
    code = main(
        [
            "--db",
            str(db),
            "adopt",
            str(write_plan(tmp_path)),
            "--project",
            "existing",
            "--work",
            str(repo),
            "--dry-run",
        ]
    )

    assert code == 0
    assert "project existing: proposed" in capsys.readouterr().out
    stored = WorkQueue(str(db))
    assert stored.items() == []
    assert stored.get_setting("adoption:existing") is None


def test_a_rewritten_brief_revives_a_failed_item_and_the_report_says_so(
    tmp_path: Path, queue: WorkQueue, repo: Path
) -> None:
    """#178: the half of the refusal loop that acts on the human's answer.

    The report used to say `T5 -> pending` in its heading and `state stays
    failed` in the mutation line two lines below, and the second one was true.
    A human who rewrote an item in response to an agent refusing it as
    impossible was then told "nothing to do".
    """
    queue.add(
        [WorkRecord(item_id="T5", title="Never started", brief="the original wording")],
        project_id="existing",
    )
    queue.set_control("running", project_id="existing")
    queue.claim("worker-1", project_id="existing")
    queue.release(
        "T5", FAILED, error="cannot be done as specified", owner="worker-1", project_id="existing"
    )

    adopter = adoption(queue, repo)
    report = adopter.inspect("existing", parse_plan(PLAN))

    t5 = by_id(report)["T5"]
    refresh = next(m for m in t5.mutations if m.kind == "refresh queue row")
    assert "state returns to pending" in refresh.detail
    assert "state stays" not in refresh.detail

    adopter.approve("existing", approved_drops=[])
    adopter.reconcile("existing")

    record = queue.get("T5", project_id="existing")
    assert record is not None
    assert record.state == PENDING
    assert not record.last_error
    # The attempt itself is history and stays history. What changed is the
    # question, not the fact that it was once attempted.
    assert record.attempts == 1
