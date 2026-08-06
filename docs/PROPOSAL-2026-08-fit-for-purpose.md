# Proposal — making agent-harness fit for purpose

> # Superseded — 2026-08-06
>
> The proposal the fit-for-purpose programme ran against. The programme finished; its report is in `docs/evidence/2026-08-04-programme-report.md`.
>
> **Current documentation:**
> [`docs/DESIGN.md`](DESIGN.md) — how the harness works and why.
> [`docs/STATUS.md`](STATUS.md) — where it stands and what is left to do.
>
> Kept for the reasoning and the evidence, which are not reproduced elsewhere.
> **Do not follow its plan, its phase order, or its statements of current
> state** — all three are out of date. Where this document and the code
> disagree, the code is right.

**Status:** proposal, not accepted. Revised 2026-08-04 after the first
sustained attempt to run a real workload through the harness.

## Executive decision

The diagnosis is credible: the first real workload exposed failures at the
seams between plan ingestion, queue admission, execution, repository context,
and external reconciliation that the component suite did not exercise.

The implementation plan is not ready to approve as one large build. It mixes
evidence, bug fixes, architecture decisions, adoption semantics, and product
packaging. Some proposed changes also conflict with contracts that already
exist in this repository:

- `Provider` currently classifies failures; the CLI transport is separately
  hard-wired to an OpenAI-compatible request shape.
- The coordination-plane design already specifies a typed dependency graph.
- Session execution checkpoints before review; the direct API executor does
  not yet have the same checkpoint boundary.
- Adoption requires repository and external-state inspection that
  `inception.py` does not perform.

This revision turns the work into bounded stages with explicit evidence gates.
Approval should be given to stages 0–3 first. Stages 4–7 remain conditional on
their preceding reports and on the open decisions listed in §9.

The outcome we are trying to earn is modest and testable: a stranger can
adopt or start a project, see what the harness knows and does not know, run a
small backlog through deterministic gates, and obtain an honest record of what
was delivered. “A real backlog produces merged work” is a later validation,
not a substitute for proving the preceding mechanics.

## 1. Evidence before design

The repository currently has 640 collected tests across 36 test files. They
cover useful components, but they do not establish a single observable
`plan → queue → executor → outcome` contract. The existing executor tests use
real temporary git repositories, but the fixtures are intentionally small and
do not represent a large, multi-directory workload.

The live-run claims in the earlier version of this proposal — defect count,
percentage attributed to patch application, NGMS issue counts, and lines of
unaccounted work — are valuable evidence but are not reproducible from this
repository alone. They must be preserved as an evidence package rather than
treated as universal baselines.

### 1.1 Evidence package

Before a behaviour change is declared complete, create a versioned report with:

- the run identifier, commit, configuration, executor mode, project and
  provider route;
- raw event/log locations and a checksum or immutable reference for each;
- the defect inventory, with one reproduction or source reference per defect;
- the observed measurements and their denominator;
- a distinction between repository-verifiable facts, live observations and
  hypotheses;
- the exact command used to collect each number;
- known blind spots, especially session-mode implementer traffic that does not
  pass through `ModelClient` (issue #128).

The report is append-only. A later run adds a new measurement; it does not
rewrite the earlier one. A claim without an artifact or reproducible command
is a lead, not an acceptance result.

### 1.2 Binding invariants

All stages retain the repository’s standing rules:

- the core stays generic; tool- or vendor-specific formats are adapters or
  configured plugins, loaded lazily;
- no rewrite of the worker or its gates;
- cost caps are never retried;
- retry and parking state is local to the worker/endpoint/role and never a
  fleet-wide pause;
- work that passes cheap gates is durable before an expensive gate runs;
- events are append-only and projections never become a second source of
  truth;
- ambiguous state is reported or sent to a human, never silently guessed;
- an unmet exit criterion is reported as unmet.

The proposal changes neither the deployment decision D7 nor the gate-plugin
decision D8 by implication. It does not answer D9 by assertion.

## 2. Stage A — a deterministic end-to-end safety slice

This is the first implementation stage because every later change is unsafe if
the suite can remain green while a complete item cannot finish.

### 2.1 Fixture repository

Add one generated, deterministic fixture repository used only by tests. It
must be small enough for CI and realistic enough to expose seams:

- several directories and a package/crate boundary;
- existing source to modify;
- a file to create;
- a file to delete or rename;
- a header/docstring at the top of a file, making wrong placement observable;
- build and test commands that fail when the change is wrong;
- enough irrelevant files to exercise context selection without committing
  thousands of generated files.

The fixture is a test asset, not a new workload-specific assumption in core
code.

### 2.2 Mock provider and transport

Provide an injectable, local, deterministic transport for the API executor.
It may be an in-process callable or a local HTTP test server; it must not call
the network in CI. It must script both successful responses and failures seen
in the evidence package:

- valid and malformed model responses;
- over-counted and truncated hunks;
- existing-file zero-context hunks;
- file creation, deletion and rename;
- prose instead of a patch;
- fallback success and all-routes-unavailable;
- burst limit, short-window cap, terminal cap, refusal, 5xx and timeout;
- slow but healthy responses.

This mock is a transport fixture, not a vendor implementation. It should
exercise the existing classifier contract without smuggling a particular
vendor’s fields into the core.

### 2.3 Observable scenarios

Each scenario starts with a plan or queue input and asserts on the queue, git
tree, external side effects where enabled, and event stream. Tests must not
reach into private attributes to establish success.

Required scenarios:

1. A happy API-executor path reaches checks, review, commit and outcome.
2. A dependent item is not claimed before its required dependency is
   satisfied.
3. A dependency correction while work is in flight is observed at the next
   durable boundary; the live agent is not killed implicitly.
4. Re-ingesting identical work does not duplicate or reset progress.
5. A fallback route answers without an unnecessary backoff, and the fallback
   is recorded.
6. All routes unavailable produces an explicit, cheap refusal.
7. A spend cap returns the item without consuming an item attempt; a refusal
   does not park a healthy endpoint.
8. A worker death releases only that worker’s claim and does not stop a
   sibling project.
9. A heartbeat keeps a slow, healthy item claimed beyond the nominal lease.
10. Concurrent workers do not claim one item twice; resizing does not kill
    an item in flight.
11. Failed checks prevent reviewer spend.
12. Every malformed patch in the fixture is repaired only when derivable,
    refused when ambiguous, and never applied at the wrong location.
13. Both API and session executors prove the checkpoint-before-review rule.
    A reported parity gap is an honest finding, but Stage A remains incomplete
    until the gap is closed and the scenario passes.

### 2.4 Stage A acceptance

Stage A is complete only when the scenarios run deterministically in CI, the
event assertions include the relevant route and gate outcomes, and the report
names any untested executor or telemetry path. A green unit suite without this
observable slice is not acceptance.

## 3. Stage E2 — make context selection evidence-driven

Issue [#146](https://github.com/TheDancingDeveloper-org/agent-harness/issues/146)
is a live blocker for the direct API executor. The current smallest-file-first
heuristic can spend the entire context budget on empty stubs and build
artefacts while omitting the file named by the task.

### 3.1 Planner contract

Extend the planner response to a validated structured result containing:

- a short implementation plan;
- an ordered list of proposed target paths, each with a reason;
- an explicit “cannot identify a target” result when the task is ambiguous.

The planner is not trusted to read arbitrary files. Paths are normalised,
confined to the repository, and checked for existence/type before they are
used. Invalid or missing targets are reported as planner uncertainty, not
silently treated as authoritative.

### 3.2 Context policy

The implementer receives named target files first, then relevant surrounding
context, then a bounded repository listing. Empty files and generated
artefacts are not allowed to consume the content budget unless explicitly
named. If the target files cannot fit, the executor must say which files were
omitted and why.

Record in the event stream:

- the planner’s target list;
- the final files supplied to the implementer;
- character/token budget and truncation;
- whether a fallback relevance heuristic was used.

The planner’s list is guidance, not permission to change unrelated files. The
reviewer still judges the resulting tree and diff.

### 3.3 Stage E2 acceptance

The NGMS-shaped fixture must reproduce the failure under the old heuristic and
show that the target file is supplied under the new one. The test must detect
both omission and wrong-location application. A report must include context
selection events, not just an improved apply rate.

## 4. Stage E1 — measure the change protocol before choosing one

The proposal must not decide by taste whether models should author unified
diffs. Measure at least these protocols against the Stage A fixture and mock
transport:

1. model-authored unified diff with the existing tolerance ladder;
2. whole-file replacement for named files;
3. search/replace blocks with unique-match validation.

For each protocol, measure separately:

- clean application rate;
- wrong-location rate (must be zero for an acceptable protocol);
- malformed-response/refusal rate;
- repair rate and repair cost;
- input/output tokens;
- time to cheap-gate completion;
- reviewer rejection rate;
- file creation/deletion/rename correctness.

The test data must include adversarial repeated text and headers so “it
applied” cannot be mistaken for “it applied where intended”.

### 4.1 Decision rule

The output of Stage E1 is a dated experiment report and a decision record. It
may recommend retaining diffs, adopting whole files, adopting search/replace,
or keeping more than one protocol behind a configured policy. No protocol is
removed before the report exists, and no tolerance rung may guess at a patch’s
location.

The API executor remains supported. Session mode remains supported, with its
model-traffic visibility limitation recorded rather than hidden.

## 5. Stage C — adoption as reconciliation, not inception by another name

Adoption is the central product capability, but it is not a wholesale reuse of
`inception.py`. Reuse its human approval, question severity, revision history,
and plan-rendering tail. Add an adoption inspection/reconciliation layer for
repository and external state.

### 5.1 Lifecycle

`agent-harness adopt` has these states:

```
draft → inspecting → proposed → approved → reconciled → stopped
                              ↘ rejected/revise
```

Inspection and proposal are read-only. Approval is required before queue rows,
issue markers, branches, or other external mutations are created. The command
must support dry-run and produce a report that can be reviewed or stored.

### 5.2 Evidence for “already done”

Use evidence in this order:

1. **Explicit:** checked plan item, closed issue with a high-confidence item
   reference, merged PR with a high-confidence reference.
2. **Runnable:** an item-declared verification command, executed with the
   same shell-free and timeout rules as project checks.
3. **Judged:** an assessor role that returns `done`, `partial`, or `not_started`
   with cited paths, symbols, tests or commits.

The assessor output is structured and retained as evidence. It cannot directly
drop work. A proposed drop requires human approval; uncertainty biases toward
`not_started`.

Plan syntax for item verification must be specified before implementation. It
must not be silently interpreted as a project-level check or as arbitrary shell
code.

### 5.3 Existing issues, branches and PRs

Matching unmarked external work is a reconciliation proposal, not an automatic
fact. The report must show:

- candidate matches and confidence;
- competing candidates;
- whether the issue is open or closed;
- the exact external mutation proposed.

Backfilling an id marker must preserve human-authored issue content. Existing
PR adoption must use explicit evidence (ID, branch, title/body references and
repository ownership) and refuse ambiguous matches. The queue must never claim
that a pre-existing branch or PR was harness-created merely because its name
looks similar.

Prior harness attempts are evidence, not a new source of truth: adoption may
avoid repeating a known failure, but it must retain the original event history.

### 5.4 Stage C acceptance

On a fixture repository with existing work, repeated adoption is idempotent and
produces the same report. It does not duplicate issues, reset queue progress,
or drop completed work. Ambiguous matches and proposed drops are surfaced for
human approval. A dry run performs no external mutation.

## 6. Stage G — one graph contract

Do not introduce the thin C5 graph design alongside
[`docs/COORDINATION-PLANE.md`](COORDINATION-PLANE.md). Use that document as the
semantic contract and choose SQLite tables as its implementation only if the
following fields and behaviours are retained.

Each dependency edge needs:

- source item;
- target kind and identity;
- required versus advisory relationship;
- resolver/adapter for external targets;
- resolution state (`unresolved`, `blocked`, `satisfied`);
- provenance/evidence;
- graph revision.

Required unresolved or missing targets are blockers. They are never equivalent
to satisfied merely because the target may exist elsewhere. Legitimate external
references need an explicit target kind and resolver outcome.

The graph implementation must provide referential validation, cycle detection,
revisioned/idempotent updates, and an explanation of why an item is not ready.
Admission and the pre-expensive-gate check must use the same authoritative
graph revision.

If a graph update invalidates a live claim, follow the existing coordination
contract: do not kill the agent; mark the attempt invalidated, notify/record it,
and prevent the next durable or external gate until the dependency is resolved
or an explicit operator override is recorded.

### 6.1 Stage G acceptance

A plan containing metadata dependencies, arrow notation, an external target,
a missing target and a cycle produces an explicit report for each case. The
ready set is correct after re-ingestion, and a mid-flight graph correction is
observable without silently committing ineligible work.

The storage migration plan must be written before changing the queue schema.
“The queue is disposable” does not mean an in-place upgrade can be assumed
safe; existing users need a rebuild/export procedure and a tested backup story.

## 7. Stage B — separate provider protocol from failure classification

The current `Provider` protocol is a failure classifier. The CLI’s transport is
an OpenAI-compatible client. These are different abstractions and must remain
different.

### 7.1 Proposed route shape

A route should identify, explicitly or through a documented preset:

- wire protocol/request adapter;
- authentication strategy;
- response and usage reader;
- failure classifier;
- model and endpoint;
- optional pricing reference.

The default core route is generic and makes no vendor-specific claims. Vendor
envelopes and protocol presets belong in lazily loaded adapters or plugins,
consistent with the generic-core rule. A vendor must be addable without editing
`model_client.py`; the registry/configuration mechanism is part of this stage.

“Provider detection from endpoint host” may be offered only as an explicitly
reported suggestion. It must never silently choose a protocol or classifier.

### 7.2 Conformance fixture

The Stage A mock transport becomes a classifier conformance fixture. For each
documented failure shape it asserts the classification and reaction:

- burst limits retry with injected sleep and jitter;
- spend caps are not retried;
- refusals keep a healthy endpoint available;
- transient upstream failures are retried within policy;
- credential failures are terminal;
- all events identify route, protocol/classifier, role and outcome.

The conformance test must also prove that unknown prices remain unknown, not
zero, and that usage extraction is conservative.

### 7.3 Stage B acceptance

Core tests pass with only the generic route. At least two protocol/configuration
implementations run through the same conformance suite. Adding a new configured
classifier or protocol does not require a `model_client.py` code change. No
vendor-specific import is added to core modules.

This stage does not claim that session-agent traffic is visible. Resolving
issue #128 is a separate telemetry work item and must be measured separately.

## 8. Stage D — make the first run honest and reproducible

“Out of the box” has two meanings and they must not be conflated.

### 8.1 Deterministic no-network demo

`agent-harness init --demo` creates a temporary fixture repository, sample plan,
queue and deterministic mock route. It performs no GitHub or provider network
operation and leaves the project stopped. One documented command then runs the
API executor against the mock and reports the tree, outcome and event file.

This is the CI and stranger-on-a-laptop path. It proves wiring, not model
quality.

### 8.2 Local-provider path

Document a separate path for Ollama or another OpenAI-compatible local server.
It must state who supplies/downloads the model, what endpoint shape is required,
how authentication is disabled or configured, and what “offline” means. A
local model is optional; the deterministic demo and required CI cannot depend
on it.

### 8.3 Doctor and external safety

`agent-harness doctor` reports, without spending work:

- configuration and route completeness;
- protocol/classifier selection;
- provider reachability;
- git/worktree availability;
- check-command validity;
- reviewer independence where applicable;
- whether session-mode implementation traffic is observable;
- whether GitHub mutations are enabled.

The first-run path defaults to no push and no GitHub mutation. Any command that
creates issues, branches, PRs or repositories remains dry-run by default and
echoes the external changes before approval.

### 8.4 Stage D acceptance

A clean checkout can run the deterministic demo from documented commands without
credentials or network. A documented opt-in smoke test covers a user-supplied
local provider; it is not part of required no-network CI. README claims are
updated to distinguish tested, observed, and proven behaviour.

## 9. Decisions and dependencies

The proposal does not silently settle open decisions:

| Decision | Effect on this proposal |
|---|---|
| **D7 — service deployment** | Blocks deployment work only. Local code and CI stages may proceed. |
| **D8 — gate plugin interface** | Blocks generalising domain-specific gates to arbitrary repositories. The fixture uses declared generic checks and does not settle D8. |
| **D9 — reviewer sees plan/rationale** | Blocks any claim about that prompt variant. Stage E1 must hold the review prompt constant or record the variable explicitly. |

The NGMS decisions are separate human decisions. They are not harness acceptance
criteria and must not be answered merely to make a queue look productive.

### 9.1 Repository boundaries

- Stage A, E2, E1, adoption, graph, provider interfaces, docs and deterministic
  demos are changes in this repository unless a later relocation is approved.
- P1 retry changes that belong to the existing worker land in
  `swack-tools/oxidex`, as required by `AGENTS.md`.
- Domain-specific gates remain in adapters or the future plugin boundary; no
  NGMS or oxidex format enters core modules.

Every implementation issue must name its repository, owner, prerequisite stage,
rollback/rebuild procedure, and acceptance artifact.

## 10. Sequence and gates

| Order | Stage | Required output | Gate to continue |
|---:|---|---|---|
| 0 | Evidence package | Reproducible run/defect report | Claims are attributable and blind spots named |
| 1 | A — deterministic e2e | Mock project, transport and observable scenarios | End-to-end API path and executor parity are measured |
| 2 | E2 — context | Structured planner targets, relevance policy, context events | #146 fixture passes with zero wrong-location applications |
| 3 | E1 — protocol experiment | Comparative report and decision record | Protocol choice is evidence-backed; no unsafe removal |
| 4 | C — adoption | Read-only inspection, evidence assessment, human-approved reconcile | Idempotent adoption with no silent drops or mutations |
| 5 | G — graph | One typed, revisioned graph contract and migration | Missing/cyclic/mid-flight cases are explicit |
| 6 | B — providers | Protocol/classifier/plugin separation and conformance | Generic core works; presets are adapters/configuration |
| 7 | D — first run | Deterministic demo, local-provider docs, doctor | Clean checkout runs with no credentials/network |
| 8 | Validation | A smaller real repository, then NGMS when decisions allow | Merged work and measurements are reported honestly |

Stages may overlap only where their acceptance artifacts do not depend on an
unsettled decision. A stage cannot be marked complete because its code exists;
its report and gate must pass.

## 11. How success will be reported

For each stage publish:

- the commit and configuration under test;
- tests and commands run;
- observed result versus the criterion;
- costs and unmeasured costs separately;
- failures, regressions and known limitations;
- an explicit continue/stop decision.

The final validation report must distinguish:

- deterministic fixture success;
- local-provider success;
- a second-repository result;
- NGMS result, including human-decision blockers;
- session-mode telemetry coverage versus direct API coverage.

“No failures observed” is not equivalent to “the requirement was exercised”.

## 12. Non-goals

- no rewrite of the worker or its gates;
- no GUI in this repository;
- no general workflow engine;
- no second gateway service;
- no automatic deletion or closure of external work;
- no automatic classification of ambiguous adoption evidence as done;
- no vendor-specific knowledge in generic core modules;
- no claim that a green component suite proves a real fleet;
- no claim that the harness has solved NGMS while its human decisions remain
  unresolved.

This proposal is fit for implementation only as the staged, evidence-gated
program above. Approval of the diagnosis is not approval to skip a gate.
