# agent-harness

A service-hosted, GUI-driven harness for running fleets of coding agents against a
repository: role-routed models (plan / implement / review), a durable gate pipeline, and
GitHub issues as the work queue.

It is an extension of the model-fix loop that already runs in `swack-tools/oxidex` — not a
replacement for it. The gate pipeline is the product; everything here is scaffolding around
it.

## Status: pre-alpha — not usable

There is no service to run yet. This repository currently contains the plan, the backlog
and CI. Phase P0 (repository and guardrails) is the only phase with any work on `main`.

| Phase | What it delivers | State |
|---|---|---|
| P0 | Repository, labels, milestones, backlog, CI, branch protection | in progress |
| P1 | Model-layer fix — lands in `swack-tools/oxidex`, not here | not started |
| P2 | Read-only dashboard (FastAPI + SQLite + HTMX/SSE) | not started |
| P3 | Service owns dispatch | not started |
| P4 | Generalise beyond oxidex | not started |

## Definition of done for v1

From Appendix A of the plan. Expressed as observed behaviour, not internal completeness.

- [ ] The fleet runs 7 days unattended with no manual restart.
- [ ] Every failure is diagnosable from the GUI alone, without opening a log file.
- [ ] Rate-limit errors are classified, and cost caps are never retried.
- [ ] No single worker's failure pauses another worker.
- [ ] Reviewer-approved work survives a killed worker.
- [ ] Delivery rate is no worse than the 56% baseline, at lower cost.
- [ ] The role→model map can be changed without a redeploy.

If all seven hold, v1 is done regardless of what remains unimplemented.

## Documentation

- [`docs/HARNESS-PLAN.md`](docs/HARNESS-PLAN.md) — the plan: evidence base, decisions,
  architecture, phase plan. Read §0 first.
- [`docs/backlog.json`](docs/backlog.json) — the machine-readable backlog that seeds the
  GitHub issues.
- [`AGENTS.md`](AGENTS.md) — binding rules of engagement for anyone, human or agent,
  working in this repository.

## Development

```bash
uv sync --all-extras
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```
