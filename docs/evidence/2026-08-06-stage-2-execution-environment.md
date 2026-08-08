# Stage 2 implementation evidence — execution environment

**Date:** 2026-08-06  
**Scope:** generic contract, Docker/OCI backend wiring, and local backend
tests. No real workload was run.

The Stage 1 loop no longer has to inherit the controller shell. With a selected
execution backend, the executor first creates a disposable, self-contained Git
checkout for the item at the exact selected base SHA; the role loop never edits
the controller checkout directly. A linked worktree is not sufficient here:
its `.git` file points at controller-only metadata that must not be mounted into
the item container. Core defines
the item-scoped execution-environment contract in
[`execution_environment.py`](../../src/agent_harness/execution_environment.py)
and resolves a selected implementation by the
`agent_harness.execution_environments` entry-point group. The shipped Docker
adapter creates one container per item, mounts only the worktree and declared
paths, passes only explicitly supplied environment variables, drops all Linux
capabilities, enables `no-new-privileges`, uses a non-root identity, applies
resource limits and removes the container on completion or startup failure.

The command line accepts `--environment-backend docker`, a pinned
`--environment-image`, `--environment-network bridge|none`, and repeatable
`--environment-mount SOURCE:TARGET[:rw]` values. `doctor` reports the selected
backend and image before work can be claimed. Environment evidence records
variable names and image digest, never variable values.

## Local checks

```console
uv run pytest -q tests/test_execution_environment.py \
  tests/test_execution_environment_live.py tests/test_agent_loop_e2e.py \
  tests/test_role_runner_e2e.py tests/test_generic.py
58 passed, 2 skipped

TMPDIR=/tmp uv run pytest
1630 passed, 3 skipped

uv run ruff check .
All checks passed!

uv run ruff format --check .
143 files already formatted

TMPDIR=/tmp uv run mypy
Success: no issues found in 138 source files
```

The Docker command-construction tests use a controlled Docker CLI seam to
assert the actual security flags, managed item labels, mount modes, allow-listed
environment and teardown calls. A restarted worker now maps each project/item
to a deterministic disposable checkout, reaps only a stale container carrying
that exact worktree label, and removes the stale checkout before reuse. The
host in this evidence environment has Docker CLI 29.7.0,
but no reachable Docker daemon (`dial unix /var/run/docker.sock: connect: no
such file or directory`). Therefore the Stage 2 exit criterion is **not
met**: repository-wide reads/writes, sibling refusal, network access and clean
teardown have not yet been exercised against a live configured backend.

This evidence authorises no real workload run. A live-daemon acceptance run is
still required before Stage 2 can exit. Stage 3 wiring may be developed against
fixture backends, but its acceptance also remains pending until the live
backend and failure-isolation criteria are exercised.
