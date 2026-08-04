# Stage L item-budgets report — 2026-08-04

**Status:** delivered. An item can be given a wall-clock ceiling and a spend
ceiling; exceeding either stops it at the next boundary in a distinguishable
state, without parking an endpoint and without consuming an attempt. Defaults
are unlimited, so an existing database upgrades with no behaviour change.

Every claim below is a **repository fact** reproducible from `7761a3f`. There
are no live observations, and no real money was measured.

Specification: §8 of
[`PROPOSAL-2026-08-finish-then-extend.md`](../PROPOSAL-2026-08-finish-then-extend.md);
acceptance §8.4.

## 1. Why this stage stopped being optional

Stage H made the hole it fills. **D11 ruled that a resumed attempt continues the
existing one**, which is right — a crash is not a failure of the work — and
which meant `max_attempts` stopped counting crashes. An item that crashes in a
loop was then bounded by nothing at all. That consequence was named when the
ruling was recorded (§11.1) and again in Stage H's report; this closes it.

The pre-existing hole is the one §8.1 describes: the lease bounds a worker's
*absence*, not an item's *duration*. `session_executor` has an agent timeout and
`Checks` has a subprocess timeout; the item itself was unbounded.

## 2. Configuration under test

| | |
|---|---|
| Branch | `fix/validator-rejects-valid-patches` |
| Base | `fa43e24` (Stage H, 982 tests) |
| Result commit | `7761a3f` |
| New module | `src/agent_harness/budgets.py` |
| New tests | `tests/test_stage_l_item_budgets.py`, 20 tests |
| `TMPDIR` | on the NVMe volume, per risk R6 |
| Network, credentials, provider traffic | none |

| Gate | Result on `7761a3f` |
|---|---|
| `pytest` | **1002 passed** |
| `ruff check .` | all checks passed |
| `ruff format --check .` | 93 files already formatted |
| `mypy` | success, no issues in 89 source files |

1002 − 982 = 20, this stage's test file exactly.

## 3. Acceptance against §8.4

| Criterion | Result |
|---|---|
| An item exceeding its wall-clock ceiling stops at the next boundary with a distinguishable state and a reason naming the ceiling | Passes. `blocked` / `escalated` / `item_wall_clock`, reason text names the ceiling in seconds. A second test asserts no model call was made after the ceiling was passed — it stopped *at* a boundary, not part-way through a stage. |
| An item exceeding its spend ceiling does the same, **and does not park the endpoint** | Passes. The parks are inspected directly for all three roles after the stop. |
| An item whose spend is unmeasurable is reported as unmeasurable, and the report says which ceiling could therefore not be enforced | Passes. `budget_unenforceable` is emitted, naming the ceiling, saying the recorded figure is a **lower bound**, and saying that unknown cost is not zero cost. The item is **not** stopped. |
| Defaults are unlimited; an existing database upgrades with no behaviour change | Passes, against a hand-built pre-Stage-L schema. |

## 4. The three rules, and how each is held

**A spend ceiling is not a provider cost cap.** `providers.WINDOW_CAP` and
`TERMINAL_CAP` are a provider's statement about our *account* and are in the
never-retry set. `item_spend` is our statement about *one item*. They are
separate reason kinds, the ceiling names are disjoint from the provider kinds,
and a test asserts both. Parking a shared endpoint because one item was
expensive would be that conflation made real, so the stop path deliberately
touches nothing in `EndpointParks`.

**Unknown cost stays unknown.** `Spend.add_call` counts a call with no reported
usage as **unpriced**, not as free. While any call is unpriced the item's
`spend_usd` is a lower bound, `unpriced_calls` is non-zero, and the spend
ceiling is reported as unenforceable rather than as satisfied. Stopping an item
on a number nobody can defend would be worse than not stopping it.

The wall clock is still enforced in that case, and the ordering is deliberate:
an item that has run for a week and whose spend is unknown should stop for the
reason that is knowable.

**A budget stop never kills work in flight.** The check runs at boundaries that
already exist — before each model call, and at each `_keepalive`. There is no
timer and no signal. The reasoning is `work.py`'s own about pause semantics.

## 5. Two decisions worth stating

**A budget stop does not consume an attempt.** The item did not fail and did not
exhaust a retry ladder; a policy stopped it. Spending an attempt would retire
sound work for a reason that is nothing to do with the work.

**The wall clock runs from the item, not the attempt.** `work.first_started_at`
is stamped on the first claim and never again. Measuring from the current
attempt would mean an item that crashes in a loop resets its own clock every
re-claim — which is precisely the failure this stage exists to catch, so
measuring it that way would have shipped a ceiling that cannot fire.

## 6. A defect found on the way

**A response reporting no usage was being skipped rather than counted.** The
first implementation of `Spend.add` treated an event with no token counts as
"not a model call" — true for a stage transition, and catastrophically false for
a provider that answered without reporting usage. It made the #128 case read as
zero cost, which is the exact failure §8.3 forbids. Found by this stage's own
unmeasurable-spend test failing.

The fix moved the judgement to the caller: `add_call` is given calls that
actually happened and decides only priced-versus-unpriced. A function that
guesses which events are calls is a function that will one day guess wrong in
the direction of "free".

## 7. Costs

- **Model cost: zero.** No provider was contacted.
- **Runtime cost added:** one arithmetic check per boundary, no I/O. The item's
  running total is written once per attempt, not per call.
- **Unmeasured:** anything about real money. The prices in these tests are a
  fixture, and the shipped price table is deliberately empty of real prices.

## 8. Blind spots

Ordered by how badly each could mislead someone reading §3 as good news.

- **This bounds the direct-API executor only.** `SessionExecutor` has no budget
  check at all. Its implementer runs inside a hosted CLI session and its traffic
  never passes through `ModelClient`, so its spend is *entirely* unmeasurable
  (#128) and its wall clock is bounded only by the pre-existing agent timeout.
  **A session-mode fleet gets nothing from this stage**, and that is the largest
  gap in it.

- **The spend ceiling is only as good as the price table**, which ships empty of
  real prices on purpose. A deployment that has not supplied
  `HARNESS_PRICE_TABLE` will find *every* call unpriced and *every* spend
  ceiling unenforceable. The harness says so, loudly and per item — but a
  ceiling that is configured, reported, and never once enforceable is a ceiling
  someone will believe in.

- **`budget_unenforceable` is emitted once per ceiling per attempt.** On a long
  run it is one line in an event stream nobody is watching. Nothing raises it to
  `doctor`, to a readiness probe, or to a rollup, so the honest report can be
  true and unread.

- **The ceilings are checked, not predicted.** An item is stopped *after* it has
  passed a ceiling, at the next boundary. A single expensive call can therefore
  overshoot a spend ceiling by its own cost, and a stage that takes an hour can
  overshoot a wall-clock ceiling by an hour. Nothing estimates a call's cost
  before making it, and nothing should be read as a guarantee that spend stays
  under the number.

- **D14 is open and this stage did not answer it.** Defaults are unlimited
  because that is what makes an upgrade a no-op. Whether a new project should
  get a ceiling by default is a real question with a real cost either way, and
  it was deliberately not decided by accident here. `doctor` warns; nothing
  refuses.

- **Nothing has ever hit one of these ceilings in a real run.** The wall-clock
  test advances an injected clock; the spend test uses a fixture price table and
  a scripted transport. The stop path has never been exercised by a real model
  being genuinely expensive or a real item genuinely running long.

- **A blocked item needs a human and nothing tells one.** The item lands in
  `blocked` with the ceiling named, which is correct and inert. There is no
  notification, no queue of budget stops, and no command to raise a ceiling and
  release — an operator must find it in the API and `retry` it after changing
  the configuration.

- **The item's total is written at the end of an attempt.** A worker killed
  mid-attempt loses that attempt's accumulated spend from the item's total, so a
  crash-looping item under-reports what it actually cost — the opposite
  direction from the one this stage cares about, and still wrong.

- **`set_item_budget` has no API route.** Per-item ceilings are reachable from
  the library and the queue, and are reported on the work item, but there is no
  HTTP surface to set one. §8.2 asks for them to be *readable* through the API,
  which they are; writing one is library-only.

- **Timing is not reported.** `TMPDIR` was on the NVMe volume per R6. No
  duration here is a measurement.

**"No failures observed" is not equivalent to "the requirement was
exercised."** Session mode was not exercised, and neither was a real cost.

## 9. Continue/stop

**Continue.** §8.4 is met for the direct-API executor, and the crash-loop hole
D11's ruling opened is closed for it. The session-mode gap is now the same gap
Stage H left, in the same place, and the two should be closed together rather
than separately.

Next in §3's order: **Stage J — durable human hold**, whose D12 ruling
(*suspend the lease, keep the claim*) makes the configured maximum hold duration
load-bearing in exactly the way this stage's ceilings are.
