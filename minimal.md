# Minimal local product contract

**Status:** target product contract

**Effective:** 2026-08-09

**Applies to:** the generic, open-source `agent-harness` core

This document defines the smallest useful product the repository is aiming to
deliver. It is the authority for product scope. [`docs/STATUS.md`](docs/STATUS.md)
records how much of this contract exists today; design and historical plan
documents may describe implemented or previously proposed capabilities without
changing this target.

## 1. Product promise

Given:

1. a local Git repository;
2. a plan that satisfies the generic minimum plan contract below; and
3. a declared local execution and deployment topology,

the harness can validate the plan, create a dependency-aware work queue, run
agents in isolated local workspaces, integrate their accepted changes into a
local plan branch, build and deploy that integrated result locally, run the
declared acceptance checks, tear it down, and retain evidence explaining every
decision.

The minimum product does not need project-specific changes to harness core. A
new language, framework, repository layout, build tool, or local deployment
shape is supplied by the plan, an execution profile, or an installed adapter.

The minimum product never pushes a branch, opens or merges a pull request,
changes a remote issue, invokes hosted CI/CD, or deploys to a shared, staging,
or production environment. A human may push the accepted local branch through
their existing delivery flow after the harness finishes; that is outside this
contract.

Building a reviewed runner image, starting project-owned supporting services,
and building/deploying the product locally are deliberate local harness
operations, not CI/CD. Each requires the admitted configuration, explicit
operator action where it mutates local resources, and durable evidence.

## 2. Why the boundary is local

Keeping the code lifecycle local removes credentials, vendor APIs, repository
governance, hosted runner semantics, deployment permissions, and production
rollback policy from the generic core. Git is still fundamental:

- every item starts from an exact local base commit;
- agents work in separate local worktrees or equivalent isolated workspaces;
- accepted item commits are promoted into one local integration branch;
- conflicts and stale-base changes are replayed and re-gated locally;
- the accepted result and its evidence remain inspectable in the local repo.

For this contract, **local merge** means promotion of accepted item commits into
the plan's local integration branch. Updating another local target ref may be a
separate, explicitly reviewed finalisation action. The harness does not need to
rewrite the developer's checked-out branch to prove completion, and neither
operation implies a remote push.

**Local deployment** means a deployment reachable and owned from the selected
harness host: for example a child process, Docker/Podman Compose project, local
Kubernetes cluster, or local VM. A plan may use an installed adapter for another
local topology. The adapter is configuration, not a reason to edit core.

## 3. The minimum lifecycle

```text
PLAN.md + local Git repo
          |
          v
deterministic validation --invalid--> one complete rejection report
          |
        valid
          v
optional semantic review --material ambiguity--> one batched question report
          |
       admitted
          v
register project + persist plan revision + create dependency-aware queue
          |
          v
isolated item work -> declared checks -> review -> local item commit
          |
          v
serial promotion/replay/re-gating on local integration branch
          |
          v
integrated build -> local deploy -> readiness -> acceptance -> teardown
          |
          v
accepted local branch + durable evidence
```

Plan admission is fail-closed. Queue rows are not created for a rejected or
unresolved plan.

## 4. Minimum plan contract

The user authors the plan from a template. The admission path does not conduct
a question-by-question interview to manufacture missing content.

A plan must contain the following information:

1. **Project identity and brief** — a stable project key, a concise description,
   and the local repository path or an explicit path supplied at invocation.
2. **Scope and non-goals** — what this plan will and will not deliver.
3. **Rough architecture** — components, responsibilities, boundaries, and the
   important flows between them.
4. **Data model** — important entities, ownership, persistence, and migration
   expectations, or an explicit `Not applicable` with a reason.
5. **GUI and interfaces** — the intended user-facing UI and/or API/CLI surfaces,
   or an explicit `Not applicable` with a reason.
6. **Local agent execution profile** — toolchains, system packages, supporting
   services, mounts, dependency provisioning, model access, and network policy.
7. **Authoritative checks** — deterministic commands that establish item and
   integrated correctness.
8. **Local delivery topology** — integrated build, local deploy, readiness,
   acceptance, teardown, and recovery/rollback behaviour.
9. **Cross-cutting requirements** — security, privacy, accessibility,
   performance, observability, compatibility, and licensing requirements that
   apply, with explicit `Not applicable` entries where appropriate.
10. **Work items** — stable IDs, deliverables, acceptance criteria, and typed or
    untyped dependencies sufficient to form an acyclic graph.
11. **Local definition of done** — the evidence that must exist before the plan
    is locally complete.
12. **Assumptions, risks, and open questions** — including who must resolve each
    blocking question.

The structured values supplied by a plan for the harness to execute must use a
versioned, machine-readable manifest embedded in the Markdown plan. Prose
explains intent; the manifest is the source of truth for executable commands
and environment requirements. Commands are argument arrays, not shell
fragments. Version 1 is exactly one fenced `harness` block containing TOML.
Unknown versions and fields are rejected. The parser, validator, canonical
representation, and future migration rules are implementation deliverables;
values must not be inferred from arbitrary prose.

The plan names required model roles and a role-runner capability, but it does
not carry model-provider endpoints, model credentials, or other deployment
secrets. The operator supplies those mappings out-of-band; admission shows and
binds the safe effective routing facts before any work can start.

### Generic plan template

This is the minimum authoring shape. Placeholder values and unexplained empty
sections are validation errors. The `harness` block below is the selected v1
authoring boundary. It is not accepted by the current CLI until its validator
is implemented; [`BACKLOG.md`](BACKLOG.md) defines that work and the field
semantics.

````markdown
# <Project name>

## Project identity and brief

- Project key: `<stable-key>`
- Local repository: `<path supplied here or at invocation>`
- Brief: <what is being built and why>

## Scope and non-goals

### In scope

- <outcome>

### Non-goals

- <explicit exclusion>

## Rough architecture

<components, responsibilities, boundaries, and important flows>

## Data model

<entities, ownership, persistence, and migrations; or "Not applicable: <reason>">

## GUI and interfaces

<GUI, API, CLI, or other user-facing surfaces; or "Not applicable: <reason>">

## Execution and local delivery

```harness
version = 1

[project]
key = "<stable-key>"
name = "<project name>"

[repository]
base_ref = "main"
integration_ref = "harness/<stable-key>"

[agents]
role_runner = "<installed-role-runner>"
required_roles = ["implementer", "reviewer"]
max_workers = 2
max_attempts = 5
max_item_seconds = 3600
max_item_spend_usd = 0.0
max_hold_seconds = 21600

[execution]
backend = "<installed-execution-backend>"
network = "<none-or-project>"
toolchains = ["<name and version>"]
system_packages = []

[execution.image]
strategy = "<existing-or-build>"
# Existing: set a digest-qualified `reference` and reviewed `pull` policy.
# Build: select a repository Containerfile or a generated recipe whose base,
# copied paths, and setup argv arrays are all present for review.

[execution.limits]
command_timeout_seconds = 300
memory = "2g"
cpus = "2"
pids = 512
user = "1000:1000"
rootfs_read_only = true
tmpfs_size = "512m"

[[execution.probes]]
name = "<toolchain-proof>"
command = ["<program>", "<argument>"]

[[execution.mounts]]
source = "<repository-relative-path>"
target = "<safe-absolute-runner-path>"
writable = false

[[execution.secrets]]
name = "<out-of-band-secret-name>"
source = "environment"
scope = "<provisioning-checks-or-delivery>"

[execution.config]
# Backend-specific supporting-service and runtime configuration. The selected
# installed adapter must validate this without changing the host.

[checks]
item = [["<program>", "<argument>"]]
integration = [["<program>", "<argument>"]]

[local_delivery]
backend = "<installed-local-delivery-backend>"
build = [["<program>", "<argument>"]]
build_context = "runner"
readiness = [["<program>", "<argument>"]]
readiness_context = "host"
acceptance = [["<program>", "<argument>"]]
acceptance_context = "host"
required_host_tools = ["<program-used-by-deploy-or-acceptance>"]
command_timeout_seconds = 900
readiness_timeout_seconds = 120
readiness_interval_seconds = 2
teardown_timeout_seconds = 30

[local_delivery.config]
# Adapter-specific start, owned-resource, command-context, teardown, and
# recovery configuration. Core owns stage order and outcomes; the selected
# adapter owns the local technology and validates these fields.
```

## Cross-cutting requirements

- Security: <requirement or "Not applicable: <reason>">
- Privacy: <requirement or "Not applicable: <reason>">
- Accessibility: <requirement or "Not applicable: <reason>">
- Performance: <requirement or "Not applicable: <reason>">
- Observability: <requirement or "Not applicable: <reason>">
- Compatibility: <requirement or "Not applicable: <reason>">
- Licensing: <requirement or "Not applicable: <reason>">

## Work items

### W1: <short title>

deliverable: code

**Deliverable:** <observable result>

**Acceptance:**

- <deterministic criterion>

depends on: none

### W2: <short title>

deliverable: code

**Deliverable:** <observable result>

**Acceptance:**

- <deterministic criterion>

depends on: W1

## Local definition of done

- Every item is accepted and promoted into the local integration branch.
- The integrated build, local deployment, readiness, and acceptance commands pass.
- Teardown succeeds and retained evidence identifies the exact accepted commit.
- <project-specific completion evidence>

## Assumptions, risks, and open questions

- Assumption: <statement and consequence if false>
- Risk: <risk, mitigation, and owner>
- Open question: <question, owner, and whether it blocks admission or an item>
````

## 5. Admission behaviour

Admission has two deliberately separate stages.

### 5.1 Deterministic minimum validation

The validator parses without model access and returns all findings in one run.
It rejects a plan when required content is absent, a placeholder remains, an
`N/A` lacks a reason, executable configuration is malformed, a command cannot
be represented safely, an item is incomplete, a dependency is unresolved, or
the graph is cyclic.

Every finding has a stable code, severity, source location, and remediation.
For example:

```text
PLAN-M104 error execution.network is missing (`none` or `project`)
PLAN-W203 error W3: dependency "W9" does not exist
PLAN-D302 error Local definition of done: acceptance evidence is not specified
```

The CLI exits non-zero and creates no project, plan revision, or queue rows.
This report replaces an onboarding interview; rerunning validation is cheap and
deterministic.

### 5.2 Post-minimum semantic review

Only after deterministic validation passes may a reviewer identify material
ambiguities that syntax cannot settle. Questions are returned together, tied to
a section or work item, and explain why the answer affects execution or
acceptance. They do not silently invent architecture or project policy.

A blocking question prevents admission until the plan is revised or an answer
is durably incorporated. Non-blocking questions become explicit assumptions or
item holds. Agent questions during execution use the existing hold mechanism;
they do not weaken admission requirements.

## 6. Dependencies and runner images

Local-only does not mean dependency-free. A project may need language runtimes,
compilers, package downloads, browsers, databases, containers, or a model
endpoint. Genericity comes from declaring those needs rather than baking one
workload's assumptions into core.

A required dependency in the minimum path cannot depend on a hosted issue or
other external resolver. It may name work in this plan, an explicit human
decision, or an exact admitted revision of another local harness project.
External references may remain advisory evidence or belong to an optional
remote extension, but they cannot make the local minimum wait on remote state.

The minimum product supports a per-project execution profile by either:

- accepting an existing immutable runner image reference; or
- generating a reviewable runner recipe from declared requirements, building
  it locally, testing it, and pinning the resulting digest before work begins.

The harness must never silently execute arbitrary installation prose. Image
generation, mounts, services, network access, and named secret injection are
shown during preflight and are auditable. Secrets are referenced by name and
supplied out-of-band; they do not belong in `PLAN.md` or the event store.

The **agent runner image** and the **product deployment image/topology** are
separate concepts. A plan may use the former to edit and test code while using
a process, Compose project, local cluster, VM, or installed adapter for the
latter.

## 7. Genericity and extension rules

- Core consumes protocols and configuration, never a named project's layout or
  a vendor-specific log/deployment format.
- Vendor or topology knowledge lives in an installed adapter discovered through
  metadata, following the repository's existing adapter rule.
- A supplied baseline is data, not a universal constant.
- Project commands cannot weaken the harness's evidence, policy, or review
  gates. The gates remain the product.
- The local command runner is a screened execution boundary, not a claim of
  perfect sandboxing.
- Unsupported requirements fail preflight before any item is claimed.

## 8. Current product compared with this contract

As of 2026-08-09, the repository is **partially aligned, not yet the minimal
product described above**.

| Capability | Current product state | Gap to this contract |
|---|---|---|
| Generic plan parsing | Parses Markdown work items, dependencies, duplicates, unresolved references, malformed dependency clauses, cycles, and unattached prose. | No normative project-level plan schema or validation of architecture, data, interfaces, execution profile, local delivery, or definition of done. |
| Admission diagnostics | Some parse/dependency findings block plan sync. | No complete stable-code rejection report and no fail-closed admission transaction covering project, plan revision, and queue creation. |
| Plan authoring | `inception`/survey flows can ask questions and produce Markdown. | Interactive authoring is currently too central. It must become optional tooling, not the minimum admission path. |
| Work execution | Queue leases, attempts, budgets, holds, checks, review, audit, role runners, and local worktrees exist. | The full path has not been accepted against a real project fleet; unsupported project requirements are not yet captured by the minimum plan contract. |
| Local Git integration | Item commits, per-plan integration branches, serialized promotion, replay, re-gating, and dependent-item waiting are implemented and fixture-tested. | End-to-end live acceptance remains incomplete; local finalisation semantics need to be exposed as a reviewed product action. |
| Execution environment | Host and Docker execution paths exist; the service can be configured with image and mount values. The Docker agent loop is isolated, but authoritative item and promotion checks still execute on the controller host. | Image, mounts, services, network policy, and toolchains are primarily deployment-wide rather than admitted and pinned per project. No reviewable image-generation lifecycle exists, and the admitted profile does not yet answer every gate. |
| Integrated checks | Project checks and promotion-time re-gating exist. | No distinct, plan-declared final build/readiness/acceptance contract. |
| Local product deployment | Monitoring/API/GUI deployment is documented for the harness itself. | There is no generic lifecycle that deploys the product-under-development locally, verifies readiness and acceptance, tears it down, and records the result. |
| Remote workflow | Remote publication, issue, review, and hosted-flow integrations exist in parts of the code and tests. | They are outside the minimum target and must not remain prerequisites for the local path. They may survive as optional extensions. |
| Evidence from real work | A real-daemon Stage 2 run produced partial evidence; fixture evidence covers later Git integration mechanics. | No complete real-project run has satisfied this contract. The repository must not claim otherwise. |

Detailed current status and the implementation sequence are in
[`docs/STATUS.md`](docs/STATUS.md).

## 9. Minimum release acceptance

The target is reached only when all of the following are demonstrated:

1. A fresh user can copy the published plan template, fill it without an
   interview, and get one deterministic rejection report for an invalid plan.
2. A valid plan is admitted atomically into a local project and dependency-aware
   queue without remote credentials.
3. Preflight proves that the pinned per-project execution environment can run
   every required tool and access every declared local service.
4. At least one representative project completes through isolated item work,
   checks, review, local Git integration, integrated build, local deployment,
   readiness, acceptance, and teardown.
5. The evidence names the plan revision, base and accepted commits, runner image
   digest/profile, commands, outcomes, and any human decisions.
6. A second project using a materially different toolchain and local topology
   can use the same core without modifying an execution-path module.
7. The genericity tests and all repository gates pass.
8. No success criterion depends on GitHub/GitLab, hosted CI, a remote branch, a
   remote issue tracker, or a non-local deployment target.

Until these criteria pass, status remains pre-alpha and capability claims must
name the narrower evidence actually obtained.

## 10. Explicitly deferred

The following may be useful later but are not prerequisites for the minimum
open-source product:

- remote branch pushes and pull/merge request automation;
- hosted issue tracker state mutation;
- hosted CI/CD triggering or observation;
- shared development, staging, or production deployments;
- automatic production rollback;
- organisation-specific approval, identity, or repository policy;
- a registry for arbitrary third-party gates (decision D8 remains open).

Deferring these features does not delete existing code or invalidate historical
evidence. It prevents them from defining or blocking the generic local path.
