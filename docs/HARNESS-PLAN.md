# agent-harness: Comprehensive Plan

**Status (2026-08-02).** P0 complete. P1's code is written and under review as
`swack-tools/oxidex#417`; its *deliverable* — the 72-hour measurement — has not been run.
P2's code is merged and the dashboard runs, but it has never seen live traffic, is not
deployed (D7), and has no soak. **Neither P1 nor P2 has met its exit criteria, and P3 has
not been started.** See §2.6 for what P1 found wrong with this document and §7 P2 for what
P2 found.

Everything now outstanding needs either the fleet host or a human decision. Nothing further
is buildable from a dev machine.
**Date:** 2026-08-02
**Owner:** TheDancingDeveloper-org
**Repository:** `TheDancingDeveloper-org/agent-harness` (private)
**Scope:** Turn the oxidex model-fix loop into a general, service-hosted, GUI-driven agent
harness: role-routed models (plan / implement / review), a durable gate pipeline, and
GitHub issues as the work queue.

Every number in §2 was measured by the oxidex harness itself between 2026-07-22 and
2026-07-30 and is quoted from `swack-tools/oxidex@main:docs/AI_HARNESS.md`. Where this plan
asserts a defect, the evidence is cited. Re-derive rather than trust.

---

## 0.1 SUPERSEDED IN PART — the harness is generic (owner ruling, 2026-08-02)

**Read this before anything else in this document.**

This plan was written on the assumption that one specific repository is the harness's only
consumer until a late "generalise" phase, and that the model-layer work would be applied
*inside* that repository. The owner has ruled otherwise:

> "this framework should be generic and not be tied/targeted at any work"
> "this is meant to go into its own repo; not tied to oxidex"

What that changes:

| The plan says | Now |
|---|---|
| P1 edits `swack-tools/oxidex` in place | The model client lives **here**, provider-agnostic: `providers.py` + `model_client.py`. The oxidex change survives only as a bug fix for that repo, not as this project's home. |
| One consumer until P4; "not generalising before P4" | Generic **from the start**. No workload-specific knowledge in the core; anything format-specific is an opt-in adapter. |
| §2 evidence and §7 exit criteria are stated in one workload's numbers | Those numbers are *evidence for the design*, not targets this repo asserts. A baseline is supplied by whoever runs the harness; there is no built-in one. |
| Gates 1–13, `cargo build`, ExifTool comparison, tag gaps | Not this project's concern. The harness routes model calls, records what happened, and shows it. What the agents *do* is the caller's. |

**What survives unchanged, and is the valuable part:** the failure model (§2.2, §5.1), the
locality principle (§3.3), durability-before-expense (§3.4), events-as-spine (§3.5), and
every correction in §2.6 and §5.3 — all of which came from measurement rather than
argument.

Phase numbering (P0–P4) is kept only so existing issues and milestones still resolve. Do
not treat the phase *order* as current: P4's "generalise" happened first, by ruling.

---

## 0. Execution handoff

**Read this section first. It is written for the agent that will execute the plan.**

### 0.1 Where the code is

Two repositories are in play:

| Repo | Role |
|---|---|
| `swack-tools/oxidex` | Source of the existing harness. `scripts/model_fix_loop.py` (~8,200 lines, the worker) and `scripts/parallel_model_fix_loop.py` (~2,960 lines, the dispatcher). P1 edits it **in place**. |
| `TheDancingDeveloper-org/agent-harness` | New. The service, GUI, and eventually the dispatcher. Created in P0. |

GitHub is the source of truth for code, issues and CI for `agent-harness`. There is no
second remote and no mirror.

### 0.2 What already exists — do not recreate

- The 13-gate pipeline, the `git apply` tolerance ladder, the reviewer-model call and the
  evidence-trailer commit format. All of it works. **P1 does not touch the gates.**
- `~/.oxidex/logs/model-fix-requests/manifest.log` (call outcomes),
  `~/.oxidex/logs/model-fix-diffs/manifest.log` (patch apply outcomes),
  `~/.oxidex/logs/lessons.jsonl` (semantic outcomes). These are the data the P2 dashboard
  reads. They already exist and already contain 8 days of history.
- `scripts/test_model_fix_loop.py` and `scripts/test_parallel_model_fix_loop.py` — the
  regression home for P1's retry-classification tests.
- The Claw Bay gateway already unifies GPT, Codex, Claude, Gemini and DeepSeek behind
  OpenAI-, Anthropic- and Gemini-compatible routes. **No second gateway is needed.**

### 0.2b Decision log

Ratified by the owner on 2026-08-02. These are **settled** — do not re-litigate them.

| # | Decision | Ruling |
|---|---|---|
| D1 | Repo identity | **`TheDancingDeveloper-org/agent-harness`, private.** GitHub only. |
| D2 | Implementation language | **Python.** The harness is already 11k lines of Python; the workload is HTTP, JSON, subprocess supervision and SQLite. Nothing here is hot. Rewriting in Rust buys nothing and makes P3 an IPC boundary instead of an import. |
| D3 | Gateway architecture | **No LiteLLM, no second proxy.** Claw Bay already unifies providers. The failures that hurt are cost-window caps and rpm 429s, neither of which a router config expresses. The fix is an in-process client — see §5.1. |
| D4 | Forge | **GitHub only.** The P4 dispatcher binds to the GitHub API directly. No forge abstraction. |
| D5 | GUI stack | **HTMX + SSE + Jinja**, server-rendered. No node build step in the iteration loop. Revisitable in P4 if the control surface outgrows it; nothing is lost by starting here. |
| D6 | State store | **SQLite + WAL**, single file. An append-only `events` table is the spine; every view is a projection over it. |

D7–D9 remain open and block later phases — see §4.

### 0.3 Order of operations

1. Read §1 (the decision), §3 (principles) and §5 (architecture) in full.
2. P0, then **P1 before anything else is built**. P1 is ~half a day and it is what makes
   P2's dashboard show something other than a wall of undifferentiated 429s.
3. Do not begin P3 until P2's exit criteria are met and the dashboard has run against live
   fleet traffic for at least 48 hours.
4. P4 is gated on D8 (§4).

### 0.4 Rules of engagement

Not style preferences. Each one is why a specific measured failure happened.

1. **Measure before and after.** The 429 work is not done when the code is written; it is
   done when the error-class breakdown proves the number moved. §2.1 is the baseline.
2. **No rewrite.** Every phase moves Python between modules. The 8,200-line worker is never
   ported, never rewritten, and in P1 is barely touched.
3. **Never retry a cost cap.** `weekly_cost_limit_reached` and `5h_cost_limit_reached` are
   not transient. Retrying them is the defect described in §2.2, not a mitigation for it.
4. **No global state in the retry path.** One worker's rejection must never pause another
   worker. This is the single most important invariant in P1.
5. **Checkpoint before the expensive gate.** Work that has passed cheap gates must be
   durable before an expensive one runs. §2.3 is the cautionary tale.
6. **Report honestly.** If an exit criterion is unmet, say so. Do not mark it done. The
   reviewer-model gate exists because this rule cannot be enforced by asking politely.
7. **Correct this document when reality disagrees.** It is a working artefact, not a
   record.

### 0.5 What "done" means

Appendix A is the v1 definition of done. It is deliberately expressed as *observed
behaviour over a week of unattended running*, not internal completeness. If the fleet runs
seven days without a human touching it, and the dashboard answers "why did that fail?"
without anyone opening a log file, the project has succeeded regardless of what remains
unimplemented.

---

## 1. The decision

**Do not adopt an off-the-shelf harness. Extend the one that already works.**

The surveyed alternatives — OpenCode, Roo/Kilo Code, Crush, OpenHands, Aider — all provide
multi-model routing and subagent delegation. None provides a gate pipeline remotely as
rigorous as oxidex's 13 gates, and adopting one would mean discarding the reviewer gate,
the tolerance ladder and the evidence-trailer commit discipline in exchange for features
that amount to a dictionary lookup.

What oxidex lacks is not intelligence. It lacks:

- **role routing** — one uniform random pool serves every call, so a plan/implement/review
  split is not expressible;
- **a correct failure model** — three distinct 429 classes are handled identically;
- **durability** — nothing survives a killed worker;
- **observability** — eight days of data existed before anyone noticed 27,662 errors;
- **generality** — the gates are welded to `cargo build` and ExifTool comparison.

Each of those is additive. None requires replacing what works.

### What we are explicitly not doing

- Not rewriting in Rust (D2).
- Not introducing LiteLLM or any second gateway (D3).
- Not touching gates 4–11 in P1.
- Not building a SPA before a server-rendered dashboard has proven insufficient (D5).
- Not generalising the domain gates before P4 — oxidex remains the only consumer until then.

---

## 2. Evidence base

All figures from `swack-tools/oxidex@main:docs/AI_HARNESS.md`, measured 2026-07-22 to
2026-07-30 unless stated.

### 2.1 The rate-limit baseline

**27,662 rate-limit errors over 8 days**, concentrated on two provider-outage days
(2026-07-23 and 2026-07-28). This is the number P1 must move, and the number P2 must make
visible without anyone reading a log.

The harness cannot currently say how many of those were rpm limits versus cost-window caps,
because it does not read the discriminator. That is itself a P1 finding.

### 2.2 Three defects in the model layer

**(a) The cooldown is global.** A rate-limit response "sets a **global** cooldown that
pauses every worker, growing exponentially per consecutive limited outcome (30s, 60s, 120s,
240s, capped at 300s)."

Two consequences:

- Against an **rpm** limit it is actively harmful. It does not merely stall the fleet, it
  *phase-locks* it: all workers are released at the same instant, emit a synchronised
  burst, trip the limit together and park together. A rate limiter is designed to reject
  exactly that shape.
- Against a **cost-window** cap it is futile. A 300s ceiling against a weekly cap means the
  fleet wakes, is limited, and parks again — indefinitely, until the window rolls.

**(b) `max_retries` is pinned at 1** because "at 3, a single 429 ladder fires four limited
reports and parks the entire fleet near the cap." This is a workaround for (a), not a
policy. Once the governor is local, the pin has no reason to exist.

**(c) Cost caps are treated as transient.** The Claw Bay gateway returns a standard OpenAI
error shape with an extra `theclawbayError` field carrying `weekly_cost_limit_reached`,
`5h_cost_limit_reached` or `invalid_api_key`. The harness does not read it, so a weekly cap
— which cannot clear for days — is retried on the same ladder as a momentary rpm rejection.

### 2.3 No durability

"The commit is the very last step of all", and there is "**no checkpointing of a
passed-gates-but-uncommitted candidate anywhere.**" The document names this "the harness's
sharpest design cost."

A worker killed after gate 12 — reviewer-approved, structurally validated, gap-count
verified — loses all of it. The only durable residue is the raw diff text in
`~/.oxidex/logs/model-fix-diffs/`.

This interacts with §2.2: a fleet parked on a governor is a fleet that is likely to be
killed, and every kill discards work that had already passed the expensive gates.

### 2.4 No role separation

`pick_model_fn` "defaults to `random.choice` over a configured pool", with "a fresh uniform
pick" drawn "before every individual call." One pool serves worker and reviewer alike.

Two problems:

- **Roles cannot be expressed.** Planning, implementing and reviewing are the same draw.
- **The reviewer may be the author.** The document does not specify whether gate 12 draws
  from a dedicated pool. If it does not, some fraction of reviews are a model grading its
  own patch. **360 `review_rejected` out of 14,546 semantic outcomes is 2.5%** — low enough
  that self-review is a live hypothesis for why.

There is also no plan stage at all: gate 3 asks for a unified diff cold, with no preceding
reasoning step to route a stronger model to.

### 2.5 Delivery baseline

- **67 tag gaps closed**, with evidence trailers, July 2026
- **56% delivery rate** for assigned tags
- **48.8% live coverage** across 13 formats
- **patch apply rate 25–80%**, model-dependent

These are the numbers against which P1's role split must be judged. If routing
implementation to a cheaper model drops the apply rate to the bottom of that band, the
extra repair rounds may consume the saving — and §7 P1's exit criteria require that to be
measured, not assumed.

### 2.6 Corrections — what P1 found when it read the code

Recorded per §0.4 rule 7. The §2.1 and §2.3 evidence held up. Three claims did not, and
one method note is worth carrying forward. **Re-derive the rest rather than trusting it.**

**(a) §2.2(b) is wrong: `max_retries` is not pinned at 1, it is 1000.**
`DEFAULT_MAX_RETRIES = 1000`, and `config.example.toml` sets 1000 under both `[worker]` and
`[reviewer]`, with a test asserting it is high-but-not-unlimited. The *reasoning* in §2.2(b)
still held — a long ladder was dangerous because it fed the global governor — so removing
the governor's cooldown leaves the value needing no change. Task T12 was closed as
not-applicable.

**(b) §2.4 overstates the absence of role separation.** A phase split already exists and is
already shipped in the example config:

- `models_for_phase(models, phase)` filters the pool by a per-entry `phase` tag, and
  `attempt_build` re-selects per phase on every turn — `explore` (cheap tier, medium effort)
  for investigation, `patch` (strong tier, max effort) for diffs.
- `[reviewer]` is its own table with its own pool, `base_url` and `api_key`; any `models[]`
  entry may override `base_url`/`api_key`, so one pool can mix providers. The shipped example
  reviewer pool is already disjoint from the worker pool.
- `scripts/model_rotator.py` manages `worker`, `reviewer` and `table_job` as distinct pools.

`random.choice` survives, but it draws *within* a phase-filtered tier, not over one
undifferentiated fleet-wide pool. Making that draw deterministic is a real change with a real
trade-off — it removes the pool-is-the-retry failover the config relies on — and §0.4 rule 1
says measure first. Deferred to P2.

Consequently §2.4's "there is also no plan stage at all: gate 3 asks for a unified diff cold"
is not what the code does; the `explore` phase is that reasoning step. T14 was **not** built:
adding a second planner in front of an existing one would cost a call per attempt and
confound the very measurement P1 exists to produce.

**(c) §7 P1 puts the global governor in the wrong file.** It is in
`scripts/model_fix_loop.py` (`governor_report`, plus `cooldown_until`/`consecutive_limited`
in the flock-guarded state), not `parallel_model_fix_loop.py`. The dispatcher contains no
rate-limit code at all.

**(d) Method note: the 27,662 baseline is not decomposable.** §2.1 already said the harness
cannot say how many were rpm versus cost caps. That is permanent for the historical data —
nothing recorded the discriminator, so it cannot be recovered. P1's `model-calls.jsonl`
makes the *successor* number decomposable, which means P2's error panel compares a total
against a breakdown, not like against like. Say so on the panel rather than implying a
clean delta.

**(e) `ModelClient` landed as a change to `call_model`, not a new class.** §5.1's sketch
assumes an `openai`-SDK client; the worker uses `urllib` directly. A parallel abstraction
beside it would have meant two retry paths, which is exactly the kind of scaffolding §3.1
warns about. All five §5.1 responsibilities are present in the one function.

---

## 3. Guiding principles

1. **The gates are the product.** Everything else is scaffolding around them. Any change
   that weakens a gate to make the scaffolding simpler is the wrong trade.
2. **Failures are classified, not retried.** A retry loop that cannot distinguish transient
   from terminal is a busy-wait with extra steps.
3. **Locality.** No control-plane decision may serialise the fleet. Per-worker state, per-
   endpoint state, never global.
4. **Durability precedes expense.** Anything cheap that has passed must be persisted before
   anything expensive runs.
5. **Events are the spine.** Every state transition is an append-only event. Dashboards,
   metrics and post-hoc analysis are projections. Never a second source of truth.
6. **Additive phases.** Each phase must leave a working harness. There is no phase whose
   midpoint is a broken system.

---

## 4. Decisions required

D1–D6 are resolved (§0.2b). These are open and each blocks a phase.

### D7 — Where does the service run? *(blocks P2)*

> **RESOLVED, then superseded.** First ruled "its own stack"; that is no longer
> the shape. The harness now ships **inside the session host's image** and is
> reached on loopback, so there is no second stack, no second port on the
> tailnet, and no second thing to deploy. The host proxies its API and holds
> its token, so the browser never sees either.

~~Options: its own stack; or inside the existing dev pod. The service is
long-lived, holds SQLite state and serves a GUI, which argues for its own stack with its own
port and its own blast radius. Deciding this is a prerequisite for P2's deploy task, not for
its code.

### D8 — Gate plugin interface *(blocks P4)*

Gates 5, 9 and 13 are `cargo build`, targeted tests and the full workspace suite. Gates 6, 7
and 8 are ExifTool comparison, structural validation and gap-count — wholly oxidex-specific.

P4 needs a boundary between "gates any repo has" (build, lint, test — expressible as the
repo's own required checks) and "gates this repo has". Whether that boundary is a Python
entry-point plugin, a subprocess contract, or a declarative config is undecided and should
not be guessed at before P3 has shown what the dispatcher actually needs.

### D9 — Does the reviewer see the plan? *(blocks P1 task T9)*

Feeding the planner's rationale into the review prompt risks anchoring: the reviewer
approves by construction because it has already read the justification. Withholding it risks
the reviewer lacking context to judge intent.

Recommendation: withhold in P1, and treat it as an A/B once P2 can measure the
`review_rejected` rate per configuration. Do not settle it by argument.

---

## 5. Target architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  agent-harness service  (Python / FastAPI / uvicorn)             │
│                                                                  │
│   HTMX + SSE + Jinja GUI ─────────── fleet · pipeline · errors   │
│                                      quota · diffs · verdicts    │
│   SQLite (WAL)  ── events (append-only) ── projections           │
│                                                                  │
│   queue · claims · role→model map · retry policy   [P3]          │
│   GitHub issues as queue · draft-PR checkpoint     [P4]          │
└───────────────┬──────────────────────────────────────────────────┘
                │ supervises (separate processes)
                ▼
      ┌────────────────────┐  ×N
      │ worker             │
      │  model_fix_loop.py │
      │  gates 1..13       │
      └─────────┬──────────┘
                │ ModelClient
                ▼
        The Claw Bay  ──  /v1 · /anthropic · /v1beta · /quota
        (+ one direct account as fallback path)
```

Workers stay **separate processes**. The existing worker is synchronous and subprocess-heavy
(`cargo build`, `git apply`, test suites); running it as an asyncio task would stall the
loop, and process isolation is already the crash boundary. Do not change that model.

### 5.1 `ModelClient` — the P1 core

Replaces both `pick_model_fn` and the global governor. Five responsibilities:

1. **Role table.** `role → model`, deterministic. `model=role` at the call site.
2. **Error classification.** Read `theclawbayError` before deciding anything. Terminal caps
   raise; rpm limits retry.
3. **Per-worker retry with jitter.** Honour `retry-after` when present; otherwise capped
   exponential. Jitter is not rate shaping — it is what stops N workers rejected together
   from retrying together.
4. **Per-endpoint cooldown.** Claw Bay and one direct account, independent state. No global
   anything.
5. **Structured emission.** Every outcome appended to the event stream with role, model,
   endpoint, error class and latency. This is what P2 renders.

```python
TERMINAL = {"weekly_cost_limit_reached", "invalid_api_key"}
WINDOW   = {"5h_cost_limit_reached"}

class CapExhausted(Exception): ...

def call_model(client, role, messages, max_attempts=6):
    for attempt in range(max_attempts):
        try:
            return client.chat.completions.create(model=role, messages=messages)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429:
                raise
            kind = (e.response.json().get("error", {}) or {}).get("theclawbayError")
            if kind in TERMINAL or kind in WINDOW:
                raise CapExhausted(kind) from e      # retrying cannot help
            ra = e.response.headers.get("retry-after")
            delay = float(ra) if ra else min(2 ** attempt, 30)
            time.sleep(delay + random.uniform(0, delay))
    raise RuntimeError(f"{role}: 429 after {max_attempts} attempts")
```

`CapExhausted` propagates to the worker, which exits cleanly and lets the dispatcher decide
whether to reallocate or stop the fleet. It is not caught inside the gate pipeline.

### 5.2 Model roles

| Role | Model | Rationale |
|---|---|---|
| `planner` | strongest available reasoning tier | Runs once per attempt. Cheap in aggregate, highest leverage. |
| `implementer` | cheaper reasoning tier | The volume role. Where cost actually lives. |
| `reviewer` | **a different vendor** — Claude or Gemini via Claw Bay's `/anthropic` or `/v1beta` | The strongest available form of reviewer independence, at zero integration cost since it is the same key. Directly addresses §2.4. |

Enumerate `/models` against the live key before pinning identifiers. The gateway
documentation lists `gpt-5.5`, `gpt-5.4` and `gpt-5.4-mini` as the reasoning-capable set and
explicitly instructs callers not to hardcode availability.

**Enumerated 2026-08-02** (42 models). The reasoning-capable OpenAI tier is wider than the
documentation says: `gpt-5.6`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`,
`gpt-5.4`, `gpt-5.4-mini`. More importantly, **cross-vendor models are reachable on the same
key and the same OpenAI-compatible route** — `claude-opus-4-8`, `claude-sonnet-4-6`,
`claude-haiku-4-5`, `gemini-3.1-pro-preview`, `glm-5.2`, `kimi-k2.7-code`,
`deepseek-v4-pro`. So reviewer independence needs no `/anthropic` route and no second
integration: it is a model name. The instruction not to hardcode availability stands — this
list is evidence of what was true on one day, not a constant.

### 5.3 Quota awareness

> **CORRECTED 2026-08-02 — `/quota` does not exist.** Probed live against
> `api.theclawbay.com`: `/quota`, `/v1/quota` and `/api/quota` all return 404. `GET
> /v1/usage` does exist, but returns `{aggregation_timestamp, n_context_tokens_total,
> n_generated_tokens_total}` — **token counts, not cost windows and not remaining
> headroom**. There is currently no way to ask the gateway how much budget is left.
>
> This invalidates the mechanism below, not the goal. Everything in this section that says
> "poll `/quota`" has to become inference from the event stream instead: the only signal
> that a window is spent is the `5h_cost_limit_reached` / `weekly_cost_limit_reached` 429
> itself, **after the fact**. That is a materially weaker instrument — it detects
> exhaustion rather than predicting it — and P3's T35 (quota-gated admission with reviewer
> headroom) is the task that has to absorb the difference. Re-probe before designing it;
> the gateway may grow the endpoint.
>
> What survives unchanged: the *reason* for reserving headroom, below.

~~The gateway exposes `/quota` over 5-hour and weekly windows. Poll it, cache it, and check
before dispatching a *round* — not mid-attempt.~~

**Reserve headroom for the reviewer.** If implementers exhaust the weekly window, the fleet
holds patches that passed every gate but cannot be reviewed or merged — which is §2.3's
durability loss reproduced at the budget layer. Cap implementer spend at a fraction of the
window and ringfence planner/reviewer allocation.

---

## 6. Engineering practice

### 6.1 Testing standard

P1's retry classification is the highest-risk logic in the project: it decides whether the
fleet stalls for a week. It is also trivially testable — a fake response object and a table
of error payloads.

- **Red-first** for every branch of the classifier: rpm 429, `5h_cost_limit_reached`,
  `weekly_cost_limit_reached`, `invalid_api_key`, non-429 error, `retry-after` present,
  `retry-after` absent, attempt exhaustion.
- Existing homes: `scripts/test_model_fix_loop.py`,
  `scripts/test_parallel_model_fix_loop.py`.
- **No sleeping in tests.** Inject the sleep function.

For the service: `pytest` + `httpx.ASGITransport` against the FastAPI app. The event store is
a temp SQLite file per test.

### 6.2 CI

GitHub Actions on `agent-harness` from P0: `ruff`, `mypy`, `pytest`. Branch protection on
`main` — no direct push, PR with green checks, linear history.

`swack-tools/oxidex` keeps its own CI; P1's tests run there.

---

## 7. Phase plan

Phases are sequential. Work within a phase is parallelisable. Every exit criterion is
objectively checkable.

### P0 — Repository and guardrails

No functional code. Stand up where the work is tracked.

| Item | Detail |
|---|---|
| Repo | `TheDancingDeveloper-org/agent-harness`, **private**, GitHub Actions enabled |
| Labels | 13. `area:model-client`, `area:dispatch`, `area:gui`, `area:store`, `area:github`, `area:ci`, `area:docs`; `type:epic`, `type:task`, `type:decision`, `type:spike`; `risk:high`, `blocked`. **No `phase:*`** — milestones already carry phase, and two encodings of one fact drift. |
| Milestones | `P0`…`P4` |
| Issues | From `docs/backlog.json` via the Appendix B script |
| Project | Projects v2 board, fields `Phase`, `Area`, `Size`, `Risk` |
| Docs | `README.md` (honest pre-alpha status), `AGENTS.md`, this plan |
| Branch protection | `main`: PR required, `lint`/`test` required, linear history |
| CI | `.github/workflows/ci.yml` — ruff, mypy, pytest |

**Exit:** repo exists with green CI; branch protection active; all backlog items created,
labelled, milestoned and on the board.

**Met 2026-08-02.** Two things the plan did not anticipate, recorded per §0.4 rule 7:

- The org runs Actions with `enabled_repositories: selected`, so a new repository has
  Actions **disabled** regardless of its own settings. Enabling it requires adding the repo
  to the org allowlist (`PUT /orgs/{org}/actions/permissions/repositories/{id}`), not
  `gh repo edit`. Expect the same for any future repo in this org.
- The 13 labels coexist with GitHub's 9 defaults, which are unused. The plan's "13" is the
  set this backlog uses, not the total.

`Size` on the board is deliberately unset: it is a judgement per item and is not derivable
from `backlog.json`.

---

### P1 — Fix the model layer *(in `swack-tools/oxidex`, ~half a day)*

The cheapest, highest-ratio work in the project. It is also the prerequisite for P2 being
worth looking at.

**Read §2.6 before starting — several of the tasks below rest on claims that turned out to
be stale.** Code status: `swack-tools/oxidex#417`, open. Measurement status: not run.

| Task | Detail |
|---|---|
| Delete the global governor | Remove the fleet-wide cooldown and its 30/60/120/240/300 ladder from **`model_fix_loop.py`** (§2.6c corrects the file). This is the §2.2(a) fix. **Done in #417.** |
| Add `ModelClient` | §5.1. Per-worker retry, `theclawbayError` classification, `retry-after`, jitter, per-endpoint cooldown. **Done in #417**, as a change to `call_model` rather than a new class — §2.6e. |
| ~~Un-pin `max_retries`~~ | **Not applicable** — it is 1000, not 1 (§2.6a). |
| Role table | **Largely already exists** (§2.6b). Deferred to P2, where the delta is measurable per role. |
| ~~Add the plan stage~~ | **Not built** — the `explore` phase already is one (§2.6b). Re-scoped to P2 as an A/B against it. |
| Reviewer independence | Mechanism already exists and the example config already uses a different vendor (§2.6b). **Unverified against the live fleet config**, which is gitignored — needs host access. |
| Structured emission | Every call outcome appended with role, model, endpoint, error class, latency. Superset of today's `manifest.log`; P2 depends on it. **Done in #417** — `~/.oxidex/logs/model-calls.jsonl`, one record per *attempt*. |
| Tests | §6.1, red-first, in the existing test files. **Done in #417** — 713 pass. |
| **Measure** | The actual deliverable (§9). **Not run.** |

**Exit:**
- No code path pauses more than one worker.
- Every 429 is recorded with its `theclawbayError` class; cost caps are never retried.
- Role split live; reviewer on a different vendor than the implementer.
- A 72-hour run produces: rate-limit error count, broken down by class, versus the §2.1
  baseline of 27,662/8 days; and delivery rate and patch-apply rate versus the §2.5
  baseline of 56% / 25–80%.
- **If the apply rate falls to the bottom of the §2.5 band, that is a finding, not a
  failure — record it and re-evaluate the implementer tier before P3 hardcodes the map.**

**Risk:** low. The gates are untouched; the blast radius is one function and one deletion.

---

### P2 — Read-only dashboard

The measuring instrument. Nothing after this is guesswork.

FastAPI + uvicorn, SQLite (WAL), HTMX + SSE + Jinja. An ingester tails the P1 event stream
and the existing `~/.oxidex/logs/*` JSONL into the `events` table; every view is a
projection.

**Zero changes to the harness.** If the dashboard crashes, the fleet does not notice.

Panels:

| Panel | Shows |
|---|---|
| **Errors** | 429s by class — rpm vs `5h_cost_limit_reached` vs `weekly_cost_limit_reached`. **The highest-value panel; the one that would have surfaced §2.1 in a day rather than eight.** |
| Fleet | Workers, role, stage, model, elapsed, claim |
| Pipeline | Each in-flight candidate as a row of gates, so it is visible *where* things die |
| Quota & spend | `/quota` polled; per-role spend; headroom |
| Diffs & verdicts | Every logged diff with its reviewer verdict and reasoning |

**Exit:**
- Service deployed per D7, reachable over Tailscale, token-authenticated.
- Ingests the full existing 8-day history without loss.
- Live SSE updates within 2s of a worker event.
- The error panel reproduces the §2.1 baseline from historical data **and** shows the P1
  delta.
- 48 hours against live fleet traffic with no ingester restarts.

**Code merged 2026-08-02; exit criteria NOT met.** Token auth, ingest and all five panels
work against synthetic logs. Deploy is blocked on D7; the 8-day reproduction and the 48-hour
soak both need the fleet host.

One exit criterion is **unachievable as written**, and this is the plan's error rather than
the implementation's: *"shows the P1 delta"* presumes the baseline can be broken down. It
cannot. The 27,662 is a total, and nothing recorded the discriminator, so there is no
per-class value to compare a P1 number against. The panel therefore compares **totals**,
reports historical rate limits as `unclassified` in their own bucket, and states on the page
that a per-class delta must not be read from it. Amend the criterion to: *reproduces the
§2.1 total from historical data, and shows the post-P1 breakdown alongside it, labelled as
not like-for-like.*

Two smaller findings:

- **Gate-by-gate pipeline rows are not buildable from today's logs.** Nothing emits a
  per-gate event, so "each in-flight candidate as a row of gates" has nothing to project.
  That is a worker change, not a dashboard one, and it belongs with P3 or a P1 follow-up.
- **Spend is not observable without the gateway key**, which the service should not hold
  before D7. The quota panel shows cap *hits* and says plainly that spend is unavailable,
  rather than rendering an empty chart that reads as zero.

---

### P3 — Service owns dispatch

The queue, claims, role→model map and retry policy move into the service.
`parallel_model_fix_loop.py` retires into it; `model_fix_loop.py` survives as the gate
runner, invoked with a work item.

This is the phase that stays cheap because of D2 — moving functions between modules, not
crossing a language boundary.

Adds: live edit of the role→model map without redeploy; pause / drain / kill-worker /
requeue controls; quota-gated admission with reviewer headroom reserved (§5.3).

**Exit:**
- `parallel_model_fix_loop.py` deleted; the service is the only dispatcher.
- Claims survive a service restart (they are rows, not `flock` holders).
- Role map changes take effect without redeploy.
- Drain completes cleanly: no worker killed mid-gate.
- A 7-day unattended run with no manual intervention.

---

### P4 — Generalise

Claims become **GitHub issue assignments**. Gates become the target repo's **required
checks**. The checkpoint becomes a **draft PR** — which is the §2.3 fix: a candidate that
has passed the cheap gates is pushed as a branch with its evidence trailers, and the
expensive suite runs as a required check off the worker. A killed worker loses nothing.

Blocked on D8 (gate plugin interface).

**Exit:**
- Runs against a second repository with no oxidex-specific code on the path.
- A killed worker mid-gate-13 loses no reviewer-approved work.
- Reviewer verdicts appear as PR reviews; decision blockers appear as `type:decision`
  issues.

---

## 8. Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Cheaper implementer tier drops the apply rate enough to erase the saving | Medium | Medium | P1 exit criteria measure it explicitly against the §2.5 baseline before P3 hardcodes the map |
| R2 | Rate-limit errors persist after P1 because the rpm ceiling is genuinely below fleet demand | Medium | High | P2's error panel distinguishes the classes; if rpm dominates post-P1, the answer is fleet concurrency, which is a P3 lever |
| R3 | The dashboard becomes a second source of truth and drifts | Low | High | §3.5 — events are append-only, views are projections, never written back |
| R4 | P4's gate abstraction is designed before the dispatcher's needs are known | Medium | Medium | D8 explicitly blocks P4 and forbids guessing before P3 |
| R5 | Scope creep into a general-purpose agent platform | High | High | §1 "what we are explicitly not doing"; oxidex is the only consumer until P4 |
| R6 | Reviewer independence turns out not to matter and the vendor split adds cost for nothing | Low | Low | D9's A/B once P2 can measure `review_rejected` per configuration |

---

## 9. Effort shape

| Phase | Shape |
|---|---|
| P0 | Hours. Mechanical. |
| P1 | Half a day of code, then 72 hours of measurement. **The measurement is the deliverable, not the code.** |
| P2 | The largest single build. Ingester, store, five panels, deploy. |
| P3 | Moderate. Mostly relocating existing logic. |
| P4 | Open-ended; scope depends on D8. |

---

## Appendix A — Definition of done for v1

Expressed as observed behaviour, not internal completeness:

1. The fleet runs **7 days unattended** with no manual restart.
2. Every failure is diagnosable **from the GUI alone**, without opening a log file.
3. Rate-limit errors are **classified**, and cost caps are never retried.
4. No single worker's failure pauses another worker.
5. Reviewer-approved work **survives a killed worker**.
6. Delivery rate is **no worse than the 56% baseline** in §2.5, at lower cost.
7. The role→model map can be changed **without a redeploy**.

If all seven hold, v1 is done regardless of what remains unimplemented.

---

## Appendix B — Backlog

The authoritative, machine-readable manifest is **`docs/backlog.json`** — id, title, body,
labels and milestone for every item.

### Creation script

```bash
R=TheDancingDeveloper-org/agent-harness
python3 - <<'EOF' > /tmp/mk-harness-issues.sh
import json, shlex
for it in json.load(open('docs/backlog.json')):
    print("gh issue create -R $R "
          f"--title {shlex.quote(it['title'])} "
          f"--body {shlex.quote(it['body'])} "
          f"--label {shlex.quote(','.join(it['labels']))} "
          f"--milestone {shlex.quote(it['milestone'])}")
EOF
bash /tmp/mk-harness-issues.sh
```

Labels and milestones must exist first (P0). Verify with
`gh issue list -R $R --limit 100 | wc -l`.
