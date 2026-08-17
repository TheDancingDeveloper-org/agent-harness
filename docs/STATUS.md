# Product status against the minimal local contract

**Status date:** 2026-08-09

**Target authority:** [`minimal.md`](../minimal.md)

**Exploration-complete implementation backlog:** [`BACKLOG.md`](../BACKLOG.md)

**Implementation design:** [`DESIGN.md`](DESIGN.md)

**Historical evidence:** [`evidence/`](evidence/)

This document is the current, mutable status report. It records what the
repository can demonstrate today and the work needed to satisfy the target. It
does not turn a fixture, a partial run, or an older remote-delivery proposal into
a completed product capability.

## Headline

The repository is a substantial pre-alpha harness, but it does **not yet meet
the minimal local product contract**.

It has a durable queue and event model, checks and review gates, execution
backends, a same-origin API/GUI, local worktree isolation, and a tested local
plan-integration mechanism. A live daemon has also processed real attempts.

It does not yet have the generic minimum plan schema, one-pass plan admission,
per-project pinned execution profile, or final integrated
build/deploy/readiness/acceptance/teardown lifecycle. No real project has
completed that entire path. Those are product gaps, not Rainmon prerequisites.

## Target boundary now in force

The minimum supported delivery path is local:

- input is a user-authored, deterministically valid plan and a local Git repo;
- agent work, checks, review, commits, and integration occur locally;
- the integrated product is built, deployed, accepted, and torn down locally;
- the output is an accepted local integration branch plus durable evidence;
- remote Git mutation, hosted CI/CD, remote issues/reviews, and non-local
  deployment are not prerequisites and are outside minimum completion.

Existing remote workflow code may remain as an optional extension. It must not
leak credentials, vendor concepts, or required steps into the local path.

## Capability comparison

| Target capability | Evidence in the repository | Status | Required work |
|---|---|---:|---|
| Generic minimum plan template | `examples/PLAN.md` contains the required prose and exactly one v1 fenced-TOML manifest; `plan_contract.py` parses typed immutable values and preserves argv arrays. | **Partial** | Add a placeholder-bearing authoring template and complete validator/admission integration. |
| One-pass deterministic rejection | `plan_validation.py` returns stable `PLAN-*` findings for document, manifest, work-item, policy, and graph problems; CLI and `POST /api/plans/validate` expose the report without writes. | **Partial** | Expand adapter-specific and target-mode checks, then prove the full deliberately broken fixture and no-state-mutation contract. |
| No interview required for admission | `inception` and survey code can produce a plan through questions. | **Not aligned** | Make template validation the normal admission path. Keep interactive inception only as an optional authoring aid. |
| Atomic plan admission | `admission_service.apply` now persists the stopped project, immutable revision/items, work projection, and typed graph in one transaction with rollback injection, stale checks, and exact replay idempotency. Revision classification, explicit removals/reopens, reviewed CLI admission, typed API apply, and revision list/detail reads are implemented. | **Partial** | Complete GUI confirmation and live restart/replay evidence. |
| Dependency-aware queue | SQLite work items, typed dependencies, readiness, leases, attempts, holds, and dependent-item waiting exist and are tested. | **Implemented, not fully live-proven** | Exercise the path as part of a complete real-project acceptance run. |
| Isolated local agent work | Local Git worktrees, exact base commits, role runners, model/session executors, budgets, command screening, and checks exist. | **Partial** | Drive these from the admitted per-project profile and prove unsupported requirements fail before claim. |
| Per-project execution profile | Host/Docker backends and image/mount service configuration exist. | **Missing at the product boundary** | Persist toolchains, packages, services, mounts, network policy, named secrets, and immutable image identity per project/plan revision. |
| Reviewable runner image generation | Docker execution can consume an image selected by deployment configuration. | **Missing** | Generate or accept a recipe, show it for review, build/test locally, and pin its digest without silently executing installation prose. |
| Checks and reviewer gates | Declared project checks, structured outcomes, reviewer roles, audit events, and policy refusal exist. | **Implemented in components** | Preserve these gates while wiring the local product lifecycle; do not turn lifecycle commands into a way to bypass them. |
| Checks use the admitted runner profile | The Docker role runner edits inside its container, but `CheckRunner` currently executes authoritative item and promotion checks on the controller host. | **Missing at the boundary** | Add a no-shell argv operation to execution backends and run both item and integration checks in the pinned profile, with no host fallback. |
| Local item integration | Per-plan branches, serialized promotion, replay on moved tips, promotion-time re-gating, conflict handling, and dependent waiting are fixture-tested. | **Implemented, fixture-proven** | Complete live end-to-end acceptance and expose safe reviewed local finalisation semantics. |
| Integrated build | Promotion re-runs project checks. | **Partial** | Add a distinct plan-declared integrated build stage and durable outcome. |
| Local product deployment | The harness itself can be served locally in monitoring, session-host, or direct local execution configurations. | **Missing for the product under development** | Implement an adapter/config-driven local deploy action for the integrated commit. |
| Readiness, acceptance, teardown, recovery | General checks and attempt outcomes exist. | **Missing as a final lifecycle** | Implement ordered, durable lifecycle stages with guaranteed teardown/recovery attempts and honest partial-failure reporting. |
| Durable evidence | Append-only events, audit, attempts, outcomes, redaction, API projections, and evidence reports exist. | **Strong foundation** | Correlate admission, image/profile, exact commits, final lifecycle commands, outcomes, and human decisions into one delivery record. |
| Generic extension mechanism | Route presets and dependency resolvers use installed metadata; genericity tests police the execution path. | **Implemented foundation** | Add local topology support through the same generic principle; do not import or name project/vendor adapters from core. |
| Complete real-project proof | Stage 2 reached a real daemon and processed real attempts; Stage 4 mechanics have fixture evidence. The planned ten-step deterministic local E2E deliverable is documented in [`docs/E2E-ACCEPTANCE-PLAN.md`](E2E-ACCEPTANCE-PLAN.md), but not yet implemented. | **Not achieved** | Deliver the fixture after the GUI admission workflow and lifecycle prerequisites are complete; then complete one representative project and a materially different second project through the minimal contract. |

## What is already worth keeping

The target is a scope correction, not a rewrite. The following are reusable
product foundations:

- append-only redacted event and audit stores;
- SQLite queue, leases, attempts, holds, budgets, and typed dependency graph;
- provider/model routing and terminal cost-cap classification;
- deterministic command guard and worktree boundary checks;
- project checks, role-specific runners, structured gate outcomes, and reviewer
  separation;
- local worktree preparation, item commits, plan branches, serialized promotion,
  replay, re-gating, and dependency waiting;
- direct model and session-host execution backends;
- typed JSON API, OpenAPI document, same-origin browser GUI, and authenticated
  operator actions;
- installed-metadata adapter discovery and the genericity enforcement test.

No phase should rewrite these components merely to match new terminology. New
work should connect them through the admitted local contract.

## Misalignments that must not become requirements

### Interactive plan manufacture

The current inception/survey path asks questions and can inject defaults. That
may remain a convenience for someone who wants help drafting a plan, but it is
not plan admission. Missing minimum content must produce one rejection report.
Defaults that select a repository host, hosted runner, publication model,
deployment topology, or third-party gate policy are not generic facts and must
not be silently promoted into the admitted manifest.

### Deployment-wide runner assumptions

Current serve/worker configuration can choose a Docker image and mounts for the
service. The target needs these captured and pinned per project or plan revision.
A global operator default may seed a proposal, but the admitted plan must make
the effective environment visible.

### Remote publication as completion

Older plans describe GitHub issues, remote branches, pull requests, review
providers, hosted checks, and publication as the delivery path. Those features
are not evidence that the minimal local lifecycle is complete. They are optional
extensions and must remain dormant when the local path is selected.

### Workload-specific acceptance

The previous `nextsteps.md` was a Rainmon/Node-B runbook. It mixed harness
acceptance with one consumer's repository, credentials, topology, and CI policy.
It is superseded by the generic sequence below. A Rainmon plan may declare those
facts, but core and its minimum release criteria may not assume them.

## Delivery sequence

This sequence replaces the phase order in historical plans. Each milestone must
leave evidence and preserve the existing gates.

### M0 — Contract and documentation alignment

**State:** complete in the current working tree; implementation remains M1+.

- establish `minimal.md` as target authority;
- make this file the comparison against that target;
- mark older plan/status documents historical or superseded;
- publish the selected generic plan template shape and remove workload-specific
  next steps from the current path;
- publish `BACKLOG.md` with explored decisions, explicit dependencies, tests,
  evidence, and non-goals for every implementation item.

**Exit:** current documentation distinguishes target, implementation, evidence,
and history without claiming the target is implemented.

### M1 — Versioned minimum plan and validator

**State:** implementation in progress; P1/P2 core and initial CLI/API surfaces are built, but M1 exit evidence is not met.

- implement the selected fenced-TOML v1 manifest schema, canonical form, and
  unknown-version migration policy;
- validate required prose sections and explicit applicability statements;
- validate execution profile, safe argument-array commands, local topology,
  work items, acceptance, and dependency graph;
- emit all stable-code findings with source locations and remediation in one run;
- provide machine-readable and human-readable output;
- keep validation deterministic and model-free.

**Exit:** malformed or incomplete plans fail without creating project or queue
state, and a valid example passes with no interactive questions.

The selected schema, exact adapter contracts, admission transaction, execution
boundary, lifecycle journal, and evidence work are decomposed item-by-item in
`BACKLOG.md`; milestone bullets here remain status summaries rather than a
second backlog.

### M2 — Reviewed, atomic admission

**State:** A1-A3 foundations implemented; A4-A5 and exit evidence remain outstanding.

- bind the plan to the local repository identity and exact base commit;
- show the effective execution profile and local lifecycle before acceptance;
- optionally run post-minimum semantic review and batch material questions;
- persist project, plan revision, dependency graph, and queue atomically;
- prove retrying admission is idempotent.

**Exit:** a valid plan becomes one auditable queue exactly once; rejection or a
blocking question leaves no executable work.

### M3 — Per-project execution profile

**State:** not started; existing host/Docker backends are inputs.

- persist toolchains, packages, services, mounts, dependency provisioning,
  network policy, named secrets, and backend per plan revision;
- support an existing immutable image and a reviewable generated-recipe path;
- build and test generated images locally and pin digests;
- extend preflight to prove commands and declared services are available;
- run authoritative item and promotion checks inside that exact profile rather
  than on an undeclared controller toolchain;
- reject undeclared or unsupported requirements before a claim.

**Exit:** two different project profiles can coexist in one harness deployment
without changing global worker flags or core modules.

### M4 — Local fleet completion

**State:** component-rich but end-to-end incomplete.

- connect admitted profiles to role runners and item workspaces;
- prove claim, attempt, hold, resume, budget, review, check, and commit behaviour
  against a real daemon;
- retain terminal cost-cap and per-worker retry invariants;
- close the gap between fixture Stage 4 integration and real execution.

**Exit:** a real multi-item plan reaches an accepted local integration commit or
an honest terminal outcome for every item, with no remote credentials.

### M5 — Final local product lifecycle

**State:** missing.

- run the declared integrated build from the exact plan-branch commit;
- deploy through the selected local topology;
- evaluate readiness and acceptance separately;
- always attempt declared teardown/recovery when safe;
- retain commands, output references, duration, commit, profile/image identity,
  and outcome for every stage;
- expose an explicitly reviewed local target-ref finalisation action if needed.

**Exit:** the harness can say precisely whether the integrated product built,
started, became ready, passed acceptance, and cleaned up locally.

### M6 — Generic release proof

**State:** not started.

- complete the full path for one representative real project;
- complete the first through the shipped local-process topology and a second
  project through Compose with a materially different toolchain;
- demonstrate no execution-path core edit was required for the second project;
- pass pytest, Ruff check, Ruff format check, strict mypy, and genericity tests;
- publish measured failures and residual limitations.

**Exit:** every acceptance criterion in `minimal.md` is supported by retained
evidence. Only then may the minimum product be described as delivered.

## Evidence boundary

### Demonstrated

- A real supervised Stage 2 run processed attempts and exposed fleet defects:
  [`evidence/2026-08-05-06-rdpapp-m2-status.md`](evidence/2026-08-05-06-rdpapp-m2-status.md).
- Local plan-branch integration mechanics passed fixture acceptance:
  [`evidence/2026-08-06-stage-4-plan-integration.md`](evidence/2026-08-06-stage-4-plan-integration.md).
- Repository tests cover the individual queue, execution, integration, API, and
  genericity components described above.

### Not demonstrated

- deterministic admission of the new minimum template;
- per-project runner-image generation and digest pinning;
- a complete live multi-item plan through local integration;
- local deployment and acceptance of the product under development;
- a second materially different project with no core changes.

Current implementation evidence: `plan_revisions.py` persists immutable plan and
item snapshots with digest idempotency; `admission_service.preview` binds a
valid plan to read-only repository identity and exact base SHA; the typed API
preview route returns the proposal without mutation. Apply, semantic-review
question persistence, revision classification, and complete GUI/CLI admission
workflow are not implemented yet. `admission_service.apply` is the tested
atomic foundation and leaves projects stopped.

Evidence reports are append-only historical records. They are not edited to
match this target; their scope and dates remain part of what they prove.

## Decisions and blockers

- Settled decisions D1–D7 and D10–D14 remain settled where applicable. Limiting
  the minimum product does not rewrite their evidence.
- D8, registration of arbitrary third-party gates, remains open. The minimal
  product must not assume such a registry. Its fixed build/readiness/acceptance/
  teardown lifecycle and existing check outcomes do not answer D8 by accident.
- D9 remains blocked as recorded in repository guidance.
- The embedded authoring syntax is settled as exactly one fenced `harness` TOML
  block with `version = 1`; field semantics and implementation tasks are in
  `BACKLOG.md`. The parser, canonical form, validator, CLI, and API report now
  exist; admission, adapter-specific checks, and complete evidence remain
  incomplete.

## Verification for documentation alignment

Documentation-only alignment should at minimum check links, genericity wording,
and the working-tree diff. Code gates are required when implementation changes
begin. The repository-wide gates remain:

```console
TMPDIR=/path/on/a/fast/volume uv run pytest
uv run ruff check .
uv run ruff format --check .
TMPDIR=/path/on/a/fast/volume uv run mypy
```

Passing those gates is necessary but not sufficient for M6; the live local
product lifecycle evidence is also required.
