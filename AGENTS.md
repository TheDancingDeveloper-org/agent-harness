# AGENTS.md — agent-harness

Binding guidance for anyone, human or agent, working in this repository.

## The first rule: this framework is generic

It is **not tied to any project, language or workload**, and must not become so. That is an
owner ruling, not a preference.

Concretely, in the core — **the authoritative list is `EXECUTION_PATH` in
`tests/test_generic.py`, not a list here**, because a second list is a second thing to drift:

- no hardcoded log paths, file layouts or directory conventions;
- no numbers belonging to one workload — a baseline is *supplied*, never built in;
- no prose asserting one project's measurements as universal fact;
- no import of anything in `adapters/`, and **no dotted path naming one either**. A lazy
  import written as a string is still core knowing what a particular vendor is called.

Anything that must know a specific tool's format is an **adapter** (`adapters/`), opt-in and
imported lazily. If you find yourself wanting the core to know about a particular repo, the
answer is an adapter or a config value.

Adapters are reached **by name, through installed metadata**: route presets via the
`agent_harness.route_presets` entry points, dependency resolvers via
`agent_harness.dependency_resolvers`. That is what makes "add a vendor without editing
core" true rather than aspirational, and it applies to the ones this repository ships too.

This is enforced, not trusted. `tests/test_generic.py` failed during the Stage F merge
because two stages had each written the rule down and only one had encoded it.

The original design document [`docs/HARNESS-PLAN.md`](docs/HARNESS-PLAN.md) predates this
ruling and assumes a single named consumer throughout. Read it for evidence and reasoning;
do not follow its phase order or its coupling.

## Rules of engagement (binding)

Reproduced from §0.4 of the plan. These are not style preferences — each one is why a
specific measured failure happened.

1. **Measure before and after.** The 429 work is not done when the code is written; it is
   done when the error-class breakdown proves the number moved. §2.1 of the plan is the
   baseline.
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

## Standing instruction: the gates are the product

Everything else is scaffolding around them (§3.1). **A gate is never weakened to make the
scaffolding simpler.** If a change makes a gate cheaper, weaker, skippable or optional in
order to make the service, the dashboard or the dispatcher easier to build, it is the wrong
trade and it is rejected.

## Where things are

| Thing | Location |
|---|---|
| How to use it | `docs/USAGE.md` — worked example, real output |
| **Starting a project from nothing** | `docs/USAGE.md` §0 — the four routes in, and which to pick |
| **Taking on a project already in flight** | `agent-harness adopt`; `src/agent_harness/adoption.py`. **A proposal is never a decision** — nothing is dropped unless a human names it |
| The first run, needing no credentials | `agent-harness init --demo`; `src/agent_harness/demo.py` |
| What is configured and what is missing | `agent-harness doctor`; `src/agent_harness/doctor.py` — reports, spends nothing |
| How to deploy it | `docs/DEPLOYMENT.md` — the two serve modes, and a non-destructive smoke test |
| Sample plan | `examples/PLAN.md` |
| The original plan | `docs/HARNESS-PLAN.md` (superseded in part) |
| Issue tracker | GitHub, per D1. The only place an issue's *state* lives. |
| The manifest that seeded those issues | `docs/backlog-seed-2026-08-02.json` — historical, carries no state, not kept in sync |
| What a route is made of, and how a vendor is added | `src/agent_harness/protocols.py` |
| Event schema | `src/agent_harness/events.py` |
| Event store (append-only) | `src/agent_harness/store.py` |
| Scoping a project from a paragraph | `agent-harness inception`; `src/agent_harness/inception.py` — produces a `PLAN.md`, never queue rows |
| A plan, parsed into work | `src/agent_harness/plan.py` — never silently drops a heading |
| The queue, and what a claim is | `src/agent_harness/work.py` — a claim is a **lease**, not a lock |
| Direct-API agent loop | `src/agent_harness/executor.py` |
| Can this project finish an item? | `src/agent_harness/preflight.py` — refuses a start rather than failing every item |
| Typed dependency graph | `src/agent_harness/graph.py` — contract in `docs/COORDINATION-PLANE.md` §8 |
| What a gate answered, and what stopped an item | `src/agent_harness/outcomes.py` — **not** `providers.py`, which is what a *provider* answered |
| Where an attempt got to, durably | `src/agent_harness/attempts.py` — a fixed stage list, **not** a workflow engine |
| How long one item may take, and what it may spend | `src/agent_harness/budgets.py` — **not** a provider cost cap, and never parks an endpoint |
| An item waiting on a person | `src/agent_harness/holds.py` — a state of the item, **not** a projection over events, and **not** the coordination plane |
| Telemetry export | `src/agent_harness/adapters/otlp.py` — opt-in, lazily loaded, **export only**; the event store stays the source of truth |
| Queue schema migration | `docs/MIGRATION-graph.md` — backup, export, rebuild, rollback |
| Log readers | `src/agent_harness/ingest.py` |
| JSON API and in-process browser GUI | `src/agent_harness/api.py`; `src/agent_harness/ui.py` |
| Session host client | `src/agent_harness/session_host.py` |
| Agent loop | `src/agent_harness/session_executor.py` |
| The worker and its 13 gates | `swack-tools/oxidex` — `scripts/model_fix_loop.py` |
| The dispatcher (until P3 retires it) | `swack-tools/oxidex` — `scripts/parallel_model_fix_loop.py` |

**Corrected 2026-08-04.** This document used to say "P1 lands in `swack-tools/oxidex`, not
here — this repository does not contain the model client until P3 relocates dispatch into
the service." That has not been true for some time: `src/agent_harness/model_client.py` is
here, it is central, and the routing, retry ladder and endpoint parking all live in it.
The two `swack-tools/oxidex` rows above are history, kept because the evidence and the
reasoning behind the retry classifier came from that worker.

## Running the service locally

```bash
uv sync --all-extras

# See it work first: no credentials, no network, no model. Builds a real git
# repository, a plan and a queue, and prints the one command that runs them.
uv run agent-harness init --demo --into ./demo

# What would a real run need? Contacts nothing.
uv run agent-harness --db ./demo/queue.sqlite doctor

# Serve the API.
HARNESS_TOKEN=dev uv run agent-harness --db harness.sqlite serve --port 8099
```

`ingest` reads event streams and takes `--events PATH` (the harness's own JSONL, repeatable)
and/or `--adapter NAME --adapter-path DIR` for another tool's logs. It has no `--logs` flag;
an earlier version of this document said it did.

Adding `--session-host URL` makes it **supervised**: the same API, plus a
worker pool the API's start action can use. Without it, `serve` is
monitoring-only and starting a project is refused — both modes are supported,
and neither starts anything on its own. The deployment contract for each, and
the read-only smoke test that tells them apart, is
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

It has never run against a real fleet — see `README.md`. Do not describe it as proven.

## The API is a public surface

`src/agent_harness/api.py` + `schemas.py` serve a documented OpenAPI document
with Swagger UI. Treat it as a contract:

- Every route names a response model. A route returning a bare dict produces a
  schema of `{}` — valid, and useless to anyone generating a client.
- Every field carries a description. **The schema is the documentation.**
- Docs (`/docs`, `/redoc`, `/openapi.json`) need no token; the data does.
  Requiring a credential to read a schema makes an API undiscoverable for no
  benefit.
- Behind a proxy, `--root-path` must be set, or the schema advertises URLs the
  client cannot call.

Tests assert these properties, not just status codes — see
`tests/test_api.py`.

## The GUI belongs here

`agent-harness serve` owns and serves the browser GUI from the same process and
origin as its public JSON API. Templates, static assets, browser authentication,
tests and documentation live in this repository and ship in its distribution.

The GUI has no dependency on MyDevEnv, AIDevEnv or another session host: it must
not import their code, consume their assets or authentication, require their
proxy, or assume their session and terminal model. An optional session host may
still execute agents through the generic `session_host` protocol; that execution
adapter is not the owner or host of the GUI.

HTML controllers delegate to the same typed query and command services as JSON
routes. They never read SQLite directly or duplicate a gate. Browser actions
require authenticated operator identity, CSRF validation and explicit review;
navigation and drag-and-drop never imply permission for a state transition.
The JSON API remains public, typed and documented, and normal GUI operation
must not require a CDN or a separately deployed frontend.

## Two invariants the store must keep

1. **It is read-only, in both directions.** It never writes to the harness's logs, and
   nothing but the ingester writes to `events`. There is no UPDATE and no DELETE anywhere
   in `store.py`, and a test enforces that against the source (risk R3). If a change needs
   to mutate an event, it has to delete that test first — which is the point.
2. **It never fabricates the number P1 exists to produce.** Rate limits from the
   pre-classification logs are `unclassified`, counted separately, and never folded into
   `rpm` / `window_cap` / `terminal_cap`. The baseline is a total; the successor is a
   breakdown; the panel says so. Removing that caveat to make the page tidier would be
   presenting a delta that does not exist.

## The four gates, and the one thing that will waste your afternoon

Every change passes all four, from the repository root:

```console
TMPDIR=/path/on/a/fast/volume uv run pytest
uv run ruff check .
uv run ruff format --check .
TMPDIR=/path/on/a/fast/volume uv run mypy
```

`mypy` is configured for **full-project strict** typing, over `src` and `tests` both.

**Set `TMPDIR` first.** The suite creates temporary git repositories heavily. On a machine
whose `/tmp` is a slow shared disk, three runs of an *identical* tree took 276s, 587s and
617s against a ~49s baseline; pointing `TMPDIR` at a fast volume brought a full run to
116s. A slow suite is not evidence of a defect, and **no timing taken without doing this is
worth quoting**.

## Engineering practice

- **Red-first** for the retry classifier and anything else that decides whether the fleet
  stalls. **No sleeping in tests** — inject the sleep function.
- Service tests run in-process over ASGI via `fastapi.testclient.TestClient`, with a temp
  SQLite file per test. (The plan says `httpx.ASGITransport`; that transport is async-only
  in the pinned httpx, so the sync `TestClient` wraps it. Same thing, no ports.)
- `main` is protected: PR required, `lint` and `test` must pass, linear history.
- Events are append-only. Views are projections over them, never a second source of truth,
  never written back (§3.5).

## Decision hygiene

**Settled — do not re-litigate.** D1–D6 (plan §0.2b). D10, resolved by Stage E1: retain the
model-authored unified diff. D11, resolved 2026-08-04: a resumed attempt **continues** the
existing one, so `max_attempts` bounds genuine failures rather than crashes. D12, resolved
2026-08-04: a hold **suspends the lease and keeps the claim**. D13 and D14 are recorded with
the safe answer taken — telemetry is export-only, and budget ceilings default to unlimited
so an upgrade changes no behaviour.

**Open, and each blocks something.** D7. D8 — whether third-party gates get a registration
mechanism; it became load-bearing for the typed check outcomes and was deliberately not
answered, and a test fails if `outcomes.py` ever grows a registry. D9 — blocked on #84, and
**no stage may hold the review prompt as a variable** while it is.

D1–D10 are recorded in `docs/backlog-seed-2026-08-02.json`; D11–D14 in
`docs/PROPOSAL-2026-08-finish-then-extend.md` §11.

**Do not guess at a blocked decision to unblock yourself — say it is blocked.**
