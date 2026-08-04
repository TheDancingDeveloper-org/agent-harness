# Migrating the queue database to the typed dependency graph

**Status:** written before the schema change, as required by
[`docs/PROPOSAL-2026-08-fit-for-purpose.md`](PROPOSAL-2026-08-fit-for-purpose.md)
§6.1. Read it before upgrading a database that holds work you care about.

"The queue is disposable" is a statement about what the queue *means*, not a
licence to assume an in-place upgrade is safe. A deployed harness has claims in
flight, attempt counts that stop a poison item being retried forever, branch
names, and pull-request URLs that exist nowhere else. Losing those is losing
work. So the upgrade below is additive, the rebuild is explicit, and both are
tested.

## 1. What changes

Stage G replaces the untyped `work.depends_on` *interpretation* with a typed,
revisioned dependency graph. Three things are added to the queue database
(`harness.sqlite` by default):

| Object | Kind | Purpose |
|---|---|---|
| `dependency_edges` | new table | one row per declared edge |
| `graph_revision` | new table | one monotonic revision per project |
| `dependency_overrides` | new table | operator overrides, with the revision each was granted at |
| `work.admitted_revision` | new column | the graph revision a claim was admitted at |

`work.depends_on` is **not** dropped, **not** rewritten, and remains the
declaration of record. The edge table is derived from it and from the plan; it
is a projection that can be thrown away and rebuilt, which is what makes the
rebuild procedure below possible at all.

Nothing is dropped, renamed or narrowed. There is no destructive statement in
the upgrade path.

## 2. How the upgrade runs

`work.py` deliberately has no schema version number — a version number is one
more thing that can disagree with the database it describes. Migration decides
by inspecting the tables, on every open, and is therefore idempotent:

- the new tables are created with `CREATE TABLE IF NOT EXISTS`;
- `work.admitted_revision` is added through the existing `ADDED_COLUMNS`
  mechanism, which is additive-only: an older build re-opening the database
  still reads every column it knows about, and ignores the new one;
- edges are written when an item is added or refreshed, so a database upgraded
  in place has an **empty** edge table until either work is re-added or
  `agent-harness graph rebuild` runs.

That last point is the one that can surprise someone. It is deliberate, and it
fails safe in the direction the stage requires: an item with `depends_on`
entries and no edges is **not ready**, because an unbuilt graph is an unknown
graph, and Stage G's whole rule is that unknown is a blocker rather than an
assumed pass. See §5.

## 3. Backup, before anything

WAL mode means nearly all of a live database can be in the `-wal` sidecar; a
backup that copies only the `.sqlite` file can silently take almost nothing.
So the supported backup is:

```bash
# 1. stop the fleet (the API's stop action, or just stop the process)
# 2. fold the WAL back into the main file
uv run agent-harness --db harness.sqlite graph checkpoint
# 3. copy all three files if they exist
cp harness.sqlite harness.sqlite.bak
cp harness.sqlite-wal harness.sqlite-wal.bak 2>/dev/null || true
cp harness.sqlite-shm harness.sqlite-shm.bak 2>/dev/null || true
```

A file copy is a byte-level backup and restores by copying back. It is tied to
the SQLite schema of the build that wrote it, which is why the export below
exists as well.

## 4. Export: a backup that survives a schema change

```bash
uv run agent-harness --db harness.sqlite graph export --out graph.json
```

The export is plain JSON and contains, per project: the revision, every work
row's identity, state, attempts, branch, pull-request URL and `depends_on`, and
every edge with its kind, identity, required flag, resolver, provenance and
stored resolver outcome. It is readable without this codebase, diffable, and
safe to keep in an operations repository.

The export is a **record**, not a restore image: it does not carry leases or
owners, because a lease from a process that is gone is not a fact worth
restoring.

## 5. Rebuild: the supported recovery

```bash
uv run agent-harness --db harness.sqlite graph rebuild
uv run agent-harness --db harness.sqlite graph report
```

`rebuild` re-derives every edge from the authoritative declarations
(`work.depends_on`). It does **not** invent edges, and it does not clear stored
resolver outcomes for external targets that are still declared — an outcome is
evidence, and discarding evidence to make a rebuild tidier would turn a
resolved external reference back into an unresolved one for no reason.

Rebuilding an intact graph is a **no-op**: the revision does not move, and no
live claim is invalidated. The revision moves exactly where the derived edges
differed from the stored ones. That is what makes `rebuild` safe to run
whenever anyone is unsure, which is the only time anyone runs it.

`report` then prints, per project: the revision, the ready set, every
unresolved and blocked edge with its evidence, every external target with its
resolver outcome, and every cycle. Comparing that report before and after an
upgrade is the check that the migration did what it said.

Because the edge table is derived, **rebuilding is always safe**. If a future
change alters the edge schema again, the procedure is: export, upgrade, rebuild,
compare reports.

## 6. Rollback

1. Stop the fleet.
2. Restore the backup copied in §3.
3. Run the older build.

Rolling *forward* again is the same upgrade, because it is idempotent. An older
build reading a database an upgraded build has written works: it ignores
`admitted_revision` and the three new tables entirely, and its own dependency
handling still reads `depends_on`. The one thing it does not do is enforce the
Stage G admission rule — which is a downgrade of a gate, and is the reason
rollback is a deliberate operator action rather than something the code does on
its own.

## 7. What an operator will actually notice

The behaviour change is intentional and is the point of the stage: a required
dependency naming something the queue does not hold used to be treated as
satisfied, on the grounds that it might be tracked elsewhere. It is now a
blocker. Concretely:

- an item whose `depends_on` contains a typo stops instead of running, and
  `GET /api/work/{item_id}/readiness` says which target could not be resolved;
- a genuine external dependency must say so — `external:<resolver>:<identity>`
  in the plan — and must have a recorded resolver outcome before it counts as
  satisfied;
- after an in-place upgrade with no rebuild, items with dependencies are held
  until `graph rebuild` runs or the plan is re-ingested.

Before upgrading a database with a live backlog, run:

```bash
uv run agent-harness --db harness.sqlite graph rebuild
uv run agent-harness --db harness.sqlite graph report
```

and read the unresolved list. Every entry on it is an item that was previously
running on an assumption nobody had checked.

## 8. How this procedure is tested

`tests/test_graph_migration.py` covers, with no mocks of the database:

1. a pre-Stage-G database, built by hand from the shipped DDL of the previous
   build, opens under the new build with every row, state, owner, attempt count
   and branch intact, and gains the new tables and column;
2. re-opening the same file a second time changes nothing (idempotent);
3. `export` of a populated graph is JSON containing every edge and its
   provenance;
4. dropping the edge tables and running `rebuild` reproduces exactly the same
   ready set and the same readiness explanations as before the drop;
5. a stored external resolver outcome survives a rebuild;
6. an upgraded-but-not-rebuilt database holds dependent items rather than
   admitting them, and the readiness explanation says why.
