# agent-harness

A service-hosted, GUI-driven harness for running fleets of coding agents against a
repository: role-routed models (plan / implement / review), a durable gate pipeline, and
GitHub issues as the work queue.

It is an extension of the model-fix loop that already runs in `swack-tools/oxidex` — not a
replacement for it. The gate pipeline is the product; everything here is scaffolding around
it.

## Status: pre-alpha — runs locally, not deployed, not yet proven against a live fleet

The dashboard runs and serves all five panels. It has never been pointed at a real fleet:
every number below was produced from synthetic logs, because the machine it was built on is
not the fleet host. Nothing here is proven until it ingests real traffic.

| Phase | What it delivers | State |
|---|---|---|
| P0 | Repository, labels, milestones, backlog, CI, branch protection | **done** |
| P1 | Model-layer fix — lands in `swack-tools/oxidex`, not here | code in review ([oxidex#417](https://github.com/swack-tools/oxidex/pull/417)); **the 72-hour measurement, which is the actual deliverable, has not been run** |
| P2 | Read-only dashboard (FastAPI + SQLite + HTMX/SSE) | code done; **not deployed** (blocked on D7), no 48-hour soak |
| P3 | Service owns dispatch | not started |
| P4 | Generalise beyond oxidex | not started |

### What the dashboard will and will not tell you

It breaks 429s into `rpm`, `window_cap` and `terminal_cap` — the question the harness could
not answer for eight days. It will **not** show you a per-class before/after delta, because
there isn't one to show: the 27,662-error baseline is a total, and the harness that produced
it never recorded the discriminator, so no per-class value exists or can be recovered.
Historical rate limits are counted as `unclassified` and shown separately rather than being
folded into a class. Every panel that could imply otherwise says so on the page.

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

## Running it

```bash
uv sync --all-extras

# Read the harness's logs into the store. Idempotent — safe to re-run and
# safe on a timer; re-ingesting the same history inserts nothing.
uv run agent-harness --db harness.sqlite ingest --logs ~/.oxidex/logs

# Serve. HARNESS_TOKEN is required; without one the service refuses every
# request rather than coming up open.
HARNESS_TOKEN=$(openssl rand -hex 16) \
  uv run agent-harness --db harness.sqlite serve --port 8099
```

Keep it fed with `ingest --watch 30`. The service never writes to the harness's logs, and
never writes to `events` — if it crashes, the fleet does not notice.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```
