# AGENTS.md — agent-harness

Binding guidance for anyone, human or agent, working in this repository.

## The first rule: this framework is generic

It is **not tied to any project, language or workload**, and must not become so. That is an
owner ruling, not a preference.

Concretely, in the core (`providers`, `model_client`, `store`, `ingest`, `sources`, `app`):

- no hardcoded log paths, file layouts or directory conventions;
- no numbers belonging to one workload — a baseline is *supplied*, never built in;
- no prose asserting one project's measurements as universal fact;
- no import of anything in `adapters/`.

Anything that must know a specific tool's format is an **adapter** (`adapters/`), opt-in and
imported lazily. If you find yourself wanting the core to know about a particular repo, the
answer is an adapter or a config value.

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
| The plan | `docs/HARNESS-PLAN.md` |
| Backlog manifest (seeds GitHub issues) | `docs/backlog.json` |
| Event schema | `src/agent_harness/events.py` |
| Event store (append-only) | `src/agent_harness/store.py` |
| Log readers | `src/agent_harness/ingest.py` |
| Dashboard | `src/agent_harness/app.py` + `templates/` |
| The worker and its 13 gates | `swack-tools/oxidex` — `scripts/model_fix_loop.py` |
| The dispatcher (until P3 retires it) | `swack-tools/oxidex` — `scripts/parallel_model_fix_loop.py` |

Note that **P1 lands in `swack-tools/oxidex`, not here.** This repository does not contain
the model client until P3 relocates dispatch into the service.

## Running the service locally

```bash
uv sync --all-extras
uv run agent-harness --db harness.sqlite ingest --logs ~/.oxidex/logs
HARNESS_TOKEN=dev uv run agent-harness --db harness.sqlite serve --port 8099
```

It has never run against a real fleet — see `README.md`. Do not describe it as proven.

## Two invariants the dashboard must keep

1. **It is read-only, in both directions.** It never writes to the harness's logs, and
   nothing but the ingester writes to `events`. There is no UPDATE and no DELETE anywhere
   in `store.py`, and a test enforces that against the source (risk R3). If a change needs
   to mutate an event, it has to delete that test first — which is the point.
2. **It never fabricates the number P1 exists to produce.** Rate limits from the
   pre-classification logs are `unclassified`, counted separately, and never folded into
   `rpm` / `window_cap` / `terminal_cap`. The baseline is a total; the successor is a
   breakdown; the panel says so. Removing that caveat to make the page tidier would be
   presenting a delta that does not exist.

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

D1–D6 are settled (plan §0.2b) — do not re-litigate them. D7, D8 and D9 are open and each
blocks a phase; they are tracked as `type:decision` issues. Do not guess at a blocked
decision to unblock yourself — say it is blocked.
