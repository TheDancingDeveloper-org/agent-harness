# A durable audit layer

> # Superseded — 2026-08-06
>
> The durable audit layer, which is built. What survives is in `DESIGN.md`; the plan itself is history.
>
> **Current documentation:**
> [`docs/DESIGN.md`](DESIGN.md) — how the harness works and why.
> [`docs/STATUS.md`](STATUS.md) — where it stands and what is left to do.
>
> Kept for the reasoning and the evidence, which are not reproduced elsewhere.
> **Do not follow its plan, its phase order, or its statements of current
> state** — all three are out of date. Where this document and the code
> disagree, the code is right.

Status: proposed, not started. Written 2026-08-02.

Tracking whether the harness is getting better or worse, over months, across
projects and model changes — and being able to prove it.

---

## 0. What exists, and the three things wrong with it

The event store is already append-only, already deduplicated by a
writer-assigned `run_id` + `seq`, and already paged by monotonic row id rather
than timestamp. That foundation is sound and none of it needs replacing.

### 0.1 The audit log shares a file with the operational queue

```python
queue = WorkQueue(args.db)  # mutable: claims, leases, retries
store = EventStore(args.db)  # append-only: what happened, forever
```

Same database. So the history shares fate with the state: a queue migration
that goes wrong, a corrupted WAL, or a reasonable-sounding "let's reset the
queue and re-sync the plan" takes six months of measurement with it.

Phase 1 rewrote the `work` table in place. That migration was tested and it
worked — but the fact that a routine schema change *can* touch the audit log
at all is the defect. **Durable means it does not share fate with anything
disposable.**

### 0.2 Cost is not recorded, so the stated goal is unmeasurable

`Event` carries `latency_s` and nothing about tokens or spend. The README's
definition of done for v1 says:

> Delivery rate is no worse than the workload's own pre-harness baseline, **at
> lower cost**.

There is no way to evaluate that sentence with the data being collected. Not
approximately — at all.

### 0.3 Unbounded growth, no rollups

No retention, no aggregation, no vacuum. Raw rows accumulate forever on a
container volume, and every long-range question rescans them. The series you
most want — "cost per completed item, monthly, for a year" — is the one that
gets slowest as it gets more valuable.

Deleting old events is not the answer either: that destroys exactly the
long-run comparison the layer exists for.

---

## 1. Architecture

### 1.1 Separate the fates

Two databases, different lifecycles, different backup policies:

| | `harness.sqlite` | `audit.sqlite` |
|---|---|---|
| Contents | queue, claims, control, projects | events, rollups, baselines |
| Mutability | mutable | **append-only, no UPDATE, no DELETE** |
| If lost | re-sync the plan and carry on | irreplaceable |
| Migrations | rewrites tables in place | additive columns only, forward-only |
| Safe to delete | yes, deliberately | never |

The harness must keep running if the audit database is unavailable —
observation failing should not stop work. The reverse is not true: the audit
layer must never be the thing that wedges the fleet.

### 1.2 Never rewrite history to fit a new schema

Add columns; leave old rows alone. Readers tolerate absence. A row written in
March under an older shape stays exactly as written — a "migration" that
backfills history is indistinguishable from falsifying it, and the moment you
do it once, no number in the series can be trusted again.

Every row carries the schema version it was written under.

### 1.3 Roll up, then thin — never thin alone

- **Raw events**: retained ~90 days. High-cardinality, useful for debugging a
  specific week.
- **Daily rollups**: written once per day, immutable thereafter, kept forever.
  Small, and the long series lives here.
- Raw rows are only ever removed *after* the rollup covering them exists.

That ordering is the whole discipline. Thinning before aggregating is silent
data loss with a tidy-looking outcome.

### 1.4 Record the price you used, not just the tokens

Model prices change. A cost series computed by applying today's prices to last
year's tokens is not history, it is a projection — and it will quietly rewrite
the past every time a vendor changes a rate.

So each `model_call` records tokens **and** the unit price applied **and** the
price-table version. Historical cost becomes reproducible, and a price change
is visible as a step in the series rather than an invisible retroactive edit.

### 1.5 Store pointers, not payloads

Prompts, briefs and diffs do not go in the audit database. They are large, and
they contain the workload's proprietary content. Store a content hash, a size,
and where it lives. The audit layer answers "how much, how often, how well" —
not "what was in it".

---

## 2. Scope: what to audit

Ordered by what earns its place first.

### 2.1 Delivery — is it producing work?

| Metric | Why it matters |
|---|---|
| Items completed per day, per project | The headline. Everything else explains it. |
| **Lead time**: claim → PR opened, and claim → merged | Two different numbers. The gap between them is *your* latency, not the harness's. |
| **First-pass yield**: % of items done with one attempt | The single best quality proxy; degrades before delivery does. |
| Attempts per completed item | Rework. Rising means briefs are getting worse or the model is. |
| Items that never finish | Failure by silence, which nothing currently counts. |

### 2.2 Cost — what did it buy?

| Metric | Why |
|---|---|
| Tokens in / out / cached, per role, per model | The raw input to everything below. |
| **Cost per completed item** | The number that decides whether this is worth running. |
| Cost of rejected work | Pure waste, and the fastest thing to act on. |
| Cost per project | Which workload is expensive, not just which model. |
| Spend against caps, by class | Ties directly into the existing rate-limit classification. |

### 2.3 Quality — is the work any good?

| Metric | Why |
|---|---|
| Review approve / reject rate, by implementer model | Distinguishes "cheap model" from "cheap model that wastes reviewer calls". |
| Check pass rate *before* review | Measures whether the cheap gate is doing its job. |
| **PR merged vs closed unmerged** | Ground truth, and it lives on GitHub rather than here. |
| **Revert rate within N days of merge** | The only honest quality metric. Everything above is a proxy for this. |
| Items a human retried or overrode | Where the harness needed a person. |

### 2.4 Reliability — does it stay up unattended?

| Metric | Why |
|---|---|
| Rate limits by class over time | `rpm` rising means concurrency is wrong; `terminal_cap` means stop. |
| Stale claims per day | Workers dying. A rising count is the seven-day-run killer. |
| `claim_lost` events | Leases actually contending — should be near zero. |
| Abandoned sessions: created vs reaped | If created outpaces reaped, something is not finishing. |
| Uninterrupted run length | Directly measures the v1 goal of seven unattended days. |

### 2.5 Model and role performance — the one that pays for the layer

Per `(role, model, endpoint)` and per project:

- approval rate, cost per **approved** item, median latency, retry rate,
  rate-limit rate

This is what turns the live role map from a convenience into a decision. Right
now a model swap is a vibe; with this it is a before-and-after with a
denominator. It is also the thing that makes reviewer independence auditable —
you can see whether a vendor approves its own work more often than it approves
someone else's.

### 2.6 Human interaction

How often agents stop to ask, how long they wait, and how often a human
intervened. If `waiting_for_input` is rising, the briefs are underspecified —
which is a plan-quality signal, not an agent-quality one.

### 2.7 Baselines

A recorded, dated baseline per project, so "better than before" has a *before*.
Immutable once written, and stamped with what it measured. Without this, §2.1
and §2.2 are numbers with nothing to compare against — which is the state the
README already admits to.

---

## 3. What NOT to audit

- **Prompt and diff contents** — §1.5.
- **Anything requiring an UPDATE.** If a metric needs restating, it is a new
  row, not an edit.
- **Per-token streaming detail.** One row per call, not per chunk.
- **Vanity counts** (events written, API requests served). They rise with
  usage and mean nothing about performance.

---

## 4. Phasing

**A. Split the databases.** `audit.sqlite`, separate lifecycle, harness
survives its absence. Existing events copied once, then the old table left
alone. Nothing else can proceed safely until history stops sharing fate with
the queue.

**B. Capture cost.** Tokens, unit price, price-table version on every
`model_call`; `project_id` and `attempt_no` on every event so any series can
be sliced by project. Without this the layer measures activity, not
performance.

**C. Rollups and retention.** Daily immutable aggregates; raw retained ~90
days and only ever removed once the covering rollup exists.

**D. External ground truth.** A reconciler that stamps merged / closed /
reverted from GitHub onto items. Quality is not observable from inside the
harness.

**E. Query surface.** `/api/metrics` over rollups, with explicit windows and
an honest `partial` flag when a window is not fully covered.

**F. Baselines and comparison.** Record one, compare against it, and make the
v1 definition of done evaluable rather than aspirational.

---

## 5. Definition of done

Answer these from the API, for any month in the past year, in one call each:

1. What did a completed item cost, and how did that change?
2. Which implementer model has the best cost per *approved* item?
3. How many items were reverted after merge?
4. What was the longest unattended run?
5. Is delivery better or worse than the recorded baseline?

And one property, which is what "durable" actually means: **deleting
`harness.sqlite` entirely must not change a single answer above.**
