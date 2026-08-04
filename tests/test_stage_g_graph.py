"""Stage G: one typed, revisioned dependency-graph contract.

The acceptance criterion in `docs/PROPOSAL-2026-08-fit-for-purpose.md` §6.1 is
a single plan containing five cases -- metadata dependencies, arrow notation,
an external target, a missing target and a cycle -- each of which must produce
an **explicit report**, plus a ready set that survives re-ingestion and a
mid-flight correction that is observable without ineligible work committing.

That plan is `STAGE_G_PLAN` below, and it is used by every case here, so the
five are proven against one document rather than five convenient fragments.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.api import create_api
from agent_harness.audit import AuditStore
from agent_harness.executor import Executor
from agent_harness.graph import (
    BLOCKED,
    CROSS_PROJECT_WORK,
    EXTERNAL_REFERENCE,
    HUMAN_DECISION,
    LOCAL_WORK,
    SATISFIED,
    UNRESOLVED,
    ExternalTarget,
    ResolverOutcome,
)
from agent_harness.plan import parse_plan
from agent_harness.session_executor import AgentSpec, SessionExecutor
from agent_harness.store import EventStore
from agent_harness.work import DONE, PENDING, Project, WorkQueue, WorkRecord
from stage_a_support import (
    DeterministicTransport,
    FixtureRepository,
    Reply,
    event_sink,
    generated_repository,
    git,
)
from test_stage_a_e2e import EditingSessionHost, add_item, checks_for, client_for

TOKEN = "stage-g-token"  # noqa: S105 - deterministic test credential
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: One plan, all five §6.1 cases.
#:
#:   G1  nothing depends on it -- the root the others need
#:   G2  (a) metadata dependency, `depends on: G1`
#:   G3  (b) arrow notation, declared in the ```dependencies block
#:   G4  (c) an external target with an explicit resolver
#:   G5  (d) a missing target -- G404 is not in this plan
#:   G6/G7 (e) a cycle: each requires the other
STAGE_G_PLAN = """\
# Stage G fixture plan

Narrative, not work.

```dependencies
G1 -> G3
```

### G1: Lay the foundation

The root item nothing waits on.

### G2: Build on the foundation

depends on: G1

### G3: Also build on the foundation

Its dependency is declared by the arrow block above, not here.

### G4: Ship once the tracker ticket closes

depends on: external:demo-tracker:TICKET-9

### G5: Depend on something that is not here

depends on: G404

### G6: One half of a loop

depends on: G7

### G7: The other half of a loop

depends on: G6
"""


def records_from(plan_text: str = STAGE_G_PLAN) -> list[WorkRecord]:
    parsed = parse_plan(plan_text)
    return [
        WorkRecord(item_id=i.id, title=i.title, brief=i.brief(), depends_on=list(i.depends_on))
        for i in parsed.deduplicated()
    ]


@pytest.fixture
def queue(tmp_path: Path) -> WorkQueue:
    queue = WorkQueue(str(tmp_path / "queue.sqlite"), lease_seconds=100.0)
    queue.add_project(Project(project_id="g", name="Stage G"))
    queue.set_control("running", project_id="g")
    queue.add(records_from(), project_id="g")
    return queue


@pytest.fixture
def api(tmp_path: Path, queue: WorkQueue) -> TestClient:
    store = EventStore(tmp_path / "events.sqlite")
    return TestClient(create_api(store, queue=queue, token=TOKEN))


# ------------------------------------------------------- §6.1 case (a) + (b)


def test_a_metadata_dependency_is_a_typed_required_local_edge(queue: WorkQueue) -> None:
    """Case (a): `depends on: G1` in the prose."""
    edges = queue.graph.edges("g", "G2")

    assert [(e.target_kind, e.target_id, e.required) for e in edges] == [(LOCAL_WORK, "G1", True)]
    assert edges[0].state == BLOCKED
    assert edges[0].evidence == "G1 is pending, not done"
    assert queue.readiness("G2", project_id="g").ready is False


def test_arrow_notation_declares_the_same_kind_of_edge_as_metadata(queue: WorkQueue) -> None:
    """Case (b): `G1 -> G3` in a ```dependencies block.

    The arrow follows the work: the left side is the prerequisite. Both
    notations have to produce the same edge, or a plan's two halves would
    gate differently.
    """
    parsed = parse_plan(STAGE_G_PLAN)
    assert {i.id: i.depends_on for i in parsed.items}["G3"] == ["G1"]

    metadata = queue.graph.edges("g", "G2")[0]
    arrow = queue.graph.edges("g", "G3")[0]
    assert (arrow.target_kind, arrow.target_id, arrow.required) == (
        metadata.target_kind,
        "G1",
        metadata.required,
    )


def test_an_arrow_naming_no_item_is_reported_rather_than_dropped() -> None:
    """An arrow that lands nowhere is the one outcome worse than a refusal."""
    parsed = parse_plan("```dependencies\nG1 -> NOPE\nnot an arrow at all\n```\n\n### G1: One\n")

    report = parsed.dependency_report()
    assert [text for _line, text in report.unattached_arrows] == [
        "not an arrow at all",
        "G1 -> NOPE",
    ]
    assert any("names no item" in line for line in report.lines())


# ------------------------------------------------------------- §6.1 case (c)


def test_an_external_target_is_reported_with_its_kind_and_resolver_outcome(
    queue: WorkQueue,
) -> None:
    """Case (c). An external reference is legitimate and is NOT satisfied by
    being external: it needs an explicit kind and an answer from a resolver."""
    edge = queue.graph.edges("g", "G4")[0]

    assert edge.target_kind == EXTERNAL_REFERENCE
    assert edge.resolver == "demo-tracker"
    assert edge.target_id == "TICKET-9"
    assert edge.state == UNRESOLVED
    assert "has not reported an outcome" in edge.evidence
    claimed = queue.claim("w", project_id="g")
    assert claimed is None or claimed.item_id != "G4"

    def closed(target: ExternalTarget) -> ResolverOutcome:
        return ResolverOutcome(SATISFIED, f"{target.identity} closed in the tracker")

    outcomes = queue.graph.resolve_external("g", resolvers={"demo-tracker": closed})
    assert [o.state for _edge, o in outcomes if o] == [SATISFIED]
    assert queue.readiness("G4", project_id="g").ready is True
    assert queue.graph.edges("g", "G4")[0].evidence.startswith("resolver 'demo-tracker'")


def test_an_external_target_with_no_resolver_stays_unresolved(tmp_path: Path) -> None:
    """`external:` with nothing to answer it is a blocker that says so,
    not a silent pass."""
    queue = WorkQueue(str(tmp_path / "q.sqlite"))
    queue.add_project(Project(project_id="g", name="G"))
    queue.set_control("running", project_id="g")
    queue.add([WorkRecord(item_id="X", title="x", depends_on=["external:TICKET-9"])], "g")

    reason = queue.readiness("X", project_id="g").reasons[0]
    assert reason.state == UNRESOLVED
    assert "does not name a resolver" in reason.explanation
    assert queue.claim("w", project_id="g") is None


def test_a_failing_resolver_leaves_the_edge_unresolved_not_satisfied(queue: WorkQueue) -> None:
    """A resolver that cannot be reached is an answer of "I do not know",
    and "I do not know" is a blocker."""

    def broken(_target: ExternalTarget) -> ResolverOutcome:
        raise RuntimeError("tracker is down")

    queue.graph.resolve_external("g", resolvers={"demo-tracker": broken})

    edge = queue.graph.edges("g", "G4")[0]
    assert edge.state == UNRESOLVED
    assert "tracker is down" in edge.evidence


# ------------------------------------------------------------- §6.1 case (d)


def test_a_missing_target_is_an_explicit_blocker_with_the_id_that_was_missed(
    queue: WorkQueue,
) -> None:
    """Case (d). This is the behaviour Stage G exists to change: a target the
    graph cannot find used to be treated as satisfied."""
    state = queue.readiness("G5", project_id="g")

    assert state.ready is False
    assert [(r.target_kind, r.target_id) for r in state.reasons] == [(LOCAL_WORK, "G404")]
    assert "no item 'G404' in project 'g'" in state.reasons[0].explanation
    assert "not an assumed external dependency" in state.reasons[0].explanation
    assert parse_plan(STAGE_G_PLAN).unresolved_dependencies() == {"G5": ["G404"]}


# ------------------------------------------------------------- §6.1 case (e)


def test_a_cycle_is_named_as_a_cycle_rather_than_as_two_items_waiting(
    queue: WorkQueue,
) -> None:
    """Case (e). One item at a time, a cycle is invisible: each member merely
    looks like it is waiting for the other."""
    assert queue.graph.cycles("g") == [("G6", "G7")]

    for item_id in ("G6", "G7"):
        reasons = queue.readiness(item_id, project_id="g").reasons
        cycle = [r for r in reasons if r.kind == "cycle"]
        assert cycle, (item_id, reasons)
        assert cycle[0].evidence == "G6 -> G7 -> G6"

    assert parse_plan(STAGE_G_PLAN).cycles() == [("G6", "G7")]


def test_the_report_covers_all_five_cases_in_one_answer(queue: WorkQueue) -> None:
    """§6.1 asks for an explicit report for each case, from one plan."""
    report = queue.graph.report("g")

    assert report.ready == ("G1",)
    not_ready = {state.item_id: state for state in report.not_ready}
    assert set(not_ready) == {"G2", "G3", "G4", "G5", "G6", "G7"}
    assert not_ready["G2"].reasons[0].target_id == "G1"  # (a) metadata
    assert not_ready["G3"].reasons[0].target_id == "G1"  # (b) arrow
    assert not_ready["G4"].reasons[0].target_kind == EXTERNAL_REFERENCE  # (c)
    assert not_ready["G5"].reasons[0].state == UNRESOLVED  # (d)
    assert report.cycles == (("G6", "G7"),)  # (e)


def test_the_api_publishes_the_same_report_under_a_named_schema(api: TestClient) -> None:
    """The API is a public surface: a graph a client cannot read is a graph
    only this process can act on."""
    with api as client:
        payload = client.get("/api/graph?project_id=g", headers=AUTH).json()
        readiness = client.get("/api/work/G5/readiness?project_id=g", headers=AUTH).json()
        schema = client.get("/openapi.json").json()

    assert payload["ready"] == ["G1"]
    assert payload["cycles"] == [["G6", "G7"]]
    kinds = {e["source_item"]: e["target_kind"] for e in payload["edges"]}
    assert kinds["G4"] == EXTERNAL_REFERENCE
    assert readiness["ready"] is False
    assert readiness["reasons"][0]["target_id"] == "G404"
    assert readiness["graph_revision"] == payload["revision"]

    for route in ("/api/graph", "/api/work/{item_id}/readiness"):
        content = schema["paths"][route]["get"]["responses"]["200"]["content"]
        assert content["application/json"]["schema"]["$ref"].startswith("#/components/schemas/")
    edge_schema = schema["components"]["schemas"]["DependencyEdgeModel"]["properties"]
    assert all(field.get("description") for field in edge_schema.values())
    assert "NEVER a synonym" in edge_schema["state"]["description"]


# ------------------------------------------ ready set survives re-ingestion


def test_re_ingesting_the_same_plan_changes_neither_the_ready_set_nor_the_revision(
    queue: WorkQueue,
) -> None:
    """§6.1: "the ready set is correct after re-ingestion".

    A revision that ticked on every sync would tell a reader the graph moved
    when nothing did -- and the pre-gate check would then invalidate live
    claims for no reason at all.
    """
    before = queue.graph.report("g")

    queue.add(records_from(), project_id="g")

    after = queue.graph.report("g")
    assert after.revision == before.revision
    assert after.ready == before.ready
    assert [e.describe() for e in after.edges] == [e.describe() for e in before.edges]


def test_finishing_a_dependency_moves_the_ready_set_without_moving_the_revision(
    queue: WorkQueue,
) -> None:
    """Work finishing is work state, not a graph change. Conflating the two
    would make every completion look like a plan correction."""
    revision = queue.graph.revision("g")
    claimed = queue.claim("worker", project_id="g")
    assert claimed is not None and claimed.item_id == "G1"
    queue.release("G1", DONE, owner="worker", project_id="g")

    report = queue.graph.report("g")
    assert set(report.ready) == {"G1", "G2", "G3"}
    assert report.revision == revision


def test_re_ingesting_a_corrected_plan_moves_the_revision_and_the_ready_set(
    queue: WorkQueue,
) -> None:
    """The other half: a plan that really did change must move the revision."""
    revision = queue.graph.revision("g")
    corrected = STAGE_G_PLAN.replace("depends on: G404", "depends on: G1")

    queue.add(records_from(corrected), project_id="g")

    assert queue.graph.revision("g") > revision
    assert queue.graph.edges("g", "G5")[0].target_id == "G1"


# ------------------------------------------------- mid-flight graph correction


@pytest.fixture
def fixture_repo(tmp_path: Path) -> FixtureRepository:
    return generated_repository(tmp_path / "project")


def test_a_midflight_correction_to_a_missing_target_stops_the_commit_and_says_why(
    fixture_repo: FixtureRepository, tmp_path: Path
) -> None:
    """§6.1: "a mid-flight graph correction is observable without silently
    committing ineligible work".

    The correction here is the Stage G case specifically: an item gains a
    dependency on something the graph cannot find. Under the old rule that
    was equivalent to satisfied, and this work would have committed.
    """
    queue = WorkQueue(str(tmp_path / "queue.sqlite"), lease_seconds=100.0)
    queue.add_project(Project(project_id="fixture", name="Fixture", max_workers=2))
    queue.set_control("running", project_id="fixture")
    add_item(queue)
    audit = AuditStore(tmp_path / "audit.sqlite")
    sink = event_sink(audit)

    def correct_graph() -> None:
        """An operator edits the plan while the agent is working."""
        queue.add(
            [WorkRecord(item_id="A1", title="t", brief="b", depends_on=["A404"])],
            project_id="fixture",
        )

    transport = DeterministicTransport(
        {
            "planner": Reply("plan"),
            "implementer": Reply(fixture_repo.canonical_change(), before=correct_graph),
            "reviewer": Reply("APPROVED"),
        }
    )
    executor = Executor(
        queue,
        client_for(transport, events=sink),
        fixture_repo.root,
        checks=checks_for(fixture_repo),
        push=False,
        on_event=sink,
        project_id="fixture",
    )
    outcome = executor.run_once()

    assert outcome is not None and outcome.state == PENDING
    assert "A404" in outcome.reason and "unresolved" in outcome.reason
    assert "admitted at graph revision" in outcome.reason
    # Nothing durable was produced: no branch, and no reviewer spend.
    assert git(fixture_repo.root, "branch", "--list", "harness/a1").strip() == ""
    rows = audit.since_id(0)
    assert any(row["outcome"] == "dependency_invalidated" for row in rows)
    assert not any(row["role"] == "reviewer" for row in rows)
    # The agent was not killed -- it ran to completion and produced its patch.
    assert any(row["role"] == "implementer" and row["outcome"] == "ok" for row in rows)


def test_the_session_executor_makes_the_same_check_before_its_own_checkpoint(
    fixture_repo: FixtureRepository, tmp_path: Path
) -> None:
    """Session mode reached its checkpoint without re-reading the graph at all.

    That was the one place the two executors disagreed about a gate: a plan
    corrected while an agent was working produced a durable, externally
    visible candidate for work that was no longer eligible. The agent is
    still not killed -- it finishes, and the item goes back to pending.
    """
    queue = WorkQueue(str(tmp_path / "queue.sqlite"), lease_seconds=100.0)
    queue.add_project(Project(project_id="fixture", name="Fixture", max_workers=1))
    queue.set_control("running", project_id="fixture")
    add_item(queue)
    audit = AuditStore(tmp_path / "audit.sqlite")
    sink = event_sink(audit)

    def edit(tree: Path) -> None:
        # The operator corrects the plan while the agent is in its session.
        queue.add(
            [WorkRecord(item_id="A1", title="t", brief="b", depends_on=["A404"])],
            project_id="fixture",
        )
        operations = tree / "src/mathkit/operations.py"
        operations.write_text(
            operations.read_text()
            + "\n\ndef multiply(left: int, right: int) -> int:\n    return left * right\n"
        )

    executor = SessionExecutor(
        queue,
        EditingSessionHost(edit),
        fixture_repo.root,
        agent=AgentSpec(command=("fixture-agent", "{prompt_file}"), poll_seconds=0),
        checks=checks_for(fixture_repo),
        reviewer=None,
        worktrees=tmp_path / "worktrees",
        push=False,
        on_event=sink,
        project_id="fixture",
    )
    outcome = executor.run_once()

    assert outcome is not None and outcome.state == PENDING
    assert "A404" in outcome.reason
    assert "commit" not in outcome.stages, "an ineligible candidate must not be checkpointed"
    rows = audit.since_id(0)
    assert any(row["outcome"] == "dependency_invalidated" for row in rows)
    assert not any(row["outcome"] == "checkpointed" for row in rows)
    # The agent ran to completion rather than being killed.
    assert any(row["outcome"] == "agent_finished" for row in rows)


def test_admission_and_the_pre_gate_check_name_the_same_authoritative_revision(
    queue: WorkQueue,
) -> None:
    """§6: both must use the same authoritative graph revision.

    Not "both must be right" -- both must be able to say which graph they
    looked at, or a disagreement is indistinguishable from a race.
    """
    claimed = queue.claim("worker", project_id="g")
    assert claimed is not None and claimed.item_id == "G1"
    assert claimed.admitted_revision == queue.graph.revision("g")
    assert queue.readiness("G1", project_id="g").revision == claimed.admitted_revision

    queue.add(
        [WorkRecord(item_id="G1", title="Lay the foundation", depends_on=["G404"])],
        project_id="g",
    )

    after = queue.readiness("G1", project_id="g")
    assert after.ready is False
    assert after.revision > claimed.admitted_revision
    stored = queue.get("G1", project_id="g")
    assert stored is not None and stored.admitted_revision == claimed.admitted_revision


def test_an_operator_override_unblocks_exactly_one_revision(queue: WorkQueue) -> None:
    """§6: the gate lifts on a resolved dependency OR an explicit, recorded
    override -- and the override does not survive the next correction."""
    assert queue.readiness("G5", project_id="g").ready is False

    queue.graph.record_override("g", "G5", reason="tracked in the other repo", who="ops")

    state = queue.readiness("G5", project_id="g")
    assert state.ready is True and state.overridden is True
    assert state.override_reason is not None and "ops" in state.override_reason
    # The edge is NOT marked satisfied. The gate still reports the truth.
    assert queue.graph.edges("g", "G5")[0].state == UNRESOLVED
    claimed = queue.claim("worker", project_id="g")
    assert claimed is not None and claimed.item_id in {"G1", "G5"}

    queue.add([WorkRecord(item_id="G5", title="t", depends_on=["G404", "G405"])], project_id="g")
    assert queue.readiness("G5", project_id="g").ready is False


def test_the_override_route_records_who_and_reports_the_new_readiness(api: TestClient) -> None:
    with api as client:
        refused = client.post(
            "/api/work/G5/dependency-override?project_id=g", headers=AUTH, json={}
        )
        response = client.post(
            "/api/work/G5/dependency-override?project_id=g",
            headers=AUTH,
            json={"reason": "tracked in the other repo", "who": "ops"},
        )
        missing = client.post(
            "/api/work/NOPE/dependency-override?project_id=g",
            headers=AUTH,
            json={"reason": "x"},
        )

    assert refused.status_code == 422, "an override with no reason is not an override"
    assert missing.status_code == 404
    payload = response.json()
    assert payload["ok"] is True
    assert payload["readiness"]["ready"] is True
    assert payload["readiness"]["overridden"] is True
    assert "ops" in payload["readiness"]["override_reason"]


# ---------------------------------------------------- other target kinds


def test_a_human_decision_must_exist_as_work_before_it_can_be_made(tmp_path: Path) -> None:
    """The queue already parks a decision as a `blocked` item. A decision
    target that names nothing cannot be satisfied by anyone."""
    queue = WorkQueue(str(tmp_path / "q.sqlite"))
    queue.add_project(Project(project_id="p", name="P"))
    queue.set_control("running", project_id="p")
    queue.add([WorkRecord(item_id="X", title="x", depends_on=["decision:D9"])], "p")

    reason = queue.readiness("X", project_id="p").reasons[0]
    assert reason.target_kind == HUMAN_DECISION
    assert "has to exist before it can be made" in reason.explanation

    queue.add([WorkRecord(item_id="D9", title="Which database?")], "p")
    assert queue.readiness("X", project_id="p").reasons[0].state == BLOCKED
    queue.claim("w", project_id="p")
    queue.release("D9", DONE, project_id="p")
    assert queue.readiness("X", project_id="p").ready is True


def test_a_cross_project_target_resolves_in_the_project_it_names(tmp_path: Path) -> None:
    """Ids are unique only within a project, so crossing the boundary has to
    be spelled out -- a bare id must never resolve to another project's work."""
    queue = WorkQueue(str(tmp_path / "q.sqlite"))
    for project in ("a", "b"):
        queue.add_project(Project(project_id=project, name=project))
        queue.set_control("running", project_id=project)
    queue.add([WorkRecord(item_id="T1", title="in a")], "a")
    queue.add([WorkRecord(item_id="T2", title="in b", depends_on=["project:a/T1"])], "b")

    edge = queue.graph.edges("b", "T2")[0]
    assert edge.target_kind == CROSS_PROJECT_WORK
    assert edge.state == BLOCKED
    queue.claim("w", project_id="a")
    queue.release("T1", DONE, project_id="a")
    assert queue.readiness("T2", project_id="b").ready is True


def test_a_malformed_token_blocks_its_own_item_rather_than_failing_the_parse(
    tmp_path: Path,
) -> None:
    """A bad line in one plan item must not take the whole plan down, and
    must not be quietly read as something else."""
    queue = WorkQueue(str(tmp_path / "q.sqlite"))
    queue.add_project(Project(project_id="p", name="P"))
    queue.set_control("running", project_id="p")
    queue.add(
        [
            WorkRecord(item_id="OK", title="fine"),
            WorkRecord(item_id="BAD", title="bad", depends_on=["project:no-slash"]),
        ],
        "p",
    )

    assert queue.readiness("OK", project_id="p").ready is True
    reason = queue.readiness("BAD", project_id="p").reasons[0]
    assert reason.state == UNRESOLVED
    assert "does not name a project" in reason.explanation


def test_the_github_issue_resolver_is_an_adapter_loaded_only_when_named() -> None:
    """The generic-core rule: format knowledge lives in `adapters/`, and the
    core imports it lazily, by name, only when a plan asks for it."""
    import sys

    from agent_harness import graph as graph_module

    sys.modules.pop("agent_harness.adapters.github_issue", None)
    assert "agent_harness.adapters.github_issue" not in sys.modules

    resolver = graph_module.load_resolver("github-issue")
    assert resolver is not None
    assert "agent_harness.adapters.github_issue" in sys.modules

    from agent_harness.adapters import github_issue

    def gh(_args: Sequence[str]) -> str:
        return '{"number": 4, "state": "CLOSED", "url": "https://example/4"}'

    outcome = github_issue.resolve(
        ExternalTarget("github-issue", "owner/repo#4", "p", "T1"), runner=gh
    )
    assert outcome.state == SATISFIED and "closed" in outcome.evidence

    unreadable = github_issue.resolve(
        ExternalTarget("github-issue", "not-a-reference", "p", "T1"), runner=gh
    )
    assert unreadable.state == UNRESOLVED
    assert "OWNER/REPO#NUMBER" in unreadable.evidence


def test_the_plan_command_prints_a_line_for_every_dependency_case(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """§6.1 asks for an explicit report per case. This is that report at the
    place a person first meets the plan."""
    from agent_harness.__main__ import main

    path = tmp_path / "PLAN.md"
    path.write_text(STAGE_G_PLAN)

    class Refuses:
        def __init__(self, repo: str) -> None:
            self.repo = repo

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("agent_harness.github.sync", lambda *a, **k: _FakeReport())
        patch.setattr("agent_harness.github.GitHub", Refuses)
        main(["--db", str(tmp_path / "q.sqlite"), "plan", str(path), "--repo", "o/r", "--dry-run"])

    err = capsys.readouterr().err
    assert "G5: unresolved local target(s) G404" in err
    assert "G4: external target(s) external:demo-tracker:TICKET-9 — needs a resolver" in err
    assert "cycle: G6 -> G7 -> G6" in err


class _FakeReport:
    labels_created: list[str] = []
    milestones_created: list[str] = []
    orphaned: list[str] = []

    def __str__(self) -> str:
        return "created 0, updated 0, unchanged 0"


def test_the_graph_cli_reports_rebuilds_and_exports(
    tmp_path: Path, queue: WorkQueue, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_harness.__main__ import main

    db = queue.path
    assert main(["--db", db, "graph", "report", "--project", "g"]) == 4
    out = capsys.readouterr().out
    assert "ready: G1" in out
    assert "cycle: G6 -> G7 -> G6" in out
    assert "G5: not ready at graph revision" in out

    export = tmp_path / "graph.json"
    assert main(["--db", db, "graph", "export", "--out", str(export)]) == 0
    assert '"target_kind": "external_reference"' in export.read_text()

    assert main(["--db", db, "graph", "rebuild"]) == 0
    assert "graph rebuilt" in capsys.readouterr().out
