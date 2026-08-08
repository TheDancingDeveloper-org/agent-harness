# Stage 1 evidence — generic role runner

**Date:** 2026-08-06
**Scope:** local fixture repositories only; no provider, GitHub, AIDevEnv,
session-host or CLI-agent process was contacted.

**Implementation commit:** `aabdfdb` (`Add metadata-selected implementer role
runner`). This is a local commit only; nothing was pushed or published.

This package records the exit evidence for Stage 1 in
[`docs/STATUS.md`](../STATUS.md). It is deliberately not a real-workload
acceptance record. Stage 2 (OS-enforced confinement) is still required before a
secret-bearing or real repository may be run.

## What was exercised

The installed `agent-loop` entry point was selected by name. The fixture
implementer made multiple tool calls, inspected the initial file, ran a
feedback check, edited a tracked file, and in a separate case created an
untracked file. The executor then captured the complete candidate tree and sent
it through the existing diff validation, authoritative checks, checkpoint,
review and attempt-record paths. A policy refusal was terminal. Step and
item-spend bounds were exercised, including a resumed item with an earlier
unpriced call; the latter keeps the spend total as a lower bound rather than
re-enabling a misleading dollar ceiling.

Model events in the fixture carry `project_id`, `item_id` and `work_attempt`.
The implementation artefact records the runner name, call count and submission;
the runner-started event and doctor output record its implementation version.
The queue records priced and unpriced calls and cumulative spend; the CLI event
fan-out also writes the same attributed events to its append-only audit sink.
Doctor and preflight report or refuse the selected metadata entry point.

## Commands and results

All commands were run from the repository root with the repository's required
fast temporary directory:

```console
TMPDIR=/home/sprooty/Working/Active/apps/.agent-harness-tmp.8pjfQd \
  uv run pytest -q tests/test_role_runner_e2e.py tests/test_role_runners.py \
  tests/test_agent_loop_e2e.py tests/test_preflight.py tests/test_generic.py
89 passed

uv run ruff check .
All checks passed!

uv run ruff format --check .
all files already formatted

TMPDIR=/home/sprooty/Working/Active/apps/.agent-harness-tmp.8pjfQd uv run mypy
Success: no issues found in 121 source files
```

The full repository suite was rerun after this package was written:

```console
TMPDIR=/home/sprooty/Working/Active/apps/.agent-harness-tmp.8pjfQd uv run pytest
1509 passed, 1 skipped in 448.59s (0:07:28)
```

The working tree was then checked with the repository-wide lint, format and
strict typing gates shown above. The focused command collected 89 tests; the
full-suite count is the regression denominator.

## Acceptance table

| Criterion | Result | Test/evidence |
|---|---|---|
| Generic contract and installed-metadata lookup | pass | `tests/test_role_runners.py`, `tests/test_generic.py` |
| Multi-turn inspect/edit/test through the executor | pass | `test_loop_changes_feed_the_existing_checks_review_and_attempt_pipeline` |
| New files and local candidate tree are retained | pass | `test_a_new_file_created_by_the_loop_reaches_the_authoritative_pipeline` |
| Feedback checks do not replace authoritative checks | pass | fixture check runs before edit, after edit, and in the harness gate |
| Project/item/attempt call attribution | pass | item-scoped model event assertions in the e2e test |
| Terminal policy refusal | pass | `test_a_policy_refusal_is_terminal_in_the_harness_path` |
| Whole-loop step and item-spend bounds | pass | the two bound tests in `tests/test_role_runner_e2e.py` |
| Unknown pricing remains a lower bound across attempts | pass | unpriced-loop tests in `tests/test_agent_loop_e2e.py` and role-runner e2e |
| Direct/session paths remain available | pass | full repository suite |
| OS-enforced confinement and fleet concurrency | pending | Stage 2 and Stage 3; not authorised by this evidence |

## Boundary

This is local scripted evidence of the Stage 1 seam, not delivery evidence. No
real item was merged or pushed, no pull request was opened, and no claim is made
about delivery rate, cost per merged item, unattended reliability or portability
to a second repository. The next implementation block is Stage 2: replace the
adapter's inherited controller shell with an OS-enforced execution boundary and
test it before any real workload run.
