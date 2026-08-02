# AGENTS.md — agent-harness

Binding guidance for anyone, human or agent, working in this repository. The canonical
design document is [`docs/HARNESS-PLAN.md`](docs/HARNESS-PLAN.md); read §0, §3 and §5 before
writing code.

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
| Event schema | `src/agent_harness/events.py` — **not yet written** (P2, issue T21). Until it exists, the append-only `events` table in the plan §5 is the only specification. |
| The worker and its 13 gates | `swack-tools/oxidex` — `scripts/model_fix_loop.py` |
| The dispatcher (until P3 retires it) | `swack-tools/oxidex` — `scripts/parallel_model_fix_loop.py` |

Note that **P1 lands in `swack-tools/oxidex`, not here.** This repository does not contain
the model client until P3 relocates dispatch into the service.

## Running the service locally

There is no service yet (pre-alpha; see `README.md`). When P2 lands, this section states
the command. Do not describe unbuilt behaviour in the present tense — §0.4 rule 6 applies
to documentation.

Toolchain, available now:

```bash
uv sync --all-extras
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

## Engineering practice

- **Red-first** for the retry classifier and anything else that decides whether the fleet
  stalls. **No sleeping in tests** — inject the sleep function.
- Service tests use `pytest` + `httpx.ASGITransport` against the FastAPI app, with a temp
  SQLite file per test.
- `main` is protected: PR required, `lint` and `test` must pass, linear history.
- Events are append-only. Views are projections over them, never a second source of truth,
  never written back (§3.5).

## Decision hygiene

D1–D6 are settled (plan §0.2b) — do not re-litigate them. D7, D8 and D9 are open and each
blocks a phase; they are tracked as `type:decision` issues. Do not guess at a blocked
decision to unblock yourself — say it is blocked.
