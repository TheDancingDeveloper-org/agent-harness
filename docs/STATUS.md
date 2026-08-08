# STATUS — where agent-harness actually stands

**Date:** 2026-08-06. **This is the only status document for this repository.**

It says where the project stands, what is left to do, what the first real
workload is, and what must be true before it is run again. It does not describe
how the harness works today — that is [`DESIGN.md`](DESIGN.md), which owns the
implemented design. Section 2 records the product decisions and development
sequence agreed on 2026-08-06; where the current code does not implement one of
them, that is pending work rather than permission for DESIGN.md to claim it
already exists.

[`FIT-FOR-PURPOSE-STATUS.md`](FIT-FOR-PURPOSE-STATUS.md) is frozen at
2026-08-04 and is being deprecated. Read it for the history of the stage
programme; do not read it as current, and do not link to it as current.

Three words are used precisely throughout, exactly as `README.md` defines them:

| word | meaning |
|---|---|
| **tested** | a test in this repository fails if it stops being true |
| **observed** | seen in a real run, without preserved artefacts that would let anyone reproduce it |
| **proven** | measured against a stated criterion, with the denominator and the commands published |

---

## 1. Where it actually stands

**The harness has never delivered a work item against a real workload.**

Not once. Four passes of the direct executor and one standalone run of the
agent loop against `rdpapp` produced no merged work, and no other real workload
has been attempted. There is no delivery rate, no cost per merged item, and no
comparison against any baseline, because the numerator has never been greater
than zero.

What *is* true, and is worth stating alongside it:

- The deterministic paths are **tested** — the queue and leases, the dependency
  graph, holds, attempts, budgets, the outcome taxonomy, the patch-apply
  ladder, the checks gate, the reviewer gate, the API contract, the redactor,
  the command guard, and the first-run/demo path. `uv run pytest` fails if any
  of them stops being true.
- The service runs and is deployed inside AIDevEnv. That is **observed**, but
  AIDevEnv is not part of the accepted product architecture below and this
  deployment does not satisfy the execution requirement.
- Real model calls have been made against a real gateway, against a real
  repository, and they exposed real defects — several of which now have
  regression tests (#216, #217, #218). The defects are tested; the runs that
  found them are observed.
- Nothing about live behaviour is **proven**. No column entry exists.

The diagnosis of why nothing has been delivered is recorded in **#195**: the
single-shot model call is the defect. Every role — planner, implementer,
reviewer, surveyor, assessor, inception — is a model asked to answer questions
about a repository from a snapshot it was handed, unable to look at anything it
was not given and unable to check its own answer. Four separate repairs to the
implementer's output format on 2026-08-05 (unified diffs → edit blocks →
indentation tolerance → quoting the file back on a failed match) each improved
the output and each delivered nothing. The format was never the problem.

The same item, same models, same gateway, run as a **loop** with tools reached
`cargo test` green in 31 turns. That real run went through a standalone script.
The loop has since been put through the queue, gates, append-only audit sink,
attempt record, item budgets and reviewer against local scripted fixture
repositories; that path is **tested**, while a real workload through it remains
unobserved. The standalone
run is **observed**, once, on one item — it is not evidence that the harness
works, and it is explicitly not a delivered item.

**Stage 1 / #215 is implemented and tested locally. Stage 2 live acceptance
remains a prerequisite; Stage 3 wiring is now the next implementation block.**
The GitHub issue remains open while this exists only locally, per P12 and D1. A
useful loop must now be confined before it touches a real workload, then become
reachable from an AIDevEnv-independent service fleet, execute in isolated worktrees, promote
several items safely to one plan branch, surface exceptions, and attribute its
calls and outcomes to the item that caused them. None of that end-to-end path
has run.

The first-party browser control plane from `codex/gui-plan` is now imported on
this tree and **tested** at the in-process application boundary. It includes the
packaged authenticated shell, project/work/hold/event views, guarded single-item
actions, inception and plan review, adoption, dependency exploration, routing,
worker and analytics views, audit operations, process/gateway projections, and
the typed/shared services those paths use. Importing it did not replace the
Stage 1 role-runner path: `role_runners.py`, the metadata-selected adapter and
their end-to-end fixture tests remain present.

That is an implementation statement, not release evidence. No browser runtime,
phone viewport, screen reader, real deployment, forced SSE reconnect, real
GitHub concurrency or real fleet has exercised the imported GUI on this tree.
It remains an execution-independent client: monitoring works without an
executor, and a start action is still refused when the deployment cannot claim
work. The GUI's remaining work is recorded in §2.8 and in the consolidated
pending tables below; `GUI_PLAN.md` is design/history, while this file remains
the status authority.

---

## 2. Settled product direction and development plan

This section records owner decisions made on 2026-08-06 after the failed
single-shot runs and the successful standalone loop experiment. They are the
requirements for the next implementation cycle. They are written with their
reasoning because changing one in isolation recreates the four days of local
repairs that did not improve delivery.

These decisions do not claim the code implements them. The first local
acceptance run in §2.4 is what moves them from requirements to observations.

### 2.1 Immediate outcome

The immediate success criterion is **a local, autonomous, multi-item coding
fleet**, not one agent completing one item. Given a plan, a repository and its
declared checks, the service must:

1. start and supervise workers without AIDevEnv, a terminal session host, or a
   local Codex/Claude/OpenCode process;
2. run at least two independent items concurrently in separate worktrees;
3. use a tool-using model loop for implementation, with all model traffic still
   routed through `ModelClient` and the configured API provider;
4. wait for prerequisites, then base dependent work on the locally promoted
   results of every prerequisite;
5. run the configured gates authoritatively, serialise promotion, and build one
   local integration branch for the plan;
6. continue other work when one item fails, and ask a person only for a real
   question, policy refusal, ambiguity or unrecoverable failure; and
7. preserve enough item-scoped evidence to explain every call, change, gate,
   promotion, hold and failure.

The first acceptance is deliberately local. After it passes, remote
acceptance publishes the integration branch and opens one pull request against
the target branch for human review. Later corrections update that same branch
and PR. There are no per-item pull requests and no automated merge to the
target branch.

### 2.2 Decisions that are not to be re-litigated during this cycle

| ID | decision | context and consequence |
|---|---|---|
| **P1 — multi-item first** | The delivery slice is a fleet executing a plan, not a one-item demo. | A one-item loop already reached green checks once and still proved neither scheduling nor delivery. The minimum acceptance graph therefore contains two independent items and one item that depends on promoted work. |
| **P2 — harness-owned execution** | The primary executor is an in-process role runner owned by `agent-harness`. AIDevEnv and subscription-backed CLI agents are not runtime dependencies. | The current supervised service can execute only when `--session-host` is supplied. That is useful historical scaffolding, but it fails the product requirement. AIDevEnv may be used to develop the repository and the session-host adapter may remain supported; neither may be required by `serve`, preflight, starting a project or completing an item. |
| **P3 — loop, not proxy** | `mini-swe-agent` supplies the tool-using interaction loop; it is not a proxy to the provider. Every model call continues through `ModelClient` to the configured route. | Direct API transport was healthy; the single-shot interaction model produced unusable work. The same API and model became useful when allowed to inspect, edit and test iteratively. Core owns a generic `RoleRunner` contract and resolves implementations by installed metadata; it must not import or name a particular adapter, preserving the generic-core rule. The selected runner and compatible version are load-bearing deployment configuration and must be reported by doctor/preflight. |
| **P4 — checks have two jobs** | Agents may run project checks during their loop for feedback. The harness always runs the configured gates again after the loop, on the exact tree proposed for promotion. | Preventing feedback recreates the blind single-shot defect. Treating an agent's claim that tests passed as the gate weakens the product. Feedback never substitutes for the authoritative gate and never changes its command or outcome taxonomy. |
| **P5 — real local confinement** | Every item receives its own git worktree and an OS-enforced filesystem boundary. The repository is writable; related dependency roots are explicitly declared per project with read/write mode, and default to read-only. A minimal platform runtime/toolchain may be mounted read-only and reported; dependency caches are declared or ephemeral writable mounts. No other undeclared host data path is readable or writable. | `CommandGuard` is screening, explicitly not a sandbox, and cannot enforce this requirement against inline programs. The current loop subprocess also inherits the controller environment. Before a real run, command execution needs a deliberately constructed environment, with provider, GitHub and harness credentials retained only by the controller. |
| **P6 — internet is available** | Agents have near-full outbound internet access, subject only to explicit platform policy and auditable denials. | Agents are expected to consult documentation and obtain ordinary dependencies. Filesystem and credential isolation still apply. This is not a confidentiality boundary: an agent that can read repository content and use the internet can transmit that content, so secret-bearing source must not be placed in an agent-readable checkout unless that exposure is accepted. |
| **P7 — one integration branch per plan** | The harness creates a plan branch from an exact target-branch commit. Workers use disposable item branches/worktrees; gated item commits are promoted to the plan branch under a single promotion lock. | Independent work can run in parallel, while promotion remains deterministic. If the plan branch advanced after an item started, the item is replayed onto the new head and the authoritative gates run again before promotion. An item with several prerequisites starts only after all have been promoted, so it needs no arbitrary choice among unmerged bases. “Automatic approval” means this gate-controlled local promotion, never a fabricated remote review approval. |
| **P8 — one human-reviewed PR** | When the whole plan is locally acceptable, the integration branch is pushed and one PR is opened against the target branch. Only a person approves and merges that PR. | Per-item/stacked PRs make dependency bases, review ordering and rebases the product's main complexity. The integration branch solves that locally and leaves one coherent human decision. Item-level commits, dependencies, gate results and summaries must remain visible in the final PR and API. |
| **P9 — review feedback resumes automatically** | New actionable review comments on the final PR create or resume correction work on the same integration branch, rerun the gates and update the existing PR. | Human review must not require an operator to reconstruct an agent session. Ambiguous, contradictory, policy-changing or already-resolved comments become holds for a person; they are not guessed at. Webhook delivery is preferred and polling is an acceptable recovery path, both deduplicated by immutable remote event identity. |
| **P10 — human involvement is exceptional** | Before publication, people are involved only for questions, holds and failures. The single final PR review and merge is the normal human approval point. | There is no approval ceremony per item and no need to watch agents work. A held item keeps its claim under D12; unrelated workers continue. The GUI/API must make the exception and the evidence needed to answer it visible. |
| **P11 — calls are not the optimisation target** | Use generous configurable loop bounds. Keep time, spend and call ceilings as emergency controls and continue measuring them, but do not shorten the loop to minimise request count. | The useful run took 31 turns; the failed design used one. Number of calls is not currently a material product concern. A limit still must stop a pathological loop, and a provider cost cap is still terminal and never retried. |
| **P12 — local development cadence** | Develop, commit, gate and integrate locally. Do not push, open a PR, wait for hosted CI, or deploy each implementation slice. | Those remote steps are materially delaying the feedback loop. GitHub support is retained and tested with local fakes; GitHub issues remain the state record required by D1, but issues stay open while implementation exists only locally. Publication happens once at the explicit milestone in §2.4. |
| **P13 — GUI is a client, not an execution dependency** | The GUI consumes typed API state and event/notification contracts. It is owned and served by `agent-harness`, but is not an input to executor design. | The imported browser control plane is a first-party client over shared services. Authenticated notification delivery and a generic review-source contract exist, while a deployed external source adapter remains optional. Execution must continue with the GUI offline. Holds, failures, completion and review events live in durable harness stores/API; the GUI or a later external channel presents and notifies. |

### 2.3 Plan-branch and dependency semantics

The integration branch replaces stacked pull requests as the coordination
mechanism:

```text
target branch at an exact SHA
          │
          ▼
   local plan branch ────────────────────────────────────────┐
          │                                                  │
          ├── item A worktree ── gates ── promote A ─────────┤
          ├── item B worktree ── gates ── promote B ─────────┤  concurrent
          │                                                  │
          └── item C waits for A+B, starts from their         │
              promoted plan head ── gates ── promote C ───────┘
                                                             │
                                      publish one branch, one PR to target
                                                             │
                                      human review and merge only
```

“Done” inside the plan means promoted to the local plan branch, not merged to
the target branch. The queue may then release dependants. Only the completed
plan has a PR. If a promotion conflicts, the item returns to agent work with
the current plan head and the conflict evidence; it is not resolved by an
unreviewed merge strategy. If the target branch moves, the plan branch must be
updated and the full integration gates rerun before publication.

### 2.4 Development sequence and exit evidence

Work proceeds in this order. A stage is not complete because its code exists;
its exit evidence must be retained locally with the commands, commit and
denominator. Safety defects discovered in an earlier stage pre-empt the order.

| stage | implementation | exit evidence before moving on |
|---|---|---|
| **0. Preserve the baseline** | Keep the direct and session executors while building the new path. Record these decisions and align affected tracker issues at the publication milestone. | The current four repository gates pass. No existing gate or historical evidence is removed to simplify the runner. |
| **1. Put the loop behind a generic runner** | Define the core role-runner protocol and installed-metadata lookup; adapt the existing mini-SWE loop to it; pass role, item, project and whole-loop bounds explicitly. Let the loop use checks for feedback, then feed its resulting tree into the existing checks/review/attempt/audit pipeline. | In local fixture repositories, a multi-turn implementer can inspect, edit and test; the harness reruns the declared gates; all calls are attributable to one project/item/attempt; no AIDevEnv or CLI-agent process is involved. A real workload is not run yet. |
| **2. Enforce the execution boundary** | Replace inherited shell execution with an OS-enforced local sandbox, a minimal allow-listed environment, an item worktree, and declared dependency mounts. The recommended first backend is Docker/OCI, behind a generic execution-environment contract and selected through deployment metadata; keep outbound internet available. Treat `CommandGuard` as an earlier explanatory refusal, not the security boundary. | Tests prove an agent can work throughout its repository, cannot read or write an undeclared sibling/host path, cannot read controller credentials from its environment, can use an allowed dependency root according to its mode, and can reach an allowed network fixture. The tests must exercise the actual backend and its configured security options, not only a mocked subprocess. Do not run rdpapp before this passes. |
| **3. Make `serve` own a local fleet** | Add an AIDevEnv-independent executor factory and worker pool to `serve`; make executor capability—not presence of `--session-host`—drive readiness and preflight. Allocate one worktree and runner per claimed item, with item-scoped telemetry and failure isolation. | With no AIDevEnv variables or session host, the API starts a project and two fixture items are observed running concurrently. Killing or failing one does not stop, park or corrupt the other; restart/reaping leaves claims and worktrees consistent. |
| **4. Build local plan integration** | Create and durably record the plan branch/base SHA; serialise promotion; rebase/replay and regate work produced from an older plan head; release dependants only after every prerequisite is promoted. Preserve item commits and promotion events. | A local fixture plan with two independent items and one item depending on both completes into one branch. The dependent item demonstrably sees both promoted changes. A conflicting promotion is returned for repair, and no remote is contacted. |
| **5. Complete exception and feedback control** | Expose item-scoped runner progress, questions, holds, gate evidence and promotion state through typed API/events. Add a deduplicated remote-review event contract and automatic correction-item/resume path, exercised against a local fake. Connect the GUI/notification workstream only through those contracts. | A question pauses only its item and can be answered through the API; an injected actionable review comment resumes work once; duplicates do nothing; ambiguous feedback opens a hold; the fleet continues throughout. The test does not require a GUI or GitHub. |
| **6. Local multi-item acceptance** | Run a real supplied plan against rdpapp, or another explicitly authorised real repository, entirely locally. Use at least two workers and a graph containing two independent items plus a dependent item. Build the local plan branch, run the real project gates and retain an evidence package. | The plan branch contains the promoted item commits and passes its declared integration gates. There was no AIDevEnv/session-host/CLI-agent dependency, no push, no PR and no deployment. Report delivery rate, failures, turns and cost honestly; one successful run is **observed**, not proven. |
| **7. Remote publication acceptance** | On a repository whose authoritative remote permits it, update from the target branch, rerun integration gates, publish the one plan branch and open one PR. Keep remote credentials in the controller. Detect review comments and exercise one automatic correction if review supplies one. | Exactly one plan PR is raised against the target branch with item/dependency/gate evidence. Corrections update that PR's branch; no item PR exists. The harness never merges it, and publication does not authorise deployment. A person reviews and merges or rejects it. rdpapp's GitHub mirror is not used for this while its own plan forbids that publication path. |
| **8. Measure, then broaden** | Convert the reviewer to its read-only loop (#226), then run #33/#44/#51 as their prerequisites become true. Move surveyor, assessor and deletion work only after implementer-fleet evidence exists. | Published denominators establish delivery, cost, gate and unattended reliability. Until then, do not describe the fleet as proven and do not spend the critical path on more roles or framework breadth. |

### 2.5 Local development operating rule

During stages 0–6, a coherent slice is committed locally after its focused
tests pass. The four repository gates run at every stage boundary. Local
commit identifiers and gate output go into the stage evidence package; they do
not need a remote PR to be valid evidence.

Do **not** push, deploy, open a PR or wait for hosted CI between slices. Do not
close the corresponding GitHub issue while its only implementation is local,
because D1 makes that tracker the issue-state authority. At stage 7, publish
the accumulated, locally accepted milestone in one branch and one PR. This is
a cadence decision only: the GitHub client, PR support, reconciliation and
tests remain product functionality.

Until the stage-6 evidence exists, pause work on additional role conversions,
planner/context deletion, lesson memory, UI features that are not needed to
expose the contracts above, and further output-format repairs. None addresses
the currently measured delivery failure.

### 2.6 Current development position

- **Stage 0:** passes on the current Stage 1 tree. The direct and session
  executors remain present, and no historical gate or evidence was removed.
- **Stage 1:** implemented and tested locally. `run --role-runner agent-loop`
  resolves the adapter through installed metadata before claiming, runs a
  multi-turn implementer through `ModelClient`, captures the complete candidate
  tree, and rejoins the existing checks/checkpoint/reviewer/attempt pipeline.
  Model-call events carry project, item and work-attempt identity; item budgets
  and terminal policy refusals stop at loop boundaries. Evidence is in
  [`evidence/2026-08-06-stage-1-role-runner.md`](evidence/2026-08-06-stage-1-role-runner.md).
- **Stage 2 is in implementation; its exit is pending.** The generic
  execution-environment contract, metadata-selected Docker backend, disposable
  per-item self-contained Git checkout, explicit image/network/mount configuration,
  controller-environment allow-list, pre-claim readiness check and teardown path
  are implemented and covered by local contract tests. The current host has
  Docker CLI but no reachable daemon, so the required tests against the actual
  backend — repository-wide work, undeclared sibling/host refusal, declared
  mount modes, allowed network access and clean teardown — have not yet run. No
  real workload run is authorised by the Stage 2 implementation evidence. See

  [`evidence/2026-08-06-stage-2-execution-environment.md`](evidence/2026-08-06-stage-2-execution-environment.md).
- **Stage 3 wiring is present but its exit is not claimed.** `serve` can now
  construct an AIDevEnv-independent local fleet from the metadata-selected
  role runner and execution backend; readiness and preflight use that executor
  capability, fixture coverage runs two items through separate self-contained
  checkouts, and restart cleanup now has deterministic item paths plus
  worktree-scoped Docker reaping. The live-backend evidence required by Stage 2
  is still missing, and Stage 3 has not yet proved killed-backend isolation or
  the full acceptance graph. Plan-branch promotion now has local fixture
  evidence: two independent items are promoted under the in-process lock and
  durable cross-process lease, and a dependent item sees both changes. No real
  workload run is authorised.
- **Stage 4 implementation has local fixture evidence but its exit is not
  claimed.** The fleet fixture runs two independent items and one item depending
  on both, preserves promotion records, exposes both promoted files to the
  dependent runner, and retains the conflicting-promotion repair path. It does
  not contact a remote. The live execution boundary and Stage 3 failure
  isolation criteria remain open prerequisites.
- **Stage 5 is in implementation; its exit is not claimed.** A generic
  normalized remote-review contract now accepts an immutable source/event id,
  an explicit adapter-supplied disposition, and bounded feedback. Intake is
  durably deduplicated in the queue database: actionable feedback creates one
  correction item dependent on the reviewed item, ambiguous feedback creates a
  pending correction held for a person, and already-resolved feedback creates
  no work. An answer returns an ambiguity correction to `pending`; audit
  delivery is best-effort and item-scoped. The typed API route and local tests
  cover these semantics. `WorkEvidence` now also projects retained runner
  progress, authoritative gate answers (including argv), plan-promotion state
  and normalized review intake without replacing the raw event history.
  A local-plan fleet acceptance now proves an actionable correction is claimed
  once on the configured integration branch while a sibling item continues;
  duplicates remain no-ops. A durable generic notification outbox now records
  selected hold, failure, completion and review outcomes and retries them
  through an authenticated bearer/HMAC webhook channel.
  Two of the three remaining items now have implementations and local tests.
  An installed review source (`github-pr-review`) resolves through metadata,
  gives reviews and review comments distinct immutable identities, and decides
  disposition by explicit markers and review state — with unmarked human prose
  defaulting to a hold rather than to guessed work. A `PlanPublisher` pushes
  one plan branch under `--force-with-lease` and maintains exactly one pull
  request: a correction updates that same PR, an unchanged plan head touches no
  remote, an existing PR is adopted rather than duplicated, a branch moved by
  somebody else is refused, and nothing merges, approves or marks ready. That
  publisher is wired into `direct_executor_factory`: `push=True` on a project
  with a plan branch now means one plan branch and one pull request rather than
  being refused, the executor is given no GitHub client and pushes no item
  branch, publication waits until nothing is in flight and nothing failed, and
  a remote failure is an event rather than a failed item.
  Evidence is in
  [`evidence/2026-08-08-stage-5-review-source-and-publication.md`](evidence/2026-08-08-stage-5-review-source-and-publication.md).
  **No real remote was contacted for any of it**: the pull-request client is a
  fake and the Git remote is a bare repository in a temporary directory. The
  review source has never polled a real pull request, no publication has ever
  reached GitHub, and remote workload acceptance remains open. No Stage 5 exit
  and no remote workload run is authorised.

### 2.7 Execution backend recommendation — Docker/OCI, selectively adopted

The target state needs a real operating-system boundary before a real workload
is run. The recommended first backend for that boundary is **Docker/OCI**,
behind a generic execution-environment contract and selected through deployment
metadata. This recommendation follows a review of
[`desplega-ai/agent-swarm`](https://github.com/desplega-ai/agent-swarm/tree/30f79a927bb6c95b53da8797629cf13b67360159)
at commit `30f79a9` (2026-08-05). It is an execution-backend recommendation,
not a decision to adopt that project's control plane or task model.

#### What to borrow

- Use multi-stage OCI builds so stable, expensive toolchain layers are kept
  separate from frequently changing harness code.
- Publish deliberately different image targets (for example, a small base and
  a fuller development image), and measure uncompressed size and layer changes
  in CI. A single universal image containing every language and browser is not
  the default; it becomes too large and makes the toolchain contract unclear.
- Pin base images, operating-system packages, CLIs and toolchains. Record the
  resolved image digest in preflight and item evidence so a result can be
  explained after an image tag moves.
- Run the agent's commands as a non-root user and make writable locations
  explicit. The container still needs a deliberate security profile:
  `no-new-privileges`, dropped capabilities, a read-only root filesystem where
  practical, resource limits, and no Docker socket or privileged host mount.
- Keep project toolchains in image/configuration metadata. The first real
  workload is Rust, so the first acceptance image must contain the required
  Rust/Cargo toolchain; the reviewed upstream image is not itself sufficient
  because its published worker image focuses on Ubuntu, Node/Bun, Python,
  browser tooling and supporting services rather than Rust.

#### What remains harness-owned

The harness remains the control plane and source of truth:

- `serve` owns claiming, leases, attempts, budgets, holds, events, audit and
  failure isolation;
- `ModelClient` owns every model call, route, retry ladder, endpoint parking,
  pricing and terminal cost-cap policy;
- the selected role runner remains a generic metadata-resolved adapter;
- each item gets a disposable worktree and execution container;
- the plan branch, promotion lock, replay/rebase and authoritative gates stay
  in the harness; and
- provider, GitHub and harness credentials remain in the controller. They are
  not placed in the agent-readable environment merely because a container is
  being used.

The container is therefore a tool-command environment, not a long-lived model
worker. The model loop stays in the harness process and asks the backend to
inspect, edit and test the item's mounted worktree.

#### What not to adopt from agent-swarm

Do not copy its lead/worker task topology, persistent worker-owned repository
clones, or shared source volume as the coordination mechanism. Those are useful
for a different product, but they do not provide the target semantics here:

- isolation is per worker there, while this target requires isolation per item;
- a reused clone can carry branches, stashes or uncommitted changes between
  tasks, which recreates the stale-worktree failure already found here;
- model-provider CLIs and their credentials run inside the worker image, which
  conflicts with P2/P3; and
- shared volumes cannot replace the exact plan-head and serialized promotion
  rules in P7.

E2B or another remote sandbox may later implement the same generic backend
  contract, but it is not a reason to add a second orchestration plane now.

#### Required backend contract before Stage 2 can exit

The Docker implementation must be tested as deployed, not only through a fake
subprocess. For one item it must specify and durably report:

```text
image reference and resolved digest
item worktree mount (read/write)
declared dependency and toolchain mounts (read-only by default)
ephemeral or declared writable caches
minimal allow-listed environment
network policy (outbound internet remains available)
resource and command limits
container identity and security profile
```

Stage 2 is complete only when tests demonstrate repository-wide work, refusal
of undeclared host/sibling reads and writes, absence of controller credentials,
declared mount modes, allowed network access, and clean teardown. Stages 3 and
4 then prove that one such container/worktree is allocated per claimed item,
that killing one item does not affect another, and that Docker execution does
not weaken plan-branch promotion or the authoritative gates.

### 2.8 GUI import and remaining work

The browser implementation from `codex/gui-plan` at `deed5a1` has been
reconciled onto the current Stage 1 tree. The import is deliberately additive:
the current `DESIGN.md`/`STATUS.md` authority, generic role-runner contract,
metadata lookup and adapter tests remain. `agent-harness serve` now packages
and serves HTML, static assets and the typed JSON API from one origin, with no
session-host dependency for browser access.

Implemented and covered by in-process tests:

- bounded opaque browser sessions, token-rotation revocation, login throttling,
  CSRF/origin checks and authenticated operator attribution;
- project, work, hold, event, dependency, worker and analytics views, plus an
  SSE stream over the existing monotonic cursor;
- guarded project/work/hold controls and one-time reviewed project, routing,
  plan-sync, adoption, audit-maintenance and reconciliation actions;
- typed shared services for queries, configuration, routing, plan sync,
  adoption and audit operations rather than browser-only business rules;
- item evidence, worker inventory, process metrics, narrowed/redacted gateway
  event projections, and an accessible no-script dependency table with an
  optional packaged graph enhancement; and
- clean-wheel coverage for the templates and vendored assets. Normal operation
  has no CDN or separate frontend build/service.

Remaining work, in dependency order:

1. **Finish the remaining execution-facing work in Stage 5.** The single-PR
   publication/resume mechanism and an installed `github-pr-review` source now
   exist with local tests, and the typed evidence projections, deduplicated
   intake contract, local fleet acceptance and generic notification outbox were
   already present, and publication is now wired into the executor factory so a
   promoted correction updates the plan PR without an operator. What remains:
   run that path against a real remote (Stage 7's milestone), deploy and poll
   the review source against a real pull request, and prove fleet continuation
   while an item is held or receives review feedback. The GUI must consume
   these contracts and remain optional to execution.
2. **Complete existing operator controls.** Add exact-state reviewed bulk
   transitions, complete continue/force-start and refusal parity, preserve hold
   answers across expiry/version conflicts, and add any missing item filters or
   artifact/diff links. No visual gesture is authority for a transition.
3. **Extend notification delivery beyond the first channel.** The durable
   generic contract and authenticated webhook/channel adapter are present.
   Add any deployment-specific presentation or phone channel only as an
   opt-in adapter; do not infer one from the optional session host.
4. **Prove the browser boundary.** Add browser-runtime journeys for forced SSE
   disconnect/replay and polling fallback, keyboard-only use, focus handling,
   reduced motion, desktop/phone layouts and screen-reader semantics. Add the
   planned XSS/CSP, cookie, root-path/proxy and simultaneous-operator security
   exercises. In-process ASGI coverage is not a substitute for these.
5. **Exercise real integrations and packaging.** Run monitoring-only and
   supervised deployment smoke tests from a built wheel/container; exercise
   plan sync, adoption and reconciliation against an authorised non-critical
   remote, including concurrent remote changes and partial-write recovery.
   Retain release evidence and unmet criteria; the imported branch's historical
   evidence does not prove the reconciled tree.
6. **Later product subsystems remain unbuilt.** Agent-harness-owned persistent
   sessions/chat/terminal; memory, knowledge, skills and tools; scheduling and
   external channels; multi-user identity/RBAC; and WAL-aware backup/restore
   retain the sequencing in `GUI_PLAN.md` Milestones 5–8. They must use generic
   protocols and may not create a gate registry while D8 remains open.

GUI work outside item exceptions and evidence remains behind Stages 2–6 of the
delivery programme. A polished control plane cannot authorize the next real
workload before confinement, fleet ownership and plan integration pass their
own exits.

---

## 3. All pending work

Every open issue, organised by what a reader can act on. Issue state lives on
GitHub (D1); this section is a reading of it on 2026-08-06 and will drift.

### 3.1 First implementation block

| # | what it is | why it is where it is |
|---|---|---|
| **#215** | Build the generic agentic role runner and put the implementer through it. | Implemented and tested on the local development branch; the tracker stays open until the publication milestone (P12/D1). It is stage 1, not the whole milestone. The agent may run checks for feedback, the harness reruns them as gates, and bounds apply to the whole loop with generous call limits rather than forcing it back toward one-shot behaviour. The implementation follows P3's installed-metadata boundary: core imports no shipped runner. |

### 3.2 The #195 programme

**#195** is the organising idea and should be read in full before any of its
parts. It says the interaction model is the defect, names what survives
(`ModelClient` and its routing/retry/spend-cap behaviour, `protocols.py`, the
queue, holds, the graph, budgets, the guard, the audit, and the gates
themselves) and what is revealed as scaffolding (the planner, context
pre-selection, edit blocks and `to_diff`, structured-text parsing). It also
states the cost being accepted: `mini-swe-agent` is load-bearing in the selected
execution deployment rather than an experiment, and turn count rises from 1 to
roughly 30 per role per item. P3 refines the dependency wording: the core path
depends on a generic runner contract and metadata lookup, never on a named
adapter import.

Its parts, in the order the workload's own evidence puts them:

| # | what it is | why it is where it is |
|---|---|---|
| #215 | implementer through the role runner | implemented/tested locally; publication and tracker closure wait for the milestone — see above |
| #226 | reviewer through the runner, with a **read-only** environment | highest value after delivery works. The gate that rejected rdpapp T1 was *inferring* from a diff; it was right and it was guessing. A false rejection costs an attempt and blames a model that was correct. Read-only is not a detail: a gate with write access to the tree it judges can be talked out of a rejection. |
| #224 | surveyor through the runner, read-only | a plan should be written by something that read the repository. Matters more for the *next* project than for rdpapp M2, whose plan already exists. |
| #225 | assessor (`adopt`) through the runner, read-only | `adopt` asks a model to find evidence it cannot go and find. Real, and on nobody's critical path today. |
| #227 | retire the planner and context pre-selection | **deliberately last, and gated on evidence rather than a date.** Deleting the only path that has tests in favour of one that has never run in-harness is the trade AGENTS.md rejects. It moves once the loop has delivered items *through the harness*. |

### 3.3 Defects with no blocker

Each of these can be picked up today. All were found by reading code or by
reviewing a real failure, and none is waiting on anything.

| # | what it is | why it is where it is |
|---|---|---|
| #219 | two edit blocks naming one file by different path strings (`a.txt` and `./a.txt`) render its diff twice; the second copy cannot apply | found during the #216 review and deliberately left out of it, because the fix changes `plan_edits`' public keying. Low severity and fails safely — but the message blames the model for an edit it got right, which is the class of bug this repository spent a day removing. |
| ~~#220~~ | two API routes compare a lease against `time.time()`, not `queue.now()` | **fixed locally, 2026-08-08.** The ruling taken is that `queue.now()` is authoritative for a lease everywhere; the retry and block routes now use it. Red-first: with the wall-clock comparison both new tests return 200 where 409 is correct. Tracker stays open until the publication milestone (P12/D1). `tests/test_api.py::test_retry_honours_the_queue_clock_not_the_wall_clock`, `…::test_blocking_honours_the_queue_clock_not_the_wall_clock`. |
| #221 | two holds opened by one attempt in the same tick raise a bare `sqlite3.IntegrityError` instead of a `HoldError` | `asked_at` is a float used as part of an identity. Effectively unreachable against a real clock, immediately reachable with an injected one. The fix needs a small design call: may one attempt hold twice at all? |
| ~~#223~~ | `_WIRE_ROLES` permits a `tool` message that `_for_the_wire` has already stripped the `tool_call_id` from | **fixed locally, 2026-08-08.** `tool` is removed from the allow-list, so the role a reduced message cannot validly carry can no longer reach the wire; the observation still goes back as a `user` turn and no content is lost. Tracker stays open until the publication milestone (P12/D1). `tests/test_agent_loop_e2e.py::test_a_tool_message_cannot_reach_the_wire_without_its_id`. |
| #207 | `test_pausing_a_project_stops_claiming` asserts completions stop within 100 ms, which is a timing assumption about the host | a CI flake on an unrelated branch. The property worth protecting is that pausing stops *claiming*; the assertion instead measures how fast an in-flight item finishes. The resume half of the same test already waits on a condition and is not flaky. |
| #209 | a stored model answer is redacted, so it cannot be used to reproduce what the model actually said | two promises in tension — "what did the model say" and "no credential reaches an append-only store" — with the second silently winning. It bites hardest on rdpapp, a credential vault whose fixtures are full of credential-shaped source. It matters most for exact-match edit failures, which are questions about characters, in a record whose characters were changed. |
| #103 | silent-but-active CLI sessions are indistinguishable from hangs | session-host path: PTY output is the only activity signal, so a working agent that prints nothing reports `activity: idle`. Independent of the #195 programme; note that #195 also deprecates `--session-host` in help and docs, so weigh effort here against that. |

### 3.4 Blocked on a decision or a measurement

These are not waiting on effort. Each names what it is waiting for.

| # | what it is | blocked on |
|---|---|---|
| #184 | a generated phase heading is both a tracking umbrella and a claimable item, and it cannot be both | **a measurement**, from a deployment that has actually run a generated plan: are phase items ever claimed and completed, or do they sit `pending` while their children finish? `render_plan(..., phases_as_items=...)` currently splits the behaviour by caller, which the issue calls a holding position rather than a design. |
| #189 | a correction learned on one item is paid for again on every item after it | **a decision, and a measurement.** The mechanism is easy; whether a lesson store is compatible with this repository's measurement discipline is the question, because a store that mutates the implementer's prompt between items makes two runs incomparable. The issue's own recommendation is *do not build it* until a real multi-item run says how many check failures share a cause with an earlier item's — a number #33/#44/#51 would produce as a by-product. |
| #222 | an item the claim scan gives up on is left with no disposition, and empty means "not finished with yet" | **decision D8** — whether third-party gates get a registration mechanism in `outcomes.py`. Recording `exhausted` properly needs a new reason kind (probably `gave_up` under `DECIDED`), and adding one would answer part of D8 sideways. A test fails if `outcomes.py` grows a registry, precisely so D8 is not answered by accident. |

Open decisions generally: **D7**, **D8** (above), **D9** (blocked on #84 — and
no stage may hold the review prompt as a variable while it is). See AGENTS.md
§ Decision hygiene; D1–D6 and D10–D14 are settled and are not to be
re-litigated.

### 3.5 Blocked on a real run

These cannot be closed by writing code. They need the harness to run against a
real workload for a real duration — which needs stages 1–6, not merely #215.

| # | what it is | why it is where it is |
|---|---|---|
| #33 | 72-hour measurement run: rate-limit errors broken down by class, delivery rate, patch-apply rate, `review_rejected` rate, against the plan's §2.1/§2.5 baselines | **the P1 deliverable is this measurement, not the code.** Cannot start: no item has ever been delivered, so there is nothing to measure a rate over. |
| #44 | 48-hour ingester soak against live fleet traffic — no restarts, no dropped events, store growth within expectation | needs live fleet traffic, which needs a fleet that delivers. |
| #51 | 7-day unattended run — no manual restart, no human intervention, every failure diagnosable from the GUI alone | the top-level fit-for-purpose criterion. Furthest out; everything else is upstream of it. |
| #84 | A/B whether the reviewer seeing the planner's rationale changes its verdict | blocked on a **real backlog run twice over**. It is the experiment D9 deferred to, and the audit layer (`review_approved`/`review_rejected` per item, `GET /api/audit/cost`, `reconcile`'s merged/closed/reverted) now makes it measurable. The metric that matters is revert rate, not approval rate: a higher approval rate with a higher revert rate is anchoring, not insight. **Do not settle it by argument.** |

Note that #84 and #226 interact: whether a read-only environment is enough to
keep the reviewer honest is untested, and #195 says so explicitly — it could
still be argued into a pass by its own reading of the code.

---

## 4. rdpapp is the first application under test

agent-harness is being exercised against **`TheDancingDeveloper-org/rdpapp`**,
also hosted on Forgejo at `repo.indexarr.net/indexarr/rdpapp`. **The Forgejo
remote is authoritative; GitHub is a mirror** — rdpapp's own `plan.md` says so,
and both remotes are configured in the working checkout (`origin` → Forgejo,
`github` → GitHub).

It is the first real workload, and it was chosen deliberately at the hard end:

- **Rust**, so a check gate means a real compile and a real test run, not a
  linter;
- a **704 KB `main.rs`**, which is where the miscounted-hunk failures came from
  — the arithmetic a unified diff header demands gets harder as a file grows,
  and that evidence is what reopened decision D10;
- a **credential vault**, whose own test fixtures trip the harness's redactor,
  which is how #209 was found.

The workload's own running record is
[`evidence/2026-08-05-06-rdpapp-m2-status.md`](evidence/2026-08-05-06-rdpapp-m2-status.md)
(evidence package `rdpapp-m2-2026-08-05-06-v1`). Read it for the detail; it is
not duplicated here. In summary:

- **No item has been delivered.** Four executor passes and one standalone loop
  run.
- Pass 1 failed on miscounted hunk headers — a format defect, fixed, and the
  reason D10 was reopened in favour of edit blocks.
- Pass 2 reached `checks passed → commit → review` and was rejected on
  substance. That is the gate working correctly.
- Passes 3–4 failed with `SEARCH text does not occur in the file`. **That was
  the harness's fault**: the diff was computed against a working tree still
  holding the *previous item's* branch. The model was right every time and the
  harness blamed it — and the better the previous item did, the more certain
  the next was to fail. Fixed in #216.
- The standalone loop run hit `LimitsExceeded` at 40 turns, ~15 of them lost to
  a guard false positive. Guard defect fixed (#217).

Its decision — **do not repeat the existing delivery command** — remains this
repository's operating instruction. A rerun today uses the execution model
measured to deliver nothing. The next real run is stage 6 and must wait for the
loop, confinement, local fleet and plan-integration exits in stages 1–5.

**These numbers are rdpapp's.** They are one repository, one gateway, one model
family, and nothing in them is a universal measurement about the harness. A
second repository is the only thing that would make any of it general, and
none has been attempted.

Two limits of that evidence deserve repeating here because they qualify
everything above: the 31-turn loop run **cheated and was caught by hand, not by
a gate** — it appended tables to a tracked SQL fixture so its own registry
matched, and no reviewer ever saw it. And **cost is unmeasured**: turn counts
are recorded, spend is not, and `pricing` has never attributed a ~30-call role
to a single item.

---

## 5. Last rdpapp run recipe — retained for evidence, do not execute

This is the recipe that produced the observations in §4. It is retained so the
evidence is reproducible, **not** as the command for the next run. It invokes
the old direct executor and must be replaced by the stage-6 local-fleet command
after stages 1–5 pass. Its content comes from the evidence package above and
from an **unversioned** file at
`~/Working/Active/.harness-runs/rdpapp-m2/env.sh`.

Paths below assume the layout of the machine this was run on
(`~/Working/Active/...`). Adjust them; nothing in the harness requires them.

### 5.1 Preconditions, each verifiable

```bash
# 1. Base lineage is not stale. This is what invalidated the abandoned
#    2026-08-04 attempt: work was based on a branch 121 commits behind the
#    authoritative remote.
cd ~/Working/Active/rdpapp
git fetch --all
git rev-list --count harness/m2-base..origin/master     # must be 0

# 2. The tree is clean.
git status --porcelain                                   # must be empty

# 3. Both remotes agree. `origin` (Forgejo) is authoritative; GitHub is a
#    mirror. Confirm rather than assume.
git rev-parse origin/master github/main                  # must match

# 4. The gateway answers, and with what. Claw Bay is frequently and broadly
#    degraded: 8 of 42 models answered on 2026-08-05, all one family.
curl -s -H "Authorization: Bearer $THECLAWBAY_API_KEY" \
  https://api.theclawbay.com/v1/models | head -c 200
```

### 5.2 The environment

Run-scoped rather than written into a shell profile: the attempt is meant to be
discardable by deleting one directory, and a profile edit would outlive it.
Every model here is on Claw Bay; nothing routes to a local CLI agent.

```bash
export HARNESS_ENDPOINT="https://api.theclawbay.com/v1"
export HARNESS_ROUTE_PRESET="claw-bay"
export HARNESS_API_KEY="${THECLAWBAY_API_KEY:?THECLAWBAY_API_KEY is not set}"

# gpt only, by owner's decision 2026-08-05: measured 8 of 42 models answering
# and all 8 in this family. Chains are preference order, first that answers
# wins. gpt-5.6 leads because gpt-5.5 timed out the gateway origin (524) on
# long generations while 5.6 answered 200 throughout.
export HARNESS_PLANNER="gpt-5.6,gpt-5.4-mini"
export HARNESS_IMPLEMENTER="gpt-5.6,gpt-5.5,gpt-5.4"
export HARNESS_SURVEYOR="gpt-5.6,gpt-5.5"
export HARNESS_ASSESSOR="gpt-5.6"

# NOT independent: same vendor as the implementer, because no second vendor is
# reachable. Every approval taken under this configuration is weaker than one
# taken when two vendors answer. Check GET /api/routes/health before trusting a
# review; when independence_possible turns true, move this to another vendor.
export HARNESS_REVIEWER="gpt-5.5,gpt-5.4"

# No local CLI agent. Direct API mode is used, so this is belt and braces: if a
# session host is ever passed, the agent must still not be a
# subscription-backed local binary.
export HARNESS_AGENT_COMMAND=""

# Shared so each item does not pay a cold Rust build in a fresh worktree.
export CARGO_TARGET_DIR="$HOME/Working/Active/.harness-runs/rdpapp-m2/cargo-target"
```

### 5.3 The historical run

```bash
cd ~/Working/Active/apps/agent-harness
R=~/Working/Active/.harness-runs/rdpapp-m2
. $R/env.sh

uv run agent-harness --db $R/queue.sqlite run --project rdpapp-m2 \
  --work ~/Working/Active/rdpapp \
  --plan ~/Working/Active/rdpapp/docs/harness/M2-PLAN.md \
  --base harness/m2-base --no-push --reroute \
  --context-budget 300000 \
  --events $R/events.jsonl \
  --check 'cargo test -p rdpapp-models -p rdpapp-sessions -p rdpapp-gateway' \
  2>&1 | tee $R/run-$(date +%H%M).log
```

Flags that are not decoration:

- **`--no-push`** — rdpapp's `plan.md` calls GitHub a mirror and says the CI
  cutover is **not authorised**. Work stays on local branches, so a discarded
  attempt is branches to delete rather than a remote to clean.
- **`--reroute`** — without it the **stored** role map wins and the role-chain
  environment above silently does nothing. The harness warns, and the warning
  is emitted *before* the reroute applies, so it can read as a failure when it
  is not.
- **`--base harness/m2-base`** — cut from `4dff7e2`, equal to both remotes.
- **`--context-budget 300000`** — the implementer chain's head is `gpt-5.6`,
  which holds 372k. Anything above that silently exceeds the model.
- **`--check '…'`** — Rust only, and deliberately: `migration-tool` is
  workspace-`exclude`d so its FluentGUI path dependency does not affect the
  gated crates (do not add it), and a fresh worktree has no `node_modules`, so
  `tsc`/`vitest` cannot start. **Do not gate on `cargo fmt --check`** — it
  refused five otherwise-correct attempts in the abandoned attempt. #155 lets a
  declared formatter's fix run and re-checks, but it is off unless
  `apply_fixes` is set on the project.

### 5.4 Retrying failed items without destroying `last_error`

```bash
uv run python -c "
import sqlite3; c=sqlite3.connect('$R/queue.sqlite')
c.execute(\"update work set state='pending', owner=NULL, lease_until=0 where state='failed'\")
c.commit()"
```

**Keep `last_error`.** The harness feeds it into the next attempt's prompt, and
that is the only thing that makes a retry different from a repeat. Clear
`attempts` too **only** when the previous failure was the harness's fault
rather than the item's; otherwise the attempt ceiling stops meaning anything.

### 5.5 Monitoring

```bash
R=~/Working/Active/.harness-runs/rdpapp-m2

# What each item is doing. The stages are the whole story: an item that reached
# `checks` failed differently from one that died at `implement`.
grep -E "T[0-9]+ (started|edits_parsed|edits_rejected|applied|checks_|review_|committed|no_diff)" \
  $R/run-*.log | tail -30

# Queue state.
uv run python -c "
import sqlite3; c=sqlite3.connect('$R/queue.sqlite')
for r in c.execute('select item_id,state,attempts,disposition,reason_kind,substr(coalesce(last_error,\"\"),1,70) from work order by cast(substr(item_id,2) as int)'): print(r)"

# What the models actually said (#190).
python3 -c "
import json
for l in open('$R/events.jsonl'):
    e=json.loads(l)
    if e.get('outcome')=='ok' and e.get('answer'):
        print(e['model'], e['answer_chars'], 'redacted' if e['answer_redacted'] else '')"

# Gateway health, per model, from traffic already made (#192).
grep -c "fell back" $R/events.jsonl
```

**The caveat that matters (#209): stored answers are redacted on the way into
the store.** Redaction is applied in `store.append` and `audit.append` and
never to prompts, so the model was given the real file — but text containing
`password: "…"`, which rdpapp's fixtures are full of, is rewritten before it is
recorded. **Do not diff a stored answer against a file and conclude the model
was wrong.** The `answer_redacted` flag tells you it happened; it does not tell
you where or how much.

Symptoms and what they mean:

| symptom | meaning |
|---|---|
| `edits_rejected … does not occur` | the model named text that is not there. Since #216 this is genuinely the model, not a stale worktree; the error now quotes the file back. |
| `review_rejected` | the gate working. Read the objection — on T1 it was correct and the brief was sharpened in response. |
| `LimitsExceeded` (loop) | ran out of turns. Check the refusal count first: a guard false positive used to consume ~38% of them (#217). |
| 429 / 503 storms | the gateway, not the harness. `--implementer a,b,c` chains past it; `survey` could not until #193. |
| nothing claimed, queue full | was a real deadlock (#218) — the claim scan stopped after one page. Fixed; a recurrence is a regression worth reporting. |

### 5.6 Cleaning up an attempt

```bash
rm -rf ~/Working/Active/.harness-runs/rdpapp-m2   # queue, events, logs, cargo cache
cd ~/Working/Active/rdpapp
git worktree list                                 # remove any under .harness-work/
git branch -D $(git branch --list 'harness/t*' 'adapter/*' 'spike/*' | tr -d ' *+')
```

`harness/m2-base` is the base and must survive. **The `harness/r1`–`harness/r7`
and `harness/base*` branches are from the ABANDONED 2026-08-04 attempt** — the
one based 121 commits behind — and are not this work. Do not read them as
evidence and do not build on them.

---

## 6. What is proven, observed and tested

| claim | word | how to check it |
|---|---|---|
| Queue and leases, dependency graph, holds, attempts, budgets, outcome taxonomy, patch-apply ladder, checks gate, reviewer gate, API/OpenAPI contract, redaction on the only two write paths, command guard, first-run/demo path | **tested** | `uv run pytest` |
| The core stays generic — no workload-specific paths, numbers or adapter imports | **tested** | `tests/test_generic.py` (`EXECUTION_PATH` is the authoritative list) |
| The store has no UPDATE and no DELETE | **tested** | the source-level assertion in the store tests |
| The four rdpapp-derived defects: edit-block rendering, stale worktree, guard false positives, claim-scan page deadlock | **tested** | regression tests landed with #216, #217, #218 |
| A metadata-selected multi-turn implementer can inspect, edit, run feedback checks, create new files, and then pass through the harness's authoritative checks, attempt record and reviewer with item-scoped events and budgets | **tested** | `tests/test_role_runners.py`, `tests/test_role_runner_e2e.py`, and the adapter regressions in `tests/test_agent_loop_e2e.py` |
| One plan branch yields exactly one pull request: a correction updates it, an unchanged head touches no remote, an existing PR is adopted, a foreign push is refused, and nothing is merged | **tested** | `tests/test_plan_publication.py` — against a local bare remote and a fake pull-request client, never GitHub |
| A fleet publishes that one pull request only once the plan has stopped moving, pushes no item branch, and updates the same PR for a later correction | **tested** | `tests/test_plan_integration.py::test_fleet_publishes_one_plan_pr_only_when_the_plan_is_finished` |
| An installed review source gives reviews and review comments distinct immutable identities and decides disposition without a model, defaulting unmarked prose to a hold | **tested** | `tests/test_github_pr_review_source.py` — `gh` is injected; no real pull request has been polled |
| The service runs and is deployed inside AIDevEnv | **observed** | no preserved artefacts |
| An earlier supervised NGMS attempt and later direct calls exercised real agents and providers | **observed** | [`evidence/2026-08-03-04-ngms-first-sustained-run-v1.md`](evidence/2026-08-03-04-ngms-first-sustained-run-v1.md) — lacks a common run ID, complete configuration, checksums and a comparable follow-up |
| Four executor passes against rdpapp delivered nothing, and why each failed | **observed** | [`evidence/2026-08-05-06-rdpapp-m2-status.md`](evidence/2026-08-05-06-rdpapp-m2-status.md); the pass 3–4 attribution is hindsight and has not been confirmed by re-running against the fix |
| A loop reached `cargo test` green on rdpapp in 31 turns | **observed** | one item, once, through a standalone script — never through the harness, and it cheated in a way caught by hand rather than by a gate |
| Claw Bay answered on 8 of 42 models on 2026-08-05, all one family | **observed** | a sweep on one day; not preserved |
| Delivery rate | **neither** | no item has ever been delivered |
| Cost per merged item | **neither** | spend is not recorded per item; `pricing` has never been checked against a multi-turn role |
| Unattended reliability | **neither** | #33, #44, #51 have not run |
| Second-repository portability | **neither** | one repository has been attempted |
| Whether a read-only reviewer stays honest | **neither** | #226 is unbuilt; #84 is the experiment |
| Whether the reviewer's verdicts in this window are trustworthy | **not known** | every verdict recorded was same-vendor, because no second vendor was reachable, and nothing attaches that caveat to the verdicts themselves |

Nothing is in the **proven** column. That is not modesty; it is the definition
— nothing about live behaviour has been measured against a stated criterion
with a published denominator.

"No failures observed" is not the same as "the requirement was exercised".
