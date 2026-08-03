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

## Status: pre-alpha — every path is tested, none is proven against a real workload

It runs, it is deployed inside [AIDevEnv](https://github.com/TheDancingDeveloper-org/aidevenv),
and every stage has been driven end to end against a real git repository with a scripted
model. **It has never executed a real agent against a real provider.** Nothing here is
proven until it does.

| Module | What it does |
|---|---|
| `plan` | Parses a markdown plan into work items, reporting what it could **not** read |
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
  real output: write a plan, sync it, execute it, resume it, drive it from the API, and
  read the failures.
- [`examples/PLAN.md`](examples/PLAN.md) — the sample plan that walkthrough uses.
- [`docs/HARNESS-PLAN.md`](docs/HARNESS-PLAN.md) — the original plan. **Superseded in
  part:** it was written assuming one specific consumer, and the harness is now generic.
  Read it for the evidence and the reasoning, not the phase order, and see §0.1 for what
  changed.
- [`docs/MULTI-PROJECT-PLAN.md`](docs/MULTI-PROJECT-PLAN.md) — project scoping, the GUI
  that scoping makes possible, and project inception. Phases 0-2 are built.
- [`docs/AUDIT-PLAN.md`](docs/AUDIT-PLAN.md) — the durable audit layer: what is worth
  measuring and the rules that keep the numbers defensible.
- [`docs/backlog.json`](docs/backlog.json) — the machine-readable backlog that seeds the
  GitHub issues.
- [`AGENTS.md`](AGENTS.md) — binding rules of engagement for anyone, human or agent,
  working in this repository.

## Using it

A five-minute tour. The full walkthrough, with real output, is in
[`docs/USAGE.md`](docs/USAGE.md).

### 1. Write a plan, get a backlog

Keep writing plans the way you already do. Items are recognised as `### T1: Title`
headings, `- [ ] T1 Title` checkboxes, or table rows with an id column; `labels:`,
`milestone:` and `depends on:` lines in the prose become metadata.

```bash
agent-harness plan PLAN.md --repo owner/name --dry-run   # see what it would do
agent-harness plan PLAN.md --repo owner/name
```

Re-run it after editing the plan and it **updates** those issues rather than duplicating
them — matching is by a marker in the issue body, not by title, so improving the wording of
an item does not fork it into two.

It refuses a plan that states an id twice, because each id becomes one issue. It never
closes or reopens anything: the plan says what work *is*, the issue says where it *got to*.

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
from agent_harness import providers
from agent_harness.model_client import ModelClient, Route

client = ModelClient(
    roles={
        "planner": Route("a-strong-model", "https://api.example", providers.CLAW_BAY),
        "implementer": Route("a-cheaper-model", "https://api.example", providers.CLAW_BAY),
        # Reviewer independence: a different vendor, so a model is not
        # grading its own work.
        "reviewer": Route("another-vendor", "https://api.example", providers.CLAW_BAY),
    },
    transport=my_http_call,  # you own the HTTP; this owns the policy
    on_event=lambda e: log.write(json.dumps(e) + "\n"),
)

client.call("implementer", messages)  # names a ROLE, never a model
```

`transport` is injected rather than imported, so the retry logic is testable without a
network and you can keep whatever HTTP client you already have.

If your provider is not one of the two shipped, write a `classify` — `GENERIC` works, but
it cannot tell a spend cap from a burst limit, because nothing in HTTP can.

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
GET  /api/roles                       where each role's calls go
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
