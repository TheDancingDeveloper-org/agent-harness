# Stage 4 implementation evidence — local plan integration

**Date:** 2026-08-07  
**Scope:** local fixture repositories only; no provider, remote, GitHub,
AIDevEnv, session-host or CLI-agent process was contacted.

This package records the local evidence for the Stage 4 implementation slice in
[`docs/STATUS.md`](../STATUS.md). It does not authorise a real workload run or
claim the Stage 4 exit while the live execution boundary and Stage 3 failure
isolation criteria remain pending.

## What was exercised

The fleet ran a project with two independent items (`A` and `B`) and a
dependent item (`C`) using two workers, the generic executor factory and a
fixture execution backend. Each independent item ran from the same plan base.
Their item changes were promoted serially to the durable local plan branch.
Only after both promotion records existed did the queue release `C`; its
runner observed both promoted files in its checkout. Queue admission now
checks those promotion records inside the claim transaction, so an item that
is merely `done` cannot race ahead of its integration branch. The test also
covers the conflicting-promotion path and verifies that a conflict leaves the
plan head unchanged and records no successful promotion for the conflicting
item. Advisory dependencies remain reportable without becoming mandatory
promotion prerequisites. Promotion records are marked `applying` before the
Git ref update; coordinator startup reconciles that record against the old and
new ref, so a crash around the ref update is recoverable rather than silently
becoming plan drift.

The coordinator now serialises first-use plan creation as well as promotion,
including across separate coordinator instances in the same serving process
and across processes through a durable, expiring SQLite lease with heartbeat.
An actual two-process fixture holds one process inside the authoritative gate,
proves the other waits without entering its gate, and then verifies both
promotions complete on the shared branch.
The executor computes the candidate diff against the immutable item base SHA.
This keeps a dependent self-contained checkout independent of controller local
branch refs while still preserving its complete candidate tree. A
promotion's plan-head projection and successful promotion history are written
in one SQLite transaction, so a restart cannot expose an advanced head without
the prerequisite fact that releases dependants.

A promotion conflict is a repairable item outcome at the executor boundary.
The runtime preserves the typed conflict status, the item returns to `pending`
with a `withheld/plan_promotion_conflict` disposition, its retry budget is not
consumed, and the detail names the exact current plan head to repair against.
Other coordinator errors remain escalated rather than being silently retried.

When the target branch moves, the coordinator journals the refresh before
replaying any promoted item. It rebuilds from the new target SHA, reapplies the
recorded item deltas in promotion order, reruns the authoritative checks after
each replay, and advances the local plan ref and projection only after the
rebuild is ready. A conflict or failed gate closes as a durable refresh result
and leaves the prior plan untouched. If the process stops after the Git ref
update, startup reconciles the refresh journal and completes the projection.
The target is also revalidated around item integration: a target move during
the authoritative gate supersedes the stale promotion attempt and retries from
the newer target. Promotion events retain the immutable promotion id, item
commit, item base, prior and resulting plan heads, target SHA, status and
detail in the existing event stream; the item commit is also retained as the
second parent of the local
plan merge commit. Event delivery is diagnostic and cannot make a durable
promotion fail.

## Commands and results

```console
uv run pytest -q tests/test_plan_integration.py
26 passed

TMPDIR=/tmp uv run pytest -q
passed at 100%; Docker-dependent tests were skipped because this host has no
reachable Docker daemon.

TMPDIR=/tmp uv run pytest -q tests/test_plan_integration.py tests/test_generic.py
35 passed

uv run ruff check .
All checks passed!

uv run ruff format --check .
145 files already formatted

TMPDIR=/tmp uv run mypy
Success: no issues found in 140 source files

git diff --check
clean
```

The full-suite run is the repository regression denominator for this slice.
The Docker skips are expected environment limitations, not passing evidence
for the live execution boundary. The implementation remains in the shared
working tree and has not been pushed or published.

## Acceptance table

| Criterion | Result | Test/evidence |
|---|---|---|
| Exact target SHA and durable plan identity | pass | `test_plan_branch_is_created_from_exact_target_and_survives_restart` |
| Serialised independent promotions | pass | `test_independent_promotions_then_dependent_sees_both` and fleet acceptance |
| Separate coordinator instances and processes share serialized promotion ownership | pass | `test_separate_coordinators_serialize_integration_gates`, `test_plan_promotion_lease_blocks_live_owner_and_allows_expiry_takeover` |
| Separate OS processes do not overlap authoritative promotion gates | pass | `test_separate_processes_serialize_authoritative_promotion_gates` |
| Older item base is replayed onto the current plan head | pass | `test_promotion_replays_item_created_from_older_plan_head` |
| Item commits remain in plan-branch ancestry after promotion and refresh replay | pass | `test_promotion_replays_item_created_from_older_plan_head`, `test_target_move_rebuilds_plan_and_replays_promoted_items` |
| Dependent item commits replay cleanly after a target move | pass | `test_target_move_replays_dependent_item_created_from_promoted_plan_head` |
| Queue admission waits for every prerequisite promotion | pass | `test_dependent_admission_waits_for_every_prerequisite_promotion` |
| Configured plan dependants cannot be admitted before durable plan initialization | pass | `test_dependent_admission_waits_for_plan_initialization` |
| Advisory dependency does not block promotion | pass | `test_advisory_local_dependency_does_not_block_promotion` |
| Restart recovers or abandons an interrupted Git ref update safely and emits its durable identity | pass | `test_restart_recovers_promotion_after_git_ref_advanced`, `test_restart_abandons_promotion_when_git_ref_did_not_move` |
| Restart recovery re-emits the same durable promotion identity | pass | `test_restart_recovers_promotion_after_git_ref_advanced` |
| Target movement replays promoted items from the new target and reruns gates | pass | `test_target_move_rebuilds_plan_and_replays_promoted_items` |
| Long-lived promotion refreshes a moved target before applying new work | pass | `test_promotion_refreshes_plan_when_target_moves_during_long_lived_run` |
| Target movement during replay supersedes the stale rebuild and retries from the newer target | pass | `test_refresh_restarts_if_target_moves_while_replaying_promotions` |
| Target movement during promotion is rechecked before and after publication | pass | `test_promotion_rechecks_target_after_integration_gates` |
| Target-refresh conflict/failure is durable and leaves the existing plan unchanged | pass | `test_target_move_conflict_leaves_existing_plan_and_projection_unchanged` |
| Restart recovers a refresh after the Git ref advanced | pass | `test_restart_recovers_refresh_after_git_ref_advanced` |
| Restart abandons an unadvanced refresh conservatively | pass | `test_restart_abandons_refresh_when_git_ref_did_not_move` |
| Promotion reconciles a pending refresh before continuing | pass | `test_promotion_recovers_pending_refresh_before_continuing` |
| Refresh gate failure closes its durable journal | pass | `test_refresh_gate_failure_closes_refresh_journal` |
| Dependent item waits for and sees both prerequisites | pass | `test_fleet_promotes_two_independent_items_before_the_dependent_item` |
| Conflict is surfaced without advancing the plan head | pass | `test_conflicting_promotion_is_returned_for_repair_and_head_is_unchanged` |
| Promotion conflict returns the item to agent work without consuming an attempt | pass | `test_promotion_conflict_returns_item_to_work_without_consuming_attempt` |
| Promotion records are retained | pass | fleet acceptance assertions on all three items |
| Promotion events retain immutable promotion-row identities for success, conflict, and recovery | pass | `test_promotion_event_retains_item_and_plan_commit_identity`, `test_conflicting_promotion_is_returned_for_repair_and_head_is_unchanged`, `test_restart_recovers_promotion_after_git_ref_advanced` |
| Remote publication or real workload delivery | not exercised | explicitly outside this fixture evidence |
