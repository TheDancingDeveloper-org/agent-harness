# Stage J durable-hold report — 2026-08-04

**Status:** delivered. An item can wait on a person as a durable state rather
than as a projection over recent events; the hold survives its worker being
killed, is answerable from any process, and returns the item to `blocked` — not
`ready` — when nobody answers in time.

Every claim below is a **repository fact** reproducible from `ab04085`. There
are no live observations.

Specification: §9 of
[`PROPOSAL-2026-08-finish-then-extend.md`](../PROPOSAL-2026-08-finish-then-extend.md);
acceptance §9.4. Owns issue **#103**.

## 1. Configuration under test

| | |
|---|---|
| Branch | `fix/validator-rejects-valid-patches` |
| Base | `90d9714` (Stage L, 1002 tests) |
| Result commit | `ab04085` |
| New module | `src/agent_harness/holds.py` |
| New tests | `tests/test_stage_j_durable_hold.py`, 27 tests |
| `TMPDIR` | on the NVMe volume, per risk R6 |
| Network, credentials, provider traffic | none |

| Gate | Result on `ab04085` |
|---|---|
| `pytest` | **1031 passed** |
| `ruff check .` | all checks passed |
| `ruff format --check .` | 95 files already formatted |
| `mypy` | success, no issues in 91 source files |

1031 − 1002 = 29: this stage's 27 tests, plus two added to Stage H's file for
the project-level durability configuration committed alongside it.

## 2. What changed, and why the old thing was wrong

`waiting_for_input` was a **projection**: the API scanned the last two hundred
events and reported which items had most recently emitted one. The work row
stayed `claimed`, `_on_waiting` extended the lease, and the item kept looking
exactly like an item being worked on.

So a lease — whose entire purpose is to distinguish *slow* from *dead* — was
being used to hold open a human's inbox. Nothing bounded it, nothing survived
the worker dying, and the answer could only come from the process that happened
to be attached.

There is now a durable `held` state carrying the question, who may answer, a
single-use resume token, and an expiry. It is a state of the item, so it does
not depend on an event surviving in a rolling window.

## 3. D12, and its cost

**D12, resolved 2026-08-04 by decision: a hold suspends the lease and keeps the
claim.** The owner stays on the row, `lease_until` goes to zero, and `claim`
never selects a held row. The worktree and the agent's context survive, so
answering resumes where the item stopped — the reasoning `work.py` already gives
about pause semantics applies unchanged.

**The cost was named when the decision was recorded and is real: a worker slot
is tied up for the whole hold.** That is why `max_hold_seconds` exists, why its
default is six hours rather than unlimited, and why — unlike Stage L's budgets,
where unlimited is the safe upgrade default — an unbounded default would have
been the wrong choice here. A test asserts the default is not unlimited and says
why.

## 4. Acceptance against §9.4

| Criterion | Result |
|---|---|
| An item held for longer than a lease, whose worker is then killed, is still held — not re-claimed, not failed, not silently resumed | Passes. Held with a 100s lease, clock advanced 10 000s, `claim` by a second worker returns `None`, the row is still `held` and still owned by the worker that asked. |
| The hold is answerable from a second process with no attachment to the original session | Passes. The answering side is a **fresh `WorkQueue` over the same file**, holding nothing of the asker — which is what a phone hitting the API is. |
| A held item is visible through the API with its question and its age, and #103's case is distinguishable from a hang | Passes. `GET /api/work/{id}` carries the question, the age in seconds, the expiry and the session deep link. A companion test asserts a merely-claimed item has `hold: null`, which is how a hang now reads. |
| Hold expiry returns the item to a blocked state with the question preserved | Passes. `blocked`, never `pending`, with the question in the blocked reason and `escalated` / `hold_expired` as the disposition. |

## 5. What it must not do, and how each is held

**No model interprets the answer.** `holds.py` has no model client, no prompt
and no call. Asserted against the module's **code with its docstrings and
comments stripped** — the prose forbids these things at length, so grepping the
raw text would have failed on the very sentences saying it must not happen.

**No text is injected into a live PTY.** Same test, same list. Delivery is
through the protocol; the process may be at a shell and an answer becomes a
command.

**Being held is not approval.** An answered item returns to `claimed` — back
into the pipeline at the point it stopped, past nothing. A test asserts the
disposition is still empty.

**It is not the coordination plane.** No ledger, no rooms, no oversight actor;
asserted the same way.

## 6. Two decisions worth stating

**Expiry is swept by the claim scan, not by a scheduler.** `WorkQueue.claim`
expires due holds before it looks for work, and `GET /api/holds` sweeps before
it lists. A sweep that only ran under a cron would leave a held item stuck for
as long as the cron was broken, and there is no cron in this repository to hang
it on.

**`who_may_answer` is recorded and reported, and not enforced.** This service
has one bearer token. Enforcing an identity against it would be a security
claim it cannot keep, and the schema says so in as many words rather than
implying an access control that does not exist.

## 7. Blind spots

Ordered by how badly each could mislead someone reading §4 as good news.

- **The harness does not know what the agent asked.** The session host reports
  *that* a session is waiting, not what it said. The question recorded is
  therefore `"the agent is waiting for input in its terminal session — attach
  at <url>"`, which is a pointer rather than a question. **The single most
  useful thing this stage could deliver — the actual text of the prompt in the
  inbox — is not delivered**, and closing that needs the session host to report
  it. Said plainly rather than papered over with an invented question.

- **Only session mode ever opens a hold.** The direct-API executor never asks
  anybody anything, so it never holds. `talk ask --wait` from
  `COORDINATION-PLANE.md` §5 is not implemented; there is no way for an item to
  raise a question of its own accord in either executor.

- **Answering does not wake the worker.** The item returns to `claimed` with a
  fresh lease, and the worker that asked is inside `wait_for_exit` polling the
  session — it learns nothing from the hold being answered. In practice the
  human answers *in the terminal* and the agent continues; the hold is the
  record and the inbox, not the delivery mechanism. **An answer submitted only
  through the API, to an agent that is genuinely blocked on stdin, does not
  reach the agent.** That is the honest limit of this stage.

- **A hold that keeps a claim keeps a worker.** By design, per D12. On a
  one-worker project a single question stops the project for up to six hours.
  Nothing warns about that, `doctor` does not report it, and the first person
  to hit it will experience it as the fleet being stuck.

- **Nothing notifies anybody.** The inbox exists and must be polled. There is no
  push, no webhook and no `doctor` finding for open holds, so the "answer from
  your phone" story requires something else to tell you there is a question.

- **A held item is invisible to the readiness and rollup surfaces.** `counts`
  reports it under `held`, which nothing displays specially, and the older
  `waiting_for_input` projection still exists in `/api/summary` alongside the
  new state. **Two things now describe the same situation** and they will
  diverge; retiring the projection was out of scope and is owed.

- **`expire_holds` runs inside every claim scan.** One indexed query per scan on
  a table that is empty in the common case. Measured on nothing; assumed cheap.

- **A hold is never re-opened.** If an agent asks, times out, is returned to
  `blocked`, and an operator retries the item, the question is in the blocked
  reason and the hold row is `expired`. Nothing carries the answer forward, and
  nothing stops the next attempt asking the identical question.

- **No test kills a real process.** "The worker was killed" is a clock advanced
  past a lease with nothing renewing it, which is a faithful model of the state
  a `kill -9` leaves and not a test of one.

- **Timing is not reported.** `TMPDIR` was on the NVMe volume per R6.

**"No failures observed" is not equivalent to "the requirement was
exercised."** The end-to-end path — an agent asks a real question, a person
answers it from a real phone, the agent continues — has never been run, and
§7's third bullet says why it would not currently work.

## 8. Continue/stop

**Continue.** §9.4 is met. Two things are owed and are named rather than
deferred silently: the agent's actual question reaching the inbox, and retiring
the `waiting_for_input` projection now that a real state exists.

Next in §3's order: **Stage M — telemetry export**, which is explicitly off the
critical path and blocks nothing.
