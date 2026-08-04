# Fit-for-purpose programme — status and handoff

**Frozen:** 2026-08-04. **Rewritten 2026-08-04 after stages F, D, K, H, L, J and M.**
**Read this before doing any work on the proposal.**

> ## Where it actually got to
>
> Everything below §1 describes the state *before* the integration, and is kept
> because the per-branch history in §3 and §4 is still the record of how each
> stage was verified in isolation. **It is out of date wherever it says nothing
> is merged.**
>
> The current state is:
>
> | | |
> |---|---|
> | Branch | `fix/validator-rejects-valid-patches` at `3dbb764` |
> | Merged | all four stage branches, plus seven further stages |
> | Gates | `pytest` **1063 passed**, ruff, format and full-project strict `mypy` all clean |
> | Stage 8 | still blocked, and still not startable from this repository |
> | Pushed | **nothing.** 26 commits on the branch (48 counting the merged branches' own history), all local. |
>
> Delivered since the freeze: **F** (integration), **D** (first run — the
> deferral was lifted by explicit go-ahead), **K** (outcome and check
> taxonomy), **H** (resumable attempts), **L** (item budgets), **J** (durable
> human hold) and **M** (telemetry export). Each has its own report in
> [`evidence/`](evidence/), and
> [`evidence/2026-08-04-programme-report.md`](evidence/2026-08-04-programme-report.md)
> is the §11 report over all of them — **read that one first.**
>
> **D11 and D12 were resolved by decision** and are recorded in
> [`PROPOSAL-2026-08-finish-then-extend.md`](PROPOSAL-2026-08-finish-then-extend.md)
> §11.1. **D7, D8 and D9 remain open**, untouched.
>
> **The three things a later agent should not assume**, because they are the
> ones this document's earlier text would leave you assuming:
>
> 1. **Nothing here is evidence about live behaviour.** Not one provider was
>    contacted in any stage. Deterministic success is proven; live success is
>    not, and Stage 8 is what would change that.
> 2. **Session mode is behind.** Stages H and L are the direct-API executor
>    only. A session-mode fleet has no resumable attempts and no per-item
>    budgets.
> 3. **The per-branch test counts in §4 are history.** The number is **1063**,
>    derived on the merged tree.
>
> The suggested next actions in §6 are done. The current ones are §8 of the
> programme report.

This document is the pickup point for
[`PROPOSAL-2026-08-fit-for-purpose.md`](PROPOSAL-2026-08-fit-for-purpose.md).
The proposal says what the programme is; this says how far it got, where the
work physically lives, and what a later agent must not assume.

It is a working status document, not an evidence package. It may be rewritten
as reality changes. The reports in [`evidence/`](evidence/) may not — those are
append-only (see [`evidence/README.md`](evidence/README.md)).

## 1. The one-paragraph summary

**As of the freeze**, before the integration above. Kept as the record of what
the earlier session handed over.

Stages 0, A and E2 were complete before this session. This session completed
**E1, C, B and G** — each on its own branch, each with its own evidence report
and all four gates passing on that branch alone. **Stage D was deliberately not
started**, on instruction. Stage 8 (live validation) remains blocked.

**Nothing has been pushed, no PR has been opened, and nothing has been merged.**
The integration and the merged-tree verification are the next work, and they
have not begun. Every stage sits on its own branch in its own worktree, and
every worktree is committed and clean — there is no uncommitted work anywhere
in this programme.

## 2. Where the work physically lives

The integration branch is `fix/validator-rejects-valid-patches`, in the main
checkout at `/home/sprooty/Working/Active/apps/agent-harness`. At freeze time
it is at `afdc3bc`, which already contains stages 0, A and E2.

Each remaining stage is a **separate branch in a separate worktree**, all based
on `afdc3bc`. None of them has been merged.

| Stage | Branch | Worktree (under `../agent-harness-worktrees/`) |
|---|---|---|
| E1 | `codex/fit-stage-e1` | `fit-stage-e1` |
| C | `codex/fit-stage-c` | `fit-stage-c` |
| B | `codex/fit-stage-b` | `fit-stage-b` |
| G | `codex/fit-stage-g` | `fit-stage-g` |

`git worktree list` is the authority. Other worktrees exist (`chat-*`,
`fixes`, and two under `.claude/worktrees/`) — **they belong to unrelated
earlier work and are not part of this programme.**

## 3. Stage-by-stage state

The gate rule from the proposal §10 governs everything below: *a stage cannot
be marked complete because its code exists; its report and its gate must pass.*

| # | Stage | State | Evidence report |
|---|---|---|---|
| 0 | Evidence package | complete, in `afdc3bc` | `evidence/2026-08-03-04-ngms-first-sustained-run-v1.md` |
| 1 | A — deterministic e2e | complete, in `afdc3bc` | `evidence/2026-08-04-stage-a-deterministic-slice.md` |
| 2 | E2 — context selection | complete, in `afdc3bc` | `evidence/2026-08-04-stage-e2-context-selection.md` |
| 3 | E1 — protocol experiment | **complete**, on branch | `evidence/2026-08-04-stage-e1-change-protocol.md` |
| 4 | C — adoption | **complete**, on branch | `evidence/2026-08-04-stage-c-adoption.md` |
| 5 | G — graph | **complete**, on branch | `evidence/2026-08-04-stage-g-graph-contract.md` |
| 6 | B — providers | **complete**, on branch | `evidence/2026-08-04-stage-b-provider-protocols.md` |
| 7 | D — first run | **NOT STARTED — deferred by instruction** | none |
| 8 | Validation | **blocked** | none |

### Stage E1 — complete

Two commits (`07d67f1`, `00a7995`). **Zero lines changed under `src/`** — the
alternative protocols are test-only and production still names the unified diff
as its sole format, which is what proposal §4.1 requires (no protocol removed
before the report exists).

The decision is recorded as **D10 in `docs/backlog-seed-2026-08-02.json`**
(named `docs/backlog.json` when Stage E1 wrote it; renamed in Stage F), following the
repository's existing `type:decision` convention for D1–D9. There is no ADR
directory and one should not be created.

Decision: retain model-authored unified diff, keep search/replace as the named
alternative, do not adopt unguarded whole-file replacement. Whole-file was the
only protocol with a non-zero wrong-location rate (1/10), which §4 makes
disqualifying, *and* the highest clean-application rate (7/10) — the same
result read two ways.

**The most important limitation, and it is load-bearing:** failure *frequency*
is not measured. Every rate is an outcome over a hand-chosen case mix. The
decision is therefore about **containment, not expected success rate**. Do not
cite these numbers as a success-rate baseline.

### Stage C — adoption — complete

Three commits (`bc36e1c`, `be5186f`, `db327d3`). `adoption.py` is 1070 lines;
`tests/test_adoption.py` has 32 tests. No API route or schema changed.

Known gaps, all written up in the evidence report:

- "Repeated adoption produces the same report" holds **in effect, not in bytes,
  for the first cycle** — before the first reconciliation the queue has no rows,
  so report 1 says "would create" where report 2 says "would refresh". Reports
  2 and 3 are byte-identical. Forcing byte-equality would mean asserting a
  state that does not exist.
- Real GitHub behaviour is unmeasured; the `gh` runner is a fake. In
  particular `gh pr list --json ...,headRefName,url,isCrossRepository` has
  never been run against a live `gh`.
- Assessor model quality is entirely unmeasured — only the contract around it
  is tested. There is **no cost cap**: one assessor call per unresolved item,
  no budget, no batching.
- `partial` is recorded and then ignored.
- Adoption is CLI and library only; there is no HTTP surface, so a session host
  cannot offer it as a screen. §5 specifies a command, so this is not an unmet
  criterion — but it is a real product gap.

**D8 was deliberately not answered.** `verify:` is a plan-declared argv run
through the existing `executor.Checks`, not a new gate type. Whether item
verification *should* become a plugin is exactly the open D8 question.

### Stage B — providers — complete

Five commits (`92c04c1` … `8eca348`). Suite went 676 → **821 passed**, with
lint, format and strict typing clean.

`Provider` was a failure classifier while the CLI transport was separately
hard-wired to one gateway's path, header and response shape. These are now
separate. New core module `protocols.py`: a `RoutePreset` bundles a request
adapter, an auth strategy, a response/usage reader and a failure classifier —
all configured objects, not branches. `providers.py` is now vendor-free;
`CLAW_BAY` moved to `adapters/claw_bay.py`, joined by a new
`adapters/chat_completions.py`.

**Presets resolve by name through three doors**: `protocols.register()`
in-process, `HARNESS_ROUTE_PRESETS="name=module:attr"` in configuration, and an
`agent_harness.route_presets` entry point for an installed distribution. Core
never imports an adapter — it reads entry-point *metadata* and loads only the
name asked for. This repository's own two adapters deliberately go through the
entry-point door, so that "addable without editing core" is not true only for
the ones we ship.

Four configurations run through one conformance suite (`generic`,
`chat-completions`, `claw-bay`, `fixture-plugin`), three of which differ on the
wire.

Gaps, all in the evidence report:

- **Nothing was measured against a real gateway.** No request left the process.
  The claw-bay *classifier* uses live-captured payloads, but the *wire* half of
  that preset is asserted only against the fixture.
- **Issue #128 is untouched** — session-mode implementer traffic still bypasses
  `ModelClient` telemetry entirely.
- **Two behaviour changes**: a stored route with neither `provider` nor
  `preset` now gets the generic classifier rather than claw-bay (the CLI always
  writes `provider`, so it is unaffected), and `providers.CLAW_BAY` no longer
  exists. The legacy `provider` field keeps working.
- The entry-point path depends on installed dist metadata; a stale editable
  install would fail to resolve the shipped presets. The CLI refuses loudly,
  but no test covers it — a test cannot uninstall the package running it.
- **Operational gotcha:** the generic preset appends no path, so a generic
  route given a base URL will 404 until an operator configures a path or names
  a preset. That is the honest consequence of a preset that claims nothing, but
  it will look like a bug to whoever hits it first. Documented, not tested
  against a live server.

**This stage edited `AGENTS.md`** — added `protocols` to its enumeration of
core modules and one row to its "where things are" table. A factual correction
under that document's own rule 7. No rule was changed.

### Stage G — graph — complete

Twelve commits (`ac01d99` … `1af2b2d`). Suite 676 → **715 passed in 54.4s**,
lint/format/strict-typing clean.

New `src/agent_harness/graph.py`, three tables (`dependency_edges`,
`graph_revision`, `dependency_overrides`) and one additive column
(`work.admitted_revision`). `depends_on` stays `list[str]` on the row and on the
wire, but each string is now a **token with a grammar**: `T1`,
`external:RESOLVER:IDENTITY`, `decision:D9`, `project:OTHER/T1`, and `?` for
advisory. Edges are *derived* from it, which is what makes the edge table
droppable and rebuildable. Cycle detection is iterative Tarjan reporting a
concrete path. `adapters/github_issue.py` is the one format-aware resolver;
core knows only its name and loads it lazily.

`WorkQueue.claim` admits from `readiness()` and records the revision it admitted
at. **Both** executors now re-check `readiness()` before their durable gate —
session mode previously had no re-check at all, which was a real parity gap in a
gate. Operator overrides are recorded and revision-scoped, and do **not** mark
the edge satisfied.

`resolve_external` is deliberately never called from `claim`: I/O inside the
write transaction that hands out work would let one slow tracker stall a fleet.

**Correction to the brief I gave it:** the typed work graph is
**§8 of `COORDINATION-PLANE.md`**, not §6 — §6 is the oversight actor. It
implemented §8 and said so. Five divergences from §8 are listed in its evidence
report; the load-bearing one is below.

Unmet or partial, all in the evidence report:

- **§8.1's permanent `dependency_unresolved` message is not written to a
  ledger** — the coordination ledger does not exist in this repository. The
  facts go to the existing append-only audit stream instead. **A gap, not a
  completed requirement.**
- **Two existing tests asserted the behaviour §6 orders reversed, and were
  rewritten rather than deleted**: `test_a_dependency_outside_the_queue_does_not_block`
  and `test_a_dependency_tracked_elsewhere_is_not_unmet`. This *strengthens* a
  gate, but it means "the 676-test suite still passes unchanged" is **false**.
  Stated here rather than buried.
- **Nothing schedules `resolve_external`.** An external dependency is
  expressible and gated, but an operator or cron must run resolution. A real gap
  between "expressible" and "resolves itself".
- **Cycle detection covers required local edges only.** Cross-project loops are
  genuinely possible and are not reported.
- No external system was contacted; every resolver outcome is an injected
  callable or a fake `gh` runner.
- Rollback is documented and **untested** — it needs an older build run against
  a newer file, which this suite cannot do.
- No multi-project or concurrent-graph soak; performance at scale unmeasured.
- It recorded a self-inflicted defect rather than hiding it: the first
  implementation admitted dependent work on an upgraded-but-unrebuilt database
  (the permissive direction). Fixed in `660718b`; such items now block with a
  `stale_graph` reason naming the command to run.

`docs/MIGRATION-graph.md` was committed in `ac01d99`, **before** the first
schema change in `fce722b` — verifiable in `git log`, as §6.1 requires.
`tests/test_graph_migration.py` (9 tests) covers upgrade-without-loss from a
hand-built pre-Stage-G database holding a live claim, idempotent re-open, JSON
export, drop-and-rebuild reproducing identical answers, resolver outcomes
surviving rebuild, and a WAL checkpoint making a file copy a real backup.

### Stage D — not started

Deferred on explicit instruction during this session. The specification is
proposal §8 and its acceptance is §8.4. **Do not start it without an explicit
go-ahead.**

Consequence worth naming: Stage D owns `init --demo` and `doctor`, which is
what the proposal relies on for "a clean checkout runs with no credentials or
network". Until it exists, the stranger-on-a-laptop path is **unproven**, and
README first-run claims stand as they are.

### Stage 8 — validation — blocked

Requires credentials, network access, a real second repository, and the NGMS
human decisions that the proposal §9 explicitly refuses to answer by
assertion. Not startable from this repository alone.

## 4. Verification — read this before trusting any timing

**Wall-clock test timings from this session are unusable, and the cause is
known — set `TMPDIR` before you debug anything.**

Three runs of an *identical* tree took 276s, 587s and 617s against a ~49s
pre-session baseline. The cause is **not** the code and not merely CPU
contention: this machine's `/tmp` is on a slow shared disk, and the suite
creates temporary git repositories heavily. With up to four agents' suites
saturating it, a full run could fail to finish at all.

Pointing `TMPDIR` at the NVMe volume took a full 821-test run to **116
seconds**. Do that first:

```console
TMPDIR=/path/on/fast/volume uv run pytest -q
```

Do not treat a slow suite as evidence of a defect, and do not quote this
session's durations in an evidence package.

One pre-existing timing-sensitive test,
`test_an_agent_slower_than_the_lease_keeps_its_item`, failed once under heavy
load and passed on its own and on every subsequent run. It is not on any stage
path touched here, but it is the test most likely to flake on a loaded machine.

### Test counts are per-branch and do not add up

Each branch was verified **alone, on top of `afdc3bc`**. Nothing has been merged,
so **no merged-tree verification exists**:

| Branch | Suite result, that branch alone |
|---|---|
| base `afdc3bc` | 676 passed |
| `codex/fit-stage-e1` | 678 passed |
| `codex/fit-stage-c` | 708 passed |
| `codex/fit-stage-g` | 715 passed |
| `codex/fit-stage-b` | 821 passed |

These are **not** additive and the merged total is unknown. Stage G also
rewrote two pre-existing tests (see §3), so "the original 676 still pass
unchanged" is false. Re-derive the number on the merged tree; do not carry any
of these figures forward.

The four gates every stage must pass, from the relevant worktree root:

```console
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

`mypy` is configured for full-project strict typing.

## 5. Rules a later agent must not rediscover the hard way

These are enforced by [`../AGENTS.md`](../AGENTS.md) and the proposal §1.2, and
each one caught a real defect in this session:

- **The core stays generic.** No vendor or workload specifics in core modules;
  tool-specific formats live in `adapters/` and are imported lazily.
- **Report honestly.** An unmet criterion is reported as unmet. Every stage
  report in `evidence/` has a blind-spots section; a report without one is
  wrong.
- **A gate is never weakened to make scaffolding simpler.**
- **Never retry a cost cap.** Retry/parking state is local to the
  worker/endpoint/role, never fleet-wide.
- **Checkpoint before the expensive gate.**
- **Events are append-only**; projections are never a second source of truth.
- **Ambiguous state is reported or sent to a human, never silently guessed.**
  Uncertainty biases toward "not done".
- **Do not guess at an open decision to unblock yourself** — say it is blocked.
  D7, D8 and D9 remain open. D10 was resolved by Stage E1.
- Evidence reports are **append-only**. A later run adds a new dated report; it
  does not edit an existing one.
- Commit style: lowercase, conventional-commit prefix, describing the
  behaviour in plain words. No `Co-Authored-By` trailers.

## 6. Suggested next actions, in order — **all done**

Kept as the record of what was suggested and in what order, because the
integration was carried out in exactly this order and the Stage F report refers
back to it. For what to do *now*, see §8 of the programme report.

1. Merge the four stage branches into `fix/validator-rejects-valid-patches`,
   with `TMPDIR` on a fast volume. **Expect conflicts** — the overlaps are:

   | File | Touched by |
   |---|---|
   | `__main__.py` | C, B, G |
   | `schemas.py` | B, G |
   | `plan.py` | C, G |
   | `api.py` | B, G |
   | `executor.py`, `session_executor.py` | G |
   | `AGENTS.md` | B |

   Suggested order: **B → G → C → E1** (B restructures routes, G restructures
   admission, C is additive over both, E1 is test-and-docs only).
2. Re-run the four gates **on the merged tree** and derive the real test count
   there. No merged verification exists yet.
3. Write the proposal §11 per-stage programme report over the delivered stages.
   Record Stage D as not started by instruction and Stage 8 as blocked — do not
   report either as complete-with-caveats.
4. Only then consider Stage D, and only with an explicit go-ahead.

A merge conflict here is a **semantic** question, not a textual one: B and G
both changed what a route/admission decision is made of. Resolve by re-reading
each stage's evidence report, not by picking a side of the diff.

Nothing in this programme has been pushed or merged. That is deliberate — the
integration and the merged-tree verification are still to do.
