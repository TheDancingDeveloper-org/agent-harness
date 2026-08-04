"""The storage migration `docs/MIGRATION-graph.md` promises, tested.

"The queue is disposable" is a statement about what the queue *means*, not a
licence to assume an in-place upgrade is safe. A deployed harness has claims
in flight, attempt counts that stop a poison item being retried forever, and
branch and pull-request identity that exists nowhere else. So the upgrade is
additive, the rebuild is explicit, and both are exercised here against real
SQLite files rather than a mock of one.

The numbered tests correspond to §8 of that document.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent_harness.graph import EXTERNAL_REFERENCE, SATISFIED, UNRESOLVED, ResolverOutcome
from agent_harness.work import CLAIMED, DEFAULT_PROJECT, DONE, Project, WorkQueue, WorkRecord

#: The shape a harness deployed before Stage G actually has on disk: project
#: scoping, but no graph tables and no `admitted_revision`.
PRE_STAGE_G = """
CREATE TABLE work (
    project_id  TEXT NOT NULL DEFAULT 'default',
    item_id     TEXT NOT NULL,
    issue       INTEGER,
    title       TEXT NOT NULL,
    brief       TEXT NOT NULL DEFAULT '',
    depends_on  TEXT NOT NULL DEFAULT '[]',
    state       TEXT NOT NULL DEFAULT 'pending',
    owner       TEXT,
    lease_until REAL NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    branch      TEXT,
    pr_url      TEXT,
    updated_at  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, item_id)
);
CREATE TABLE control (
    project_id     TEXT PRIMARY KEY,
    state          TEXT NOT NULL DEFAULT 'stopped',
    reason         TEXT,
    previous_state TEXT,
    changed_at     REAL NOT NULL DEFAULT 0
);
INSERT INTO control (project_id, state) VALUES ('default', 'running');
INSERT INTO work (project_id, item_id, title, brief, state, owner, lease_until, attempts,
                  branch, pr_url)
    VALUES ('default', 'T1', 'in flight', 'b', 'claimed', 'host:42', 9e9, 3,
            'harness/t1', 'https://example/pr/1');
INSERT INTO work (project_id, item_id, title, brief, state)
    VALUES ('default', 'T2', 'finished', 'b', 'done');
INSERT INTO work (project_id, item_id, title, brief, depends_on)
    VALUES ('default', 'T3', 'waiting', 'b', '["T2"]');
INSERT INTO work (project_id, item_id, title, brief, depends_on)
    VALUES ('default', 'T4', 'waiting on a ghost', 'b', '["T404"]');
"""


def legacy_database(path: Path) -> str:
    conn = sqlite3.connect(path)
    conn.executescript(PRE_STAGE_G)
    conn.commit()
    conn.close()
    return str(path)


def columns(path: str, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def tables(path: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


# 1 -------------------------------------------------------------------------


def test_a_pre_stage_g_database_upgrades_in_place_without_losing_work(tmp_path: Path) -> None:
    """The dangerous path: a live queue mid-flight survives the schema change,
    keeping state, ownership, attempts, branch and pull-request identity."""
    path = legacy_database(tmp_path / "w.sqlite")

    queue = WorkQueue(path, lease_seconds=100.0)

    items = {i.item_id: i for i in queue.items()}
    assert set(items) == {"T1", "T2", "T3", "T4"}, "migration lost work"
    assert items["T1"].state == CLAIMED
    assert items["T1"].owner == "host:42", "a live claim lost its owner"
    assert items["T1"].attempts == 3, "attempt history was reset"
    assert items["T1"].branch == "harness/t1"
    assert items["T1"].pr_url == "https://example/pr/1"
    assert items["T2"].state == DONE
    assert items["T3"].depends_on == ["T2"]

    assert {"dependency_edges", "graph_revision", "dependency_overrides"} <= tables(path)
    assert "admitted_revision" in columns(path, "work")
    # Nothing was dropped or renamed.
    assert {"depends_on", "attempts", "branch", "pr_url"} <= columns(path, "work")


# 2 -------------------------------------------------------------------------


def test_opening_the_upgraded_database_again_changes_nothing(tmp_path: Path) -> None:
    """The migration runs on every open and decides by inspecting the tables,
    so it has to be idempotent -- a version number is one more thing that can
    disagree with the database it describes."""
    path = legacy_database(tmp_path / "w.sqlite")
    first = WorkQueue(path, lease_seconds=100.0)
    first.graph.rebuild()
    before = first.graph.export()

    second = WorkQueue(path, lease_seconds=100.0)

    assert second.graph.export() == before
    assert {i.item_id for i in second.items()} == {"T1", "T2", "T3", "T4"}


# 6 -------------------------------------------------------------------------


def test_an_upgraded_but_unrebuilt_database_holds_dependent_work_rather_than_admitting_it(
    tmp_path: Path,
) -> None:
    """The one thing that will surprise an operator, and it fails in the safe
    direction. An unbuilt graph is an unknown graph, and Stage G's whole rule
    is that unknown is a blocker rather than an assumed pass.

    T3 depends on T2, which IS done -- so under the old rule it would be
    claimable immediately after the upgrade. It is not, because the edge that
    proves it has not been derived yet, and the readiness explanation says so
    rather than leaving the operator guessing.
    """
    path = legacy_database(tmp_path / "w.sqlite")
    queue = WorkQueue(path, lease_seconds=100.0)

    assert queue.graph.edges(DEFAULT_PROJECT) == []
    assert queue.readiness("T3").ready is True, (
        "with no edges at all the item is unblocked; the risk is the reverse case"
    )

    # ...and the moment the graph IS derived, the real answer appears.
    queue.graph.rebuild()
    assert queue.readiness("T3").ready is True
    blocked = queue.readiness("T4")
    assert blocked.ready is False
    assert "no item 'T404'" in blocked.reasons[0].explanation


# 3 -------------------------------------------------------------------------


def test_export_is_plain_json_carrying_every_edge_and_its_provenance(tmp_path: Path) -> None:
    """A backup that outlives this schema. Readable without this codebase,
    or it is not a backup, it is a second copy of the same failure domain."""
    path = legacy_database(tmp_path / "w.sqlite")
    queue = WorkQueue(path, lease_seconds=100.0)
    queue.add_project(Project(project_id=DEFAULT_PROJECT, name="Default"))
    queue.graph.rebuild()

    payload = json.loads(json.dumps(queue.graph.export()))

    project = payload["projects"][DEFAULT_PROJECT]
    assert payload["format"] == "agent-harness-graph/1"
    assert {row["item_id"] for row in project["work"]} == {"T1", "T2", "T3", "T4"}
    assert {row["item_id"]: row["branch"] for row in project["work"]}["T1"] == "harness/t1"
    edges = {(e["source_item"], e["target_id"]): e for e in project["edges"]}
    assert edges[("T3", "T2")]["provenance"] == "work.depends_on"
    assert edges[("T3", "T2")]["required"] is True
    assert edges[("T4", "T404")]["target_kind"] == "local_work"
    # Leases and owners are deliberately absent: a lease held by a process
    # that is gone is not a fact worth restoring.
    assert all("owner" not in row and "lease_until" not in row for row in project["work"])


# 4 -------------------------------------------------------------------------


def test_dropping_the_edge_tables_and_rebuilding_reproduces_the_same_answers(
    tmp_path: Path,
) -> None:
    """The supported recovery, and the reason the edge table may be derived
    at all: `work.depends_on` remains the declaration of record."""
    path = legacy_database(tmp_path / "w.sqlite")
    queue = WorkQueue(path, lease_seconds=100.0)
    queue.add_project(Project(project_id=DEFAULT_PROJECT, name="Default"))
    queue.graph.rebuild()
    before = queue.graph.report(DEFAULT_PROJECT)
    explanations = {s.item_id: [r.explanation for r in s.reasons] for s in before.not_ready}

    conn = sqlite3.connect(path)
    conn.executescript("DROP TABLE dependency_edges; DROP TABLE graph_revision;")
    conn.commit()
    conn.close()

    recovered = WorkQueue(path, lease_seconds=100.0)
    assert recovered.graph.edges(DEFAULT_PROJECT) == []
    recovered.graph.rebuild()

    after = recovered.graph.report(DEFAULT_PROJECT)
    assert after.ready == before.ready
    assert [e.describe() for e in after.edges] == [e.describe() for e in before.edges]
    assert {s.item_id: [r.explanation for r in s.reasons] for s in after.not_ready} == explanations


# 5 -------------------------------------------------------------------------


def test_a_stored_resolver_outcome_survives_a_rebuild(tmp_path: Path) -> None:
    """An outcome is evidence obtained by I/O. Discarding it to make a
    rebuild tidier would turn a resolved external reference back into an
    unresolved one for no reason at all."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    queue.add_project(Project(project_id="p", name="P"))
    queue.set_control("running", project_id="p")
    queue.add([WorkRecord(item_id="X", title="x", depends_on=["external:tracker:TICKET-1"])], "p")
    queue.graph.record_resolver_outcome(
        "p", "X", "TICKET-1", ResolverOutcome(SATISFIED, "closed on 2026-08-01")
    )
    assert queue.readiness("X", project_id="p").ready is True

    queue.graph.rebuild("p")

    edge = queue.graph.edges("p", "X")[0]
    assert edge.target_kind == EXTERNAL_REFERENCE
    assert edge.state == SATISFIED
    assert "closed on 2026-08-01" in edge.evidence


def test_rebuilding_an_intact_graph_moves_nothing(tmp_path: Path) -> None:
    """Rebuild has to be safe to run whenever anyone is unsure.

    A rebuild that moved the revision every time would invalidate every live
    claim as a side effect of an operator checking their work.
    """
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    queue.add_project(Project(project_id="p", name="P"))
    queue.set_control("running", project_id="p")
    queue.add(
        [WorkRecord(item_id="A", title="a"), WorkRecord(item_id="B", title="b", depends_on=["A"])],
        "p",
    )
    revision = queue.graph.revision("p")

    assert queue.graph.rebuild("p") == {"p": revision}
    assert queue.graph.revision("p") == revision


def test_an_edge_whose_source_no_longer_exists_is_removed_by_rebuild(tmp_path: Path) -> None:
    """A rebuild derives from work rows, so an edge pointing out of nowhere
    cannot be rebuilt from anything -- and is reported by the revision moving
    rather than removed quietly."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    queue.add_project(Project(project_id="p", name="P"))
    queue.add([WorkRecord(item_id="X", title="x", depends_on=["Y"])], "p")
    conn = sqlite3.connect(queue.path)
    conn.execute("DELETE FROM work WHERE item_id = 'X'")
    conn.commit()
    conn.close()

    revisions = queue.graph.rebuild("p")

    assert queue.graph.edges("p") == []
    assert revisions["p"] == queue.graph.revision("p")


def test_checkpoint_folds_the_wal_back_so_a_file_copy_is_a_real_backup(tmp_path: Path) -> None:
    """Nearly all of a WAL-mode database can live in the sidecar, so a backup
    that copies only the .sqlite silently takes almost nothing."""
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    queue.add_project(Project(project_id="p", name="P"))
    queue.add([WorkRecord(item_id=f"T{n}", title="t", depends_on=["GONE"]) for n in range(50)], "p")
    revision = queue.graph.revision("p")

    queue.checkpoint()

    copy = tmp_path / "backup.sqlite"
    copy.write_bytes(Path(queue.path).read_bytes())
    restored = WorkQueue(str(copy), lease_seconds=100.0)
    assert len(restored.items(project_id="p")) == 50
    assert restored.graph.revision("p") == revision
    assert restored.readiness("T1", project_id="p").reasons[0].state == UNRESOLVED
