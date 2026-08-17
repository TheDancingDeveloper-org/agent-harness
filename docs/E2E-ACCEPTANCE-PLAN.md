# Deterministic local end-to-end acceptance plan

This is a planned project deliverable, not a claim that the full flow is
implemented today. It defines the first complete product-contract test for a
clean agent harness.

The fixture is intentionally small: a temporary local Git repository, a tiny
application, and a plan with three or four dependent work items. The harness
path remains real. The test must use the actual plan contract, admission
transaction, queue lease, worktree, checks, review, integration, GUI session,
and durable evidence surfaces. Only model, network, and external-service
effects are replaced with deterministic test doubles.

## Scope and prerequisites

The fixture must include:

- a repository created at test runtime;
- a predefined, valid `PLAN.md` committed or supplied as fixture input;
- a clean SQLite queue, event store, audit store, and browser-session state;
- a deterministic agent/runner backend with no credentials and no network;
- authoritative item and integration checks that can be deliberately failed;
- a small local delivery target whose build, readiness, acceptance, and teardown
  commands are deterministic;
- browser coverage against the same-origin GUI and JSON API;
- exact commit, branch, revision, outcome, and cleanup assertions.

The full test is gated on the GUI admission-preview and confirmation workflow.
Until that workflow is delivered, only the non-GUI portions may be implemented
as lower-level tests. The acceptance test itself must remain marked as pending
or skipped with a named prerequisite; it must not silently omit GUI coverage.

This deliverable does not prove container isolation, model quality, provider
behaviour, performance, or multi-day reliability. Those are separate backlog
items.

## Ten-step test plan

### 1. Build the clean fixture

Create a temporary Git repository containing the smallest application that can
be changed and locally exercised. Create fresh SQLite stores and deterministic
clock, polling, agent, delivery, and fault-injection dependencies. Assert that
the fixture starts with no project, revision, queue rows, claims, events, or
delivery journal entries.

### 2. Supply and validate the plan

Use the predefined fixture `PLAN.md`, with the required generic sections and a
v1 fenced manifest. Give it at least three dependent items and declarations for
the checks and local delivery lifecycle. Run deterministic validation through
the supported public path and assert that the valid plan produces no findings.

Also run a deliberately broken copy and assert that all stable findings are
returned together, with source locations and remediation, before any project,
queue, Git ref, or event-store mutation occurs.

### 3. Preview admission through the GUI

Authenticate through the browser session and open the project/plan admission
surface. Submit the plan path and repository path. Assert that the GUI shows
the exact plan digest, manifest digest, repository identity, base ref and SHA,
integration ref, execution profile, revision changes, and any semantic review
questions.

The preview must be read-only: no project, work row, immutable revision,
branch, worktree, model call, remote request, or delivery record may exist
after the preview.

### 4. Confirm admission explicitly

Review the preview in the GUI and confirm it through the browser’s authenticated,
CSRF-protected action. Assert that the exact reviewed digest is consumed once.
The project must be created or revised atomically, left stopped, and have one
immutable plan revision, item snapshots, current membership, work projection,
and typed dependency graph.

Repeat the request with the same reviewed identity and assert idempotent replay.
Submit a stale digest and assert a typed conflict with no new revision.

### 5. Prove preflight before execution

Run project preflight against the admitted revision and exact execution profile.
Assert that all cheap configuration, repository, toolchain, policy, and storage
checks pass before any item is claimed. Break each preflight boundary in a
parameterized case and assert that execution does not start and no model call
is made.

### 6. Execute dependent work with deterministic agents

Start the stopped project through the supported control surface. Let the
deterministic agent complete the dependent items using real leases, attempts,
worktrees, patches, item checks, review outcomes, commits, and branch names.
Assert that an item is not claimed before its required local dependencies are
satisfied, that claims are isolated per project, and that no sleep-based timing
is used.

Break agent, patch, item-check, review, budget, and policy boundaries. Each
case must leave the correct durable outcome and must not run the next expensive
gate after an earlier gate refuses.

### 7. Promote and integrate locally

Run the local integration path against the exact admitted base and integration
ref. Assert that promotion re-checks the item and integration gates, records
the accepted commit and graph revision, waits for unresolved dependencies, and
does not mutate remote state.

Exercise conflict, failed re-gate, moved-base, and crash/restart recovery cases.
Retained completed history must be replayed through the promotion checks; it
must never be silently relabelled or discarded.

### 8. Exercise the local delivery lifecycle

Review and start the fixture delivery through its typed local-delivery surface.
Run build, deploy, readiness, acceptance, teardown, and recovery in order, with
each stage durable before the next stage begins. Assert exact command context,
output/checksum evidence, accepted integration SHA, and cleanup state.

Parameterize every lifecycle failure. A teardown failure must remain visible
after an acceptance failure, and recovery must be attempted according to the
declared policy without converting an unmet gate into success.

### 9. Verify GUI state and durable evidence

After successful completion, reopen the GUI and verify that it reports the
stopped/running state, current revision, item states, dependency readiness,
accepted integration SHA, delivery stages, final outcome, and cleanup state.
Open item and project evidence views and assert that a reviewer can identify
the exact plan, revision, commands, commits, environment/profile, gate results,
operator decisions, and failure reasons from the GUI alone.

The browser must use the same typed query and command services as the JSON API;
it must not read SQLite directly or imply a state transition through navigation
or drag-and-drop.

### 10. Restart, audit, and release the fixture

Close and recreate the harness services from the persisted stores at each
durable boundary. Assert that claims, attempts, revisions, outcomes, delivery
stages, and evidence recover deterministically and that replay is idempotent.

Run the complete happy path and all fault cut-points under the repository’s
four gates. Record the accepted SHA, test identifiers, stage outcomes, and
known limitations as acceptance evidence. The deliverable is complete only
when the GUI path is implemented and the full ten-step test passes; partial
component coverage must be reported as partial.

## Exit criteria

The deliverable is accepted when:

1. the happy path passes without network, credentials, model calls, or sleeps;
2. invalid-plan, admission, preflight, execution, promotion, and delivery
   cut-points prove that later expensive or state-changing stages do not run;
3. the GUI preview and confirmation path is covered, including authentication,
   CSRF, stale review, replay, and evidence views;
4. restart recovery and immutable revision history are asserted;
5. the final evidence identifies the exact accepted SHA and every lifecycle
   stage honestly.

