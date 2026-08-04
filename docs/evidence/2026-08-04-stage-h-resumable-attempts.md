# Stage H resumable-attempts report — 2026-08-04

**Status:** delivered for the direct-API executor. A killed worker resumes at
the last durable stage without re-paying for the planner or the implementer,
under three configurable durability modes. **Session mode is not resumable and
this stage did not make it so** — see §8.

Every claim below is a **repository fact** reproducible from `f430d40`. There
are no live observations. The cost numbers in §6 are model calls counted on an
in-process transport, not money.

Specification: §7 of
[`PROPOSAL-2026-08-finish-then-extend.md`](../PROPOSAL-2026-08-finish-then-extend.md);
acceptance §7.4. The proposal calls this "the largest new stage and the one with
the most value".

## 1. Configuration under test

| | |
|---|---|
| Branch | `fix/validator-rejects-valid-patches` |
| Base | `a4e8d6b` (Stage K, 950 tests) |
| Result commit | `f430d40` |
| New module | `src/agent_harness/attempts.py` |
| New tests | `tests/test_stage_h_resumable_attempts.py`, 32 tests |
| `TMPDIR` | on the NVMe volume, per risk R6 |
| Network, credentials, provider traffic | none |

## 2. Commands run, and the result

```console
TMPDIR=/path/on/fast/volume uv run pytest
uv run ruff check .
uv run ruff format --check .
TMPDIR=/path/on/fast/volume uv run mypy
```

| Gate | Result on `f430d40` |
|---|---|
| `pytest` | **982 passed** |
| `ruff check .` | all checks passed |
| `ruff format --check .` | 91 files already formatted |
| `mypy` | success, no issues in 87 source files |

982 − 950 = 32, this stage's test file exactly.

The formatted-file count fell from 219 to 91 because `.claude/` — two unrelated
embedded git worktrees that had been sitting in the tree — is now gitignored and
ruff respects that. No project file stopped being checked.

## 3. How a killed worker is simulated

This matters more than the mechanism it tests. The transport raises
`KeyboardInterrupt`, a `BaseException`, so it escapes `run_once`'s
`except Exception` **without releasing the item**. The row stays `claimed` with
a live lease, which is the state a `kill -9` leaves behind. The clock is then
advanced past the lease and a *second* executor, over the *same database*,
claims it.

Raising an ordinary exception would have released the item cleanly and proved
something much weaker: that the harness can resume work it tidied up after
itself. Nothing in this file sleeps; the clock is injected.

## 4. What is recorded, and what can be resumed at

Six stages, in a fixed list known at compile time, keyed
`(project_id, item_id, attempt, stage)`.

| Reached | Resumes at | Artefact | Why |
|---|---|---|---|
| `planned` | `planned` | plan, targets, uncertainties | the planner is not re-asked |
| `implemented` | `implemented` | the diff | the implementer is not re-asked |
| `applied` | `implemented` | branch, base, how it applied | an uncommitted tree does not survive a crash, so the stored diff is re-applied to a fresh branch |
| `checked` | `implemented` | — | same, and re-running checks is cheap and idempotent |
| `checkpointed` | `checkpointed` | commit sha, branch, PR url | the commit is in git |
| `reviewed` | `reviewed` | verdict and its text | the verdict is reused, never re-asked |

**Recording a stage is not the same as being able to resume at it**, and
`RESUMES_AT` says which is which rather than leaving it implied. An implied
resumable position is a promise nothing keeps.

**A recorded verdict is reused rather than re-asked**, and the reason is not
only cost. A model is not deterministic, so re-asking would make a crash a way
to shop for a different verdict.

## 5. Acceptance against §7.4

| Criterion | Result |
|---|---|
| Killed after checks pass, before review → reaches review **without a second planner or implementer call**, proven by the event stream | Passes. The resumed worker's call counter reads `{"reviewer": 1}` and the stream carries a `resumed` event naming `implemented`. |
| Killed at each stage boundary in turn, resumes correctly from each | Passes, parameterised over four boundaries: planner, implementer, just-after-checks, reviewer. |
| Cost per completed item under induced crashes, measured against the same fixture with resumption disabled, **both numbers reported** | §6. |
| An attempt briefed at revision N and re-claimed after the plan moved to N+1 is handled explicitly | Passes. `brief_moved` is emitted, the position is discarded, and the attempt re-plans against the current brief. |
| Every durability mode exercised, and the mode recorded in the events | Passes. All three complete an item; the mode is on every stage row and in every `resumed` event. |

## 6. Cost, both numbers

The same fixture, the same induced crash — killed immediately after the checks
passed — run twice.

| | Model calls to complete one item |
|---|---|
| Resumption on (`boundary`) | **3** — planner, implementer, then the resumed worker's reviewer |
| Resumption off (`exit`) | **5** — planner, implementer, then planner, implementer, reviewer again |

A 40% reduction on this fixture, with one crash at that point. **Read that
number narrowly.** It is a property of one induced crash at one boundary on one
fixture; a crash in the planner saves nothing at all, and the honest floor is in
the parameterised test that says so. There is no real-world crash distribution
here to weight it with.

**The happy path costs exactly what it did.** A test asserts three calls in
every mode for an item that never crashes.

## 7. Three defects found on the way

All three were found by tests — two pre-existing, one written for this stage —
which is the system working.

**A killed worker left its half-applied diff in the working tree.**
`_abandon_branch` cleans up after an attempt that *ended*; a worker killed
mid-apply ended nothing. The next attempt carried those changes across the
checkout and its own patch then failed against a tree that already contained it,
reported as `the diff did not apply` — which reads as "the model wrote a bad
diff" and is not. `_prepare_branch` now discards before it checks out. This was
a latent contamination bug before this stage, not one it introduced.

**An operator's retry would have replayed the verdict it was retrying.** A
Stage K test caught it the moment resumption landed: a rejected item, retried,
resumed into its own recorded rejection and re-reported it at no model cost.
The fix is the distinction the module now turns on — a worker that reached a
*decision* seals the attempt; only a worker that was *killed* leaves a position.
`requeue` additionally forgets every attempt at the item, because "retry this"
means from the start, against the current plan.

`withheld` is the deliberate exception: a spend cap or a provider that would not
answer decided nothing about the item, so the next claim continues. Those are
the common interruptions in practice, not `kill -9`, so that is where the saving
actually lives.

**Stage history came back in the wrong order under a frozen clock.** Several
stages of one attempt can share a timestamp; the ordering now breaks the tie on
`rowid`, which is insertion order.

## 8. What it deliberately did not do

**It did not become a workflow engine.** No DSL, no user-defined graph, no
dynamic step registration. The stage list is the executor's own and fixed. A
test fails if `attempts.py` ever grows an identifier containing `register` or
`plugin`, and a second asserts the tuple's exact contents. Risk R2 is held by a
test rather than by intent.

**It did not assume replay is deterministic.** Nothing re-runs a log. Every
resume continues from a stored artefact.

**It did not weaken the pre-review checkpoint.** The durability mode governs the
attempt *record*; the git commit before the expensive gate happens in all three
modes, and a test asserts `checkpointed` precedes `review_approved` in each.

**It did not silently change attempts accounting.** D11 was answered by decision
and recorded before the code was written: a resumed attempt **continues** the
existing one. `claim` does not increment for an item with a live resumable
position. `max_attempts` therefore bounds genuine failures.

**It did not touch session mode.** See the first blind spot.

## 9. Blind spots

Ordered by how badly each could mislead someone reading §5 as good news.

- **Session mode has none of this.** `SessionExecutor` records no stages and
  resumes nothing; a killed session-mode worker restarts exactly as it did
  before. This is the largest gap in the stage and it is not a small follow-up:
  a session-mode attempt's "artefact" is a live PTY holding an agent's context,
  which is a different durability problem from a diff in a database. Stage J's
  durable hold is the likelier place for it. **Every §5 result is a claim about
  the direct-API executor only.**

- **The cost number is one crash on one fixture.** 3 against 5 is arithmetic
  about a single induced failure at a single boundary. A crash in the planner
  saves nothing; a fleet's real distribution of interruptions is unknown because
  there has been no sustained live run. Do not cite 40% as an expected saving.

- **Nothing was killed for real.** `KeyboardInterrupt` from inside a transport
  is a faithful model of a process that stops between two statements. It is not
  a model of a process killed while SQLite is mid-write, of a machine losing
  power, or of a container evicted mid-`git push`. The durability claim is
  tested against the failure mode that is cheap to simulate.

- **`sync` mode's intent records are tested at the log, not through a crash.**
  A test drives `opening`/`closed` directly and asserts an unconfirmed effect
  survives. No test kills a worker between the intent and the push, because
  doing so needs a real push to a real remote. The `effect_unconfirmed` path is
  therefore reachable and unexercised end to end.

- **A resumed attempt re-runs the checks, and that is a cost the numbers hide.**
  A project whose check suite takes ten minutes pays it again on every resume.
  It is the right trade — trusting a check result from a tree that may have been
  rebuilt would be trusting a gate nobody ran — but it means resumption is not
  free, and the §6 counter, which counts model calls, does not see it.

- **Resuming at `checkpointed` trusts git and does not verify it.** The resumed
  worker checks the branch out and reviews `base...branch`. If the branch was
  force-pushed, rewritten or deleted by something outside the harness, the
  behaviour is whatever git does. There is no integrity check against the
  recorded sha.

- **The brief-moved check compares the brief and `depends_on`, not the graph
  revision.** An operator who re-syncs a plan with no textual change to an item
  moves the revision without moving that item's brief, and the attempt resumes —
  correctly, in every case anyone has thought of, and untested against a case
  where the *graph around it* changed meaningfully while its own text did not.

- **Nothing prunes `attempt_stages`.** Every stage of every attempt at every
  item is kept forever, including the stored diff, which is the largest artefact
  in the table. A long-running fleet's queue database grows without bound, and
  `maintenance.py`'s rollups do not know this table exists.

- **The `sealed` distinction is new and load-bearing.** Whether an attempt is
  resumable now depends on it, and it was added late, in response to a defect.
  The rule — a decision seals, a kill does not — is tested in both directions,
  but it is the newest part of the design and the least worn in.

- **Attempt history is not exposed anywhere.** It is in the database and
  readable through `AttemptLog.history`; no API route, no CLI command and no
  `doctor` finding surfaces it. An operator cannot see how far an attempt got
  without opening SQLite.

- **No migration test for the three new tables or the `projects.durability`
  column.** They are `CREATE TABLE IF NOT EXISTS` and an additive column, which
  is the mechanism Stage G proved; that is an argument, not evidence.

- **Timing is not reported.** `TMPDIR` was on the NVMe volume per R6. No
  duration here is a measurement.

**"No failures observed" is not equivalent to "the requirement was
exercised."** Session mode was not exercised at all, and neither was a real
crash.

## 10. Continue/stop

**Continue.** §7.4 is met for the direct-API executor and the two gaps that
matter are named rather than hidden: session mode, and the unbounded growth of
the record.

The crash-loop hole D11's ruling creates — an item that crashes forever is now
bounded by nothing, because `max_attempts` no longer counts crashes — is real,
was predicted in §11.1 when the decision was recorded, and is owed by **Stage L,
which is next in §3's order**. It should not be left long.
