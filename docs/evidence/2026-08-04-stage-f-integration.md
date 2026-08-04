# Stage F integration report — 2026-08-04

**Status:** the four unmerged stage branches are merged and the merged tree
passes all four gates. This is the first merged-tree verification the programme
has ever had. It says nothing about live behaviour.

Stage F is specified in
[`PROPOSAL-2026-08-finish-then-extend.md`](../PROPOSAL-2026-08-finish-then-extend.md)
§4. It exists because §10 of the original proposal will not let an action
without a gate complete anything, and four stages had per-branch gates and no
merged-tree gate.

## 1. Configuration under test

| | |
|---|---|
| Integration branch | `fix/validator-rejects-valid-patches` |
| Base before this stage | `01be448` (docs only, over `afdc3bc`) |
| Result commit | `a33fd84` |
| Merged, in order | `codex/fit-stage-b` (`8eca348`), `codex/fit-stage-g` (`1af2b2d`), `codex/fit-stage-c` (`db327d3`), `codex/fit-stage-e1` (`00a7995`) |
| `TMPDIR` | a directory on the NVMe volume, not `/tmp` — see §7 |
| Network, credentials, provider traffic | none; nothing left the process |

The merge order is the one
[`FIT-FOR-PURPOSE-STATUS.md`](../FIT-FOR-PURPOSE-STATUS.md) §6 derived with the
worktrees in front of it: B restructures routes, G restructures admission, C is
additive over both, E1 is test-and-docs only. It was not re-derived here.

## 2. Commands run, and the result

From the integration checkout root:

```console
TMPDIR=/path/on/fast/volume uv run pytest
uv run ruff check .
uv run ruff format --check .
TMPDIR=/path/on/fast/volume uv run mypy
```

Observed on 2026-08-04, on `a33fd84`:

| Gate | Result |
|---|---|
| `pytest` | **895 passed** |
| `ruff check .` | all checks passed |
| `ruff format --check .` | 214 files already formatted |
| `mypy` | success, no issues in 80 source files |

**895 is the real merged count and it is derived here, not carried forward.**
The per-branch figures — 676 base, 678 E1, 708 C, 715 G, 821 B — are not
additive, Stage G rewrote two pre-existing tests, and "the original 676 still
pass unchanged" was already known to be false before this stage began. None of
those numbers appears anywhere in this report as a claim about the merged tree.

Intermediate counts, recorded because they show where the tests arrived rather
than being facts about the delivered tree: 861 after B+G with the divergence in
§3.1 resolved, 893 after C, 895 after E1.

`mypy` is configured for full-project strict typing, so the 80-file figure is
the whole project, not a subset.

## 3. Divergences found while resolving conflicts

Two files conflicted textually. One stage pair conflicted **semantically
without conflicting textually**, which is the case this stage was most worried
about and the only one that required a decision.

### 3.1 Stage B and Stage G stated the same rule and only one of them could state it in code

**This is the load-bearing divergence.** Git merged both branches cleanly and
the merged tree failed a gate.

Stage B added `test_no_module_on_the_path_names_a_vendor_preset`: no core
module on the execution path may contain a dotted path to an adapter module,
because a lazy import written as a string is still core knowing what a
particular vendor is called. Stage G added `graph.py` to that execution path,
and `graph.py` held:

```python
ADAPTER_RESOLVERS = {
    "github-issue": "agent_harness.adapters.github_issue",
}
```

Both stages' evidence reports claim the same property. Stage B's says presets
resolve by name through metadata so core never imports an adapter; Stage G's
says core knows only a resolver's name and loads it lazily. They were written
apart, reached the same principle, and implemented it two ways — and Stage G's
way was the one its own author would have rejected had Stage B's test existed
on that branch.

**Resolved by making G's lookup use B's mechanism**, not by picking a side and
not by relaxing the test. `graph.py` now reads the
`agent_harness.dependency_resolvers` entry-point group from installed
distribution metadata; `pyproject.toml` declares
`github-issue = "agent_harness.adapters.github_issue:resolver"`. Core imports
one adapter, on demand, only when a token names it. Commit `affc0b6`.

**What this changed about Stage G's report.** Its description of resolver
lookup — a name-to-module map in `graph.py` — no longer describes the code.
The behaviour it documents is unchanged: `external:github-issue:owner/name#42`
resolves through the same adapter, lazily, and an unknown resolver still leaves
the edge `unresolved` rather than raising inside a claim scan. What changed is
where the name-to-module mapping lives.

**What was deliberately *not* built.** Stage B gives presets three doors:
in-process `register()`, a `HARNESS_ROUTE_PRESETS` environment variable, and
entry points. Resolvers got **one** door plus the `extra` argument
`load_resolver` already had. Mirroring all three would have been symmetrical
and would have been new capability that no stage specified, which §4.2 forbids
an integration stage from adding. The asymmetry is a real gap: a deployment
with a resolver that is not an installed distribution and cannot pass `extra`
has nowhere to declare it. Named here rather than closed here.

`tests/test_generic.py` gained
`test_the_shipped_dependency_resolvers_are_declared_the_same_way`, which is the
assertion that the resolution holds. It is a test of the merge, not a new
capability.

### 3.2 `plan.py` — a docstring conflict where both halves were true

Stage G documented the two dependency notations and the token grammar; Stage C
documented `verify:` as a JSON argv array. Both describe the merged module.
Both kept, in that order. No code conflicted.

### 3.3 `__main__.py` — two subcommands added at the same line

Stage G added `graph`, Stage C added `adopt`, both immediately after `plan`, in
the parser construction and again in the dispatch chain. Both kept. No
behaviour question was involved.

### 3.4 Auto-merges that were checked rather than trusted

Four files were touched by two stages each and merged with no marker. A clean
auto-merge is not evidence, so each was inspected for a hunk one branch had and
the merged tree does not:

| File | What the merge chose, and why it is right |
|---|---|
| `api.py` | Stage G's project-scoped `_role_routes(queue, project)` **with** Stage B's `default_preset` parameter threaded through it. Neither stage's version survives alone; the combination is what both reports describe. |
| `executor.py`, `session_executor.py` | Stage G's `readiness()` re-check replaces the older `unmet_dependencies()` list walk that Stage B inherited from the base, and Stage B's preset response reader replaces Stage G's inherited `_text_of`. Both replacements are the later stage's own work superseding base code, not one stage overwriting another. |
| `schemas.py` | Field descriptions from both; the two differing lines are each stage's own rewrite of a base description. |

**Verified positively, not just by absence:** both executors still re-check
`queue.readiness()` before their durable gate and both still compare
`record.admitted_revision` against it. That parity — session mode previously
had no re-check at all — is the gate Stage G strengthened, and a merge that
silently dropped it would have passed every other check in this report.

### 3.5 An evidence report now has a dead link, and it was left dead

`docs/evidence/2026-08-04-stage-e1-change-protocol.md` links to
`../backlog.json`, which §5 renamed. Evidence reports are append-only, so it
was not edited. The correction is this paragraph: D10 is in
`docs/backlog-seed-2026-08-02.json`, same content, same position.

## 4. What this stage did not do

- **No new capability.** The only behaviour change is §3.1, which exists to
  make two stages agree and adds nothing either stage did not claim.
- **No stage is marked complete below on per-branch evidence.** Every stage
  reported as delivered had its own tests re-run on the merged tree as part of
  the 895.
- **No gate was weakened.** The one gate that failed on the merged tree was
  satisfied by changing the code it caught, not the test.

## 5. Housekeeping (proposal §2.1)

- `docs/backlog.json` → `docs/backlog-seed-2026-08-02.json`. The name now
  states what the file is: the manifest that seeded the issues on that date. It
  has no state field, so it never could report status, and it had drifted — 56
  items against 100 issues on GitHub, 44 of which it never held.
- `T43` backfilled (58 items now, `D1`–`D10`, `E0`–`E4`, `T1`–`T43`). `D10` was
  already present, having arrived with Stage E1.
- `AGENTS.md` now names GitHub as the issue tracker per D1 and the seed as
  historical. `README.md`, `docs/HARNESS-PLAN.md` and `tests/test_backlog.py`
  follow the rename.
- No sync between the file and GitHub was built, per §2.1.4.
- `tests/test_backlog.py` keeps `MILESTONES = {"P0"…"P4"}`. That is the
  superseded phase order and it is still load-bearing for this file, because
  the items in it were filed under it. The docstring now says so. **A new stage
  naming would still fail this test** — the proposal's §2 finding is unfixed,
  only labelled.
- Issue **#146 closed**, with a comment recording that Stage E2 fixed it
  deterministically and that the live NGMS improvement is unmeasured and not
  reproducible from this repository.

## 6. Per-stage programme report (proposal §11)

Every row below is a **repository fact** on `a33fd84` unless it says otherwise.
No row is a live observation. Costs are §8.

| Stage | Criterion | Observed on the merged tree | Decision |
|---|---|---|---|
| 0 — evidence package | a published package exists | `2026-08-03-04-ngms-first-sustained-run-v1.md` | complete |
| A — deterministic e2e | a slice runs end to end with no network | passes within the 895; full-project strict typing enforced | complete |
| E2 — context selection | the implementer sees what it patches | passes within the 895; closes #146 deterministically | complete |
| E1 — change protocol | a protocol decision settled by comparison, no protocol removed before the report | passes within the 895; **zero lines under `src/`**; D10 recorded | complete |
| C — adoption | a project with existing work can be adopted and reconciled | passes within the 895; `adopt` reachable from the merged CLI | complete |
| G — typed work graph | a required target the graph cannot resolve is a blocker | passes within the 895; readiness re-check present in **both** executors; resolver lookup changed per §3.1 | complete |
| B — provider protocols | a route says how an endpoint is spoken to, not only who it is | passes within the 895; four configurations through one conformance suite | complete |
| D — first run | `init --demo` and `doctor` | **not started, deferred by instruction.** Not complete, not complete-with-caveats. | see §9 |
| 8 — validation | live runs, second repository, NGMS | **blocked** on credentials, network, a real second repository and human decisions §9 of the original proposal will not answer by assertion | blocked |

**Continue/stop, for the programme:** continue. The integration risk the
proposal called the highest-value asset in the programme is discharged — the
four branches are merged and verified together for the first time.

## 7. Timing

Not quoted as a measurement. `TMPDIR` was pointed at the NVMe volume before
anything was run, per the status document §4 and proposal risk R6. The suite
completed in roughly two and a half minutes on a machine also doing other work;
that number is offered as reassurance that the pathology described in the
status document was avoided, not as a benchmark. There is no before/after
comparison here and none should be inferred.

The `/tmp` pathology itself was not reproduced or re-measured in this stage.

## 8. Costs

- **Model cost: zero.** No provider was contacted by anything in this stage. No
  request left the process, in the tests or outside them.
- **Unmeasured:** everything about live behaviour. Nothing here observes a
  model, a gateway, a real `gh`, or a repository other than this one.

## 9. Blind spots

Ordered by how badly each could mislead someone reading §6 as good news.

- **A merged tree that passes tests is not a tree that has run.** Every stage
  in §6 is verified by tests written by the stage that is being verified. There
  is still no live run of the merged harness against anything. Risk R1 —
  a merge that passes while silently reversing a stage's intent — is
  *mitigated* by §3.4, not eliminated: the check there is directed at the four
  files two stages both touched, and an intent reversal in a file only one
  stage touched would not have been looked for.

- **The tests that prove each stage are the tests that stage wrote.** 895 is a
  count of assertions this programme chose to make. It is not coverage of the
  behaviours a fleet will exercise, and the four gates cannot distinguish the
  two.

- **§3.1 is a resolution, not a proof that no other pair disagrees.** It was
  found because Stage B happened to encode its rule as a test. Where two stages
  agreed in prose and diverged in code with **neither** side asserting it, this
  merge would not have noticed. No systematic search for that class was done.

- **The resolver door asymmetry in §3.1 is a real gap**, deliberately left
  open. A resolver that is neither an installed distribution nor passable as
  `extra` cannot be declared. It is not a regression — Stage G had no such door
  either — but it is now the only asymmetry between how a preset and a resolver
  are found.

- **Nothing in this stage touched what the stage reports already said was
  unmeasured**, and the merge does not improve any of it. Carried forward
  unchanged and still true: no real gateway contacted (B); no real `gh`
  contacted and assessor quality entirely unmeasured with no cost cap (C); no
  external system contacted, nothing schedules `resolve_external`, cross-project
  cycles undetected, rollback documented and untested (G); failure *frequency*
  not measured and the rates not a success-rate baseline (E1); live NGMS
  improvement unmeasured (E2).

- **Issue #128 remains open and is not addressed.** Session-mode implementer
  traffic still bypasses `ModelClient` entirely, so the harness's own account of
  what it spent is incomplete by an unknown amount.

- **#146 was closed on deterministic evidence.** The original observation that
  opened it — 54 files, 60,981 characters, identical between two unrelated items
  on a 26k-file repository — has never been re-run. If it recurs, this closure
  was premature and the comment on the issue says so.

- **The stranger-on-a-laptop path is still unproven.** Stage D is not started,
  so `init --demo` and `doctor` do not exist and the README's first-run claims
  stand unverified. Nothing in this stage tested a clean checkout.

- **`tests/test_backlog.py` still enforces the superseded `P0`–`P4`
  milestones.** The proposal §2 named this as making a dead phase order
  load-bearing. Stage F renamed the file and documented the situation; it did
  not fix it.

- **Timing is not evidence here.** §7 is a precondition being honoured, not a
  measurement. Do not cite two and a half minutes as a baseline.

- **Nothing has been pushed and no pull request exists.** The merge is local.

**"No failures observed" is not equivalent to "the requirement was
exercised."** Nine of the entries above are requirements this stage did not
exercise.
