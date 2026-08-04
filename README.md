# agent-harness

Turns a plan you wrote in markdown into work that coding agents actually do, and tells you
honestly what happened.

```
PLAN.md ──▶ GitHub issues ──▶ claim ──▶ agent in a terminal ──▶ checks ──▶ review ──▶ PR
                                          │
                                          └── you can attach to it, on any device
```

It is **not tied to any particular project, language or workload.** You supply the plan, the
provider and the checks; the harness supplies the queue, the claims, the failure model and
the record of what happened.

## Status: pre-alpha — deterministic paths are tested; real use is observed, not proven

It runs, it is deployed inside [AIDevEnv](https://github.com/TheDancingDeveloper-org/aidevenv),
and direct execution has been driven end to end against a real git repository with a
scripted model. A first supervised NGMS attempt and later direct calls used real agents and
providers, and exposed defects; the surviving evidence is reconstructed in
[`docs/evidence/2026-08-03-04-ngms-first-sustained-run-v1.md`](docs/evidence/2026-08-03-04-ngms-first-sustained-run-v1.md).
It lacks a common run ID, complete configuration, raw-artifact checksums and a comparable
follow-up run, so it is an observation, not proof that the harness works against a real
fleet. Deterministic fixture success proves wiring, not model quality or unattended
reliability.

Three words are used precisely throughout this README, and they are not
interchangeable:

| | What it means | How to check it yourself |
|---|---|---|
| **tested** | A test in this repository fails if it stops being true. Deterministic, no network. | `uv run pytest` |
| **observed** | Seen happen in a real run, without a preserved artefact that would let anyone reproduce it. | the reports in [`docs/evidence/`](docs/evidence/) |
| **proven** | Measured against a stated criterion, with the denominator and the commands published. | nothing about live behaviour is in this column yet |

Concretely: the first-run path, the queue, the dependency graph, the patch
ladder, the checks gate and the reviewer gate are **tested**. Behaviour against
real models and a real fleet is **observed**. Unattended reliability, cost per
merged item and second-repository portability are **neither** — they are
blocked on runs this repository cannot perform on its own.

"No failures observed" is not the same as "the requirement was exercised".

| Module | What it does |
|---|---|
| `plan` | Parses a markdown plan into work items, reporting what it could **not** read |
| `inception` | Scopes a project from a paragraph, argues about it, and produces a `PLAN.md` — never queue rows |
| `adoption` | Takes on a project already part-built: proposes what is *already done*, with evidence. **A proposal is never a decision** |
| `graph` | The typed dependency graph. A required target it cannot resolve is a blocker, not an assumption |
| `demo` / `doctor` | A first run with no credentials; and what a real run would need, reported without spending anything |
| `outcomes` | What a gate answered and what stopped an item — five check outcomes, five dispositions, not one bit |
| `attempts` | Where an attempt got to, durably, so a killed worker resumes instead of re-paying |
| `budgets` | How long one item may take and what it may spend. **Not** a provider cost cap |
| `holds` | An item waiting on a person: durable, survives worker death, answerable from anywhere |
| `fleet` | One worker pool per project, so no project starves another |
| `audit` | Append-only history in its **own** database, with no mutation surface |
| `pricing` | Token usage and the price applied to it — unknown is never zero |
| `maintenance` | Rolls up complete days, then thins only what a rollup covers |
| `reconcile` | Merged / closed / reverted, fetched from GitHub |
| `github` | Syncs those items to issues, idempotently — re-running an edited plan updates rather than duplicates |
| `work` | The queue. Claims are **leases**, so a dead worker releases its item by doing nothing |
| `session_executor` | Runs an item as a CLI agent in a terminal session you can attach to |
| `executor` | The same loop for direct API calls, plus the diff-apply tolerance ladder and checks |
| `session_host` | Client for whatever owns the PTY sessions (AIDevEnv is the reference) |
| `providers` | Classifies failures — burst limit vs spent window vs spent cap vs refused |
| `protocols` | What a route is made of: wire protocol, auth, response reader, classifier — resolved by name |
| `model_client` | Routes roles to models; per-worker jittered retry; per-endpoint parking |
| `store` / `ingest` / `sources` | Append-only SQLite event store, idempotent ingest |
| `api` | Documented HTTP API + Swagger. No GUI — the session host renders it |
| `adapters` | Opt-in readers for other tools' logs |

### Two ideas worth stealing

**A rate limit is not one thing.** `429` covers "slow down" (retry in a moment), "your
5-hour budget is gone" (hours), "your weekly budget is gone" (days) and "we refuse this"
(never). Providers bury the difference in a vendor-specific field, and a harness that does
not read it cannot tell a half-second problem from a week-long one.

Getting that wrong is expensive in both directions: retrying a spent cap is a busy-wait
that burns quota checking whether quota exists, and a *fleet-wide* cooldown in response to
one worker's 429 does not merely stall the fleet — it phase-locks it, so every worker wakes
together, bursts together, and is limited together, which is exactly the shape a rate
limiter exists to reject.

So: classify first, never retry a cap, keep all reaction per-worker and per-endpoint, and
jitter the backoff. See `providers.py` and `model_client.py` — the reasoning is in the
docstrings, with the live evidence that corrected it twice.

**Classifying a failure is not the same job as speaking to an endpoint.** They were one
thing here once: `Provider` read error envelopes while the CLI's transport separately
assumed one gateway's path, header and response shape. Neither knew about the other, so
"add a provider" meant editing the transport. They are separate now — a route names a
preset, a preset supplies both — and adding a vendor is a module nobody in core imports.
See `protocols.py`.

**A claim is a lease, not a lock.** A lock held by a process that died is a lock nobody can
release, and the usual workaround — a human clearing stale state — is exactly the
unattended-operation failure the queue exists to prevent. A lease expires on its own, so a
worker killed mid-item releases it by doing nothing. A heartbeat keeps genuinely-slow work
alive, because "slow" and "dead" look identical from outside and only a live process can
keep stamping one.

## Definition of done for v1

Expressed as observed behaviour, not internal completeness.

- [ ] The fleet runs 7 days unattended with no manual restart.
- [ ] Every failure is diagnosable from the GUI alone, without opening a log file.
- [ ] Rate-limit errors are classified, and cost caps are never retried.
- [ ] No single worker's failure pauses another worker.
- [ ] Reviewer-approved work survives a killed worker.
- [ ] Delivery rate is no worse than the workload's own pre-harness baseline, at lower cost.
- [ ] The role→model map can be changed without a redeploy.
- [ ] Two projects run concurrently without either starving the other.
- [ ] Deleting `harness.sqlite` changes no audited answer.

If all seven hold, v1 is done regardless of what remains unimplemented.

## Documentation

- **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how it fits together.**
  Diagrams: the whole system, what an agent actually is, the components, the
  life of a work item, failure classification, the audit layer, and project
  isolation.
- **[`docs/INTERNALS.md`](docs/INTERNALS.md) — a layer deeper.** What actually happens
  inside: backlog building, triage and claiming, model routing, the retry ladder,
  completion, review, and merge/revert reconciliation.
- **[`docs/USAGE.md`](docs/USAGE.md) — start here.** A worked example end to end, with
  real output. Its **"Which way in?"** table routes you by where you are starting from:
  a demo with no credentials, a new project from a paragraph, a plan you already wrote,
  or a project already half-built that you want to take on. Then: sync it, execute it,
  resume it, drive it from the API, and read the failures.
- **[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — running it as a service.** The two
  `serve` modes, what a supervised deployment must provide, and a non-destructive smoke
  test that distinguishes "healthy" from "able to run anything".
- [`examples/PLAN.md`](examples/PLAN.md) — the sample plan that walkthrough uses.
- [`docs/HARNESS-PLAN.md`](docs/HARNESS-PLAN.md) — the original plan. **Superseded in
  part:** it was written assuming one specific consumer, and the harness is now generic.
  Read it for the evidence and the reasoning, not the phase order, and see §0.1 for what
  changed.
- [`docs/MULTI-PROJECT-PLAN.md`](docs/MULTI-PROJECT-PLAN.md) — project scoping, the GUI
  that scoping makes possible, and project inception. Phases 0-2 are built.
- [`docs/AUDIT-PLAN.md`](docs/AUDIT-PLAN.md) — the durable audit layer: what is worth
  measuring and the rules that keep the numbers defensible.
- [`docs/backlog-seed-2026-08-02.json`](docs/backlog-seed-2026-08-02.json) — the manifest
  that seeded the GitHub issues on that date, plus the decision records `D1`–`D10`. It is
  a historical artefact: it has no state field and is not kept in sync. GitHub is the
  tracker (D1); this file cannot tell you whether anything is open.
- [`docs/COORDINATION-PLANE.md`](docs/COORDINATION-PLANE.md) — the typed work graph (§8,
  built) and the message ledger, rooms and oversight actor (**proposed, not built**).
- [`docs/MIGRATION-graph.md`](docs/MIGRATION-graph.md) — backing up, exporting, rebuilding
  and rolling back the queue's schema.
- [`docs/evidence/`](docs/evidence/) — append-only evidence packages, one per stage, each
  with a blind-spots section saying which of its own claims are untested.
  [`2026-08-04-programme-report.md`](docs/evidence/2026-08-04-programme-report.md) is the
  summary over all of them.
- [`AGENTS.md`](AGENTS.md) — binding rules of engagement for anyone, human or agent,
  working in this repository.

## Using it

A five-minute tour. The full walkthrough, with real output, is in
[`docs/USAGE.md`](docs/USAGE.md).

### 0. See it work, before you configure anything

```bash
agent-harness init --demo --into ./demo
```

That builds a real git repository, a one-item plan, a queue and a route in
`./demo`, and leaves the project **stopped**. It prints one command; run it and
the harness takes the item from plan to implement to apply to checks to commit
to review, and leaves a branch you can read.

No credentials, no network, no provider account, no GitHub. Nothing is pushed
and no repository is configured, so nothing here *can* reach GitHub.

**It proves the wiring and nothing else.** The model calls are answered by a
fixed script, so a green demo says the harness is plumbed together correctly.
It says nothing about whether a model writes a good diff, because there is no
model. Only the transport is replaced — the queue, the graph, the worktree, the
patch validator, the checks and the reviewer gate are the same code a real run
uses.

Then ask what a real run would need:

```bash
agent-harness --db ./demo/queue.sqlite doctor
```

`doctor` reports route completeness, which wire protocol and failure classifier
each route resolved to, whether the checkout and the check commands are
actually runnable, reviewer independence, whether the spend is visible, and
whether anything is permitted to mutate GitHub. It contacts nothing: asking a
model whether it answers is `--probe-models` and is otherwise reported as *not
asked*, which is not the same as passing.

### 1. Write a plan, get a backlog

Keep writing plans the way you already do. Items are recognised as `### T1: Title`
headings, `- [ ] T1 Title` checkboxes, or table rows with an id column; `labels:`,
`milestone:` and `depends on:` lines in the prose become metadata. A whole
dependency graph can also be drawn in one ```dependencies block as `W1 -> W2`.

A dependency names what **kind** of thing it waits for — work here, work in
another project, a human decision, or something outside the harness entirely
(`external:RESOLVER:IDENTITY`). A required target the graph cannot resolve
blocks the item and says why, rather than being assumed to be tracked
somewhere else.

```bash
agent-harness plan PLAN.md --repo owner/name --dry-run   # see what it would do
agent-harness plan PLAN.md --repo owner/name
```

Re-run it after editing the plan and it **updates** those issues rather than duplicating
them — matching is by a marker in the issue body, not by title, so improving the wording of
an item does not fork it into two.

It refuses a plan that states an id twice, because each id becomes one issue. It never
closes or reopens anything: the plan says what work *is*, the issue says where it *got to*.

### 1b. Or adopt a project that is already half-built

Most real projects are not blank. `adopt` reads the plan, the repository, the queue and
the repository's issues and pull requests, and proposes what is already delivered —
without writing a queue row, editing an issue or touching a branch.

```bash
agent-harness adopt PLAN.md --project widgets --work ./widgets --repo owner/name
agent-harness adopt PLAN.md --project widgets --work ./widgets --repo owner/name \
    --approve --approve-drop W1 --reconcile
```

Evidence is ranked and every rung is kept in the report: a checked plan item or a closed
issue naming the item, then the item's own `verify:` command, then an `assessor` model
with citations. **A proposal is never a decision** — nothing is dropped unless a human
names it, and uncertainty always resolves to "still to do". Ambiguous matches, competing
candidates and prior failed attempts are reported rather than resolved.

### 2. Execute it

```bash
agent-harness run --repo owner/name --work ./target \
    --plan PLAN.md --check 'pytest -q' \
    --planner MODEL --implementer MODEL --reviewer A-DIFFERENT-VENDOR
```

Each item gets its own git worktree, an agent, then checks, then a review, then a pull
request. **Cheap checks run before the reviewer** — paying a model to tell you the build is
broken is paying the dearest gate to catch what the cheapest one already did. Nothing is
ever committed to your default branch.

Kill it at any point and re-run: claims are leases, so whatever was in flight comes back on
its own.

### 3. Watch it

The session executor runs each agent as a terminal session in the host, so an item in
flight deep-links to the terminal doing the work — with scrollback, from a phone — and an
agent that stops to ask something surfaces as `waiting_for_input` rather than looking hung.

### Calling models directly

```python
from agent_harness.model_client import ModelClient, Route

client = ModelClient(
    roles={
        "planner": Route("a-strong-model", "https://api.example/v1", preset="chat-completions"),
        "implementer": Route(
            "a-cheaper-model", "https://api.example/v1", preset="chat-completions"
        ),
        # Reviewer independence: a different vendor, so a model is not
        # grading its own work.
        "reviewer": Route("another-vendor", "https://other.example/v1", preset="claw-bay"),
    },
    transport=my_http_call,  # you own the HTTP; this owns the policy
    on_event=lambda e: log.write(json.dumps(e) + "\n"),
)

client.call("implementer", messages)  # names a ROLE, never a model
```

`transport` is injected rather than imported, so the retry logic is testable without a
network and you can keep whatever HTTP client you already have.

A route names a **preset**: the wire protocol, the authentication strategy, the
response/usage reader and the failure classifier, as one name. Omit it and you get the
generic one, which claims nothing about any vendor — and cannot tell a spend cap from a
burst limit, because nothing in HTTP can. Adding a vendor is a new preset registered by
name; no core module changes, and nothing in core imports it. See
[`docs/INTERNALS.md`](docs/INTERNALS.md#route-presets-adding-a-vendor-without-touching-core).

### The API

There is **no GUI here on purpose.** The session host already owns tabs, auth,
push notifications, mobile and the terminal sessions agents run in; a second
web UI would mean a second URL and a second login to do the same job worse.
The harness serves JSON and the host renders it.

What it does own is a **documented API**: every route typed, every field
described, and the schema served next to it.

```
# Inception — describe a project, argue, approve
POST /api/inception                   a paragraph, not a plan
POST /api/inception/{id}/scope        propose, or revise with feedback
POST /api/inception/{id}/questions/{q} answer · defer · re-grade
POST /api/inception/{id}/approve      refused while a BLOCKING question is open
GET  /api/inception/{id}/plan         the proposal as a PLAN.md

# Projects — separate streams, no co-mingling
GET  /api/projects                    every project, counts and control, one call
POST /api/projects                    register one; it starts STOPPED
GET  /api/projects/{id}
POST /api/projects/{id}/start         the only thing that creates workers
POST /api/projects/{id}/stop          never interrupts work in flight

# Work
GET  /api/work                        backlog, counts and stale claims in one call
GET  /api/work/{id}                   one item
POST /api/work                        add items directly
POST /api/work/{id}/retry             re-queue; refuses while a claim is live
POST /api/plan/parse                  parse a plan, reporting what it could NOT read
POST /api/plan/sync                   plan -> GitHub issues, dry-run by default

# Audit — history that outlives the queue
GET  /api/audit/health                IS anything being recorded? (see below)
GET  /api/audit/cost                  spend by project, role and model
GET  /api/audit/delivery              what was delivered
GET  /api/audit/rollups               the long series, kept forever
GET  /api/audit/events                raw history, paged by row id
GET  /api/audit/baselines             what "better than before" is measured against
POST /api/audit/baselines             record one; immutable
POST /api/audit/reconcile             pull merged/reverted from GitHub
POST /api/audit/maintenance           roll up and thin now

# Live view and control
GET  /api/errors                      rate limits by class
GET  /api/events                      paged by row id, not timestamp
GET  /api/summary                     enough for a status line
GET  /api/control                     is the fleet claiming work?
POST /api/control                     pause, drain or resume
GET  /api/roles                       where each role's calls go, and which are called
PUT  /api/roles                       re-route a role, live
GET  /healthz                         open, cheap, needs no credential
```

**`/api/audit/health` is worth checking deliberately.** Audit writes are
dropped rather than raised when the store cannot be opened — observation must
never stop work — which means nothing else will tell you that history is not
being kept. A fleet running unaudited looks exactly like one running audited.

| | |
|---|---|
| `/docs` | Swagger UI, with an Authorize button |
| `/redoc` | ReDoc |
| `/openapi.json` | the schema — generate a client from it |

Auth is a bearer token, and inside a session host it is the **same token that
reaches the GUI**: one credential, one thing to rotate.

```bash
curl -H "Authorization: Bearer $TOKEN" localhost:8099/api/work
curl -H "Authorization: Bearer $TOKEN" localhost:8099/api/summary
curl -H "Authorization: Bearer $TOKEN" -X POST localhost:8099/api/work/T4/retry
```

Behind a proxy, pass `--root-path /api/harness` so the schema advertises URLs
a client can actually call.

[AIDevEnv](https://github.com/TheDancingDeveloper-org/aidevenv) is the
reference host and ships a Work tab that consumes this API.

```bash
uv sync --all-extras

# Idempotent: safe to re-run, safe on a timer, safe to run twice by mistake.
uv run agent-harness --db harness.sqlite ingest --events ./run/events.jsonl

# HARNESS_TOKEN is required; without one the service refuses every request
# rather than coming up open.
HARNESS_TOKEN=$(openssl rand -hex 16) \
  uv run agent-harness --db harness.sqlite serve --port 8099

# Supervised: the same API, plus a worker pool it can actually start.
# Still nothing runs until someone starts a project through the API.
HARNESS_TOKEN=$(openssl rand -hex 16) HARNESS_API_KEY=… \
  uv run agent-harness --db harness.sqlite serve --port 8099 \
    --session-host https://your-devenv.example \
    --reviewer claude-sonnet-4-6 --endpoint https://api.your-gateway.example
```

Without `--session-host` the service is **monitoring only**: everything reads,
and starting a project is refused rather than setting a flag no worker acts on.
Both modes, and the read-only check that tells them apart after a deploy, are
in [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

Keep it fed with `ingest --watch 30`. Optionally pass `--baseline TOTAL:DAYS:LABEL` to
compare against a prior measurement — there is no built-in number, because a baseline
belongs to a workload.

The service never writes to your logs and never writes to `events` — if it crashes, the
fleet does not notice.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```
