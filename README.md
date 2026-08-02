# agent-harness

A generic harness for running fleets of coding agents: role-routed models, a failure model
that can tell a burst limit from a spent budget, and a dashboard that makes both visible.

It is **not tied to any particular project, language or workload.** You supply the roles,
the provider, and what the agents do; the harness supplies routing, a retry policy that
does not take the fleet down with it, and the measurement.

## Status: pre-alpha — runs locally, not deployed, not yet proven against a live fleet

The model client and the dashboard both run. Neither has been pointed at a real workload:
every number seen so far came from synthetic traffic. Nothing here is proven until it runs
against something real.

| Piece | What it does | State |
|---|---|---|
| `providers` | Classifies a provider's failures — burst limit vs spent window vs spent cap vs refused | done |
| `model_client` | Routes roles to models; per-worker jittered retry; per-endpoint parking; event emission | done |
| `store` / `ingest` | Append-only SQLite event store; idempotent ingest from any source | done |
| `api` | Headless JSON API — no GUI; the session host renders it as a Work tab | done |
| `adapters` | Opt-in readers for other tools' logs | one example |
| Dispatch (queue, claims, worker supervision) | — | not started |
| Gates / work definition | — | not started; deliberately not designed before there is a real workload |

### The one idea worth stealing

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
docstrings, with the live evidence that corrected it.

## Definition of done for v1

Expressed as observed behaviour, not internal completeness.

- [ ] The fleet runs 7 days unattended with no manual restart.
- [ ] Every failure is diagnosable from the GUI alone, without opening a log file.
- [ ] Rate-limit errors are classified, and cost caps are never retried.
- [ ] No single worker's failure pauses another worker.
- [ ] Reviewer-approved work survives a killed worker.
- [ ] Delivery rate is no worse than the workload's own pre-harness baseline, at lower cost.
- [ ] The role→model map can be changed without a redeploy.

If all seven hold, v1 is done regardless of what remains unimplemented.

## Documentation

- [`docs/HARNESS-PLAN.md`](docs/HARNESS-PLAN.md) — the original plan. **Superseded in
  part:** it was written assuming one specific consumer, and the harness is now generic.
  Read it for the evidence and the reasoning, not the phase order, and see §0.1 for what
  changed.
- [`docs/backlog.json`](docs/backlog.json) — the machine-readable backlog that seeds the
  GitHub issues.
- [`AGENTS.md`](AGENTS.md) — binding rules of engagement for anyone, human or agent,
  working in this repository.

## Running it

### Calling models

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
```

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
