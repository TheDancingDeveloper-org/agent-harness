# Stage K outcome-and-check-taxonomy report — 2026-08-04

**Status:** delivered. A check has five answers instead of two, an attempt
carries why it stopped as well as where it landed, and both are visible through
`GET /api/work/{id}` without reading a log. Every claim below is a **repository
fact** reproducible from `1edf002`. There are no live observations in this
report.

Specification: §6 of
[`PROPOSAL-2026-08-finish-then-extend.md`](../PROPOSAL-2026-08-finish-then-extend.md);
acceptance §6.4. It is the tenth stage of the programme, after Stage D.

## 1. Configuration under test

| | |
|---|---|
| Branch | `fix/validator-rejects-valid-patches` |
| Base | `dfafb7e` (Stage D, 920 tests) |
| Result commit | `1edf002` |
| New module | `src/agent_harness/outcomes.py` |
| New tests | `tests/test_stage_k_outcomes.py`, 30 tests |
| `TMPDIR` | on the NVMe volume, per risk R6 |
| Network, credentials, provider traffic | none |

## 2. Commands run, and the result

```console
TMPDIR=/path/on/fast/volume uv run pytest
uv run ruff check .
uv run ruff format --check .
TMPDIR=/path/on/fast/volume uv run mypy
```

| Gate | Result on `1edf002` |
|---|---|
| `pytest` | **950 passed** |
| `ruff check .` | all checks passed |
| `ruff format --check .` | 219 files already formatted |
| `mypy` | success, no issues in 85 source files |

950 − 920 = 30, this stage's test file exactly. **No pre-existing test's
expectations were changed.** Two tests failed during development and both were
signals rather than obstacles; both are in §6.

## 3. What a check now answers

`Checks.run()` returned `(bool, str)`. It returns a `CheckResult` carrying one
of five outcomes, and **still unpacks as `(ok, detail)`** — three call sites
already did that, and a flag day across them would have been change for its own
sake.

| Outcome | Condition | Queue state | Attempt |
|---|---|---|---|
| `pass` | exit 0 | continues to the reviewer | — |
| `fail` | exit non-zero | `failed` / `refused` | consumed |
| `fix_available` | exit non-zero **and** a fix is declared for that command | `failed` / `refused` | consumed |
| `retry` | the command did not finish in time | `pending` / `withheld` | **not** consumed |
| `escalate` | the program is not installed, or the disk is full | `blocked` / `escalated` | **not** consumed |

### 3.1 The classification is structural, never semantic

The harness reads **how the subprocess ended** — timed out, could not be
started, exited non-zero, reported no space — and never reads a project's
output to guess what its failure meant. Guessing at another ecosystem's
messages is how a generic harness stops being one, and a misread failure here
would turn a real defect into a retry, which is the expensive direction.

Disk exhaustion is the one content read, and it uses the pre-existing
`is_disk_exhaustion`, which the audit layer already counted. It is a machine to
fix, not a diff to reject: every subsequent item fails the same way and each
pays a planner and an implementer first.

### 3.2 `fix_available` records and does not apply

A fix is declared per project, keyed to a check command verbatim:

```json
{"checks": ["ruff format --check ."],
 "fixes": {"ruff format --check .": ["ruff", "format", "."]}}
```

**It is never run.** §6.3 requires this and there is a test that runs a
"fix" which would create a witness file, and asserts the file does not exist.
The item still fails; somebody now knows what would clear it, and the fix is
announced in the event stream as `fix_available`.

A fix keyed to a command that is not a check is **refused**, not ignored. It is
almost always a typo, and a fix that silently never applies is worse than no
fix because someone believes it is there.

## 4. What stopped an item

`Outcome.state` reused the queue's states, so a checks failure, a reviewer
rejection, a spend cap and a crashed worker all arrived looking similar. Every
item now also carries a **disposition** and a **reason kind**, stored on the row
and published on `GET /api/work/{id}`.

| Disposition | Means |
|---|---|
| `completed` | done |
| `refused` | a gate said no about *this item's work*. A verdict, not a crash. |
| `crashed` | the worker or harness broke, or never finished. Nothing judged the work. |
| `withheld` | never attempted, or discarded through no fault of the item |
| `escalated` | a person has to resolve something |
| *(empty)* | nobody has finished with it yet. **Not a sixth disposition.** |

Twelve reason kinds carry the specific answer as a token rather than English,
so a client branches on it instead of matching prose — the same idea, and the
same justification, as `graph.REASON_*`.

## 5. What it deliberately did not do

**It did not answer D8.** There is no registry, no discovery and no way for a
third party to add a gate type. A richer *result* from the gates that already
exist is a different thing from a mechanism for adding new ones, which is the
whole reason this stage could be built with D8 open. There is a test that fails
if `outcomes.py` ever grows an identifier containing `register` or `plugin`.

**It did not weaken a gate.** `escalate` is an additional outcome and never a
softer `fail`; `SATISFIED` is spelled as a one-element set so a sixth outcome
forces a decision rather than defaulting to a side. A test drives an escalating
check through a real run and asserts the item does not reach `done`, `review`
is not in its stages, and no `review_*` event was emitted.

**It did not change what `max_attempts` bounds.** This is the load-bearing
restraint, and it took a redesign mid-stage to keep.

The first implementation put a `CONSUMES_ATTEMPT` map on the disposition and
applied it at every release. That is tidier, and it would have quietly changed
the accounting of three pre-existing paths — `RetryExhausted`,
`dependency_invalidated` and the session-mode agent timeout would all have
stopped consuming attempts. `max_attempts` is currently the *only* bound on an
item that repeatedly kills its worker, so removing it for a provider that will
not answer would have made a permanently-down endpoint an unbounded re-claim
loop. Whether that is right is **D11**, it is open, and a stage that names
distinctions is not the place to answer it by moving a number.

So the attempt flag moved onto `Stop`, defaulting to `True`, and only the two
outcomes that **did not exist before** decline to consume one. Every path that
consumed an attempt before still does, and there are tests for both directions.

**It did not move any existing item state**, with one exception that is a new
path rather than a moved one: an escalating check lands in `blocked`, which no
check could previously produce. Everything else keeps the state it had. The
session-mode agent timeout is the clearest case: it was briefly reclassified to
`withheld`/`pending` during development, which would have let a second worker
claim a tree a live agent is still writing to. It is `crashed` and stays
`failed`.

## 6. Two defects the stage surfaced

Both were caught by pre-existing tests failing, which is the system working.

**A verification timeout was reaching `adoption` as an exception.** `Checks.run`
now catches `TimeoutExpired` itself, so adoption's `except TimeoutExpired` never
fired and a slow `verify:` reported as `failed` — which in adoption means *this
item is not done*, a materially wrong answer. It now reads the typed outcome:
`retry` → `timeout`, `escalate` → `unavailable`, otherwise `failed`. A
verification that could not run is not evidence that the work was not done, and
uncertainty there has always resolved to "still to do".

**A missing interpreter was reaching adoption the same way.** Same fix, and it
is now `unavailable` rather than `failed`.

## 7. Acceptance against §6.4

| Criterion | Where |
|---|---|
| A fixture exercises all five check outcomes | `tests/test_stage_k_outcomes.py`, one test each, against real subprocesses for four of the five |
| Each reaches the queue as a distinguishable state, **visible through the API without reading a log** | every distinguishability assertion goes through `GET /api/work/{id}` |
| A reviewer rejection and a worker crash produce different item states, visible in `GET /api/work/{id}` | `test_a_reviewer_rejection_and_a_worker_crash_are_distinguishable`, which first asserts both are `failed` — the precondition — then that their dispositions differ |
| No existing gate becomes skippable, optional or cheaper | `test_no_gate_became_skippable_optional_or_cheaper` |
| Evidence artefact with a blind-spots section | this file |

## 8. Costs

- **Model cost: zero.** No provider was contacted. Nothing left the process.
- **Runtime cost added to a run:** one `shutil.which`-free subprocess
  classification per check, which is the same subprocess that already ran. No
  new process is started, and `fix_available` starts nothing at all.
- **Unmeasured:** whether the taxonomy is *useful*. See §9.

## 9. Blind spots

Ordered by how badly each could mislead someone reading §7 as good news.

- **Nothing here has been operated.** The claim is that a human reading a queue
  can now tell a rejection from a crash. That claim is tested as *data being
  present and different*; whether it changes what anybody does is unmeasured and
  unmeasurable from this repository. A taxonomy nobody uses is a schema, not an
  improvement.

- **The five outcomes are a guess at the right five.** They come from
  agentspan's guardrail outcomes and Mastra's run-result union, adapted. No
  measurement here says these are the distinctions that matter for *this*
  harness's failures — there is no corpus of real check failures to classify
  against, because there has been no sustained live run. If a sixth is needed,
  or if `retry` and `escalate` turn out to be one thing, this stage will have
  been wrong in a way its tests cannot detect.

- **`retry` returning an item to `pending` without consuming an attempt is a
  new unbounded loop, and it is deliberate.** A check that times out every time
  — a genuinely hanging test suite — now re-claims forever, where before it
  crashed the attempt and eventually exhausted the item. That is the correct
  reading of "the question was not answered", and it is also a hole. **Stage L's
  wall-clock budget is what closes it**, and until Stage L lands, a
  permanently-timing-out check is bounded only by an operator noticing. Named
  here rather than discovered later.

- **`escalate` sends an item to `blocked`, and nothing unblocks it
  automatically.** That is the intent — a person has to act — but it means a
  full disk during a fleet run parks every item that hits it, and each stays
  parked after the disk is fixed until somebody retries it. There is no
  "re-examine everything that escalated for this reason" operation.

- **Structural classification cannot see a project's own semantics.** A test
  suite that exits 0 while reporting failures, or exits non-zero for a
  configuration problem, is classified wrongly and the harness has no way to
  know. That is the price of not guessing at output, and it is the right price,
  but it is a price.

- **`fix_available` is recorded and there is nowhere for it to go.** Nothing
  consumes the event, no API surfaces the fix on the item, and no command
  applies it. It is a fact written to a stream. Making it actionable is the
  later decision §6.3 reserves, and until then the feature's whole value is that
  a human reading events sees the fix.

- **The disposition is a projection of one attempt, and it is overwritten.**
  Each release writes the current attempt's disposition over the previous one's.
  An item that was refused, then crashed, then refused again reads as refused,
  with no history in the row. The event stream has the sequence; the queue does
  not, and a dashboard reading the queue will not know. **Stage H's per-attempt
  record is where that history belongs.**

- **The session executor's coverage is thinner than the direct executor's.**
  The disposition is set at every session-mode exit and typechecks, but the
  parametrised end-to-end assertions in §7 run against `Executor` only. Session
  mode's dispositions are exercised by its own pre-existing tests continuing to
  pass, which proves the states did not move — not that the new fields are right.

- **`crashed` for an agent timeout is a judgement call.** The agent did not
  break; it ran out of time, and its session is deliberately still alive. It is
  classified `crashed` because nothing judged the work, which is what that
  disposition means, and because the alternative moved a queue state this stage
  had no business moving. It may want its own disposition later. **Stage J's
  durable hold is the likelier home** for a live session waiting on something.

- **No migration test for the `projects.fixes` column.** The `work` table's two
  new columns have an upgrade-from-an-old-schema test; the project column does
  not. It is the same additive mechanism, which is an argument and not evidence.

- **Timing is not reported.** `TMPDIR` was on the NVMe volume per R6. No
  duration here is a measurement.

**"No failures observed" is not equivalent to "the requirement was
exercised."** The usefulness of the taxonomy — the entire point of the stage —
was not exercised at all.

## 10. Continue/stop

**Continue.** §6.4 is met. The two named risks this stage creates —
the unbounded `retry` loop and the missing per-attempt history — are owned by
**Stage L** and **Stage H** respectively, which are the next two stages in §3's
order. Neither is a reason to stop here; both are reasons not to stop before
them.

D8 remains open and untouched. D11 remains open and was deliberately not
answered, at the cost of one accounting inconsistency that is documented in
§5 rather than hidden.
