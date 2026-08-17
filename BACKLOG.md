# Minimal local product implementation backlog

**Prepared:** 2026-08-09

**Target:** [`minimal.md`](minimal.md)

**Current state:** [`docs/STATUS.md`](docs/STATUS.md)

**Authoring example:** [`examples/PLAN.md`](examples/PLAN.md)

The repository has no root `plan.md`; the review therefore treats
`examples/PLAN.md`—the sample named by `AGENTS.md`—as the requested plan
document.

This is the implementation backlog for closing the gap between the current
pre-alpha repository and the minimal generic local product. It replaces the
short sequence in [`nextsteps.md`](nextsteps.md) with work items that are ready
for implementation.

“Exploration complete” has a precise meaning here: the product decision,
boundary, current footing, expected interfaces, failure behaviour, tests, and
exit evidence are stated for every item. It does **not** mean the implementation
or live evidence exists. Items that require a real daemon, model, or application
are marked evidence-bound; their software prerequisites are still explicit.

## 1. Review findings

The three reviewed documents agree on the important direction:

- the product is generic and local-only at its minimum boundary;
- a user writes a complete plan from a template;
- deterministic validation rejects an incomplete plan in one response;
- optional questions happen only after minimum validation and are returned as
  one batch;
- toolchains, dependencies, services, mounts, network, and local delivery are
  project declarations, not Rainmon or harness-core assumptions;
- accepted work is integrated through local Git, then built, deployed, checked,
  and cleaned up locally;
- remote issues, branches, pull requests, hosted CI/CD, and non-local deployment
  are extensions, not prerequisites.

The review also found four details that needed settling before implementation:

1. [`minimal.md`](minimal.md) showed an illustrative manifest but did not yet
   select its exact authoring syntax.
2. [`examples/PLAN.md`](examples/PLAN.md) deliberately omitted that manifest
   because the validator does not exist; it is compatible with the current item
   parser, but is not a valid target-plan fixture yet.
3. The current Docker image and mount settings are deployment-wide. Admission
   cannot honestly claim a project environment until those values are versioned
   with the project plan.
4. The repository has local integration readiness hidden inside remote
   publication code, while it has no product-under-development delivery journal
   or topology owner.

The decisions below close those exploration gaps.

## 2. Settled cross-cutting decisions

These decisions apply to every backlog item and are not reopened inside an
implementation item.

### 2.1 Plan format

- A target plan is Markdown containing **exactly one** fenced `harness` block.
- The block contains TOML. Python 3.12's standard-library `tomllib` parses it;
  core gains no YAML dependency.
- `version = 1` is mandatory. Unknown versions and unknown fields are errors,
  not ignored future configuration.
- Prose remains authoritative for human intent. The TOML manifest is
  authoritative for values the harness executes or persists as configuration.
- Work items remain Markdown headings parsed by the existing item parser. The
  manifest does not duplicate item titles, briefs, or dependencies.
- Every executable command is a non-empty array of non-empty strings. No shell
  fragment is inferred from prose.

### 2.2 Required Markdown shape

The exact level-two headings from `minimal.md` are required once each,
case-insensitively after whitespace normalisation. Order is recommended but not
load-bearing. Duplicate required headings are errors.

Each work item uses the existing `### W1: Title` family of headings and must
contain:

- `deliverable: code` or `deliverable: findings` using the existing metadata;
- a non-empty `**Deliverable:**` observable outcome;
- a non-empty `**Acceptance:**` list;
- explicit dependency intent through `depends on: none`, one or more existing
  dependency tokens, or a fenced `dependencies` graph plus `depends on: none`
  for items with no incoming edge. The v1 parser treats the exact token `none`
  as the empty set; omission is an error because “independent” and “forgot to
  state dependencies” are not distinguishable.

For the local minimum, required dependency targets may be local work, a human
decision retained by the harness, or another already admitted local harness
project/revision. A required `external:` resolver target would make remote or
third-party state a prerequisite, so target-mode admission rejects it. Advisory
external references remain evidence only; optional remote workflow modes may
continue to resolve required external dependencies outside this contract.

`Not applicable: <reason>` is the only way to satisfy a required prose section
or cross-cutting field that does not apply. An empty reason is an error.

### 2.3 Version-one manifest boundary

The version-one manifest owns these typed categories:

```toml
version = 1

[project]
key = "widgets"
name = "Widget service"

[repository]
base_ref = "main"
integration_ref = "harness/widgets"
# finalise_ref = "main" # omit unless a reviewed local fast-forward is wanted

[agents]
role_runner = "agent-loop"
required_roles = ["implementer", "reviewer"]
max_workers = 2
max_attempts = 5
max_item_seconds = 3600
max_item_spend_usd = 0.0 # zero means unlimited, preserving the current default
max_hold_seconds = 21600

[execution]
backend = "docker"
network = "none" # none | project
toolchains = ["Python 3.13"]
system_packages = []

[execution.image]
strategy = "existing" # existing | build
reference = "example/runner@sha256:<digest>"
# pull = false
# strategy = "build" instead uses source + context below
# source = "containerfile" # containerfile | generated
# dockerfile = ".harness/Containerfile"
# context = "."
# build_network = "none" # none | egress; egress is reviewed preparation only
# A generated source uses immutable_base, workdir, copy, setup, and user:
# immutable_base = "example/base@sha256:<digest>"
# workdir = "/workspace"
# copy = ["pyproject.toml", "uv.lock"]
# setup = [["uv", "sync", "--frozen"]]
# user = "1000:1000"

[execution.limits]
command_timeout_seconds = 300
memory = "2g"
cpus = "2"
pids = 512
user = "1000:1000"
rootfs_read_only = true
tmpfs_size = "512m"

[[execution.mounts]]
source = "./.cache"
target = "/opt/project-cache"
writable = false

[[execution.probes]]
name = "python"
command = ["python", "--version"]
expect_regex = "^Python 3\\.13"

[[execution.secrets]]
name = "PRIVATE_INDEX_TOKEN"
source = "environment"
scope = "provisioning" # provisioning | checks | delivery

[execution.config]
# Backend-specific, validated by the explicitly selected installed backend.

[checks]
item = [["python", "-m", "pytest", "-q"]]
integration = [["python", "-m", "pytest", "-q"]]

[local_delivery]
backend = "local-process"
build = [["python", "-m", "build"]]
build_context = "runner" # runner | host
readiness = [["python", "scripts/readiness.py"]]
readiness_context = "host" # host | runner
acceptance = [["python", "-m", "pytest", "-q", "acceptance"]]
acceptance_context = "host" # host | runner
required_host_tools = ["python"]
command_timeout_seconds = 900
readiness_timeout_seconds = 120
readiness_interval_seconds = 2
teardown_timeout_seconds = 30

[local_delivery.config]
# Backend-specific start and ownership values.
```

Settled interpretations:

- The repository worktree path is supplied at admission. It is deliberately
  not embedded in a reusable plan.
- `base_ref` and `integration_ref` are local refs. Validation uses
  `git check-ref-format`; admission resolves the exact base SHA without fetch.
- The admitted base SHA is immutable for that revision. Movement of `base_ref`
  after admission neither rebases nor refreshes work silently. A later plan
  revision may bind a new base SHA and explicitly invoke the existing durable
  replay/re-gate machinery before work resumes.
- `integration_ref` is a stable project-owned local working ref and may not be
  the base/finalisation ref or collide with an unowned ref. Item refs are
  derived under a harness-owned project/revision namespace, so identical item
  IDs in two projects sharing a repository cannot overwrite each other.
- Optional `finalise_ref` merely enables the separately reviewed fast-forward
  action in L5. Omitting it leaves the accepted integration ref as the product.
- `toolchains` and `system_packages` are declarations for review. `probes` are
  the generic executable proof that the resulting environment has them.
- `execution.config` and `local_delivery.config` are opaque to core but not
  unvalidated: the selected installed adapter validates them and returns the
  same diagnostic type as core.
- `none` and `project` are the only v1 item-runtime network policies.
  `project` means the isolated internal network owned by the execution backend
  for the revision's supporting services. Unrestricted bridge/host networking
  is not silently translated into either.
- Model transport stays in the controller. A runner container does not receive
  model credentials merely because agents use a remote model.
- Authoritative item and integration checks execute in the pinned execution
  environment, against the same candidate tree the implementer changed. Final
  delivery build/readiness/acceptance arrays each declare `runner` or `host`
  context. Runner context reuses E6 at the detached accepted tree. Host context
  uses direct argv on the harness host and must name its programs in
  `required_host_tools`; preflight proves them. Delivery adapters do not choose
  or silently change a command's context.
- The image strategy accepts an immutable local image or builds a reviewed
  repository-owned Containerfile. Core does not invent package-manager commands
  from toolchain prose.
- `deploy` and `teardown` are not arbitrary core command lists. The selected
  delivery adapter owns resource creation, identity, inspection, recovery, and
  cleanup. Build/readiness/acceptance remain guarded argv commands.

### 2.4 Admission and revision rules

- Validation is deterministic, model-free, read-only, and returns all findings.
- Optional semantic review is a separate, explicit, spend-bearing action after
  deterministic success. It uses a new `plan_reviewer` role and never changes
  the item-review prompt; D9 therefore remains untouched.
- Questions are returned as one report. There is no question-by-question plan
  completion loop. Blocking questions require a revised plan; advisory ones are
  retained with the admitted revision.
- Apply is bound to the exact plan-byte digest, exact local repository identity,
  exact base SHA, effective adapter versions, and preview digest.
- Project, immutable plan revision, current-plan pointer, work-item projection,
  and dependency graph change in one `BEGIN IMMEDIATE` SQLite transaction.
- Reapplying the same accepted digest is an idempotent no-op.
- Omitting a previously admitted item is not permission to drop it. A revision
  must explicitly name removed item IDs and a reason in its admission request.
  Active/held items prevent revision; completed items remain historical; a
  never-started removed item becomes inactive with a recorded reason.
- Admission never starts workers. Every admitted project is stopped.

### 2.5 Execution and delivery boundaries

- Effective execution configuration is persisted per plan revision. Global
  settings may seed a preview but are never the hidden source of truth.
- Model route credentials and endpoints remain deployment/operator
  configuration. A revision binds route requirements and the exact effective
  route snapshot used for preview/run evidence; it does not put credential
  values or necessarily portable endpoints in a reusable plan.
- Runner preparation produces an immutable image digest/profile digest before
  a worker can claim.
- Profile/image/service preparation is explicit local operator infrastructure
  work and does not count as project build/deploy. The no-CI/CD boundary means
  the harness neither invokes hosted pipelines nor hides these local effects
  behind one; it does not mean a useful runner must already exist by magic.
- Item execution continues through the existing attempt, check, checkpoint,
  reviewer, and plan-promotion gates. Those gates are not weakened or copied.
- Whole-plan delivery is a separate fixed lifecycle with its own durable
  journal. It does not add stages to `attempts.STAGES` and is not a workflow
  engine.
- Fixed delivery stages are `prepared`, `built`, `deployed`, `ready`,
  `accepted`, and `torn_down`. Recovery is an outcome on an interrupted run,
  not a user-registerable stage.
- Delivery occurs from a disposable detached worktree at the exact accepted
  integration SHA, never from the developer's checkout.
- Delivery starts only through an explicit reviewed action after local plan
  readiness. Admission authorises the declared commands; the delivery action
  confirms the exact accepted SHA and effective profile that will run.
- Teardown is attempted after every successful deploy, including readiness or
  acceptance failure. A teardown failure cannot turn failed acceptance into
  success or erase the earlier answer.
- A successful delivery marks the integration SHA locally accepted. An
  optional, separately reviewed finalisation may fast-forward a named local ref
  with compare-and-swap. No operation pushes.

### 2.6 Extension and evidence rules

- Execution and delivery adapters are selected by name through installed
  metadata. Core does not import or contain dotted paths to adapters.
- Delivery adapters implement resource ownership; they do not register new
  gate types or outcomes. D8 remains open and irrelevant to this backlog.
- New core modules on admission/execution/delivery paths are added to
  `tests/test_generic.py::EXECUTION_PATH` in the same change that introduces
  them.
- Every durable free-text payload passes existing redaction before its first
  write. A plan that contains a known or credential-shaped value is rejected
  before its exact text is persisted; secret names alone are valid.
- Large command output is a bounded, checksummed artifact. Events retain the
  outcome, command, bounds, checksum, size, and artifact reference rather than
  unbounded output.

## 3. Milestones and dependency spine

| Milestone | Outcome | Backlog items |
|---|---|---|
| M1 | A complete plan can be validated without a conversation. | P1, E0, L1–L2, P2–P4 |
| M2 | A valid plan can be reviewed and admitted atomically. | A1–A5 |
| M3 | Each admitted revision has a proved, pinned execution environment. | E1–E6 |
| M4 | The existing item and integration path is wholly local and live-proven. | I1–I3 |
| M5 | An accepted integration commit can be delivered and cleaned up locally. | L2–L6 |
| M6 | Evidence and two-project proof satisfy `minimal.md`. | V1–V5 |

The critical path is:

```text
P1 -> E0/L1 -> P2 -> P3 -> A2 -> A3 -> E1 -> E3 -> E5 -> E6 -> I3
                           \                     \
                            A1 -------------------+-> L4 -> L5 -> V2 -> V3 -> V4 -> V5
                                       E4 -> L2/L3 /
```

The complete machine-readable edge list, using the current parser's arrow
direction (“left is prerequisite; right waits”), is:

```dependencies
P1 -> E0
P1 -> L1
P1 -> P2
E0 -> P2
L1 -> P2
P2 -> P3
P2 -> P4
L2 -> P4
P1 -> A1
P2 -> A2
P3 -> A2
A1 -> A3
A2 -> A3
A3 -> A4
P3 -> A5
A3 -> A5
A4 -> A5
A1 -> E1
P1 -> E1
E0 -> E1
E1 -> E2
A5 -> E2
E1 -> E3
E3 -> E2
E1 -> E4
E3 -> E4
E0 -> E4
E2 -> E5
E3 -> E5
E4 -> E5
A5 -> E5
E1 -> E6
E5 -> E6
A5 -> I1
E6 -> I1
A4 -> I2
I1 -> I2
I1 -> I3
I2 -> I3
E6 -> I3
L1 -> L2
L1 -> L3
E3 -> L3
I2 -> L4
E6 -> L4
L1 -> L4
E3 -> L4
L4 -> L5
L4 -> L6
L5 -> L6
P3 -> V1
A5 -> V1
E6 -> V1
I2 -> V1
L6 -> V1
I3 -> V2
L5 -> V2
L6 -> V2
V1 -> V2
L2 -> V2
V2 -> V3
L3 -> V3
V2 -> V4
V3 -> V4
V1 -> V5
V2 -> V5
V3 -> V5
V4 -> V5
```

## 4. Plan contract and validation

### P1: Implement the versioned plan-contract domain model

**State:** ready; exploration complete.

**Outcome:** Core has typed, immutable Python models for the v1 manifest,
required prose sections, item completion fields, commands, mounts, probes,
secret references, execution profile, and local delivery profile.

**Current footing:** `plan.py` owns item parsing and dependency diagnostics;
`schemas.py::ProjectSpec` owns a smaller runtime configuration; TOML is not
parsed. Commands currently cross several boundaries as shell-like strings and
are split later. The current parser treats `depends on: none` as a dependency
named `none`; v1 must special-case the exact token as an explicit empty set.

The manifest also separates reusable repository intent from operator-owned
model routing. `[agents].required_roles` and `role_runner` say which
capabilities a project needs. Endpoint URLs, model IDs, route presets, fallback
chains, price metadata, and credential values stay in the deployment/project
route map already owned by `ProjectSpec.roles` and the controller. Admission
resolves and binds their safe effective snapshot; the plan never embeds a key.

**Implementation decision:** Add `plan_contract.py`. It parses only the fenced
manifest and required section structure, then composes the existing
`ParsedPlan`; it does not replace `plan.py`. Use frozen dataclasses internally
and Pydantic response models only at the API boundary. Preserve declared
commands as `tuple[str, ...]` from parse onward. The agent role runner may still
accept screened shell text as its interactive tool protocol; that does not
permit a plan/check/lifecycle command to be converted back into shell text.
Reject unknown keys at every typed level. Pass `execution.config` and
`local_delivery.config` as immutable JSON-compatible mappings after `tomllib`
conversion.

**Primary surfaces:** `src/agent_harness/plan_contract.py`, `plan.py`,
`schemas.py`, `tests/test_plan_contract.py`, `tests/test_generic.py`.

**Required tests:** one complete parse; every missing/duplicate section;
multiple/no manifest fences; bad TOML with line/column; unknown version/key;
empty argv element; unsafe/relative mount target; invalid secret scope; invalid
ref spelling; `N/A` with and without reason; code/findings items; manifest
round-trip to a canonical JSON representation; declared commands stay argv at
every typed boundary; exact `depends on: none` becomes no edge while omission
is rejected. Tests use no model, network, or filesystem outside a temporary
plan.

**Acceptance evidence:** the example fixture plus a second materially different
fixture parse to typed objects; no adapter is imported during core parsing; the
old item-parser tests remain green.

**Depends on:** none.

**Non-goals:** executing commands, resolving a Git ref, selecting a model,
validating backend-specific config, or deciding whether prose architecture is
good.

### P2: Build the aggregate deterministic validator

**State:** ready after P1, E0, and L1; exploration complete.

**Outcome:** One validation run reports every independently discoverable plan
problem and returns non-zero without writing anything.

**Current footing:** `plan_service._validated_plan` raises one
`PlanSyncConflict` for selected parser/graph categories. `PlanParseResult`
exposes several useful findings but has no stable codes, spans, remediation, or
project-level validation.

**Implementation decision:** Add `plan_validation.py` with a `Finding` value:
`code`, `severity`, `message`, `remediation`, `path`, `line`, `column`, and
optional TOML/section/item pointer. Code families are fixed:

- `PLAN-Dxxx` document/section findings;
- `PLAN-Mxxx` manifest/schema findings;
- `PLAN-Wxxx` work-item findings;
- `PLAN-Gxxx` dependency-graph findings;
- `PLAN-Pxxx` policy/secret/path findings;
- `PLAN-Axxx` admission-context findings.

Parsing failures local to one area do not stop checks on areas already parsed.
Errors block; warnings and information never silently become errors. Findings
sort by source position then code, making CLI and JSON stable. Existing
dependency diagnostics are adapted, not reimplemented.

Backend-specific config validation runs only when an explicitly selected
adapter is installed and compatible. Unknown/unloadable adapters are ordinary
blocking findings. The adapter receives only its config and plan context; it
does not mutate or probe the host at this stage.

In target local mode, dependency validation also rejects required `external:`
edges and unresolved cross-project edges. A cross-project edge binds the exact
current local plan revision it observed so a later revision cannot silently
change what satisfied the dependency. Human decisions must name their owner and
blocking effect in the plan; admission does not guess their answer.

**Primary surfaces:** `plan_validation.py`, `plan_service.py`, `schemas.py`,
`execution_environments.py`, new `delivery_backends.py`, tests for each.

**Required tests:** a deliberately broken plan producing at least one finding
from every family in one run; stable ordering/codes; machine-readable JSON;
unknown adapter; broken adapter; adapter validation finding; secret-shaped
content rejection; required/advisory external dependency; bound/unresolved
local cross-project dependency; no project/queue/event row changes; a
regression proving the validator never calls a model.

**Acceptance evidence:** `examples/PLAN.md` with its completed v1 block validates
cleanly; mutation tests for every required field demonstrate fail-closed
coverage.

**Depends on:** P1, E0, L1.

**Non-goals:** semantic architecture review, environment readiness, image
pull/build, Git mutation, or queue creation.

### P3: Expose validation consistently through CLI, API, and GUI

**State:** ready after P2; exploration complete.

**Outcome:** A user can validate a plan cheaply in their preferred interface
and receives the same findings everywhere.

**Current footing:** top-level `agent-harness plan PATH --dry-run` parses items
and is framed around GitHub sync; `/api/plan/parse` returns the old parse view;
the GUI's plan page previews remote issue changes.

**Implementation decision:** Introduce the nested CLI contract
`agent-harness plan validate PLAN.md --work REPO [--json]`. Move existing
remote issue behaviour to `agent-harness plan publish-issues` with a documented
pre-alpha compatibility alias for one release. Add
`POST /api/plans/validate` returning a typed `PlanValidationResult`; the request
names a host-local plan and worktree path. Add a non-mutating GUI validation
form and findings table with code, location, severity, and remediation.

All surfaces call one application service. HTTP 200 carries a completed report
even when invalid; transport/input failures use 4xx. CLI exit is 0 when valid,
2 when findings contain errors, and 1 for an operational failure. Validation
never creates a browser review session because it has no effect to approve.

**Primary surfaces:** `__main__.py`, `api.py`, `ui.py`, `schemas.py`,
`templates/plans.html`, a findings partial, `plan_service.py`, CLI/API/UI tests.

**Required tests:** byte-identical finding payloads across service callers;
OpenAPI descriptions for every field; docs endpoints unauthenticated but data
authenticated; GUI escaping of malicious plan prose; CLI exits/output;
backward-compatible remote alias warning; no external client construction.

**Acceptance evidence:** the intentionally broken acceptance fixture prints one
complete report in text and JSON, and no question is asked.

**Depends on:** P2.

**Non-goals:** admission, project creation, semantic review, or remote sync.

### P4: Make every plan authoring path emit the target shape

**State:** ready after P2; exploration complete.

**Outcome:** The copyable example, deterministic demo, optional inception, and
survey output all target the same plan contract.

**Current footing:** `examples/PLAN.md` has the required prose but intentionally
lacks the manifest. `inception.render_plan` and `survey` emit the old Goal/Not
doing/Work shape. The current uncommitted exploration defaults recommend
private hosted repositories, CI runners, Compose, orchestration, publication,
and third-party gate policy.

**Implementation decision:** Complete `examples/PLAN.md` with a safe manifest
using the installed Docker execution backend and shipped local-process delivery
backend. Validation does not pull the example image or start its process;
demo/acceptance tests substitute deterministic metadata adapters while proving
the same contract. Add
`examples/PLAN-TEMPLATE.md` as a placeholder-bearing authoring copy that is
expected to fail until filled. Refactor `render_plan` to emit every required
section and an explicit manifest proposal. Inception/survey remain optional
drafting tools and must run the deterministic validator before returning
`usable=true`. They may propose unresolved values as placeholders, but must
then report the validator's rejection; they cannot invent them.

Remove hosted repository/CI/orchestrator/topology choices from generic
exploration defaults. Keep generic evidence, safety, local integration, and
acceptance principles. Do not add a third-party gate registry or policy while
D8 is open.

**Primary surfaces:** `examples/PLAN.md`, new `examples/PLAN-TEMPLATE.md`,
`demo.py`, `inception.py`, `survey.py`, `exploration_defaults.py`, usage docs,
and their tests.

**Required tests:** generated plans contain every required section and one
manifest; no hosted provider/topology default; unresolved values make a draft
invalid rather than guessed; current item IDs/dependencies survive generation;
manual and generated plans take the same validator path.

**Acceptance evidence:** a fresh user can copy the template, see all deliberate
placeholder findings at once, fill it, and reach a clean deterministic report.

**Depends on:** P2, L2.

**Non-goals:** making inception mandatory, automatically answering questions,
creating a repo, or admitting work.

## 5. Reviewed and atomic admission

### A1: Persist immutable plan revisions and current membership

**State:** ready after P1; exploration complete.

**Outcome:** The queue can retain exactly what was admitted, which project/base
it described, and which work items belong to the current revision.

**Current footing:** `projects` holds mutable `plan_path`/`plan_branch`; `plans`
holds one mutable local integration projection and a digest; `work` is keyed by
project/item; no exact plan text or immutable admitted configuration survives a
file edit. Git item branches, unlike queue rows, currently default to the global
`harness/<item-id>` namespace and can collide when two projects share a repo.

**Implementation decision:** Add, through idempotent queue migrations:

- `plan_revisions(project_id, revision, plan_digest, manifest_digest,
  plan_markdown, manifest_json, repository_identity, initial_base_sha,
  integration_ref, finalise_ref, adapter_versions_json, admitted_by,
  admitted_at)` with an immutable unique
  `(project_id, revision)` and `(project_id, plan_digest)`;
- `plan_revision_items(project_id, revision, item_id, ordinal, title, brief,
  deliverable, depends_on_json, acceptance_json, active)` as the immutable
  item snapshot;
- `plan_questions` for advisory/blocking semantic-review output;
- `projects.current_plan_revision` as the mutable pointer.

Keep `plans` as the mutable Git integration projection; add its revision link
rather than making immutable plan content mutable. Add membership-aware queue
queries so claims and whole-plan readiness consider only current active items.
Historical work rows and item outcomes remain inspectable.

Exact plan Markdown is stored only after P2 confirms no known/credential-shaped
secret. Its byte digest is computed before parsing. The manifest also gets a
canonical semantic digest for idempotency across irrelevant TOML formatting.

**Primary surfaces:** `work.py`, new `plan_revisions.py` or a tightly scoped
queue component, `graph.py`, `query_service.py`, migration tests.

**Required tests:** upgrade from every repository fixture schema; immutable old
revision after a new one; same bytes idempotent; different bytes/same semantic
manifest remain distinct source revisions; current membership controls claims;
backup/reopen; secret-shaped plan never reaches the table.

**Acceptance evidence:** editing the source `PLAN.md` after admission changes no
stored revision or queued brief, and the API can return the accepted digest and
snapshot after restart.

**Depends on:** P1.

**Non-goals:** running validation, creating Git refs, starting work, or making
the audit store mutable.

### A2: Implement admission preview and batched semantic review

**State:** ready after P2 and P3; exploration complete.

**Outcome:** A valid plan produces one reviewable proposal bound to exact local
facts; optional semantic review can return all material questions without
manufacturing plan content.

**Current footing:** browser plan sync already binds apply to plan digest and a
remote preview; browser project configuration binds updates to a version.
`inception` has question objects but is an authoring conversation, not
admission. There is no post-minimum plan reviewer.

**Implementation decision:** Add `admission_service.preview`. It:

1. runs P2 validation;
2. resolves canonical repository/worktree identity and exact `base_ref` SHA
   without fetch or mutation;
3. calculates effective project/profile/delivery configuration and adapter
   versions;
4. compares an existing current revision and reports explicit additions,
   changes, and omissions;
5. optionally calls the new `plan_reviewer` role once and parses one JSON array
   of questions with section/item pointer, blocking/advisory severity, and why;
6. returns a canonical proposal digest covering all of the above.

Semantic review is off unless requested and routed. It cannot turn a
deterministic error into a question. A malformed reviewer response fails that
optional review closed but does not corrupt the deterministic report. Blocking
questions prevent apply; advisory questions are shown and later persisted.

**Primary surfaces:** new `admission_service.py`, `model_client.py` role usage,
`schemas.py`, browser review payloads, unit tests with injected Git/model probes.

**Required tests:** no model by default; exactly one model call when requested;
all questions returned together; invalid plan makes zero calls; stale plan/base/
adapter facts alter proposal digest; reviewer output cannot add commands;
redaction; D9 item-review prompt byte-for-byte unchanged.

**Acceptance evidence:** a valid plan with three semantic ambiguities produces
one report containing all three and no durable project/queue state.

**Depends on:** P2, P3.

**Non-goals:** interactive question answering, editing the plan, judging item
code, or probing whether the execution host is ready.

### A3: Apply admission atomically and idempotently

**State:** ready after A1 and A2; exploration complete.

**Outcome:** One approved proposal creates exactly one stopped project, plan
revision, active item membership, work projection, and typed graph—or nothing.

**Current footing:** `configure_project`, `WorkQueue.add`, graph edge writes,
and `PlanCoordinator.ensure` are separate operations. `WorkQueue.add` writes
item-by-item in autocommit mode, so a mid-load failure can leave a partial plan.

**Implementation decision:** Add `admission_service.apply` and one queue method
that opens `BEGIN IMMEDIATE`, rechecks the expected current revision, writes the
project/revision/items/work/graph/current pointer, and commits. Refactor internal
queue helpers to accept an existing connection; do not coordinate this by
calling public autocommit methods in a loop.

The apply request carries proposal digest, plan digest, expected base SHA,
expected current revision, explicit removed-item decisions, and operator. Apply
reruns deterministic validation and local Git identity checks before opening the
transaction. A mismatch returns a typed stale-proposal conflict and writes
nothing. Duplicate idempotent apply returns the original revision.

Local plan-ref creation is deliberately outside the database transaction. It
occurs idempotently during preflight/start from the stored revision/base SHA;
admission itself mutates no Git state and starts no fleet.

Before the transaction, apply also revalidates that `integration_ref` is
harness-owned or absent and does not equal the base/finalisation ref. Item refs
are derived as `refs/heads/harness/<project-key>/r<revision>/<item-id>` (with
validated/normalised components), not the current global `harness/<item-id>`.
This makes `(project_id, item_id)` queue isolation true in Git as well as
SQLite. Existing legacy refs remain readable and are migrated/replayed only by
an explicit revision action; admission never renames them silently.

**Primary surfaces:** `admission_service.py`, `work.py`, `graph.py`,
`project_service.py`, API schemas, atomicity tests with injected failure points.

**Required tests:** failure after each logical insert rolls back all tables;
concurrent apply yields one revision; replay returns same revision; graph and
work snapshot agree at commit; project stopped; no Git ref/session/model/remote
write; stale base and changed plan refused; queue reopen retains all facts;
integration/base/finalisation ref collision; two projects with item `W1` in one
repository derive distinct refs.

**Acceptance evidence:** a forced exception immediately before commit leaves
the database byte-for-byte logically unchanged; a successful retry creates all
expected rows once.

**Depends on:** A1, A2.

**Non-goals:** image preparation, preflight readiness, claiming work, or remote
issue creation.

### A4: Define and implement safe plan revision semantics

**State:** ready after A3; exploration complete.

**Outcome:** A user can revise an admitted plan without silently dropping,
re-running, or relabelling existing work.

**Current footing:** `WorkQueue.add` refreshes title/brief/dependencies,
preserves done, and revives failed/blocked work when its brief changes. It does
not know current plan membership, acceptance text, or explicit removal.

**Implementation decision:** Revision is allowed only while the project is
stopped and has no claimed or held items. Preview classifies every ID:

- unchanged: retain work state and membership;
- wording/acceptance/dependency changed: snapshot the new spec; pending stays
  pending; failed/blocked revival keeps the existing `revives` rule; done stays
  done only when deliverable and acceptance are unchanged, otherwise preview
  requires explicit `reopen` approval;
- added: create pending work;
- omitted: block apply unless `removed_items` explicitly names ID and reason;
- explicitly removed pending/unstarted: retain history, mark inactive, record
  `removed_by_plan_revision`;
- explicitly removed started/done: retain outcome and make inactive only after
  high-risk explicit approval naming that state.

Dependency edges for the current graph derive only from active current items.
An active item depending on a removed local item is a deterministic error unless
the dependency is changed to an explicit external/decision target. Overrides do
not cross graph revisions.

The base SHA never follows a moving branch implicitly. If a new revision
resolves `base_ref` to a different SHA, preview names the move. Apply persists
the new immutable SHA, and before claims resume the integration coordinator
rebuilds from it and replays retained completed code items through the same
promotion checks. A replay conflict or gate failure blocks the revision with a
durable outcome; it does not discard accepted history or start an agent
silently. Changing `integration_ref` for an existing project is not supported
in v1; use local finalisation or create a new project identity.

**Primary surfaces:** admission diff model, `work.py`, `graph.py`, query/API/UI
projections, revision tests.

**Required tests:** every classification above; changed done acceptance; omitted
item refusal; explicit removal audit; active/held revision refusal; revision
number monotonicity; stale override invalidation; current readiness excludes
inactive history without deleting it; moved base replay success/conflict/gate
failure/crash recovery; no implicit refresh when only the source branch moves;
integration-ref change refusal.

**Acceptance evidence:** revising a four-item plan to add one, rewrite one, and
explicitly remove one produces the reviewed state exactly, while an accidental
omission is rejected.

**Depends on:** A3.

**Non-goals:** automatically deciding delivered equivalence, deleting rows, or
allowing a live plan to move under active workers.

### A5: Publish the admission workflow through CLI, API, and GUI

**State:** implementation in progress; CLI and typed API foundations are implemented, GUI confirmation and exit evidence remain.

**Outcome:** Users have one non-interactive validation/admission path, with an
explicit review step for mutation.

**Current footing:** API/browser project registration and remote plan sync are
separate. The CLI can load a plan directly into the queue during `run` without
the target validation or atomicity.

**Implementation decision:** Add:

- `agent-harness plan admit PLAN.md --work REPO --dry-run
  [--semantic-review]` to print the proposal and digest;
- `agent-harness plan admit ... --approve-digest DIGEST` to apply without an
  interactive prompt;
- `POST /api/plans/admission/preview` and `/apply`;
- `GET /api/projects/{project_id}/plan-revisions` and one revision detail;
- GUI preview/confirmation using the existing opaque browser-review mechanism.

Remove direct `--plan` queue loading from the target `run` path or route it
through admission. Keep a clearly named legacy compatibility path only while
documented. Apply routes require identity, CSRF in browser, exact review token,
and typed conflict responses.

**Primary surfaces:** `__main__.py`, `api.py`, `ui.py`, templates, schemas,
`browser_session.py`, `runtime.py`, `docs/USAGE.md`, surface tests.

**Required tests:** no stdin prompt; invalid plan returns all reasons; semantic
questions batch; preview creates nothing; apply consumes review once; stale
review refused; replay idempotent; OpenAPI complete; browser escaping/auth/CSRF;
legacy direct load cannot bypass target validation.

**Acceptance evidence:** starting from a copied template, a user validates,
reviews, admits, restarts the service, and sees one stopped project with the
exact revision and queue—without GitHub credentials.

**Depends on:** P3, A3, A4.

**Non-goals:** automatically starting work, creating a repository, or pushing a
branch.

## 6. Per-project execution profiles

### E0: Version the execution-backend configuration contract

**State:** ready after P1; exploration complete.

**Outcome:** Deterministic plan validation can ask an installed execution
backend whether its opaque config is structurally valid without probing or
mutating the host.

**Current footing:** `execution_environments.py` discovers factories through
installed metadata, but contract v1 exposes only `check()` and `create(...)`.
`check()` probes runtime availability and `create()` starts an item resource;
neither is a valid operation during read-only plan validation. Environment
settings are separate keyword arguments rather than a backend-owned config.

**Implementation decision:** Define execution-backend contract v2 before P2
uses it. In addition to metadata description and later runtime operations, it
exposes `validate_config(config, context) -> tuple[Finding, ...]` and
`canonicalize_config(config) -> JSON-compatible mapping`. Both are
deterministic and effect-free. The validator owns generic image/mount/network/
limits fields; the backend owns only `[execution.config]`. Contract lookup can
report metadata/API compatibility without calling `check()` or importing any
unselected adapter. E4 adds prepare/inspect/cleanup runtime operations to this
same v2 contract rather than raising the version a second time.

The shipped Docker adapter implements v2 through entry-point metadata. Upgrade
is fail-closed for target revisions; legacy global projects remain readable and
are reported outside the target until explicitly admitted.

**Primary surfaces:** `execution_environments.py`, execution-environment
protocols, Docker adapter, metadata in `pyproject.toml`, fake backend tests,
genericity list.

**Required tests:** config validation makes no subprocess/network call;
canonical output/digest stability; incompatible v1/broken/unknown backend;
unselected backend not imported; adapter exception becomes a typed finding;
Docker generic/runtime fields cannot hide inside opaque config; existing
legacy environment fixtures have an explicit compatibility path.

**Acceptance evidence:** a separately installed fake v2 backend validates a
fixture plan by name with no core edit and no call to its deliberately failing
runtime `check()`.

**Depends on:** P1.

**Non-goals:** preparing an image/service, proving the backend is installed on
the eventual worker host, or adding a topology-specific field to core.

### E1: Persist the effective execution profile per plan revision

**State:** ready after A1 and E0; exploration complete.

**Outcome:** Every claim can name the immutable environment it is meant to use,
without reading deployment-wide image or mount settings.

**Current footing:** `EnvironmentSpec` already describes image, worktree,
mounts, environment names, network, and limits. Docker and host backends exist.
`__main__.py` currently stores `execution_backend` and `execution_image` as
global settings and closes over one mount tuple when it builds all project
executors.

**Implementation decision:** Add an immutable `execution_profiles` record keyed
by project/revision with:

- semantic profile digest;
- backend name, API version, and installed adapter version;
- image strategy and reviewed source reference/recipe;
- prepared immutable image reference and content ID/digest, initially null;
- repository-relative mount declarations resolved at admission;
- secret names/scopes, never values;
- network policy and backend config;
- probes, resource limits, role runner, and required roles;
- preparation state, timestamps, and last typed failure.

The admitted manifest remains the source; this record is its effective,
queryable projection. A source mount in v1 is repository-relative and must not
escape the repository. Host-global dependency paths are not portable plan
facts: an installed backend may expose a named binding in its config, and
admission must show the operator's binding explicitly before it becomes part of
the effective digest.

Add the effective profile to project/revision API views. Keep global settings
only as migration/preview defaults. Once a project is admitted, changing a
global default changes neither its digest nor its executor.

**Primary surfaces:** `work.py` migrations, `plan_revisions.py`,
`execution_environment.py`, `project_service.py`, `schemas.py`, query/API/UI
views, profile persistence tests.

**Required tests:** two revisions retain two profiles; two projects hold
different backends/images/mounts simultaneously; mount traversal/symlink escape
refused; secret value absent from every row/API/event; canonical digest stable;
unknown adapter version change makes a new preview; old global-only projects
remain readable but not target-ready.

**Acceptance evidence:** after restart, preflight can reconstruct the exact
profile for each of two projects without any environment-backend/image/mount
CLI flag.

**Depends on:** A1, P1, E0.

**Non-goals:** building an image, injecting a secret, starting services, or
claiming work.

### E2: Implement reviewable runner-image preparation and pinning

**State:** ready after E1, E3, and A5; exploration complete.

**Outcome:** An admitted profile reaches a locally available immutable runner
image through an explicit, auditable preparation action.

**Current footing:** the Docker backend accepts an image string and records the
container image ID after start. It neither builds an image nor proves the image
before work. Passing a tag can therefore select different bytes later.

**Implementation decision:** Add an `image_preparation` application service
with two v1 paths:

1. **Existing image.** Require a digest-qualified reference or local image ID.
   Preparation may pull only when `pull = true` is present in the reviewed
   manifest. It then inspects and stores the actual content ID/repository
   digest. A mutable tag alone is invalid.
2. **Built image.** Build from a detached worktree at the admitted base SHA.
   Support either a repository-owned Containerfile or a generated recipe.
   A generated recipe has an immutable base image, explicit repository files
   to copy, and JSON-form setup argv arrays. Core renders only generic
   `FROM`, `WORKDIR`, `COPY`, `RUN [argv]`, user, and label instructions; it
   never translates “Python”, “Rust”, or a system-package name into a package
   manager command. The reviewed plan supplies those commands.

The preview shows the source commit, Containerfile or generated text, copied
paths, build args/names, build network (`none` or explicitly `egress`), and
resulting ownership labels. Secret build values use the backend's secret mount
facility and are never rendered into the recipe or image history. The recipe
digest and resulting image content ID are durable. Work starts by content ID,
not by the temporary build tag.

Preparation is a reviewed mutation because it may pull data, run setup
commands, and create local images. It has a per-profile lease and is idempotent:
an already prepared matching digest is inspected and reused. A changed source
or recipe requires a new plan revision/preview.

`system_packages` and `toolchains` remain review/evidence declarations; they do
not generate setup commands. For a repository Containerfile, the validator
requires matching probes but cannot prove how the image installed them. For a
generated recipe, all installation work appears explicitly in the manifest's
`setup` argv arrays and is rendered verbatim as JSON-form `RUN` instructions.

**Primary surfaces:** new `image_preparation.py`; execution-backend contract;
Docker adapter build/inspect functions; API/CLI/GUI review; artifact/event
recording; image tests with an injected backend plus opt-in live Docker tests.

**Required tests:** mutable tag rejection; pull disabled/enabled; exact reviewed
recipe; JSON argv escaping; context path confinement; symlink escape; no secret
in recipe/log/history evidence; failed build remains unprepared; concurrent
prepare builds once; killed build lease recovery; content ID used at create;
live tiny image build behind an explicit environment marker.

**Acceptance evidence:** delete the temporary tag after preparation and prove a
worker still starts from the stored content ID; changing a setup argv invalidates
the profile rather than reusing the image.

**Depends on:** E1, E3, A5.

**Non-goals:** automatically choosing a base image/package manager, publishing
an image, running product deployment, or treating an image scan as a new gate.

### E3: Bind named secrets without persisting their values

**State:** ready after E1; exploration complete.

**Outcome:** A plan can declare which credentials a stage needs, while values
remain outside the plan, operational database, prompts, and evidence.

**Current footing:** route/service secrets are read from known environment
variables and exact values feed the redactor. Execution environments accept a
mapping of environment values, but the project contract does not declare or
scope it and current stores know only a fixed shortlist of secret names.

**Implementation decision:** Add a `SecretResolver` protocol supplied by the
deployment, with a default environment resolver. The plan stores only
`name/source/scope`. At preflight the resolver answers present/absent and returns
an opaque in-memory value only to the stage that owns the scope. Values are
added to one process-wide, thread-safe redaction registry before any
command/model output can be persisted. `EventStore`, `AuditStore`, notification
outbox, model client, and artifact writer all use that shared live callable;
the current startup pattern creates separate frozen `from_environment()`
instances, so merely changing one store would leave another durable sink open.
Registered values remain in the registry for the process lifetime—even after
stage injection ends—because late logs and asynchronous events can still echo
them. They are never enumerable through an API.

Scopes are fixed in v1:

- `provisioning`: image pull/build and dependency provisioning only;
- `checks`: item/integration execution environment only;
- `delivery`: local delivery backend/check context only.

No scope implies another. Secrets are passed by container environment or
backend secret mount as selected by the backend; they never enter template
variables shown to an agent unless an adapter contract explicitly requires a
named value and documents that exposure in preview. Controller model-route
credentials remain controller concerns and are not project secret entries.

**Primary surfaces:** new `secrets.py`, `redaction.py`, service composition in
`__main__.py`, event/audit/notification/model/artifact construction, preflight,
execution and delivery requests, Docker adapter, schemas and tests.

**Required tests:** missing name blocks the owning stage; unrelated missing
secret does not; value absent from SQLite, event/audit JSON, API/OpenAPI
examples, generated recipes, process argv, and error detail; dynamic value is
redacted through every durable sink even when registered after the sinks open;
concurrent registration/redaction; resolver exception is a named preflight
failure; injection scope closes after use while redaction knowledge remains.

**Acceptance evidence:** a fixture secret deliberately printed by a failing
command appears only as `[redacted]`, while the delivery record says which
secret name/scope was used.

**Depends on:** E1.

**Non-goals:** becoming a secret manager, rotating credentials, storing
encrypted values, or injecting controller credentials into agents.

### E4: Add project-scoped supporting services and network preparation

**State:** ready after E1, E3, and E0; exploration complete.

**Outcome:** Test databases, caches, and similar agent-time dependencies can be
declared and owned per project without hardcoding their technology in core.

**Current footing:** the item Docker backend creates one container on `bridge`
or `none`; it has no project service lifecycle and no per-project network.
`supporting_services` currently exists only as target prose.

**Implementation decision:** Raise the execution-backend API version with
explicit operations to validate config, prepare a project-revision runtime,
describe it, create item environments, inspect it, and clean it. Preparation
returns a durable opaque binding containing only safe resource identities. Core
never interprets a service's format.

The shipped Docker adapter supports a repository-owned, admitted Compose file
for **agent-time supporting services**. It:

- validates `docker compose config` against the detached admitted tree;
- derives a unique project name from harness project/revision, never user
  shell text;
- applies harness ownership labels and creates a project-scoped network;
- starts only declared service names, with no host ports unless explicitly
  reviewed;
- joins each item container to that network;
- records health/service identities and cleans only labelled owned resources;
- refuses external volumes, host networking, privileged mode, Docker-socket
  mounts, or unowned resource deletion in v1.

The v1 network policy is `none` or `project`. `project` reaches only the
backend-owned supporting-service network; it is created as internal, so it is
not generic internet egress. Dependency downloads belong in the reviewed image
preparation action. A later egress-capable backend can expose that as explicit
adapter config; core must not claim destination enforcement it cannot provide.

**Primary surfaces:** execution environment protocols and lookup, Docker
adapter, profile persistence, preflight, recovery/reaper, tests and live Docker
fixture.

**Required tests:** adapter compatibility version; service name/config
validation; per-project network separation; no host/privileged/socket escape;
health failure blocks; two projects with same Compose service names do not
collide; killed harness recovery; cleanup refuses an unlabelled resource;
item container resolves its service in an opt-in live test.

**Acceptance evidence:** two concurrent project profiles use isolated same-named
database services and teardown one without affecting the other.

**Depends on:** E1, E3, E0.

**Non-goals:** production service orchestration, arbitrary outbound networking,
sharing mutable services across projects, or adding service-health gate types.

### E5: Make preflight prove the exact admitted profile

**State:** ready after E2–E4; exploration complete.

**Outcome:** Project start is refused before a claim when the pinned environment
cannot run the work or its declared checks.

**Current footing:** preflight checks worker presence, checkout, cleanliness,
base currency, disk, reviewer, role runner, and a deployment-wide execution
backend probe. Missing project checks are currently only a warning. It does not
inspect an admitted profile, pinned image, mounts, secrets, services, or tool
probes.

**Implementation decision:** Extend preflight with blocking checks for:

- current valid/admitted plan revision and unchanged local repository identity;
- integration ref availability/collision safety;
- compatible installed execution and delivery adapters at admitted versions;
- prepared pinned image and recipe/source consistency;
- all mount sources and named bindings;
- all required secret names by stage (without values in detail);
- supporting-service runtime readiness;
- every declared execution probe inside a disposable environment;
- every delivery command context is supported and every host-context program
  is both named in `required_host_tools` and present on the harness host;
- the operational store is available, audit is not degraded, and the bounded
  artifact root is writable with the shared redactor attached; target work may
  not begin if it cannot retain the evidence its contract promises;
- non-empty item and integration checks;
- enough disk for configured worker concurrency and image/build workspace;
- current reviewer route and role runner required by the profile.

Remote Git becomes a non-blocking optional extension whenever the admitted
target is local. Remove `force` as a way to start a target-profile project with
a blocking preflight failure; it would make fail-closed admission ceremonial.
Legacy projects may retain their existing explicit override during migration,
clearly reported as outside the minimum contract.

Preflight stays read-only with respect to project work: image/service
preparation has its own explicit action. Disposable probe containers may be
created only after preparation and must be removed before the response; the
ordinary `/api/readiness` remains a non-mutating cached/capability view and
does not run them.

**Primary surfaces:** `preflight.py`, readiness API projections, runtime
factories, doctor, GUI preflight, schemas and tests.

**Required tests:** each blocker independently; complete all-at-once report;
probe timeout/missing program/version mismatch; no claim/model call; no remote
probe in local mode; project A profile cannot satisfy project B; start and
preflight share one result; cleanup after failed probe; no force bypass on
target projects; degraded audit/unwritable artifact root/missing shared
redactor block before spend.

**Acceptance evidence:** remove one required compiler from the pinned image and
observe preflight refuse before attempt count/model spend changes; rebuild a
valid image and observe the same plan become ready.

**Depends on:** E2, E3, E4, A5.

**Non-goals:** preparing resources implicitly, measuring model quality, or
deciding whether a project's test assertions are sufficient.

### E6: Run authoritative checks inside the admitted execution boundary

**State:** ready after E1 and E5; exploration complete.

**Outcome:** The pinned per-project environment is the environment that answers
the authoritative item and integration checks, not merely the environment in
which the implementation agent edited files.

**Current footing:** `DockerItemEnvironment.run` accepts a command string and
uses `/bin/sh -lc`; the role-runner adapter needs that screened text interface
for an interactive agent. `CheckRunner`, however, always executes argv directly
on the controller host. After a container agent returns a diff, core applies it
to a controller worktree and invokes host checks. Today an image can contain
the promised compiler while the actual gate fails on the host—or the gate can
pass using an undeclared host tool the image does not contain.

**Implementation decision:** Extend the execution-environment contract with a
distinct `run_argv(argv, *, cwd, timeout)` operation implemented with no shell.
Keep the screened role-runner text operation separate and clearly named. Change
`CheckRunner` to receive an injected argv runner; item checks and
promotion-time integration checks use a disposable environment reconstructed
from the admitted profile and mounted over the exact candidate/integration
worktree. The controller's `CommandGuard` screens argv before the backend call,
and the backend also enforces cwd confinement. Fix commands, when enabled,
follow the same runner and existing post-fix review/check invariants.

Host compatibility remains fixture/legacy behaviour and is reported as no OS
boundary. A target Docker profile can never fall back to host execution after
environment creation or command failure. Environment evidence records the
profile/image digest and argv for each check result.

**Primary surfaces:** `execution_environment.py`, `execution_environments.py`,
Docker adapter, `executor.CheckRunner`, `runtime.py`, `plan_integration.py`,
check/evidence schemas, genericity list, tests.

**Required tests:** direct argv including spaces/metacharacters without shell;
missing tool in image while present on host; present in image while absent on
host; item and promotion checks use the same profile; fix/recheck uses it too;
cwd/mount confinement; backend exception is an honest escalation; no fallback;
container lifecycle cleanup and bounded/redacted output; the same runner
supports later delivery commands against a detached accepted tree.

**Acceptance evidence:** a fixture installs its only check program in the
runner image, not on the controller, and both the item gate and promotion
re-gate pass; removing it from the image makes preflight/check fail before
review without host substitution.

**Depends on:** E1, E5.

**Non-goals:** removing the role runner's screened interactive shell protocol,
executing product deployment in the runner image, or weakening `CommandGuard`.

## 7. Complete the local item and integration path

### I1: Make local execution the default target path

**State:** ready after A5 and E6; exploration complete.

**Outcome:** An admitted target project can run with no repository host, remote
issue, push, pull request, hosted check, or remote credential.

**Current footing:** `direct_executor_factory(..., push=False)` already supports
local item branches and `PlanCoordinator` promotion. CLI `--no-push` works.
Several command names, preflight docstrings, API summaries, and normal defaults
still frame a pull request as the definition of done.

**Implementation decision:** Derive delivery mode from the admitted plan:
`local` is the only v1 minimum value and the default. Executor construction for
that mode receives no GitHub client and instantiates no `PlanPublisher`.
Repository-host metadata may remain nullable extension configuration but cannot
affect local readiness or completion.

The normal target command becomes `agent-harness project start ID` (or the
existing API action) after admission/preflight. `run --no-push` remains a
compatibility/diagnostic surface. Ensure item branches, checkpoint commits,
review, and promotion are identical between local and optional publication
modes up to the explicit extension boundary.

**Primary surfaces:** `runtime.py`, `__main__.py`, `preflight.py`, API/UI text,
`plan_publication.py` call boundary, README/USAGE, executor factory tests.

**Required tests:** factories assert no GitHub construction/call; local project
with `repo=None`; remote outage cannot fail local item; optional publication
still works when selected; no push command in local events; reviewer/check/
checkpoint/promotion ordering unchanged.

**Acceptance evidence:** run a multi-item local fixture with the network
disabled after image preparation and observe accepted local branches and no
remote call.

**Depends on:** A5, E6.

**Non-goals:** deleting remote extension code, changing D1 for this repository's
own issue tracker, or weakening the reviewer gate.

### I2: Extract whole-plan local completion from remote publication

**State:** ready after A4 and I1; exploration complete.

**Outcome:** Core can answer whether a plan is ready for local delivery without
importing or constructing a remote publisher.

**Current footing:** `PlanPublisher.readiness` contains useful local counts but
lives in `plan_publication.py`; it does not bind readiness to current plan
revision membership or prove every code item reached the current integration
head.

**Implementation decision:** Add `plan_completion.py` as a pure query/service.
For the current admitted revision it reports:

- active item counts by state and IDs holding completion;
- code items with a successful promotion linked to that revision;
- findings items with a durable accepted findings result;
- exact integration ref/head and whether Git matches the durable projection;
- whether any promotion/refresh is in progress;
- whether the project is stopped with no live workers/claims/holds;
- the plan/profile digests to which the answer applies.

Ready means every active item is done, every code deliverable is represented in
the integrated branch, every findings deliverable is durably accepted, no
integration journal action is unresolved, and the branch has not moved outside
the coordinator. It does not mean the product has built or deployed; that is a
separate delivery state.

Refactor `PlanPublisher` to consume this service when optional publication is
used. Do not duplicate the rules.

**Primary surfaces:** new `plan_completion.py`, `plan_publication.py`,
`plan_integration.py`, query/API/UI schemas, tests.

**Required tests:** code vs findings; inactive historical item; failed/held/
exhausted item; missing promotion; branch moved externally; target refresh in
progress; last claimed item exclusion is no longer an ad hoc publisher rule;
same answer in local and optional remote paths.

**Acceptance evidence:** the completion endpoint names the exact one missing
promotion in a plan whose queue otherwise says all items done, then becomes
ready only after the durable promotion exists.

**Depends on:** A4, I1.

**Non-goals:** building/deploying, remote publication, local target-ref update,
or treating delivery failure as an item failure.

### I3: Obtain live local-fleet evidence through integration

**State:** evidence-bound after I1, I2, and E6; exploration complete.

**Outcome:** A real daemon and real role-runner path complete a multi-item plan
through local promotion with no remote credentials.

**Current footing:** deterministic fixtures cover the path and Stage 2 reached a
real daemon, but no retained run proves the complete admitted profile through
all items and promotions.

**Implementation decision:** Use a small dedicated acceptance repository, not
this dirty development checkout and not a workload-coded core path. The plan has
at least three code items: two independent changes that may run concurrently
and one dependent change. It also has one findings item, one deliberate first
review rejection followed by correction, and authoritative checks that would
fail if a promotion were missing.

Run with a real Docker daemon, the shipped role runner, a routed reviewer, a
prepared digest-pinned image, `push=false`, and a project-scoped supporting
service if E4 is exercised. Preserve plan/repo SHAs, profile/image digests,
route identities, event/audit DBs or exports, bounded artifacts, commands,
start/end times, and denominator counts. Credentials are redacted before the
package is retained.

**Primary surfaces:** opt-in live acceptance test/runbook and a new append-only
evidence package; implementation fixes discovered by the run belong to their
own regression tests before rerun.

**Required tests:** concurrency; dependency waiting; reviewer refusal/correction;
worker interruption and resume; promotion serialization; no remote calls;
complete plan-readiness answer.

**Acceptance evidence:** every active item has an honest terminal outcome, all
code items are promoted at the recorded integration SHA, findings are retained,
checks/review gates are present in order, and blind spots are listed.

**Depends on:** I1, I2, E6.

**Non-goals:** product deployment, seven-day reliability, or claiming model
quality from one run.

## 8. Final local product lifecycle

### L1: Define the delivery-backend protocol and metadata discovery

**State:** ready after P1; exploration complete.

**Outcome:** A local product topology is selected by admitted name and can be
added without editing or being named by core.

**Current footing:** execution environments already have an installed-metadata
contract. There is no separate product-delivery protocol. Treating the agent
container as the deployed product would conflate two trust, lifecycle, and
evidence boundaries.

**Implementation decision:** Add `delivery.py` and `delivery_backends.py` with
entry-point group `agent_harness.delivery_backends`. API version 1 defines:

- `validate(config, context) -> findings` — deterministic, no host mutation;
- `check() -> (ok, detail)` — backend availability;
- `preview(request) -> safe description` — exact resources/actions;
- `prepare(request) -> binding` — optional idempotent local setup;
- `deploy(request, binding) -> deployment_handle`;
- `inspect(handle) -> deployment_observation`;
- `teardown(handle) -> teardown_result`;
- `recover(record) -> recovery_result` for an interrupted owned deployment;
- `describe()` with name/API/implementation version.

Requests carry project/revision/run IDs, source worktree, exact integration
SHA, artifact directory, safe environment/secret-name context, and immutable
adapter config. Handles/records must be JSON-serialisable, contain ownership
identity, and contain no secret value. Core owns ordering, timeouts, command
guarding, persistence, and outcomes; adapters own resource creation and
technology-specific inspection/cleanup.

Add any new core delivery path modules to `EXECUTION_PATH`. The protocol
cannot register gates or add stages; D8 stays open. Shipped adapters are entry
points in `pyproject.toml`, and core contains no adapter dotted paths.

**Primary surfaces:** new delivery modules, `pyproject.toml`, `tests/test_generic.py`,
fake adapter contract tests, schemas.

**Required tests:** metadata-only discovery; unknown/incompatible/broken
adapter; deterministic config findings; handle JSON/secret validation; no
adapter import until selected; API compatibility; no registration terms in
`outcomes.py` or `attempts.py`.

**Acceptance evidence:** a separately packaged fixture delivery adapter is
installed and selected by name with no core edit and passes the contract suite.

**Depends on:** P1.

**Non-goals:** implementing a topology, user-defined lifecycle stages, remote
deployment, or a third-party gate registry.

### L2: Ship the local-process delivery adapter

**State:** ready after L1; exploration complete.

**Outcome:** A product that runs as one local process can be started, observed,
accepted, and reliably terminated.

**Current footing:** the harness already runs guarded subprocess commands and
owns process metrics for itself, but has no product process ownership or
restart recovery contract.

**Implementation decision:** The adapter config declares:

- `start`: non-empty argv;
- optional repository-relative `cwd`;
- safe environment names/values supplied by core according to secret scope;
- `stop_signal` from a fixed safe set, graceful timeout, and kill timeout;
- optional local port declarations used only for collision preview/evidence.

Deploy uses `subprocess.Popen(..., shell=False, start_new_session=True)`, captures
bounded stdout/stderr to run-owned artifact files, and records PID plus process
start time/process-group identity so PID reuse cannot authorise cleanup. It
refuses a start argv through `CommandGuard` before the process exists. Inspect
distinguishes running, exited, and identity mismatch. Teardown sends the
reviewed signal to the owned process group, waits, then force-kills only that
matching group. Recovery never signals a PID whose start identity differs.

The adapter does not daemonise, write a host service definition, or infer a
health check. Core runs the plan's readiness argv separately.

**Primary surfaces:** `adapters/local_process.py`, entry-point metadata,
artifact helper, unit/integration tests.

**Required tests:** argv without shell; immediate exit; long-running process;
stdout/stderr bounds; signal/timeout escalation; PID identity mismatch; crash
and recover; command refusal before start; cwd/path confinement; two concurrent
runs do not collide; no child remains after test.

**Acceptance evidence:** a fixture HTTP service starts from the exact detached
integration tree, passes a readiness client, and leaves no matching process or
port after teardown.

**Depends on:** L1.

**Non-goals:** supervisor/systemd installation, containers, remote hosts,
automatic restarts, or interpreting application logs as readiness.

### L3: Ship the Docker Compose local-delivery adapter

**State:** ready after L1 and E3; exploration complete.

**Outcome:** A repository-owned Compose application can be built and run as a
locally owned delivery topology without making Compose a core assumption.

**Current footing:** Docker CLI execution exists for agent containers and E4
adds Compose for agent-time services, but product delivery needs a separate
project/resource identity and lifecycle.

**Implementation decision:** Adapter config declares repository-relative
Compose files, optional profiles/services, allowed environment names, and
whether the adapter or the plan's build commands build images. Preview runs
`docker compose config` in the detached accepted tree and rejects v1 hazards:
external resources it cannot own, privileged mode, host PID/network, Docker
socket/device mounts, repository-external bind mounts, and unreviewed host
ports.

Every run gets a unique Compose project name derived from project/revision/run,
plus harness ownership labels where Compose supports them. Deploy uses fixed
argv `docker compose up --detach` without remote registry push. Inspect records
container IDs, image content IDs, health, ports, and project labels. Teardown
uses the exact reviewed files/project name and removes only run-owned
containers/networks plus anonymous volumes. Named volume deletion is allowed
only when the plan declares it run-owned and the preview names it. Recovery
first verifies labels/config identity; ambiguity is an escalated cleanup result,
never a broad `down` guess.

**Primary surfaces:** `adapters/compose_delivery.py`, entry-point metadata,
contract tests, opt-in live Docker tests, docs.

**Required tests:** Compose config validation; unique project naming;
privileged/socket/host/external refusal; immutable image evidence; health
observation; teardown on success/failure; killed-controller recovery; unlabelled
resource refusal; no registry push; simultaneous same-service-name deployments.

**Acceptance evidence:** a two-service fixture builds/deploys locally, passes
readiness/acceptance, tears down, and leaves no run-owned resource; another
project's resources remain.

**Depends on:** L1, E3.

**Non-goals:** Kubernetes/VM implementation, production Compose, registry
publication, or deleting shared named volumes.

### L4: Add the durable fixed delivery journal and orchestrator

**State:** ready after I2, E6, and L1; exploration complete.

**Outcome:** The harness can execute and resume the fixed whole-plan local
lifecycle without confusing it with an item attempt or losing what happened
around a crash.

**Current footing:** item attempts have a deliberately fixed six-stage list and
durable artifacts. Plan integration has a promotion journal and lease. No table
owns whole-plan delivery.

**Implementation decision:** Add `delivery_runs` and `delivery_stages` to the
operational SQLite database:

- run identity, project/revision, plan/profile/delivery digests;
- exact integration ref/SHA and detached worktree;
- adapter name/API/version and safe config digest;
- state (`pending`, `running`, `succeeded`, `failed`, `blocked`, `recovering`),
  current fixed stage, owner lease, timestamps, failure/recovery kind;
- one append-only-within-run stage result for each fixed stage containing
  argv/outcome/duration, bounded artifact checksums/references, adapter handle,
  and redacted detail;
- immutable accepted-delivery record on success.

Add `LocalDeliveryCoordinator` with a per-project delivery lease. It:

1. rechecks I2 plan completion and exact branch SHA;
2. creates a detached run worktree at that SHA;
3. materialises delivery-scoped secrets in memory and extends redaction;
4. records `prepared` after backend preparation/recovery checks;
5. runs guarded build argv arrays in their declared runner/host context and
   records `built`;
6. calls adapter deploy and durably records handle before proceeding;
7. polls adapter inspection plus readiness argv in its declared context until
   pass/deadline;
8. runs acceptance argv arrays in their declared context once the deployment
   is ready;
9. attempts teardown in `finally`, recording it independently;
10. records acceptance success only when build/deploy/readiness/acceptance pass
    and required teardown succeeds.

Commands use the same typed `CheckResult` classification where appropriate but
remain delivery-stage results, not item gate registration. No item state changes
because delivery fails. A failed delivered product blocks plan completion at
the delivery layer and requires a new plan revision/correction or an explicit
retry of a transient stage as policy permits.

Resume rules are fixed:

- `prepared` may be repeated idempotently;
- a durable successful build may be reused only from the same worktree/SHA and
  artifact identity;
- a recorded deployment handle is inspected/recovered before any new deploy;
- readiness and acceptance may be re-run only under an explicit retry action
  because they can observe mutable state;
- teardown/recovery is attempted before abandoning or superseding a run;
- an accepted run is immutable and never replayed.

**Primary surfaces:** new `local_delivery.py`, queue migrations, guard/check
reuse, delivery adapter lookup, artifact/redaction support, reaper integration,
query schemas and exhaustive state-machine tests.

**Required tests:** success ordering; build fail means no deploy; deploy fail;
process exit before ready; readiness timeout; acceptance fail; teardown fail;
both acceptance and teardown fail retained; lease contention/expiry; kill after
each boundary and recover; branch moves before start; branch moves during run
does not change pinned SHA; no item attempt stage added; no remote call; all
outputs bounded/redacted; runner-context delivery commands use the exact pinned
profile; host-context commands are direct argv and missing host tools block
preflight; no context fallback.

**Acceptance evidence:** deterministic fault injection after every external
effect proves no duplicate owned deployment, no false success, no lost failure,
and eventual safe cleanup or explicit escalated residue.

**Depends on:** I2, E6, L1, E3.

**Non-goals:** a user-defined workflow engine, automatic repair by agents,
production rollback, or merging the local target ref.

### L5: Add reviewed delivery, retry, recovery, and local finalisation actions

**State:** ready after L4; exploration complete.

**Outcome:** Operators can intentionally start and recover local delivery and,
after acceptance, optionally update a local target ref without any remote
effect.

**Current footing:** project start/stop and many GUI actions already use typed
commands, CSRF, exact browser review, and audit. There is no delivery action or
safe local ref finalisation.

**Implementation decision:** Add typed preview/apply operations and GUI/API/CLI
surfaces for:

- prepare delivery resources;
- start a delivery for the exact ready integration SHA;
- retry an explicitly eligible failed/transient stage or create a new run;
- recover/teardown an interrupted deployment;
- finalise a successful accepted SHA into a configured local target ref.

Start preview names plan/revision/SHA, profile/image digest, adapter/resources,
commands, secret names, timeouts, and cleanup behaviour. Apply uses an exact
digest/review token and rejects if plan completion, SHA, config, or adapter facts
changed.

Finalisation is optional and high risk. V1 supports **fast-forward only** by
default, using `git update-ref refs/heads/TARGET ACCEPTED EXPECTED_OLD`. A plan
may explicitly request a merge commit, but implementing that is deferred until
there is evidence it is needed; v1 target success is the accepted integration
branch. Never update the currently checked-out branch when its worktree is
dirty or when Git cannot update it safely. Never push. Record old/new SHA,
operator, delivery run, and CAS result.

Retry eligibility is deterministic: configuration/policy/acceptance failures
need a revised plan or explicit operator reason; transient backend/readiness
failures may retry after cleanup. Cost caps remain terminal and are never
treated as delivery transients.

**Primary surfaces:** `command_service.py` rules/mutation owner or a parallel
typed delivery command service, `api.py`, `ui.py`, templates, `__main__.py`,
schemas, audit events and action tests.

**Required tests:** auth/CSRF/review consumption; stale SHA/profile rejection;
start once/idempotency; retry matrix; recovery before redeploy; finalise FF;
non-FF/dirty/checked-out ref refusal; exact CAS race; no push; operator identity
and reason retained; GUI says accepted branch vs finalised ref distinctly.

**Acceptance evidence:** one reviewed delivery reaches accepted; a second actor
moves the target ref before finalisation and the CAS refuses without overwriting
it; after a fresh preview the safe FF succeeds locally.

**Depends on:** L4.

**Non-goals:** automatic finalisation, force-reset, remote merge/push, production
approval policy, or treating browser navigation as authorisation.

### L6: Expose one correlated local delivery evidence view

**State:** ready after L4 and L5; exploration complete.

**Outcome:** API and GUI can answer “what did this plan deliver locally?”
without requiring a reader to correlate raw logs or SQLite tables manually.

**Current footing:** query/API/UI surfaces already project queue, attempts,
promotions, reviews, and events. Evidence is item-centric; no final lifecycle
exists.

**Implementation decision:** Add a typed delivery summary containing:

- project/current and delivered plan revision/digests;
- base, integration, accepted, and optional finalised local ref/SHAs;
- execution profile, runner image content ID, adapter names/versions;
- active item terminal states and promotion/findings references;
- build/deploy/readiness/acceptance/teardown/recovery stage results;
- command argv, outcome, duration, bounded output/checksum/artifact links;
- secret names/scopes and redaction markers, never values;
- operator reviews/decisions and remaining owned resource warnings;
- one honest overall state: not ready, ready, running, accepted, failed,
  cleanup required, or superseded.

The view is a projection over authoritative operational rows plus append-only
events/audit; controllers do not read SQLite directly. Artifact downloads stay
inside an allowlisted run directory, require auth, and cannot follow symlinks.
OpenAPI describes every field. GUI uses no CDN and clearly separates “item
accepted”, “plan integrated”, “product accepted locally”, and “local ref
finalised”.

**Primary surfaces:** `query_service.py`, review/evidence service, `schemas.py`,
`api.py`, `ui.py`, templates/static assets, artifact route and tests.

**Required tests:** every overall-state projection; incomplete/missing old data
shown as unknown rather than false; secret redaction; artifact traversal/
symlink refusal; OpenAPI completeness; same query service for JSON/HTML;
accepted then superseded revision; cleanup-required warning survives restart.

**Acceptance evidence:** a reviewer can diagnose a readiness failure and find
the exact command/output checksum, accepted commit, environment, and cleanup
state from the GUI alone.

**Depends on:** L4, L5.

**Non-goals:** rewriting audit events, embedding full unbounded logs, a separate
frontend, or claiming an unrun stage passed.

## 9. Verification, proof, and release

### V1: Build the deterministic end-to-end acceptance fixture

**State:** planned deliverable; implementation is blocked on the unfinished GUI
admission-preview/confirmation workflow and the upstream lifecycle surfaces.

**Outcome:** One no-network/no-model fixture exercises the complete target
contract and fails at every gate boundary when deliberately broken.

**Current footing:** `init --demo`, Stage A, role-runner, integration, API, and
GUI fixtures each prove portions of the pipeline. None includes target
validation/admission/profile/final delivery.

**Implementation decision:** Create a fixture application/repository at test
runtime. It has:

- a complete v1 plan with at least three dependent items;
- a fixture execution backend and role runner with deterministic replies;
- authoritative item/integration checks;
- a fixture delivery backend exposing controlled build/deploy/readiness/
  acceptance/teardown failures;
- a local integration branch and exact evidence assertions.

The main happy-path test validates, previews/applies admission, prepares the
profile, passes preflight, runs items, promotes, confirms plan completion,
reviews/starts delivery, passes every stage, tears down, and reads final
evidence. Parameterised tests break each boundary and assert the next expensive
or state-changing stage did not occur.

Keep no sleeps: inject clocks, polling, and faults. This fixture proves wiring,
not container isolation, external model quality, or a real application.

**Primary surfaces:** new test support plus targeted acceptance tests; demo may
be upgraded only after the fixture is stable.

The ten-step implementation and acceptance plan is
[`docs/E2E-ACCEPTANCE-PLAN.md`](docs/E2E-ACCEPTANCE-PLAN.md). This is a new
project deliverable. It must not be represented as existing coverage while the
GUI admission flow is incomplete.

**Required tests:** full happy path; all validator/admission/preflight/item/
promotion/delivery failure cut-points; restart at durable boundaries; no
remote/network/model; exact final evidence; existing gates preserved.

**Acceptance evidence:** one test output identifies the accepted SHA and every
stage, while tests demonstrate a failure before a costly gate prevents that
gate from running.

**Depends on:** P3, A5, E6, I2, L6.

**Non-goals:** live Docker proof, model evaluation, performance measurement, or
seven-day reliability.

### V2: Complete a real project through local delivery

**State:** evidence-bound after I3, L5, and L6; exploration complete.

**Outcome:** One representative real application satisfies the entire minimal
contract with retained reproducible evidence.

**Current footing:** real-agent observations and fixture integration evidence
exist separately. No project has completed validation through local product
acceptance/teardown.

**Implementation decision:** Select a project supplied by the owner after the
software path is ready whose honest topology fits the shipped local-process
adapter. Rainmon is eligible only if that is its natural declared topology and
is never privileged; its plan must pass exactly the public template/validator
and may not cause core changes. A project that naturally requires Compose is a
candidate for V3 instead of being forced into this proof.

Before spending, preserve:

- repository origin description (if any), clean state, base and plan bytes/SHA;
- validation/admission proposal and approved revision;
- runner recipe/reference and pinned image content ID;
- installed adapter/role-runner/route versions;
- local topology preview, commands/timeouts, secret names, and redaction setup;
- denominators and explicit acceptance criteria.

Run from a dedicated clean local checkout. Retain bounded raw artifacts with
checksums, exported operational state/events/audit, exact accepted SHA, local
resource inventory before/after, command transcript, outcome counts, and blind
spots. If a criterion fails, report it and keep the target incomplete.

**Primary surfaces:** an append-only dated evidence package and regression fixes
for any discovered defect.

**Required tests:** more than one item; real checks/reviewer; local integration;
real integrated build; local deploy; readiness; acceptance; teardown; no remote
mutation; at least one controlled failure/recovery rehearsal before the final
run where safe.

**Acceptance evidence:** the package permits another operator with the same
declared local prerequisites to identify every input and repeat the run without
private conversational context.

**Depends on:** I3, L2, L5, L6, V1.

**Non-goals:** proving genericity from one project, pushing the accepted branch,
production deployment, or hiding project prerequisites.

### V3: Complete a materially different second-project proof

**State:** evidence-bound after V2; exploration complete.

**Outcome:** A second application proves that adding a language/toolchain and
topology does not require changing harness core.

**Current footing:** the genericity test guards names/imports and prior evidence
used multiple repositories, but the new minimal lifecycle has no two-project
proof.

**Implementation decision:** The second project must differ from V2 in all of:

- primary language/toolchain;
- dependency provisioning commands;
- authoritative check commands;
- local delivery adapter: V3 uses the shipped Compose adapter, while V2 uses
  local process;
- data/UI applicability choices;
- at least one execution profile characteristic such as supporting service or
  network policy.

Before running, record `git diff -- src/agent_harness tests/test_generic.py` at
the V2 accepted implementation baseline. Project enablement may add only its
plan, repository-owned runner recipe/config, or a separately installed adapter
package. A needed core edit is a genericity defect: implement and test the
generic missing capability, then rerun **both** V2 and V3; do not call the
second proof complete on the new path alone.

Run both projects concurrently for at least an overlapping item period to
exercise per-project profiles, services, budgets, leases, and non-starvation.

**Primary surfaces:** second append-only evidence package and genericity
regression tests.

**Required tests:** concurrent operation; distinct images/profiles; topology
isolation; independent teardown; no remote mutation; no execution-path project
name/path/toolchain constant; first project still passes.

**Acceptance evidence:** the core diff required solely to onboard project two
is empty, or any generic fix has rerun evidence for both projects.

**Depends on:** V2, L3.

**Non-goals:** supporting every language/topology, benchmarking project quality,
or adding a project adapter to core.

### V4: Measure reliability and destructive-boundary recovery

**State:** evidence-bound after V2 and V3; exploration complete.

**Outcome:** The minimum product has measured recovery evidence, not just one
green run.

**Current footing:** leases, attempt resume, promotion recovery, redaction, and
fixture failure injection exist. The target adds image/service/delivery effects
whose crash behaviour needs direct measurement.

**Implementation decision:** Run a published fault matrix against the two proof
projects or equivalent retained fixtures:

- kill controller during image build, supporting-service preparation, item
  model call, checks, reviewer, promotion, product deploy, readiness,
  acceptance, teardown, and local finalisation CAS;
- restart and invoke read-only readiness/recovery preview first;
- recover explicitly and observe duplicate resources/effects, leaked resources,
  repeated model spend, lost evidence, false success, and time to safe state;
- exercise simultaneous projects to confirm one recovery does not pause or
  delete the other;
- exercise terminal provider cost-cap classification separately and prove no
  retry/endpoint-wide stall regression.

Publish denominators by cut point. A recovery marked manual or cleanup-required
is not a pass, but can be an honest release limitation if `minimal.md` acceptance
permits it; currently target teardown/recovery requires a declared safe outcome,
so unresolved owned resources block release.

**Primary surfaces:** live fault-injection harness/runbook, evidence package,
regression tests for failures discovered.

**Required tests:** attempted/recovered/manual/failed per cut point; duplicate
effects; leaked owned resources; extra model calls/cost; cross-project impact;
evidence completeness.

**Acceptance evidence:** zero false-success outcomes and zero unexplained
run-owned residues across the stated matrix; any manual result is named and
keeps the relevant target criterion open.

**Depends on:** V2, V3.

**Non-goals:** chaos testing shared/production infrastructure, making uptime
claims from a short run, or retrying cost caps.

### V5: Close release documentation, migration, packaging, and gates

**State:** ready after V1–V4; exploration complete.

**Outcome:** The repository exposes one coherent local product, can upgrade an
existing installation safely, and makes no claim wider than its evidence.

**Current footing:** documentation is aligned to the target but the usage guide
still describes legacy current commands; plan/queue migrations have explicit
precedent; packaging already ships templates/static assets and adapter entry
points.

**Implementation decision:** Before release:

- make `examples/PLAN.md` a real valid target fixture and publish
  `examples/PLAN-TEMPLATE.md`;
- rewrite the primary USAGE path around validate → preview/admit → prepare →
  preflight/start → integrate → deliver → evidence;
- move remote issue/PR/publication material to an optional extensions section;
- document operational DB backup/export/rebuild/rollback for plan revisions,
  profiles, services, and delivery journal;
- document safe recovery of adapter-owned local resources;
- update demo/doctor to report target capability honestly;
- update OpenAPI, GUI help, README status, `minimal.md` comparison, and
  `docs/STATUS.md` milestone states;
- package new templates/static files and declare delivery adapters through
  metadata;
- run a clean install/wheel smoke test with no optional remote credentials;
- run all four repository gates with fast `TMPDIR` and retain commands/results;
- verify every new execution-path module is covered by genericity enforcement;
- link V2–V4 evidence and leave unmet criteria unchecked.

Legacy global execution settings/projects migrate as “legacy/unadmitted”, never
as fabricated target plan revisions. Users export/backup first and explicitly
admit a plan before those projects can claim under the target path. Existing
remote extension data is retained.

**Primary surfaces:** docs, examples, demo/doctor, migration tooling/docs,
packaging metadata, OpenAPI/UI help, release evidence.

**Required tests:** pre-migration DB upgrade/rollback/export; wheel contents;
fresh install demo; docs command smoke tests; link check; OpenAPI field
descriptions; genericity scans; no credential requirement on local first run.

**Acceptance evidence:** all eight release criteria in `minimal.md` have direct
links to tests or dated evidence, the four gates pass, and status contains no
“done” claim for an unmet live criterion.

**Depends on:** V1, V2, V3, V4.

**Non-goals:** remote workflow certification, hosted CI/CD integration,
production deployment, or deleting historical evidence.

## 10. Coverage matrix

This matrix is the completeness check against `minimal.md` and the gaps in
`docs/STATUS.md`.

| Target/gap | Backlog coverage |
|---|---|
| Generic plan template and normative schema | P1, E0, L1, L2, P4 |
| One-pass stable-code rejection | P2, P3 |
| No interview required | P3, P4, A5 |
| Optional post-minimum batched questions | A2, A5 |
| Atomic/idempotent admission | A1, A3, A4 |
| Local repository/base identity | A2, A3 |
| Dependency-aware active revision queue | P2, A1, A3, A4 |
| Per-project execution profile | E1 |
| Reviewed/pinned runner image | E2 |
| Named scoped secrets | E3 |
| Supporting services/network | E4 |
| Exact preflight | E5 |
| Authoritative checks use admitted profile | E6 |
| Collision-safe local Git refs and fixed base SHA | A1, A3, A4 |
| Local execution without remote prerequisites | I1 |
| Local plan completion | I2, I3 |
| Product delivery adapter boundary | L1 |
| Local process and Compose topologies | L2, L3 |
| Integrated build/deploy/readiness/acceptance/teardown/recovery | L4, L5 |
| Optional reviewed local ref finalisation | L5 |
| Correlated durable evidence | L6 |
| Deterministic full-path proof | V1 |
| One real project | V2 |
| Materially different second project/no core edit | V3 |
| Recovery and non-interference measurement | V4 |
| Migration/docs/package/gates | V5 |
| D8 remains unanswered | P2, L1, L4 genericity/non-goals |
| D9 review prompt remains controlled | A2 tests/non-goals |
| Remote workflows outside minimum | I1, V5 |

## 11. First implementation tranche

The smallest coherent first tranche is:

```text
P1 -> E0 and L1 -> P2 -> P3
                  P2 + L2 -> P4
```

E0 and L1 establish effect-free adapter config validation; L2 makes the
published example name a real shipped local topology instead of a test-only
fiction. Together P1–P4 then deliver the user-visible behaviour that motivated
the target change: a generic plan template and one complete deterministic
rejection report, with no onboarding interview and no state mutation.

Do not begin image generation or local deployment first. Without a versioned
admitted plan, those features would acquire configuration through new global
flags and recreate the project-specific harness behaviour this backlog exists
to remove.

After M1, implement **A1 and A2 in parallel conceptually but merge A1 first**,
then A3–A5. The execution profile and delivery work can then proceed on stable
revision identities. The first live item spend should occur only after E6 can
prove and use the exact environment for both agent commands and authoritative
checks before a claim.

## 12. Backlog exit rule

An item is complete only when:

1. its acceptance tests pass;
2. its documentation/API schema describes the implemented behaviour;
3. its genericity and safety assertions are encoded in tests;
4. its required deterministic or live evidence exists;
5. every dependency exit it relies on is actually met;
6. the four repository gates pass for code changes; and
7. `docs/STATUS.md` is updated honestly.

Exploration text is not completion evidence. A checkbox must not be marked done
because code exists while the live or failure-path criterion remains unmet.
