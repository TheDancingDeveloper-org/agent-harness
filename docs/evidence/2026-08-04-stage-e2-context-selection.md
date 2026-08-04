# Stage E2 context-selection report — 2026-08-04

**Status:** deterministic fixture passes; live NGMS rerun not yet measured.

## Configuration under test

- Commit: working tree based on `6a909a7962a8b9afb2750c61c81f9b1f6c5db4f0`.
- Executor: direct API `Executor` with a local scripted transport.
- Context policy: 600-character fixture budget; structured planner target
  `SECURITY.md`; no network, provider route, GitHub mutation or push.
- Fixture: generated temporary Git repository with a leading header in
  `SECURITY.md` and 80 empty service stubs.

This report describes repository-verifiable facts only. It is not evidence of
provider quality or a real-fleet run.

## Reproduction and result

```console
uv run pytest tests/test_executor.py -q
uv run ruff check src/agent_harness/executor.py tests/test_executor.py
uv run mypy src/agent_harness/executor.py tests/test_executor.py
```

Observed on 2026-08-04: 73 executor tests passed; lint and strict typing passed.

The regression test reconstructs the former smallest-file-first policy. With
the 600-character budget it fills the allowance from the empty stubs and omits
`SECURITY.md`. With the structured target and new policy, `SECURITY.md` is the
first supplied file and its leading header is visible. The same fixture proves
that a zero-context insertion cannot be applied above that header: the change
is refused and the tree remains unchanged. Denominators are one deterministic
old-policy selection, one new-policy selection, and one adversarial
wrong-location application.

Events asserted by the test contain:

- ordered planner targets and reasons;
- final supplied files;
- character budget, characters supplied and truncation state;
- every omitted file and reason;
- whether fallback path relevance was used.

Additional tests prove absolute paths, `..` traversal and escaping symlinks
cannot authorize a read; missing targets become uncertainty; empty files and
configured generated paths do not consume content budget unless named; and a
named target too large for the budget is reported explicitly.

## Costs and blind spots

- Model tokens, latency and reviewer cost: unmeasured; the transport is local.
- Token budget: unmeasured; this implementation records an exact character
  budget because the current transport has no generic tokenizer contract.
- Live NGMS improvement: unmeasured. Issue #146's 54-file/60,981-character
  observation has no preserved prompt checksum and is not reproducible from
  this repository alone.
- Session mode: unaffected; its implementer selects its own context inside a
  hosted CLI session and remains outside `ModelClient` telemetry (issue #128).

## Decision

**Continue to Stage E1 after Stage A acceptance.** The deterministic Stage E2
mechanics meet their fixture gate with zero wrong-location applications, but
do not call the live blocker closed until a versioned NGMS measurement appends
its context-selection events, configuration, raw artifact checksum and result.
