# Proposal — finish the fit-for-purpose programme, then extend it

> # Superseded — 2026-08-06
>
> A sequencing proposal. Its decisions D11–D14 are recorded in `AGENTS.md` and remain in force; the sequencing itself is spent.
>
> **Current documentation:**
> [`docs/DESIGN.md`](DESIGN.md) — how the harness works and why.
> [`docs/STATUS.md`](STATUS.md) — where it stands and what is left to do.
>
> Kept for the reasoning and the evidence, which are not reproduced elsewhere.
> **Do not follow its plan, its phase order, or its statements of current
> state** — all three are out of date. Where this document and the code
> disagree, the code is right.

**Status:** proposal, not accepted. Written 2026-08-04, after a review of five
comparable systems: [Conductor](https://github.com/conductor-oss/conductor),
[agentspan](https://github.com/agentspan-ai/agentspan),
[LangGraph](https://github.com/langchain-ai/langgraph),
[Mastra](https://github.com/mastra-ai/mastra) and
[CrewAI](https://github.com/crewAIInc/crewAI).

This document builds on
[`FIT-FOR-PURPOSE-STATUS.md`](FIT-FOR-PURPOSE-STATUS.md) and does not replace
it. That document is the frozen record of where the programme got to; this one
proposes what happens next. Where the two disagree about the *state* of the
work, the status document wins — it was written with the worktrees in front of
it.

It also does not replace
[`PROPOSAL-2026-08-fit-for-purpose.md`](PROPOSAL-2026-08-fit-for-purpose.md).
Stages 0, A, E2, E1, C, B, G, D and 8 keep their existing specifications and
acceptance criteria. This proposal adds stages after them and settles the
sequencing question the status document left open.

## 1. The decision this document exists to make

The programme is half-landed. Four stages are complete on unmerged branches,
Stage D was deferred by instruction, Stage 8 is blocked, and a review of
comparable systems has produced a further set of proposed capabilities.

The question is whether to pivot to the new work now or finish first.

**Recommendation: finish, then extend — with one exception.** Merge and verify
what exists (Stage F below), then take Stage D off the shelf, and only then
start the new stages. Do *not* wait for Stage 8; it is blocked on things this
repository cannot supply, and treating it as a prerequisite would stall
indefinitely.

The argument is not sentiment about sunk work. It is four specific facts.

**The unmerged branches are the highest-risk asset in the programme.** Four
branches, all based on `afdc3bc`, all gate-green alone, none merged, with
overlapping edits to `__main__.py`, `schemas.py`, `api.py` and `plan.py`. The
status document is explicit that B and G both changed what a route and an
admission decision are made of, so the conflict is semantic rather than
textual. That conflict does not get easier with age; every commit added to the
integration branch first makes it worse. Finished work that is not integrated
is not an asset, it is a liability with a decay rate.

**Most of the new work depends on the unmerged stages.** The resumable attempt
record (Stage H) needs Stage G's graph revision and Stage B's route/preset
separation to have anything stable to record. The durable human hold (Stage J)
needs Stage C's approval lifecycle. Starting them on `afdc3bc` means building
on a base that is about to move.

**The programme's own gate rule forbids it.** Proposal §10: *a stage cannot be
marked complete because its code exists; its report and its gate must pass.*
Four stages have per-branch gates and no merged-tree gate. Pivoting now would
leave them permanently in a state the programme has no word for — neither
complete nor abandoned.

**But finishing the whole programme first is also wrong.** Stage 8 needs
credentials, network, a real second repository, and NGMS human decisions that
§9 explicitly refuses to answer by assertion. It is not startable from here.
Sequencing new work behind it would mean sequencing it behind other people's
decisions.

So the cut is: **integration and Stage D are prerequisites; Stage 8 is not.**

## 2. Finding: `docs/backlog.json` is not a record of current state

It was never intended to be one, and it has now drifted far enough that
reading it as one would mislead.

**What it is.** A one-shot seeding manifest for `gh issue create` (task T9). It
carries no state field per item, so it cannot report a status — it can only go
stale by omission, and it has.

**Measured drift, 2026-08-04:**

| Fact | Value |
|---|---|
| Items in `docs/backlog.json` | 56 (`D1`–`D9`, `E0`–`E4`, `T1`–`T42`) |
| Issues on GitHub | 100 |
| Of those, closed | 92 |
| Of those, open | 8 |
| Items on GitHub but not in the manifest | 44, including `T43` (#84) and 26 bugs |
| `D10` | exists **only** on the unmerged `codex/fit-stage-e1` branch |

**It also encodes a superseded structure.** Every item is filed under
milestones `P0`–`P4`, which is the `HARNESS-PLAN.md` phase order that
`AGENTS.md` states plainly should not be followed. Worse, that scheme is
*enforced by a test*: `tests/test_backlog.py` hardcodes
`MILESTONES = {"P0", "P1", "P2", "P3", "P4"}`. Any new stage naming would fail
that test, so the superseded phase order is currently load-bearing.

**And it contradicts a settled decision.** D1 resolved that GitHub is the sole
source of truth for issues. A second issue list in the repository is a second
source of truth by construction — precisely the pattern `AGENTS.md` rejects for
projections over the event store.

**It is not, however, wrong about anything important.** Its decision records
D1–D9 are accurate and are still the reference for what was settled. The
problem is the file's *implied* role, not its content.

### 2.1 What to do about it

Housekeeping inside Stage F, not a stage of its own:

1. Rename to `docs/backlog-seed-2026-08-02.json` and update the one test path,
   the `AGENTS.md` table row and the README link. The name then states what it
   is: the manifest that seeded the issues on that date.
2. Add `D10` to it from the E1 branch, so the decision record set is complete
   in one place, and add `T43` so the seed matches what was actually created.
3. Record in `AGENTS.md` that GitHub is the tracker and the seed file is
   historical, per D1.
4. Do **not** attempt to regenerate the manifest from GitHub or keep the two in
   sync. Two-way sync between a file and an issue tracker is a project in
   itself, and D1 already says which one wins.

### 2.2 The eight open issues, and where they now belong

This matters because it shows the new stages are not inventions — three of the
four non-validation issues already exist and have no owning stage.

| Issue | Subject | Disposition |
|---|---|---|
| #146 | context picks smallest files | **Fixed by Stage E2, already in `afdc3bc`, but the issue is still open.** Close it in Stage F. |
| #128 | agent model traffic invisible | Partially addressed by **Stage M**; the rest stays open and is named as a blind spot. |
| #103 | silent-but-active sessions indistinguishable from hangs | Owned by **Stage J**. |
| #84 (`T43`) | A/B whether the reviewer sees the plan | Blocks **D9**. Unchanged; still needs a run. |
| #33 (`T19`) | 72-hour measurement run | Stage 8. Blocked. |
| #44 (`T30`) | 48-hour soak | Stage 8. Blocked. |
| #51 (`T37`) | 7-day unattended run | Stage 8. Blocked. |
| #56 (`T42`) | second-repository validation | Stage 8. Blocked. |

## 3. The revised sequence

Existing stages keep their letters. New stages take free letters; `I` is
skipped because it reads as a digit.

| Order | Stage | State | Gate to continue |
|---:|---|---|---|
| 0–2 | 0, A, E2 | complete, in `afdc3bc` | — |
| 3–6 | E1, C, G, B | complete, unmerged | — |
| **7** | **F — integration** | **new, next** | Merged tree passes four gates; real test count derived; programme report written |
| 8 | D — first run | deferred; **un-defer here** | Clean checkout runs with no credentials or network |
| **9** | **K — outcome and check taxonomy** | **new** | Four gate outcomes and a non-failure stop are distinguishable end to end |
| **10** | **H — resumable attempts** | **new** | A killed worker resumes at the last durable stage without re-paying for earlier ones |
| **11** | **L — item budgets** | **new** | An item cannot exceed a declared wall-clock or spend ceiling |
| **12** | **J — durable human hold** | **new** | A held item survives worker death and is resumable from another process |
| **M** | **M — telemetry export** | **new, parallel** | Spans are exported without becoming a second source of truth |
| 13 | 8 — validation | blocked | Unchanged (§9 of the original proposal) |

Stage M is deliberately off the critical path: it depends only on F and blocks
nothing.

**Merge order within Stage F is unchanged from the status document: B → G → C →
E1.** That ordering was derived with the worktrees in front of it and there is
no reason to second-guess it here.

## 4. Stage F — integration and merged-tree verification

The status document's §6 lists this as "suggested next actions". This
proposal makes it a stage, because under §10 of the original proposal an
action without a gate and an evidence report cannot complete anything.

### 4.1 What it does

1. Merge `codex/fit-stage-b`, `codex/fit-stage-g`, `codex/fit-stage-c` and
   `codex/fit-stage-e1` into `fix/validator-rejects-valid-patches`, in that
   order, with `TMPDIR` on a fast volume.
2. Resolve conflicts by re-reading each stage's evidence report, not by
   picking a side of the diff. B and G disagree about what a route and an
   admission decision are made of; that is a semantic question.
3. Re-run the four gates on the merged tree and derive the real test count
   there.
4. Write the §11 per-stage programme report over the delivered stages.
5. Close #146. Do the `backlog.json` housekeeping from §2.1.

### 4.2 What it must not do

- **No new capability.** A behaviour change smuggled into an integration
  branch cannot be attributed to a stage, which defeats the whole evidence
  scheme.
- **No stage marked complete in the programme report that this stage's own
  gates did not re-prove on the merged tree.** Per-branch results are not
  evidence about the merged tree.
- **No carrying forward of any per-branch test count.** 676 / 678 / 708 / 715 /
  821 are not additive, Stage G rewrote two pre-existing tests, and "the
  original 676 still pass unchanged" is already known to be false.
- **No quoting this session's wall-clock timings.** Set `TMPDIR` first; the
  status document §4 explains why.

### 4.3 Acceptance

- The merged tree passes `pytest`, `ruff check`, `ruff format --check` and
  `mypy`, with the test count derived on that tree and stated once.
- The programme report records Stage D as not started by instruction and
  Stage 8 as blocked. Neither is reported as complete-with-caveats.
- Every divergence found while resolving a conflict is recorded, including any
  case where a stage's evidence report turned out to describe behaviour the
  merge changed.
- Evidence artefact: `evidence/YYYY-MM-DD-stage-f-integration.md`, with a
  blind-spots section.

## 5. Stage D — un-deferred

Specification unchanged: §8 of the original proposal, acceptance §8.4.

The only change this proposal makes is to its position. It moves from
"deferred by instruction" to immediately after integration, for three reasons:

- It is blocked on nothing but the instruction. Every other remaining stage is
  blocked on integration, an open decision, or credentials.
- The status document already names the consequence of its absence: without
  `init --demo` and `doctor` the stranger-on-a-laptop path is unproven, and
  README first-run claims stand unverified.
- It is the largest single gap against every system reviewed. Mastra, CrewAI,
  agentspan and Conductor all have a one-command local start. Reviewing them
  made this the clearest comparative weakness, and it is also the cheapest to
  close.

`doctor` additionally becomes the natural home for checks the later stages
want to expose, so building it first avoids retrofitting it four times.

**This stage still requires an explicit go-ahead.** It was deferred by
instruction and this proposal does not overturn an instruction by argument.

## 6. Stage K — outcome and check taxonomy

*Prior art: agentspan's guardrail outcomes (retry / raise / fix / escalate);
Mastra's run-result discriminated union (`success | failed | suspended |
paused | tripwire`).*

### 6.1 Why

`Checks.run()` returns `(bool, str)`. That one bit currently collapses four
different situations: a transient check failure worth retrying, a defect that
is genuinely the item's fault, a failure a mechanical fix would clear, and a
condition that needs a human. Only the second is a `failed` item in any useful
sense.

The same flattening exists one level up. `Outcome.state` reuses queue states —
`done`, `failed`, `blocked`, `pending` — so a checks failure, a reviewer
rejection, a spend cap and a crashed worker all arrive at the queue looking
similar. The repository already knows this is wrong, which is why `EXHAUSTED`
exists and why `release(consume_attempt=False)` exists: both are patches over a
missing distinction. This stage names the distinction instead.

### 6.2 What it does

- A check result becomes a typed outcome with at least: `pass`, `retry`,
  `fail`, `fix_available`, `escalate`.
- An attempt result becomes a discriminated union that separates *a gate
  stopped this* from *this failed*. A reviewer rejection and a guardrail stop
  are not failures; they are refusals, and an item refused by a gate should not
  consume attempts the same way a crash does.
- `providers`' existing error classes are left exactly as they are. They
  classify a *provider's* answer; this classifies a *gate's* answer. Two
  vocabularies, deliberately.

### 6.3 What it must not do

- **It must not answer D8.** A richer check *result* is not a gate plugin
  interface. If implementing it starts to require deciding how third-party
  gates are registered, stop and report D8 as blocking.
- **It must not weaken a gate.** `escalate` is an additional outcome, never a
  way for a check to decline to fail.
- **`fix_available` must not auto-apply anything in this stage.** It records
  that a fix is derivable. Applying it is a later decision with its own
  evidence.

### 6.4 Acceptance

- A fixture exercises all five check outcomes and proves each reaches the queue
  as a distinguishable state, visible through the API without reading a log.
- A reviewer rejection and a worker crash produce different item states, and
  the difference is visible in `GET /api/work/{id}`.
- No existing gate becomes skippable, optional or cheaper.
- Evidence artefact: `evidence/YYYY-MM-DD-stage-k-outcome-taxonomy.md`.

## 7. Stage H — resumable attempts

*Prior art: Conductor's per-task persistence and "rerun only the failed task";
LangGraph's durability modes (`exit` / `async` / `sync`) and its rule that side
effects before a resume point must be idempotent.*

**This is the largest new stage and the one with the most value.**

### 7.1 Why

An attempt is currently monolithic. `Executor._execute` starts at the planner
call every time. If anything raises, `run_once` releases the item and the next
claim begins again from `PLAN_PROMPT` — re-paying for the planner, re-selecting
context, and re-calling the implementer. The only thing that survives is the
branch and PR recovered through `_partial_for`, which is real and worth having,
but it is a recovered *artefact*, not a resumed *position*.

The repository already holds the correct principle — checkpoint before the
expensive gate — and applies it at exactly one point. This stage generalises
one checkpoint into a record of where an attempt got to.

The cost argument is direct: v1's definition of done includes *delivery rate no
worse than the pre-harness baseline, at lower cost*. Re-running the planner and
implementer after every crash is a cost multiplier that no amount of routing
policy can offset.

### 7.2 What it does

- A durable per-attempt record keyed `(project_id, item_id, attempt)`, holding
  one row per stage reached: planned, implemented, applied, checked,
  checkpointed, reviewed — each with the artefact that makes it resumable
  (planner targets, the diff, the commit sha, the PR url) and the graph
  revision it was admitted at.
- On re-claim, an attempt resumes at the last recorded durable stage rather
  than at the planner.
- **Durability becomes a policy, not a constant.** A configured mode selects
  how often progress is made durable — nothing until exit, at each stage
  boundary, or synchronously before each external effect. The deterministic
  demo can run with the cheapest setting; a fleet runs with the strictest.
- The item's brief and dependency set are pinned to the revision the attempt
  was briefed with, extending Stage G's `work.admitted_revision`. `WorkQueue.add`
  currently rewrites `title`, `brief` and `depends_on` on live claimed rows, so
  a worker can be briefed from one revision and judged against another.

### 7.3 What it must not do

- **It must not become a workflow engine.** No DSL, no user-defined graph of
  stages, no dynamic step registration. The stage list is the executor's own,
  fixed, and known at compile time. §12 of the original proposal stands.
- **It must not assume replay is deterministic.** LangGraph can replay because
  it requires deterministic nodes; a model writing a diff is not deterministic
  and never will be. Resumption here means *continue from the last durable
  artefact*, never *replay the log*.
- **Anything re-executed on resume must be idempotent.** This is the rule
  LangGraph states outright and it applies unchanged: a stage that runs again
  after a resume must not double an external effect. Where a stage cannot be
  made idempotent, its effect moves after the durable boundary.
- **It must not make the pre-review checkpoint weaker or optional.** A
  durability mode may make other boundaries *more* frequent; none may remove
  that one.
- It must not silently change `attempts` accounting — see D11.

### 7.4 Acceptance

- A worker killed after checks pass and before review is re-claimed and reaches
  review **without a second planner or implementer call**, proven by the event
  stream, not by timing.
- A worker killed at each stage boundary in turn resumes correctly from each.
- Cost per completed item under induced crashes is measured against the same
  fixture with resumption disabled, and both numbers are reported.
- An attempt briefed at revision N and re-claimed after the plan moved to N+1
  is handled explicitly — not silently judged against the newer brief.
- Every durability mode is exercised, and the mode is recorded in the events.
- Evidence artefact: `evidence/YYYY-MM-DD-stage-h-resumable-attempts.md`.

## 8. Stage L — item budgets

*Prior art: Conductor's separation of `responseTimeout` (the worker must check
in) from `timeoutSeconds` (total wall clock for the task).*

### 8.1 Why

The lease bounds a worker's *absence*, not an item's *duration*. A heartbeat
proves a process is alive; it proves nothing about progress. There is no
per-item wall-clock budget in the queue at all — `session_executor` has a
3600s agent timeout and `Checks` has a 900s subprocess timeout, but the item
itself is unbounded. An item that heartbeats forever is indistinguishable from
one making progress, and it is the failure mode a 7-day unattended run is most
likely to produce.

The same hole exists for money. An item can consume an unbounded number of
model calls across unbounded attempts; the only ceiling is `max_attempts`,
which counts attempts, not spend.

### 8.2 What it does

- A per-item wall-clock ceiling and a per-item spend ceiling, declared on the
  project and overridable per item, both defaulting to unlimited so no
  existing deployment changes behaviour on upgrade.
- Exceeding either produces a distinct, diagnosable state — not `failed`, and
  not `exhausted`. It is a budget stop, and Stage K supplies the vocabulary.
- The ceilings are readable through `doctor` and the API, so an operator can
  see what a project is permitted to spend before it spends it.

### 8.3 What it must not do

- **A budget stop must never kill work in flight mid-item** unless explicitly
  configured to, for the reason `work.py` already gives about pause semantics:
  stopping mid-item destroys context and leaves a half-finished worktree.
  Default is to stop at the next boundary.
- **A spend ceiling is not a cost cap and must not be classified as one.**
  `WINDOW_CAP` and `TERMINAL_CAP` are the provider's statements about our
  budget. This is our statement about one item. Conflating them would put a
  local policy decision into the never-retry set.
- Unknown cost stays unknown. An item whose spend cannot be determined —
  session-mode traffic, per #128 — must not be treated as having spent zero.
  That is `pricing.py`'s existing rule and it decides this stage's edge case.

### 8.4 Acceptance

- An item exceeding its wall-clock ceiling stops at the next boundary with a
  distinguishable state and a reason naming the ceiling.
- An item exceeding its spend ceiling does the same, and does not park the
  endpoint.
- An item whose spend is unmeasurable is reported as unmeasurable, and the
  report says which ceiling could therefore not be enforced.
- Defaults are unlimited; an existing database upgrades with no behaviour
  change.
- Evidence artefact: `evidence/YYYY-MM-DD-stage-l-item-budgets.md`.

## 9. Stage J — durable human hold

*Prior art: agentspan's `@tool(approval_required=True)` durable pause spanning
days, approvable from any machine; LangGraph's `interrupt()` / `Command(resume=)`;
Mastra's typed suspend/resume. Owns issue #103.*

### 9.1 Why

`waiting_for_input` today is a *projection over the event stream*
(`api.py`), not a state of the work item. The row stays `claimed`, the
heartbeat keeps stamping, and the lease keeps renewing — so a lease whose
entire purpose is to distinguish *slow* from *dead* is being used to hold open
a human's inbox. Nothing bounds it, nothing survives the worker dying, and the
approval can only come from the process that happens to be attached.

`COORDINATION-PLANE.md` §5 specifies `talk ask --wait`. Without a durable hold
that is a blocked process, not a suspended attempt.

### 9.2 What it does

- A durable `held` state on the work item, with the reason, the question, who
  may answer, and a resume token.
- A hold **suspends the attempt rather than heartbeating through it**: the
  claim is recorded as held, the lease is not renewed indefinitely, and the
  item is not eligible for another worker while held.
- A hold survives worker death and is resumable from a different process,
  which is the whole point — the answer arrives from a phone, not from the
  terminal that asked.
- A configured maximum hold duration, after which the item returns to the
  queue with the question recorded rather than lost.

### 9.3 What it must not do

- **No model may interpret the human's answer into a routing decision.**
  CrewAI's `@human_feedback` routes to different branches based on an LLM
  reading the feedback; under `AGENTS.md` that is a gate decided by a model and
  it is rejected. An answer is structured data or it is a message; it is never
  a prompt that decides what the approval meant.
- **No text is injected into a live PTY.** `COORDINATION-PLANE.md` §5.1 already
  rules on this and the reason is exact: the process may be at a shell, and an
  answer becomes a command. Delivery is through the structured protocol or at
  an explicit safe checkpoint.
- **A hold must not weaken any gate.** Being held is not approval, and a hold
  that times out returns the item to blocked, never to ready.
- It must not become the coordination plane. This stage delivers the item-level
  hold only; the message ledger, rooms and oversight actor remain proposed and
  unimplemented.

### 9.4 Acceptance

- An item held for longer than a lease, whose worker is then killed, is still
  held — not re-claimed, not failed, not silently resumed.
- The hold is answerable from a second process with no attachment to the
  original session.
- A held item is visible through the API with its question and its age, and
  #103's case — a silent-but-active session — is distinguishable from a hang.
- Hold expiry returns the item to a blocked state with the question preserved.
- Evidence artefact: `evidence/YYYY-MM-DD-stage-j-durable-hold.md`.

## 10. Stage M — telemetry export (parallel, off the critical path)

*Prior art: all five systems export OpenTelemetry; this repository exports
none. Partially addresses #128.*

### 10.1 What it does

An **adapter** that projects the existing event stream to OTLP spans following
the OpenTelemetry GenAI semantic conventions. Lazily loaded, opt-in, in
`adapters/`, entirely consistent with the generic-core rule.

It also gives session-mode agents somewhere to self-report, which closes part
of #128's hole without waiting for a full telemetry rebuild.

### 10.2 What it must not do

- **Export only. The event store stays the single source of truth.** A span is
  a projection, never written back, never consulted to answer a question the
  events could answer. `AGENTS.md`'s rule about projections applies unchanged.
- **No core module imports an OTel package.** If the exporter is absent, the
  harness runs identically.
- **It must not claim to close #128.** Session-mode implementer traffic still
  bypasses `ModelClient`. Exporting what we have does not create what we do not
  have, and the remaining hole is named in the report.

### 10.3 Acceptance

- Spans are produced for model calls, gates and item lifecycle, with a run and
  item identity that joins to the event rows.
- With the exporter absent or failing, every test still passes and no work
  stops — observation never stops work, per the existing audit rule.
- The report states exactly what fraction of model traffic is represented, and
  names session-mode as excluded.
- Evidence artefact: `evidence/YYYY-MM-DD-stage-m-telemetry-export.md`.

## 11. New open decisions

These are raised by the new stages and are **not** settled here. Per the
standing rule, do not guess at one to unblock yourself — say it is blocked.
They follow D1–D10's numbering.

| Decision | Question | Blocks |
|---|---|---|
| **D11** | Does a resumed attempt consume a new attempt, or continue the existing one? | Stage H. It decides whether `max_attempts` bounds crashes or bounds genuine failures, and whether cost is attributed to one attempt or several. |
| **D12** | Does a human hold suspend the lease or release the claim? | Stage J. Suspending keeps the worktree and context; releasing frees the worker. They cannot both be true. |

### 11.1 Rulings, 2026-08-04

Recorded here because this document is not append-only and these were answered
by decision, not by argument. Each stage's evidence report restates the ruling
it was built under.

**D11 — resolved: a resumed attempt continues the existing one.** A crash is
not a failure of the work, so `max_attempts` bounds genuine failures, which is
what it reads like it means, and the whole cost of one item's attempt stays
attributed to one attempt. **The consequence is named rather than hidden:** an
item that crashes in a loop is then bounded by Stage L's budgets and by nothing
else. Stage L stops being a nice-to-have and becomes the thing that closes this
hole; until it lands, a crash loop is bounded only by the operator noticing.

**D12 — resolved: a hold suspends the lease and keeps the claim.** The worktree
and the context survive, so answering resumes where the item stopped — the
reasoning `work.py` already gives about pause semantics, that stopping mid-item
destroys context and leaves a half-finished worktree, applies unchanged to a
hold. The item is not eligible for another worker while held. **The cost is
named:** a worker slot is tied up for the whole hold, so the configured maximum
hold duration in §9.2 is not optional decoration — it is what stops one
unanswered question from consuming a worker indefinitely.

**Stage D — go-ahead given, 2026-08-04.** The deferral in
`FIT-FOR-PURPOSE-STATUS.md` §3 is lifted. Specification unchanged: §8 of the
original proposal, acceptance §8.4.

D13 and D14 are not asked as questions because this document already answers
them and the answers are the safe ones: telemetry is export-only (§10.2), and
budget ceilings default to unlimited so an existing database upgrades with no
behaviour change (§8.2). They are recorded as open only so that they are not
decided by accident later.
| **D13** | Is telemetry export-only, or may an exporter ever be the sole record of an event? | Stage M. The safe answer is export-only; asking it explicitly stops it being decided by accident. |
| **D14** | Which budget ceilings, if any, are enforced by default on a new project? | Stage L. Unlimited is safe on upgrade and unsafe on a 7-day unattended run. |

**D8 becomes load-bearing for Stage K** and is still open. If Stage K's typed
check outcome starts to require a registration mechanism for third-party
gates, it has reached D8 and must stop.

**D9 is still blocked on #84 / T43.** Nothing in this proposal changes that,
and no stage here may hold the review prompt as a variable.

## 12. Non-goals, extended

The original §12 non-goals stand unchanged. This review adds five, each
because a reviewed system does the opposite:

- **No workflow DSL.** Conductor's JSON DSL and LangGraph's graph API are the
  right answer for general orchestration and the wrong answer here. Stage H
  records positions in a fixed pipeline; it does not let anyone define one.
- **No adoption of Conductor or agentspan as a dependency.** agentspan's
  server-side durability is conceptually close and its operational footprint —
  a Spring server, a Go CLI, four SDKs — would swallow this project.
- **No deterministic-replay requirement.** LangGraph achieves replay by
  constraining nodes to be deterministic. A model authoring a diff is not, and
  designing for replay would either be a lie or a constraint on the one thing
  the harness exists to run.
- **No model-interpreted human approval.** CrewAI routes on an LLM's reading of
  human feedback. Under `AGENTS.md` that is a gate a model decides.
- **No bundled GUI, dev studio or hosted control plane.** Mastra Studio,
  CrewAI's control plane and LangGraph Platform are all the same move. The
  existing rule already forbids it; reviewing them confirms it rather than
  challenges it.

## 13. Deliberately not staged

Two things came out of the review that are worth doing and are **not**
fitness-for-purpose work. They are recorded here so they are not lost and not
smuggled into a stage.

**Extracting the rate-limit taxonomy.** `providers.py` plus the retry policy in
`model_client.py` is the one thing in this repository that none of the five
reviewed systems has — they all delegate 429 handling to a provider SDK that
will happily retry a spent weekly cap. It is publishable on its own. It is also
not a fitness-for-purpose stage and must not be allowed to delay one.

**Documenting the SQLite ceiling honestly.** LangGraph Platform separates
stateless API servers from queue workers with PostgreSQL as the truth;
Conductor offers five persistence backends. This repository is one SQLite file,
single-writer, one host. That is the right choice today and the wrong thing to
discover under load. The correct action is a paragraph in `DEPLOYMENT.md`
stating the ceiling — not building for a scale nobody has asked for.

## 14. Risk register for this proposal

| # | Risk | Mitigation |
|---|---|---|
| R1 | The four-branch merge produces a tree that passes tests while silently reversing a stage's intent | Conflicts resolved against evidence reports, not diffs; every divergence recorded in the Stage F report |
| R2 | Stage H grows into a workflow engine | Fixed stage list, no registration, no DSL; §7.3 is the test |
| R3 | Stage K reaches D8 and someone guesses | Named explicitly in §6.3 and §11; the correct output is "blocked" |
| R4 | New stages start before F lands and rot the same way the last four did | F is a hard prerequisite for K, H, L, J; only M and D may proceed alongside |
| R5 | Stage 8 stays blocked forever and the programme never reports a real result | Stage 8 is explicitly *not* a prerequisite; the honest position is that deterministic success is proven and live success is not |
| R6 | Timing measurements are taken on `/tmp` again | `TMPDIR` on a fast volume is a precondition in every stage's commands |

## 15. How success is reported

Unchanged: §11 of the original proposal governs. Every stage publishes the
commit and configuration under test, the commands run, observed result versus
criterion, costs and unmeasured costs separately, failures and known
limitations, and an explicit continue/stop decision.

Every evidence report carries a blind-spots section. A report without one is
wrong.

Evidence reports are append-only. This document is not — it is a working
proposal and may be corrected when reality disagrees with it.

**"No failures observed" is not equivalent to "the requirement was exercised".**
