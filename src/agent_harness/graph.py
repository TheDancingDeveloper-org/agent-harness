"""The typed dependency graph: what an item is waiting for, and why.

This is the implementation of the work-graph section of
[`docs/COORDINATION-PLANE.md`](../../docs/COORDINATION-PLANE.md) §8, not a
second, thinner design beside it. The contract it keeps is that document's:
every edge names a source item, a target *kind* and identity, whether it is
required or advisory, a resolver when the target is external, a resolution
state, its provenance, and the graph revision it belongs to.

The rule that motivates all of it is one line long. **A required target the
graph cannot resolve is a blocker.** The previous behaviour treated a
dependency absent from the queue as satisfied, on the grounds that plans
routinely reference work tracked elsewhere -- which is true, and which made a
typo, an omitted item and a genuine external reference completely
indistinguishable. All three ran immediately. An external reference is still
perfectly legitimate; it just has to say so, and it has to have an answer from
a resolver rather than an assumption.

Three further properties follow from being a graph rather than a list:

*Referential validation.* A local target either names a row in this project or
it does not, and the second case is reported with the id that could not be
found rather than passed over.

*Cycle detection.* Two items that each wait for the other used to be reported
as "waiting", forever, one item at a time, with nothing saying the wait could
never end.

*Revisions.* The revision is what lets admission and the check before the
expensive gate speak about the same graph. `claim` records the revision it
admitted at; the pre-gate check reports the revision it evaluated. When they
differ, the graph moved under a live claim, and that is a fact worth writing
down rather than a race to lose silently.

Edges are derived from `work.depends_on`, which remains the declaration of
record. That is deliberate: a derived table can be dropped and rebuilt, which
is the whole recovery story in `docs/MIGRATION-graph.md`.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

# ------------------------------------------------------------- target kinds

#: Work in the same project's queue. The common case, and the only kind a
#: bare `depends on: T1` can mean.
LOCAL_WORK = "local_work"
#: Something outside the harness entirely -- a ticket, a release, a review in
#: another system. Needs a resolver, because nothing here can see it.
EXTERNAL_REFERENCE = "external_reference"
#: A decision a human has to make. Kept distinct from work because a decision
#: is not a task, and the queue already has a `blocked` state for parking one.
HUMAN_DECISION = "human_decision"
#: Work in a *different* project of this harness. Distinct from local work
#: because ids are only unique within a project.
CROSS_PROJECT_WORK = "cross_project_work"

TARGET_KINDS = (LOCAL_WORK, EXTERNAL_REFERENCE, HUMAN_DECISION, CROSS_PROJECT_WORK)

# ------------------------------------------------------------- edge states

#: The graph does not know. Never a synonym for satisfied.
UNRESOLVED = "unresolved"
#: The target is known and is not finished yet.
BLOCKED = "blocked"
#: The target is known and is finished.
SATISFIED = "satisfied"

EDGE_STATES = (UNRESOLVED, BLOCKED, SATISFIED)

#: Reason kinds a readiness explanation can carry, so a client can branch on
#: something other than English.
REASON_DEPENDENCY = "dependency"
REASON_CYCLE = "cycle"
#: The item declares dependencies the edge table does not hold -- an upgraded
#: database whose graph has not been rebuilt yet.
REASON_STALE = "stale_graph"

#: Resolver names the core will load on demand, and the module that provides
#: each. Only the *name* lives here; nothing in this module imports an adapter
#: at module scope, and the core never learns an external system's format.
ADAPTER_RESOLVERS = {
    "github-issue": "agent_harness.adapters.github_issue",
}

#: Provenance for an edge declared by a work row's `depends_on`. Named once
#: because the queue writes it and a rebuild re-derives it, and a rebuild that
#: recorded a different provenance would look like a change and move the
#: revision for nothing.
WORK_DECLARATION = "work.depends_on"

_ADVISORY_SUFFIX = "(advisory)"


@dataclass(frozen=True)
class ExternalTarget:
    """What a resolver is asked about. Deliberately free of harness types:
    an adapter should not need to import the queue to answer a question."""

    resolver: str
    identity: str
    project_id: str
    source_item: str


@dataclass(frozen=True)
class ResolverOutcome:
    """A resolver's answer, and the evidence for it.

    The evidence is not decoration. A satisfied external dependency with no
    stated reason is exactly the assumption this stage exists to remove.
    """

    state: str
    evidence: str

    def __post_init__(self) -> None:
        if self.state not in EDGE_STATES:
            raise ValueError(f"unknown resolution state {self.state!r}; expected {EDGE_STATES}")


class Resolver(Protocol):
    # Positional-only: a resolver is a plain function, and requiring it to
    # name its parameter `target` would make an obvious one-liner fail to
    # type-check for a reason that has nothing to do with what it does.
    def __call__(self, target: ExternalTarget, /) -> ResolverOutcome: ...


def load_resolver(name: str, extra: Mapping[str, Resolver] | None = None) -> Resolver | None:
    """Find the resolver for a target kind, importing its adapter lazily.

    `extra` is how a deployment (or a test) supplies its own without editing
    this module. Returning None rather than raising is intentional: an
    unknown resolver leaves the edge `unresolved`, which is a blocker with an
    explanation, and that is strictly better than an exception in the middle
    of a claim scan.
    """
    if extra and name in extra:
        return extra[name]
    module_name = ADAPTER_RESOLVERS.get(name)
    if module_name is None:
        return None
    module = importlib.import_module(module_name)
    factory = getattr(module, "resolver", None)
    if factory is None:  # pragma: no cover - a broken adapter, not a state
        return None
    resolver: Resolver = factory()
    return resolver


# ------------------------------------------------------------------ parsing


@dataclass(frozen=True)
class DependencySpec:
    """One edge as *declared*, before anything tries to resolve it."""

    target_kind: str
    target_id: str
    required: bool = True
    resolver: str | None = None
    provenance: str = "declared"
    #: Set when the token itself was malformed. Carried rather than raised so
    #: a bad plan line blocks its item with an explanation instead of taking
    #: down the claim scan.
    malformed: str | None = None

    def token(self) -> str:
        """The declaration this spec came from, rebuilt. Round-trips, so the
        queue can store tokens and the graph can store kinds without the two
        drifting."""
        if self.target_kind == EXTERNAL_REFERENCE:
            body = f"external:{self.resolver or ''}:{self.target_id}"
        elif self.target_kind == HUMAN_DECISION:
            body = f"decision:{self.target_id}"
        elif self.target_kind == CROSS_PROJECT_WORK:
            body = f"project:{self.target_id}"
        else:
            body = self.target_id
        return body if self.required else f"?{body}"


def parse_dependency(token: str, *, provenance: str = "declared") -> DependencySpec:
    """Read one dependency token.

    The grammar, in full:

        T1                              local work in this project
        external:RESOLVER:IDENTITY      something outside the harness
        decision:D9                     a human decision, parked as work
        project:PROJECT/ITEM            work in another project
        ?ANY-OF-THE-ABOVE               advisory: reported, never a blocker

    Never raises. A token this cannot read becomes an edge whose state is
    `unresolved` and whose evidence quotes the token -- a blocker that
    explains itself, rather than an exception thrown from inside a claim.
    """
    text = token.strip()
    required = True
    if text.endswith(_ADVISORY_SUFFIX):
        required = False
        text = text[: -len(_ADVISORY_SUFFIX)].strip()
    if text.startswith("?"):
        required = False
        text = text[1:].strip()

    def spec(kind: str, identity: str, **kw: Any) -> DependencySpec:
        return DependencySpec(
            target_kind=kind,
            target_id=identity,
            required=required,
            provenance=provenance,
            **kw,
        )

    if not text:
        return spec(LOCAL_WORK, token.strip(), malformed=f"{token!r} names no target")
    if text.startswith("external:"):
        rest = text[len("external:") :]
        resolver, separator, identity = rest.partition(":")
        if not separator or not resolver.strip() or not identity.strip():
            return spec(
                EXTERNAL_REFERENCE,
                rest.strip() or text,
                malformed=(
                    f"{token!r} is an external target that does not name a resolver; "
                    "the form is external:RESOLVER:IDENTITY"
                ),
            )
        return spec(EXTERNAL_REFERENCE, identity.strip(), resolver=resolver.strip())
    if text.startswith("decision:"):
        identity = text[len("decision:") :].strip()
        if not identity:
            return spec(HUMAN_DECISION, text, malformed=f"{token!r} names no decision")
        return spec(HUMAN_DECISION, identity)
    if text.startswith("project:"):
        rest = text[len("project:") :].strip()
        project, separator, item = rest.partition("/")
        if not separator or not project.strip() or not item.strip():
            return spec(
                CROSS_PROJECT_WORK,
                rest or text,
                malformed=(
                    f"{token!r} is a cross-project target that does not name a project; "
                    "the form is project:PROJECT/ITEM"
                ),
            )
        return spec(CROSS_PROJECT_WORK, f"{project.strip()}/{item.strip()}")
    return spec(LOCAL_WORK, text)


def parse_dependencies(
    tokens: Iterable[str], *, provenance: str = "declared"
) -> list[DependencySpec]:
    """Parse a whole `depends_on` list, keeping declaration order and
    dropping exact duplicates -- an id stated twice is one edge."""
    out: list[DependencySpec] = []
    seen: set[tuple[str, str]] = set()
    for token in tokens:
        spec = parse_dependency(token, provenance=provenance)
        key = (spec.target_kind, spec.target_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(spec)
    return out


# ------------------------------------------------------------------- models


@dataclass
class DependencyEdge:
    """A declared edge, with whatever the graph currently knows about it."""

    project_id: str
    source_item: str
    target_kind: str
    target_id: str
    required: bool = True
    resolver: str | None = None
    provenance: str = "declared"
    revision: int = 0
    state: str = UNRESOLVED
    evidence: str = ""
    updated_at: float = 0.0

    def describe(self) -> str:
        """One line naming the edge as a human would say it."""
        target = self.target_id
        if self.target_kind == EXTERNAL_REFERENCE:
            target = f"{self.target_id} via {self.resolver or 'no resolver'}"
        return f"{self.source_item} -> {target} ({self.target_kind}, {self.state})"


@dataclass(frozen=True)
class ReadinessReason:
    """One reason an item is not ready, in a form a client can branch on."""

    kind: str
    explanation: str
    target_kind: str | None = None
    target_id: str | None = None
    required: bool = True
    resolver: str | None = None
    state: str | None = None
    evidence: str | None = None


@dataclass(frozen=True)
class Readiness:
    """Whether an item may be admitted, at a stated graph revision."""

    project_id: str
    item_id: str
    ready: bool
    revision: int
    reasons: tuple[ReadinessReason, ...] = ()
    #: Unsatisfied advisory edges. Reported, never blocking -- an advisory
    #: edge that silently vanished would be indistinguishable from one that
    #: was never declared.
    advisory: tuple[ReadinessReason, ...] = ()
    overridden: bool = False
    override_reason: str | None = None

    def explain(self) -> str:
        """The whole explanation as one string, for logs and event details."""
        if self.ready and self.overridden:
            return f"ready at graph revision {self.revision} by operator override: " + str(
                self.override_reason
            )
        if self.ready:
            return f"ready at graph revision {self.revision}"
        return f"not ready at graph revision {self.revision}: " + "; ".join(
            reason.explanation for reason in self.reasons
        )


@dataclass(frozen=True)
class GraphReport:
    """Everything the graph knows about one project, in one answer."""

    project_id: str
    revision: int
    edges: tuple[DependencyEdge, ...] = ()
    cycles: tuple[tuple[str, ...], ...] = ()
    ready: tuple[str, ...] = ()
    not_ready: tuple[Readiness, ...] = ()

    @property
    def unresolved(self) -> tuple[DependencyEdge, ...]:
        return tuple(e for e in self.edges if e.state == UNRESOLVED)

    @property
    def blocked(self) -> tuple[DependencyEdge, ...]:
        return tuple(e for e in self.edges if e.state == BLOCKED)

    @property
    def satisfied(self) -> tuple[DependencyEdge, ...]:
        return tuple(e for e in self.edges if e.state == SATISFIED)

    @property
    def external(self) -> tuple[DependencyEdge, ...]:
        return tuple(e for e in self.edges if e.target_kind == EXTERNAL_REFERENCE)


SCHEMA = """
-- One row per declared edge. Derived from work.depends_on, which stays the
-- declaration of record: this table can be dropped and rebuilt, and that is
-- the entire recovery procedure in docs/MIGRATION-graph.md.
CREATE TABLE IF NOT EXISTS dependency_edges (
    project_id        TEXT NOT NULL,
    source_item       TEXT NOT NULL,
    target_kind       TEXT NOT NULL,
    target_id         TEXT NOT NULL,
    required          INTEGER NOT NULL DEFAULT 1,
    resolver          TEXT,
    provenance        TEXT NOT NULL DEFAULT 'declared',
    malformed         TEXT,
    revision          INTEGER NOT NULL DEFAULT 0,
    -- Only external targets store an outcome. Local, cross-project and
    -- decision targets are derived from the work rows on every read, so they
    -- cannot go stale; an external target's outcome is evidence obtained by
    -- I/O and has to be kept.
    resolved_state    TEXT,
    resolved_evidence TEXT,
    resolved_revision INTEGER,
    resolved_at       REAL,
    updated_at        REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, source_item, target_kind, target_id)
);
CREATE INDEX IF NOT EXISTS dependency_edges_source
    ON dependency_edges (project_id, source_item);

-- Monotonic per project. Bumped when the declared graph changes or a
-- resolver reports something new -- never by work merely finishing, which is
-- work state and not the graph.
CREATE TABLE IF NOT EXISTS graph_revision (
    project_id TEXT PRIMARY KEY,
    revision   INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

-- An operator saying "I know, go anyway". Scoped to the revision it was
-- granted at, so a LATER graph correction re-blocks the item rather than
-- inheriting a decision made about a different graph.
CREATE TABLE IF NOT EXISTS dependency_overrides (
    project_id  TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    who         TEXT,
    reason      TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    PRIMARY KEY (project_id, item_id, revision)
);
"""


@dataclass
class _Resolution:
    state: str
    evidence: str


class DependencyGraph:
    """The graph, over the same SQLite file as the queue.

    Every method takes an optional connection so the caller can run inside
    the claim transaction. Admission has to see the same graph the claim
    writes, or the check and the write are two different moments.
    """

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        *,
        now: Callable[[], float] = time.time,
        resolvers: Mapping[str, Resolver] | None = None,
    ) -> None:
        self._connect = connect
        self.now = now
        self.resolvers = dict(resolvers or {})

    def create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA)

    # ------------------------------------------------------------ plumbing

    def _with_conn(self, conn: sqlite3.Connection | None) -> tuple[sqlite3.Connection, bool]:
        if conn is not None:
            return conn, False
        return self._connect(), True

    # ------------------------------------------------------------ revision

    def revision(self, project_id: str, *, conn: sqlite3.Connection | None = None) -> int:
        connection, owned = self._with_conn(conn)
        try:
            row = connection.execute(
                "SELECT revision FROM graph_revision WHERE project_id = ?", (project_id,)
            ).fetchone()
            return int(row["revision"]) if row else 0
        finally:
            if owned:
                connection.close()

    def _bump(self, conn: sqlite3.Connection, project_id: str) -> int:
        current = self.revision(project_id, conn=conn)
        conn.execute(
            "INSERT INTO graph_revision (project_id, revision, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET revision = excluded.revision, "
            "updated_at = excluded.updated_at",
            (project_id, current + 1, self.now()),
        )
        return current + 1

    # --------------------------------------------------------------- edges

    def set_edges(
        self,
        project_id: str,
        source_item: str,
        specs: Sequence[DependencySpec],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Declare this item's edges, idempotently. Returns the revision.

        Re-ingesting an unchanged plan must not move the revision: a revision
        that ticks on every sync tells a reader the graph changed when
        nothing did, and the pre-gate check would then invalidate live claims
        for no reason at all.
        """
        connection, owned = self._with_conn(conn)
        try:
            existing = {
                (row["target_kind"], row["target_id"]): row
                for row in connection.execute(
                    "SELECT * FROM dependency_edges WHERE project_id = ? AND source_item = ?",
                    (project_id, source_item),
                )
            }
            wanted = {(spec.target_kind, spec.target_id): spec for spec in specs}
            unchanged = set(existing) == set(wanted) and all(
                bool(existing[key]["required"]) == wanted[key].required
                and (existing[key]["resolver"] or None) == wanted[key].resolver
                and existing[key]["provenance"] == wanted[key].provenance
                and (existing[key]["malformed"] or None) == wanted[key].malformed
                for key in wanted
            )
            if unchanged:
                return self.revision(project_id, conn=connection)

            revision = self._bump(connection, project_id)
            for key in set(existing) - set(wanted):
                connection.execute(
                    "DELETE FROM dependency_edges WHERE project_id = ? AND source_item = ? "
                    "AND target_kind = ? AND target_id = ?",
                    (project_id, source_item, key[0], key[1]),
                )
            for key, spec in wanted.items():
                if key in existing:
                    # Kept, so its stored resolver outcome is kept too: an
                    # outcome is evidence, and discarding evidence because a
                    # sibling edge changed would turn a resolved external
                    # reference back into an unresolved one for no reason.
                    connection.execute(
                        "UPDATE dependency_edges SET required = ?, resolver = ?, "
                        "provenance = ?, malformed = ?, revision = ?, updated_at = ? "
                        "WHERE project_id = ? AND source_item = ? AND target_kind = ? "
                        "AND target_id = ?",
                        (
                            int(spec.required),
                            spec.resolver,
                            spec.provenance,
                            spec.malformed,
                            revision,
                            self.now(),
                            project_id,
                            source_item,
                            key[0],
                            key[1],
                        ),
                    )
                    continue
                connection.execute(
                    "INSERT INTO dependency_edges (project_id, source_item, target_kind, "
                    "target_id, required, resolver, provenance, malformed, revision, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        source_item,
                        key[0],
                        key[1],
                        int(spec.required),
                        spec.resolver,
                        spec.provenance,
                        spec.malformed,
                        revision,
                        self.now(),
                    ),
                )
            return revision
        finally:
            if owned:
                connection.close()

    def edges(
        self,
        project_id: str,
        source_item: str | None = None,
        *,
        conn: sqlite3.Connection | None = None,
        resolve: bool = True,
    ) -> list[DependencyEdge]:
        """The declared edges, with their current resolution state."""
        connection, owned = self._with_conn(conn)
        try:
            sql = "SELECT * FROM dependency_edges WHERE project_id = ?"
            params: list[Any] = [project_id]
            if source_item is not None:
                sql += " AND source_item = ?"
                params.append(source_item)
            sql += " ORDER BY source_item, target_kind, target_id"
            rows = connection.execute(sql, params).fetchall()
            out = []
            for row in rows:
                edge = _edge_from_row(row)
                if resolve:
                    resolution = self._resolve(connection, row)
                    edge.state = resolution.state
                    edge.evidence = resolution.evidence
                out.append(edge)
            return out
        finally:
            if owned:
                connection.close()

    def sources(self, project_id: str, *, conn: sqlite3.Connection | None = None) -> list[str]:
        connection, owned = self._with_conn(conn)
        try:
            return [
                row["source_item"]
                for row in connection.execute(
                    "SELECT DISTINCT source_item FROM dependency_edges WHERE project_id = ? "
                    "ORDER BY source_item",
                    (project_id,),
                )
            ]
        finally:
            if owned:
                connection.close()

    # ---------------------------------------------------------- resolution

    def _resolve(self, conn: sqlite3.Connection, row: sqlite3.Row) -> _Resolution:
        """What this edge's state is, right now.

        Local, cross-project and decision targets are derived from the work
        rows on every read. That is the point of a projection: it cannot be
        stale, and there is no second copy of the answer to disagree with the
        queue.
        """
        malformed = row["malformed"]
        if malformed:
            return _Resolution(UNRESOLVED, malformed)
        kind = row["target_kind"]
        target = row["target_id"]
        if kind == LOCAL_WORK:
            return self._resolve_work(conn, row["project_id"], target, kind)
        if kind == HUMAN_DECISION:
            resolution = self._resolve_work(conn, row["project_id"], target, kind)
            if resolution.state == UNRESOLVED:
                return _Resolution(
                    UNRESOLVED,
                    f"decision {target!r} is not recorded as work in project "
                    f"{row['project_id']!r}; a decision has to exist before it can be made",
                )
            return resolution
        if kind == CROSS_PROJECT_WORK:
            project, _, item = target.partition("/")
            return self._resolve_work(conn, project, item, kind)
        if kind == EXTERNAL_REFERENCE:
            resolver = row["resolver"]
            if not resolver:
                return _Resolution(
                    UNRESOLVED,
                    f"external target {target!r} names no resolver, so nothing here can "
                    "say whether it is done",
                )
            state = row["resolved_state"]
            if state is None:
                return _Resolution(
                    UNRESOLVED,
                    f"resolver {resolver!r} has not reported an outcome for {target!r}",
                )
            evidence = row["resolved_evidence"] or ""
            return _Resolution(
                state,
                f"resolver {resolver!r} at graph revision {row['resolved_revision']}: {evidence}",
            )
        # Unreachable through parse_dependency, but a hand-written row is
        # possible and must not be treated as satisfied.
        return _Resolution(UNRESOLVED, f"unknown target kind {kind!r}")

    def _resolve_work(
        self, conn: sqlite3.Connection, project_id: str, item_id: str, kind: str
    ) -> _Resolution:
        row = conn.execute(
            "SELECT state FROM work WHERE project_id = ? AND item_id = ?",
            (project_id, item_id),
        ).fetchone()
        if row is None:
            return _Resolution(
                UNRESOLVED,
                f"no item {item_id!r} in project {project_id!r}; a required target the "
                "graph cannot find is a blocker, not an assumed external dependency",
            )
        if row["state"] == "done":
            return _Resolution(SATISFIED, f"{item_id} is done")
        return _Resolution(BLOCKED, f"{item_id} is {row['state']}, not done")

    def record_resolver_outcome(
        self,
        project_id: str,
        source_item: str,
        target_id: str,
        outcome: ResolverOutcome,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Store what a resolver said about an external target.

        Bumps the revision when the answer changes, because a changed answer
        changes who is admissible -- which is exactly what a revision is for.
        """
        connection, owned = self._with_conn(conn)
        try:
            row = connection.execute(
                "SELECT resolved_state, resolved_evidence FROM dependency_edges "
                "WHERE project_id = ? AND source_item = ? AND target_kind = ? AND target_id = ?",
                (project_id, source_item, EXTERNAL_REFERENCE, target_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"no external edge {source_item} -> {target_id} in {project_id}")
            if row["resolved_state"] == outcome.state and row["resolved_evidence"] == (
                outcome.evidence
            ):
                return self.revision(project_id, conn=connection)
            revision = self._bump(connection, project_id)
            connection.execute(
                "UPDATE dependency_edges SET resolved_state = ?, resolved_evidence = ?, "
                "resolved_revision = ?, resolved_at = ?, updated_at = ? "
                "WHERE project_id = ? AND source_item = ? AND target_kind = ? AND target_id = ?",
                (
                    outcome.state,
                    outcome.evidence,
                    revision,
                    self.now(),
                    self.now(),
                    project_id,
                    source_item,
                    EXTERNAL_REFERENCE,
                    target_id,
                ),
            )
            return revision
        finally:
            if owned:
                connection.close()

    def resolve_external(
        self, project_id: str, *, resolvers: Mapping[str, Resolver] | None = None
    ) -> list[tuple[DependencyEdge, ResolverOutcome | None]]:
        """Ask each named resolver about the external targets it owns.

        Deliberately a separate call, never done inside `claim`: resolving an
        external target is I/O, and I/O inside the write transaction that
        hands out work is how one slow ticket system stalls a fleet.

        A resolver that is missing or raises leaves the edge `unresolved`.
        That is the safe direction: the item waits and says why.
        """
        available = {**self.resolvers, **(resolvers or {})}
        results: list[tuple[DependencyEdge, ResolverOutcome | None]] = []
        for edge in self.edges(project_id):
            if edge.target_kind != EXTERNAL_REFERENCE or not edge.resolver:
                continue
            resolver = load_resolver(edge.resolver, available)
            if resolver is None:
                results.append((edge, None))
                continue
            target = ExternalTarget(
                resolver=edge.resolver,
                identity=edge.target_id,
                project_id=project_id,
                source_item=edge.source_item,
            )
            try:
                outcome = resolver(target)
            except Exception as exc:  # noqa: BLE001 - an unreachable resolver is a state
                results.append((edge, None))
                self.record_resolver_outcome(
                    project_id,
                    edge.source_item,
                    edge.target_id,
                    ResolverOutcome(UNRESOLVED, f"resolver failed: {exc}"),
                )
                continue
            self.record_resolver_outcome(project_id, edge.source_item, edge.target_id, outcome)
            results.append((edge, outcome))
        return results

    # -------------------------------------------------------------- cycles

    def cycles(
        self, project_id: str, *, conn: sqlite3.Connection | None = None
    ) -> list[tuple[str, ...]]:
        """Groups of items that can never all become ready.

        Only required local edges are considered: an advisory edge does not
        gate anything, so a loop through one is not a deadlock, and a
        cross-project or external target cannot close a loop inside this
        project.
        """
        connection, owned = self._with_conn(conn)
        try:
            adjacency: dict[str, set[str]] = {}
            for row in connection.execute(
                "SELECT source_item, target_id FROM dependency_edges WHERE project_id = ? "
                "AND target_kind = ? AND required = 1",
                (project_id, LOCAL_WORK),
            ):
                adjacency.setdefault(row["source_item"], set()).add(row["target_id"])
            return find_cycles(adjacency)
        finally:
            if owned:
                connection.close()

    # ----------------------------------------------------------- readiness

    def readiness(
        self,
        project_id: str,
        item_id: str,
        *,
        conn: sqlite3.Connection | None = None,
        cycles: Sequence[tuple[str, ...]] | None = None,
    ) -> Readiness:
        """Why this item may or may not be admitted, and at which revision.

        The same call answers admission and the check before the expensive
        gate. Two implementations of "is it ready" is two answers, and the
        one that disagreed would be the one that let ineligible work commit.
        """
        connection, owned = self._with_conn(conn)
        try:
            revision = self.revision(project_id, conn=connection)
            reasons: list[ReadinessReason] = []
            advisory: list[ReadinessReason] = []
            edges = self.edges(project_id, item_id, conn=connection)
            if not edges:
                reasons.extend(self._stale_graph_reason(connection, project_id, item_id))
            for edge in edges:
                if edge.state == SATISFIED:
                    continue
                reason = ReadinessReason(
                    kind=REASON_DEPENDENCY,
                    explanation=(
                        f"{edge.target_kind} target {edge.target_id!r} is {edge.state}: "
                        f"{edge.evidence}"
                    ),
                    target_kind=edge.target_kind,
                    target_id=edge.target_id,
                    required=edge.required,
                    resolver=edge.resolver,
                    state=edge.state,
                    evidence=edge.evidence,
                )
                (reasons if edge.required else advisory).append(reason)

            for cycle in cycles if cycles is not None else self.cycles(project_id, conn=connection):
                if item_id in cycle:
                    reasons.append(
                        ReadinessReason(
                            kind=REASON_CYCLE,
                            explanation=(
                                "these items require each other and so can never all become "
                                "ready: " + " -> ".join([*cycle, cycle[0]])
                            ),
                            state=UNRESOLVED,
                            evidence=" -> ".join([*cycle, cycle[0]]),
                        )
                    )

            if not reasons:
                return Readiness(project_id, item_id, True, revision, advisory=tuple(advisory))

            override = connection.execute(
                "SELECT who, reason FROM dependency_overrides "
                "WHERE project_id = ? AND item_id = ? AND revision = ?",
                (project_id, item_id, revision),
            ).fetchone()
            if override is not None:
                who = override["who"]
                return Readiness(
                    project_id,
                    item_id,
                    True,
                    revision,
                    reasons=tuple(reasons),
                    advisory=tuple(advisory),
                    overridden=True,
                    override_reason=(
                        override["reason"] + (f" (— {who})" if who else "")
                        if override["reason"]
                        else None
                    ),
                )
            return Readiness(
                project_id,
                item_id,
                False,
                revision,
                reasons=tuple(reasons),
                advisory=tuple(advisory),
            )
        finally:
            if owned:
                connection.close()

    def _stale_graph_reason(
        self, conn: sqlite3.Connection, project_id: str, item_id: str
    ) -> list[ReadinessReason]:
        """A blocker for an item that declares dependencies the graph has not
        derived yet.

        This is the upgrade case. A database upgraded in place has an empty
        edge table until work is re-added or `graph rebuild` runs, and "no
        edges" would otherwise read exactly like "no dependencies" -- so
        every dependent item in an existing backlog would be admitted on the
        strength of a graph nobody had built. An unbuilt graph is an unknown
        graph, and unknown is a blocker.

        Only reached when the item has no edges at all, so the ordinary case
        (an item that genuinely declares nothing) costs one indexed lookup.
        """
        row = conn.execute(
            "SELECT depends_on FROM work WHERE project_id = ? AND item_id = ?",
            (project_id, item_id),
        ).fetchone()
        if row is None:
            return []
        declared = json.loads(row["depends_on"] or "[]")
        if not declared:
            return []
        return [
            ReadinessReason(
                kind=REASON_STALE,
                explanation=(
                    f"{item_id} declares {', '.join(declared)} but the dependency graph "
                    "holds no edges for it, so nothing here can say whether those are "
                    "done; run `agent-harness graph rebuild`"
                ),
                state=UNRESOLVED,
                evidence=", ".join(declared),
            )
        ]

    def record_override(
        self,
        project_id: str,
        item_id: str,
        *,
        reason: str,
        who: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """An operator taking responsibility for admitting blocked work.

        Recorded against the revision it was granted at, and never against a
        later one. An override is a judgement about a graph someone looked
        at; inheriting it across a correction would let the next change ride
        in on a decision nobody made about it.
        """
        connection, owned = self._with_conn(conn)
        try:
            revision = self.revision(project_id, conn=connection)
            connection.execute(
                "INSERT OR REPLACE INTO dependency_overrides "
                "(project_id, item_id, revision, who, reason, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, item_id, revision, who, reason, self.now()),
            )
            return revision
        finally:
            if owned:
                connection.close()

    def overrides(
        self, project_id: str, *, conn: sqlite3.Connection | None = None
    ) -> list[dict[str, Any]]:
        connection, owned = self._with_conn(conn)
        try:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM dependency_overrides WHERE project_id = ? "
                    "ORDER BY item_id, revision",
                    (project_id,),
                )
            ]
        finally:
            if owned:
                connection.close()

    # -------------------------------------------------------------- report

    def report(self, project_id: str, *, conn: sqlite3.Connection | None = None) -> GraphReport:
        """The whole graph for one project: edges, cycles, and who is ready.

        One call, because "which items are ready and why not" answered
        item-by-item is a report nobody assembles.
        """
        connection, owned = self._with_conn(conn)
        try:
            revision = self.revision(project_id, conn=connection)
            edges = self.edges(project_id, conn=connection)
            cycles = self.cycles(project_id, conn=connection)
            items = [
                row["item_id"]
                for row in connection.execute(
                    "SELECT item_id FROM work WHERE project_id = ? ORDER BY item_id",
                    (project_id,),
                )
            ]
            # Edge sources that are not work rows still deserve an answer:
            # an edge pointing out of nowhere is exactly the kind of thing a
            # report exists to surface.
            for source in self.sources(project_id, conn=connection):
                if source not in items:
                    items.append(source)
            ready: list[str] = []
            not_ready: list[Readiness] = []
            for item_id in sorted(items):
                state = self.readiness(project_id, item_id, conn=connection, cycles=cycles)
                (ready.append(item_id) if state.ready else not_ready.append(state))
            return GraphReport(
                project_id=project_id,
                revision=revision,
                edges=tuple(edges),
                cycles=tuple(cycles),
                ready=tuple(ready),
                not_ready=tuple(not_ready),
            )
        finally:
            if owned:
                connection.close()

    # ------------------------------------------------- export and rebuild

    def export(self, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """A backup that outlives this schema.

        Deliberately not a restore image: leases and owners are absent,
        because a lease held by a process that is gone is not a fact worth
        restoring.
        """
        connection, owned = self._with_conn(conn)
        try:
            projects = [
                row["project_id"]
                for row in connection.execute("SELECT project_id FROM projects ORDER BY project_id")
            ]
            out: dict[str, Any] = {"format": "agent-harness-graph/1", "projects": {}}
            for project_id in projects:
                out["projects"][project_id] = {
                    "revision": self.revision(project_id, conn=connection),
                    "work": [
                        {
                            "item_id": row["item_id"],
                            "state": row["state"],
                            "attempts": row["attempts"],
                            "branch": row["branch"],
                            "pr_url": row["pr_url"],
                            "depends_on": json.loads(row["depends_on"] or "[]"),
                        }
                        for row in connection.execute(
                            "SELECT item_id, state, attempts, branch, pr_url, depends_on "
                            "FROM work WHERE project_id = ? ORDER BY item_id",
                            (project_id,),
                        )
                    ],
                    "edges": [
                        {
                            "source_item": row["source_item"],
                            "target_kind": row["target_kind"],
                            "target_id": row["target_id"],
                            "required": bool(row["required"]),
                            "resolver": row["resolver"],
                            "provenance": row["provenance"],
                            "malformed": row["malformed"],
                            "revision": row["revision"],
                            "resolved_state": row["resolved_state"],
                            "resolved_evidence": row["resolved_evidence"],
                        }
                        for row in connection.execute(
                            "SELECT * FROM dependency_edges WHERE project_id = ? "
                            "ORDER BY source_item, target_kind, target_id",
                            (project_id,),
                        )
                    ],
                    "overrides": self.overrides(project_id, conn=connection),
                }
            return out
        finally:
            if owned:
                connection.close()

    def rebuild(
        self, project_id: str | None = None, *, conn: sqlite3.Connection | None = None
    ) -> dict[str, int]:
        """Re-derive every edge from `work.depends_on`.

        The supported recovery, and the reason the edge table is allowed to
        be derived at all. It invents nothing: an edge exists here only
        because a work row declares it.

        Rebuilding an intact graph is a no-op and does not move the revision.
        The revision moves exactly where the derived edges differed from the
        stored ones -- which is the only place anyone would want to be told
        something happened.
        """
        connection, owned = self._with_conn(conn)
        try:
            sql = "SELECT project_id, item_id, depends_on FROM work"
            params: list[Any] = []
            if project_id is not None:
                sql += " WHERE project_id = ?"
                params.append(project_id)
            sql += " ORDER BY project_id, item_id"
            rows = connection.execute(sql, params).fetchall()
            # Seeded from the edge table as well as from work, so a project
            # whose last work row was deleted still has its orphaned edges
            # swept. Seeding only from work rows means the project with
            # nothing left is the one project a rebuild never visits.
            edge_sql = "SELECT DISTINCT project_id FROM dependency_edges"
            edge_params: list[Any] = []
            if project_id is not None:
                edge_sql += " WHERE project_id = ?"
                edge_params.append(project_id)
            declared: dict[str, set[str]] = {
                r["project_id"]: set() for r in connection.execute(edge_sql, edge_params)
            }
            revisions: dict[str, int] = {}
            for row in rows:
                project = row["project_id"]
                declared.setdefault(project, set()).add(row["item_id"])
                # The same provenance `add` writes, because it names where the
                # edge was DECLARED, not which code path last wrote the row.
                # Recording "rebuild" would make an intact graph's rebuild
                # look like a change and move the revision for nothing.
                specs = parse_dependencies(
                    json.loads(row["depends_on"] or "[]"), provenance=WORK_DECLARATION
                )
                revisions[project] = self.set_edges(project, row["item_id"], specs, conn=connection)
            # Edges whose source no longer exists as work are not rebuildable
            # from anything, so they go. Reported through the return value
            # rather than removed quietly.
            for project, items in declared.items():
                for source in self.sources(project, conn=connection):
                    if source not in items:
                        connection.execute(
                            "DELETE FROM dependency_edges WHERE project_id = ? AND source_item = ?",
                            (project, source),
                        )
                        revisions[project] = self._bump(connection, project)
            return revisions
        finally:
            if owned:
                connection.close()


def _edge_from_row(row: sqlite3.Row) -> DependencyEdge:
    return DependencyEdge(
        project_id=row["project_id"],
        source_item=row["source_item"],
        target_kind=row["target_kind"],
        target_id=row["target_id"],
        required=bool(row["required"]),
        resolver=row["resolver"],
        provenance=row["provenance"],
        revision=int(row["revision"]),
        updated_at=float(row["updated_at"]),
    )


def find_cycles(adjacency: Mapping[str, set[str]]) -> list[tuple[str, ...]]:
    """Every strongly connected component with more than one member, plus
    every self-edge. Tarjan, iteratively: a deep plan should not be able to
    overflow the stack of the thing that checks it."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    found: list[tuple[str, ...]] = []
    nodes = sorted({*adjacency, *(t for targets in adjacency.values() for t in targets)})

    for root in nodes:
        if root in index:
            continue
        work: list[tuple[str, list[str]]] = [(root, sorted(adjacency.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, pending = work[-1]
            if pending:
                child = pending.pop(0)
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, sorted(adjacency.get(child, ()))))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    found.append(_cycle_path(component, adjacency))
                elif node in adjacency.get(node, ()):
                    found.append((node,))
    return sorted(found)


def _cycle_path(component: Sequence[str], adjacency: Mapping[str, set[str]]) -> tuple[str, ...]:
    """One concrete loop through a component, so the report can name a path
    rather than a set. A set says "these are tangled"; a path says how."""
    members = set(component)
    start = min(members)
    path = [start]
    seen = {start}
    node = start
    while True:
        nxt = next(
            (t for t in sorted(adjacency.get(node, ())) if t in members and t not in seen), None
        )
        if nxt is None:
            return tuple(path)
        path.append(nxt)
        seen.add(nxt)
        node = nxt
