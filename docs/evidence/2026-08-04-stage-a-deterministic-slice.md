# Stage A deterministic safety-slice report — 2026-08-04

**Status:** Stage A deterministic acceptance met; external provider and hosted-session
validation remain unmeasured.

## Configuration under test

- Implementation commit: `34550dbbf23f05d468d5440f4f25561d6d8fbb26`, integrated
  with Stage E2 on branch `fix/validator-rejects-valid-patches`.
- Base commit: `6a909a7962a8b9afb2750c61c81f9b1f6c5db4f0`.
- Test transport: in-process `DeterministicTransport`, with no network calls,
  provider credentials, remote pushes or GitHub mutations.
- Repository: a generated temporary Git repository containing a package
  boundary, source and tests, a leading module docstring, creation, deletion,
  rename and 24 irrelevant-file cases.
- Executors: direct API `Executor` and hosted `SessionExecutor` using a
  deterministic in-process session-host fixture.
- External side-effect boundary: a recording GitHub fake asserting draft-PR,
  verdict-comment and ready-for-review behavior.
- Event storage/projection: the real append-only `AuditStore` and authenticated
  FastAPI audit and work projections.

This is repository-verifiable deterministic evidence. It is not a real-fleet,
provider-quality, GitHub-service or hosted-session measurement.

## Reproduction and result

```console
uv run pytest tests/test_stage_a_e2e.py tests/test_executor.py -q
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Observed after integration on 2026-08-04:

- the Stage A module collected 25 test cases covering all 13 required
  scenarios and their failure/protocol matrices;
- the combined Stage A and executor-context tests passed;
- all 676 repository tests passed in 49.418 seconds wall-clock time;
- lint, formatting and strict source typing passed.

No sleeps wait for provider time in the fixture. The one slow-worker scenario
uses short real thread scheduling intervals to exercise the lease heartbeat;
its assertions wait on observable queue state rather than assuming thread
start order.

## Scenario evidence

The deterministic slice proves:

1. A plan-shaped queue item crosses the real SQLite claim path, planner and
   implementer routes, real Git apply, configured checks, a checkpoint commit,
   draft-PR boundary, independent review and a done outcome. Queue, branch
   tree, recorded GitHub effects and authenticated audit projection are all
   asserted.
2. Required dependencies gate claims.
3. A mid-flight dependency correction is observed before review or a durable
   checkpoint; the item returns pending and the temporary branch is abandoned.
4. Identical re-ingestion neither duplicates work nor resets completion,
   attempts or branch identity.
5. A fallback route answers before backoff and its model, endpoint, role and
   fallback outcome are present in the event stream.
6. Exhausted routes produce an explicit refusal without checks, review or an
   external side effect.
7. A terminal spend cap is not retried and returns the item without consuming
   its attempt; a refusal does not park a healthy endpoint.
8. One project worker's death fails only its claim while a sibling project
   remains running with its own claim.
9. Heartbeats keep a slow, healthy claim beyond its nominal lease.
10. Concurrent claims remain unique, and reducing fleet size does not kill an
    item already in flight.
11. Failed cheap checks prevent reviewer traffic.
12. Valid and derivably over-counted diffs apply; truncated, zero-context and
    prose responses are refused without changing the named source at the wrong
    location. Creation, deletion and rename are checked in the happy tree.
13. Both direct API and session executors commit a checkpoint before reviewer
    transport failure. The branch survives, is explicitly marked
    `Reviewed: not yet`, and the `checkpointed` event precedes the reviewer
    error.

The transport conformance portion separately covers burst limit, short-window
cap, terminal cap, refusal, 5xx, timeout, malformed success and slow healthy
success. Real `httpx` timeout and network exception families are normalized at
the CLI transport boundary into the same vendor-neutral retry contract.

## Costs and blind spots

- Provider input/output tokens, model latency and monetary cost: unmeasured;
  the transport is local and scripted.
- Remote GitHub behavior and push authentication: unmeasured. The tests assert
  the public GitHub-client boundary without changing external state.
- Hosted session-host process and PTY behavior: unmeasured. The session fixture
  exercises the executor contract in-process.
- Session implementer model traffic remains outside `ModelClient` telemetry,
  so its model, tokens and provider latency are not represented. The session
  work/checkpoint/reviewer events are represented.
- Process-level crash recovery after a remote push is not induced. Existing
  lost-PR/reconciliation tests remain the evidence for restart recovery; this
  slice proves worker exception isolation and checkpoint ordering.
- This result says nothing about a particular workload's success rate. The
  first sustained-workload record remains a historical reconstruction with
  the limitations stated in its own evidence package.

## Decision

**Stage A's deterministic acceptance gate is met. Continue to Stage E1.**
The result establishes composition of the required safety seams, not live
fleet fitness. A later live run must append a new package with immutable raw
artifacts, routes, costs and a single run denominator rather than editing this
report.
