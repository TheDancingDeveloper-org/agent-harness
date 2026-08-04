# Stage G dependency-graph contract report — 2026-08-04

**Status:** Stage G's §6.1 acceptance cases are met deterministically. The
external-resolver path is proven only against injected resolvers and a fake
`gh` runner; no real external system was contacted. Two previously passing
tests asserted the behaviour this stage reverses and were rewritten — see
"Behaviour changed on purpose".

## Configuration under test

- Implementation commit: `660718b771f5d93be3c7a15d716c8d3cf03b0c85`, branch
  `codex/fit-stage-g`.
- Base commit: `afdc3bc998cfc5f6b0e763782023acf3b860de43` ("test: enforce
  full-project typing for Stage A").
- Python 3.14.4, `uv` resolved dev extras, SQLite through the standard
  library.
- No network, no provider credentials, no GitHub mutation, no remote push.
  Every external target in the tests is answered by an injected resolver or a
  fake `gh` runner.
- New modules: `src/agent_harness/graph.py` (the graph) and
  `src/agent_harness/adapters/github_issue.py` (one resolver, in `adapters/`,
  imported lazily by name).
- Storage: the existing `harness.sqlite` queue database, gaining three tables
  and one column. Migration plan written first, at
  `docs/MIGRATION-graph.md`, commit `ac01d99`, before any schema change.

This is repository-verifiable deterministic evidence. It is not a real-fleet,
provider, GitHub-service or multi-project-soak measurement.

## Reproduction

```console
uv run pytest -q
uv run pytest tests/test_stage_g_graph.py tests/test_graph_migration.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Observed on 2026-08-04 at the commit above:

- 715 tests passed in 53.66 s wall-clock (the base commit collected 676; the
  Stage G modules add 33, and two existing tests were rewritten rather than
  added);
- `tests/test_stage_g_graph.py` collected 24 cases, `tests/test_graph_migration.py`
  collected 9; both modules passed in 4.04 s;
- `ruff check` reported no findings; `ruff format --check` reported 74 files
  already formatted;
- `mypy` reported no issues in 70 source files under the project's strict
  settings.

Timings are from a shared build machine running other work concurrently. An
earlier run of the same suite on the same commit took 351 s under heavy load;
the 53.66 s figure is the quiet-machine number, and the base commit measured
47.16 s on the same machine for comparison. Neither number is a benchmark.

## How each §6.1 acceptance case is proven

All five cases come from **one** plan, `STAGE_G_PLAN` in
`tests/test_stage_g_graph.py`, so they are proven against a single document
rather than five convenient fragments. The plan defines G1–G7 and a
```dependencies block.

| §6.1 case | Where in the plan | Test |
|---|---|---|
| (a) metadata dependency | `G2` — `depends on: G1` | `test_a_metadata_dependency_is_a_typed_required_local_edge` |
| (b) arrow notation | `G1 -> G3` in the fenced block | `test_arrow_notation_declares_the_same_kind_of_edge_as_metadata` |
| (c) external target | `G4` — `external:demo-tracker:TICKET-9` | `test_an_external_target_is_reported_with_its_kind_and_resolver_outcome` |
| (d) missing target | `G5` — `depends on: G404` | `test_a_missing_target_is_an_explicit_blocker_with_the_id_that_was_missed` |
| (e) cycle | `G6` and `G7` require each other | `test_a_cycle_is_named_as_a_cycle_rather_than_as_two_items_waiting` |
| all five in one report | — | `test_the_report_covers_all_five_cases_in_one_answer` |
| all five over the API | — | `test_the_api_publishes_the_same_report_under_a_named_schema` |
| all five at the CLI | — | `test_the_plan_command_prints_a_line_for_every_dependency_case`, `test_the_graph_cli_reports_rebuilds_and_exports` |

Each case asserts an explicit report, not merely that the item did not run:

- (a) and (b) assert the same typed edge — `local_work`, `G1`, required — with
  the evidence string `G1 is pending, not done`. Both notations must produce
  the same edge or a plan's two halves would gate differently.
- (c) asserts `target_kind == external_reference`, `resolver == demo-tracker`,
  state `unresolved` with the evidence "resolver 'demo-tracker' has not
  reported an outcome", and then that an injected resolver's `satisfied`
  outcome makes the item ready and is quoted back as evidence.
  `test_an_external_target_with_no_resolver_stays_unresolved` and
  `test_a_failing_resolver_leaves_the_edge_unresolved_not_satisfied` cover the
  two ways an external answer can be absent.
- (d) asserts the reason names `G404` and carries the sentence "a required
  target the graph cannot find is a blocker, not an assumed external
  dependency", and that `parse_plan` reported it at parse time too.
- (e) asserts `cycles() == [("G6", "G7")]` and that each member's readiness
  carries a `cycle` reason whose evidence is the path `G6 -> G7 -> G6`.

**Ready set after re-ingestion** —
`test_re_ingesting_the_same_plan_changes_neither_the_ready_set_nor_the_revision`
re-adds the identical plan and asserts the revision, the ready set and every
edge are unchanged. Its two companions separate the cases that must differ:
`test_finishing_a_dependency_moves_the_ready_set_without_moving_the_revision`
(work finishing is work state, not a graph change) and
`test_re_ingesting_a_corrected_plan_moves_the_revision_and_the_ready_set`.

**Mid-flight correction** —
`test_a_midflight_correction_to_a_missing_target_stops_the_commit_and_says_why`
runs the real direct-API executor against the Stage A fixture repository, and
adds a dependency on a non-existent `A404` while the implementer call is in
flight. It asserts: the item returns to `pending`; the reason names `A404`,
the word `unresolved` and both graph revisions; **no branch exists**; no
reviewer event was recorded; a `dependency_invalidated` event was; and the
implementer call completed normally, i.e. the agent was not killed.
`test_the_session_executor_makes_the_same_check_before_its_own_checkpoint`
proves the same for hosted-session mode, additionally asserting no
`checkpointed` event and `agent_finished` present.

**Same authoritative revision** —
`test_admission_and_the_pre_gate_check_name_the_same_authoritative_revision`
asserts that the revision recorded on the claimed row equals the graph
revision, that the pre-gate `readiness` call reports the same value, and that
a correction moves the current revision while leaving the recorded
`admitted_revision` alone — so a disagreement is legible as "the graph moved"
rather than as an unexplained refusal.

**Override** — `test_an_operator_override_unblocks_exactly_one_revision` and
`test_the_override_route_records_who_and_reports_the_new_readiness` prove the
gate lifts only by an explicit, recorded operator decision, that the edge is
**not** rewritten to `satisfied`, that an override with no reason is refused
(422), and that the next graph correction re-blocks the item.

## How the edge model maps onto the coordination plane

`docs/COORDINATION-PLANE.md` specifies the typed work graph in **§8**, not §6
(§6 is the oversight actor). The Stage G brief cited §6; the implementation
follows §8, which is the section that actually defines the edge. Every field
§8 names is present, as a column in `dependency_edges` or as the identity of
the row:

| §8 requirement | Implementation |
|---|---|
| source work item | `source_item` (with `project_id`) |
| target kind and target identity | `target_kind` ∈ {`local_work`, `external_reference`, `human_decision`, `cross_project_work`}, `target_id` |
| resolver or adapter when external | `resolver`, resolved through `graph.load_resolver`, adapters imported lazily |
| required versus advisory | `required` |
| resolution state | `unresolved` / `blocked` / `satisfied` |
| provenance and evidence | `provenance`, plus `evidence` computed or `resolved_evidence` stored |
| graph revision | `revision` per edge, `graph_revision` per project |

§8's admission rule ("every required edge must be explicitly satisfied before
claim; an unresolved or missing target is not equivalent to a satisfied
external dependency") is now the actual behaviour of `WorkQueue.claim`.

§8.2's post-claim rule is implemented as written: the attempt is marked
invalidated through a `dependency_invalidated` event, the agent is not killed,
and the item cannot cross the next durable or external gate until the
dependency resolves or an explicit operator override is recorded.

### Where this diverges from the document, and why

1. **The message ledger is not built.** §8.1 says admission "records a
   permanent `dependency_found` / `dependency_unresolved` message". There is no
   coordination ledger in this repository yet (COORDINATION-PLANE is marked
   "proposed, not implemented"). Stage G records the same facts in the existing
   append-only audit event stream and exposes them over the API. When the
   ledger lands, those records should move to it. **This is a divergence, not a
   completed requirement.**
2. **Resolution state is stored only for external targets.** §8 lists
   resolution state as an edge field. For local, cross-project and decision
   targets the state is derived from the work row on every read rather than
   stored, so it cannot go stale and there is no second copy of the answer to
   disagree with the queue — which is the repository's standing rule that a
   projection is never a second source of truth. External outcomes are obtained
   by I/O and therefore are stored, with the revision they were obtained at.
3. **Overrides are revision-scoped.** §8.2 requires "an explicit operator
   override has been accepted and recorded" but does not say how long one
   lasts. An override here applies to the graph revision it was granted at
   only. That is a judgement, made because an override inherited across a
   later correction would admit work on a decision nobody made about it.
4. **A human decision resolves against a work row.** §8 lists `human decision`
   as a target kind without saying what satisfies one. `decision:D9` here is
   satisfied when an item `D9` exists in the project and is `done`, reusing the
   queue's existing `blocked` parking for decisions. An alternative design (a
   separate decision register) was not built.
5. **`depends_on` remains a list of strings.** §8 says "replace
   `depends_on: list[str]` with explicit dependency edges". The edges are
   explicit and typed, but the *declaration* is still a list of string tokens
   with a grammar, on the row and on the wire. The edge table is derived from
   it. This was chosen so that the declaration of record stays in one place and
   the edge table stays droppable and rebuildable, which is the entire recovery
   story. If the intent of §8 was to make `depends_on` itself structured, this
   is a partial implementation of that sentence.

## Behaviour changed on purpose

Two tests at the base commit asserted the rule §6 requires be reversed, and
were rewritten rather than deleted:

- `tests/test_work.py::test_a_dependency_outside_the_queue_does_not_block`
  became `test_a_dependency_the_graph_cannot_find_blocks_and_says_so`;
- `tests/test_queue_lifecycle.py::test_a_dependency_tracked_elsewhere_is_not_unmet`
  became `test_a_dependency_the_graph_cannot_find_is_unmet`.

Both now assert the new contract and the explanation it produces. This
strengthens a gate; it does not weaken one. It is stated here because "the
existing suite still passes" would otherwise be misleading: the suite passes,
and two of its assertions are deliberately the opposite of what they were.
`docs/MULTI-PROJECT-PLAN.md` §2.6 recorded this as a known defect ("an unknown
dependency is silently not a blocker"); that document is a dated audit and has
not been rewritten.

## Migration and rebuild procedure, and how it was tested

The plan is `docs/MIGRATION-graph.md`, committed at `ac01d99` **before** any
schema change (commit order is verifiable in `git log`).

The change is additive: three new tables (`dependency_edges`,
`graph_revision`, `dependency_overrides`) created with `CREATE TABLE IF NOT
EXISTS`, and one new column (`work.admitted_revision`) added through the
queue's existing `ADDED_COLUMNS` mechanism. Nothing is dropped, renamed or
narrowed, and `work.depends_on` remains the declaration of record.

Operator procedure, all four verbs exercised by hand at this commit against a
temporary database seeded from `examples/PLAN.md`:

```console
$ uv run agent-harness --db harness.sqlite graph report
widgets: revision 3, 4 edges
  ready: W1
  W2: not ready at graph revision 3: local_work target 'W1' is blocked: W1 is pending, not done
  W3: not ready at graph revision 3: local_work target 'W1' is blocked: W1 is pending, not done
  W4: not ready at graph revision 3: external_reference target 'owner/name#42' is unresolved:
      resolver 'github-issue' has not reported an outcome for 'owner/name#42';
      local_work target 'W3' is blocked: W3 is pending, not done
$ echo $?
4
$ uv run agent-harness --db harness.sqlite graph rebuild
widgets: graph rebuilt, now at revision 3
$ uv run agent-harness --db harness.sqlite graph export --out g.json   # 2,440 bytes
$ uv run agent-harness --db harness.sqlite graph checkpoint
harness.sqlite: WAL folded back into the database file
```

Note the rebuild left the revision at 3: rebuilding an intact graph is a
no-op, which is what makes it safe to run when an operator is unsure. That was
not true in the first implementation — it bumped once per source item — and
was fixed in commit `38673156`.

`tests/test_graph_migration.py` covers the procedure against real SQLite
files, numbered to match §8 of the migration document:

1. `test_a_pre_stage_g_database_upgrades_in_place_without_losing_work` — a
   pre-Stage-G database built by hand from the previous build's DDL, with a
   live claim, opens under the new build keeping state, owner, attempt count,
   branch and pull-request URL, and gains the new tables and column.
2. `test_opening_the_upgraded_database_again_changes_nothing` — idempotent.
3. `test_export_is_plain_json_carrying_every_edge_and_its_provenance` —
   the export is readable JSON containing every work row's identity and every
   edge's kind, identity, required flag, resolver and provenance, and
   deliberately no leases or owners.
4. `test_dropping_the_edge_tables_and_rebuilding_reproduces_the_same_answers` —
   the graph tables are dropped outright, then rebuilt, and the ready set, the
   edge list and every readiness explanation are identical.
5. `test_a_stored_resolver_outcome_survives_a_rebuild`.
6. `test_an_upgraded_but_unrebuilt_database_holds_dependent_work_rather_than_admitting_it` —
   an item that declares dependencies but has no edges is refused with a
   `stale_graph` reason naming `graph rebuild`, while an item that declares
   nothing is unaffected.
7. `test_rebuilding_an_intact_graph_moves_nothing`,
   `test_an_edge_whose_source_no_longer_exists_is_removed_by_rebuild`, and
   `test_checkpoint_folds_the_wal_back_so_a_file_copy_is_a_real_backup`.

Rollback is documented (restore the copied files, run the older build) and is
**not** tested: it would require running a build from the base commit against
a file the new build wrote, which this suite has no mechanism for. Stated as
untested rather than implied.

## Costs and blind spots

- **No external system was contacted.** Every resolver outcome in the tests
  comes from an injected callable or a fake `gh` runner. The GitHub-issue
  adapter's parsing, its three outcomes and its lazy loading are tested; its
  behaviour against a real repository, a rate-limited `gh`, or an issue in a
  repository the token cannot read is unmeasured.
- **No resolver scheduling.** `resolve_external` exists and is deliberately
  never called from `claim` (I/O inside the write transaction that hands out
  work would let one slow ticket system stall a fleet), but nothing calls it on
  a schedule yet. In a deployment today an operator or a cron job has to. This
  is a real gap between "an external dependency is expressible" and "an
  external dependency resolves itself".
- **No multi-project or concurrent-graph soak.** Concurrency is covered only
  by the existing claim-race tests, which now run the graph evaluation inside
  the same transaction. Two processes editing one project's graph
  simultaneously is not exercised.
- **Performance is not characterised.** `claim` now evaluates cycles once per
  scan and readiness per candidate. At the test suite's scale this is not
  measurable against the base commit (47.16 s versus 53.66 s for a suite that
  grew by 39 tests). Behaviour at thousands of items or hundreds of edges per
  item is unmeasured, and `CLAIM_SCAN_LIMIT` still bounds the scan at 200 rows.
- **Cycle detection covers required local edges only.** A loop that passes
  through an advisory edge, a cross-project target or an external target is not
  detected as a cycle. Cross-project loops in particular are genuinely possible
  and are not reported.
- **The coordination message ledger does not exist**, so §8.1's "permanent
  `dependency_unresolved` message" is an audit event today (see divergence 1).
- **Existing databases need `graph rebuild`.** An in-place upgrade leaves the
  edge table empty until work is re-added or a rebuild runs. This fails safe:
  an item whose `depends_on` is non-empty and which has no edges is refused
  with a `stale_graph` reason naming the command. The cost is that an upgraded
  fleet stalls on its dependent work until an operator runs the rebuild, and
  nothing runs it for them. The first implementation of this stage got it
  wrong in the permissive direction — such items were admitted — which is
  recorded here because the report was drafted before the fix.

## Decision

**Stage G's §6.1 acceptance gate is met. Continue.**

The five plan cases, the re-ingestion property and the mid-flight correction
are each proven by a named test against one plan, admission and the pre-gate
check share one evaluation and one revision, and the migration was planned
before the schema changed and is tested including a drop-and-rebuild.

Three things are explicitly **not** claimed. External resolution is expressible
and tested only against injected resolvers, with no scheduler to run it.
Rollback is documented and untested. The coordination ledger §8.1 assumes does
not exist, so dependency findings live in the audit stream rather than in a
permanent per-project room. A later run must append a new package rather than
edit this one.
