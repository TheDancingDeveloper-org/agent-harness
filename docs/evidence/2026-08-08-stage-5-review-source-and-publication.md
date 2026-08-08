# Stage 5 implementation evidence — installed review source and single-PR publication

**Date:** 2026-08-08
**Scope:** local fixture repositories, a local bare Git remote and injected
clients only. No provider, no GitHub, no network, no AIDevEnv, no session host
and no CLI-agent process was contacted. Nothing was pushed to any remote this
repository does not create inside a temporary directory.

This package records one Stage 5 implementation slice from
[`docs/STATUS.md`](../STATUS.md) §2.6. It does **not** claim the Stage 5 exit,
does not authorise a real workload run, and does not authorise publication.

## What was added

Two of the three items STATUS listed as open for Stage 5 now have
implementations and local tests. The third — remote workload acceptance —
cannot be closed by code and is untouched.

### 1. An installed review source, `github-pr-review`

Previously the normalized review-event contract had no installed adapter at
all: `agent_harness.review_sources` was an empty entry-point group, so a
deployment could not select a source by name. `adapters/github_pr_review.py`
now supplies one, on exactly the terms `review_sources.resolve` already
enforced — core imports nothing from it, and the adapter is reached only when
installed metadata is asked for that name.

The adapter owns the two things GitHub knows and the harness does not:

- **Immutable identity.** Reviews and review comments number independently on
  their own endpoints, so identity is `REPO#PR/endpoint/id` rather than the id
  alone. Deduplication remains the queue's, on that identity.
- **Disposition.** Decided by explicit, deterministic rules, never by a model
  reading prose. Explicit markers (`harness: fix` / `hold` / `resolved`) win;
  otherwise `CHANGES_REQUESTED` is actionable and `APPROVED` is already
  resolved; **anything else defaults to ambiguous**, which opens a hold for a
  person rather than sending an agent after a guess.

The item a comment concerns is likewise explicit — `harness-item: T3` names
one, and the configured `default_item_id` is used otherwise, because a plan
pull request carries every item in the plan and this adapter will not infer
which one a line comment belongs to.

`gh` is injected, so the tests exercise the real polling, cursor and identity
logic without a network or a credential.

### 2. Single-pull-request publication with resume, `plan_publication.py`

`PlanPublisher` is the one remote step the product permits, kept in its own
module so local promotion never depends on a remote being reachable and a
deployment that never publishes never loads it. Three properties make it safe
to call repeatedly, which is what "corrections resume automatically" requires:

- **One pull request per plan.** A durable record is consulted first, then
  `find_open_pr`, before anything is created. A failure to *ask* is a refusal,
  not permission to create a second one. There is never a per-item PR.
- **An unchanged plan head does nothing.** A duplicate review event, a retried
  poll or a correction that promoted nothing does not push, does not comment
  and does not re-present a decision a person already has.
- **The harness never merges.** Nothing here approves, marks ready or merges.

The push is `--force-with-lease` against the sha this harness last published,
because a plan branch legitimately rewinds when a moved target rebuilds it and
replays every promotion. The lease turns "the branch was rebuilt" into a safe
update and "somebody else pushed to it" into a named refusal.

With no durable record — a first publication, or an adopted pull request whose
record was lost — the lease is derived rather than skipped. The remote branch
is fetched; if it is absent the lease is empty, which asserts the ref does not
exist; if it is present and this plan already contains it, it becomes the
lease; and if it is present and unexplained, publication is refused by name
rather than discarding work the harness cannot see.

### 3. Publication wired into the fleet, on the plan's terms

`direct_executor_factory` previously refused `push=True` whenever a project had
plan integration, because the only thing push could have meant was publishing
item branches. It now means what P7/P8 say it should: the executor is given no
GitHub client and never pushes an item branch, and a `PlanPublisher` is built
for the plan instead.

After each successful promotion the factory asks whether the plan has stopped
moving. `PlanPublisher.readiness()` answers no while anything is pending,
claimed or held, and also no while anything failed, exhausted or blocked —
that second case is an exception for a person under P10, not a reason to ship
a partial plan whose gaps only the queue knows about. The item whose promotion
is asking is excluded from the in-flight count, because it is still claimed at
that moment while its work is already in the plan branch; without that, the
last item of a plan could never trigger publication.

A publication failure cannot fail the item. The promotion is already gated and
local when this runs, so a remote that is down, slow or refusing is reported as
a `plan_publication_failed` event and the promotion stands.

`test_generic.py`'s `EXECUTION_PATH` was extended to hold plan integration,
publication, review intake and notifications to the same no-adapter,
no-workload-name rule as the rest of the path.

## Commands and results

```console
uv run pytest -q tests/test_github_pr_review_source.py
12 passed

uv run pytest -q tests/test_plan_publication.py
17 passed

uv run pytest -q tests/test_plan_integration.py
27 passed

uv run pytest -q tests/test_generic.py
9 passed

TMPDIR=/tmp uv run pytest -q
1712 collected; passed at 100% with one skip. The skip is the live Docker
backend test: this host has a Docker CLI but no reachable daemon.

uv run ruff check src tests
All checks passed!

uv run ruff format --check src tests
150 files already formatted

TMPDIR=/tmp uv run mypy
Success: no issues found in 150 source files
```

The full-suite run is the regression denominator for this slice. It also
covers two unblocked defects fixed on the same tree and recorded in
`STATUS.md` §3.3 — #220 (`queue.now()` is authoritative for a lease on the
retry and block routes) and #223 (`tool` removed from `_WIRE_ROLES`) — which
are not part of this Stage 5 slice and are not claimed by it. Docker-backed
tests skip on this host, which has no reachable daemon; those skips remain an
environment limitation and are not evidence for the Stage 2 execution
boundary.

## Acceptance table

| Criterion | Result | Test/evidence |
|---|---|---|
| A review source is installed and resolves by name through metadata | pass | `test_the_source_is_installed_and_resolves_by_name` |
| Unmarked human prose becomes a hold, not guessed work | pass | `test_unmarked_prose_is_ambiguous_rather_than_guessed_at`, `test_polling_the_installed_source_creates_correction_work_once` |
| Explicit markers and review state decide disposition deterministically | pass | `test_explicit_markers_decide_the_disposition`, `test_review_state_decides_when_no_marker_is_present` |
| Reviews and review comments sharing a number stay distinct | pass | `test_identity_separates_reviews_from_review_comments_sharing_a_number` |
| An unsubmitted draft review produces no work | pass | `test_an_unsubmitted_draft_review_is_not_feedback_yet` |
| Actionable feedback creates correction work once; replay is a no-op | pass | `test_polling_the_installed_source_creates_correction_work_once` |
| A source failure leaves the cursor unadvanced | pass | `test_unreadable_output_fails_loudly_rather_than_polling_empty`, existing `test_poller_leaves_cursor_unchanged_when_processing_fails` |
| First publication pushes the plan branch and opens exactly one PR | pass | `test_the_first_publication_pushes_the_branch_and_opens_one_pr` |
| A correction updates that same PR's branch; no second PR | pass | `test_a_correction_updates_the_same_pr_rather_than_opening_another` |
| An unchanged plan head touches no remote | pass | `test_republishing_an_unchanged_head_touches_nothing` |
| An existing remote PR is adopted rather than duplicated | pass | `test_an_existing_remote_pr_is_adopted_instead_of_duplicated` |
| A rebuilt (rewound) plan branch still publishes | pass | `test_a_rebuilt_plan_branch_still_publishes_under_the_lease` |
| A branch moved by somebody else is refused, not clobbered | pass | `test_a_branch_moved_by_somebody_else_is_refused` |
| A lost record does not strand an adopted PR this plan already contains | pass | `test_an_adopted_branch_this_plan_already_contains_is_published` |
| An unexplained remote branch is refused rather than discarded | pass | `test_an_unexplained_remote_branch_is_not_discarded` |
| An unreadable or mismatched record never opens a second PR | pass | `test_an_unreadable_record_is_reported_rather_than_overwritten`, `test_publishing_a_second_branch_for_one_plan_is_refused` |
| A failed PR comment does not lose the published head | pass | `test_a_failed_comment_does_not_lose_the_published_head` |
| Publication is reported as one event | pass | `test_publication_is_reported_as_one_event` |
| Publication waits for work that could still change the tree | pass | `test_readiness_waits_for_work_that_could_still_change_the_tree` |
| An item that did not deliver withholds publication for a person | pass | `test_readiness_withholds_publication_when_an_item_did_not_deliver` |
| The promoting item does not block its own plan | pass | `test_the_promoting_item_does_not_block_its_own_plan` |
| An empty plan is not published | pass | `test_an_empty_plan_is_not_something_to_publish` |
| A fleet publishes one plan PR only once the plan is finished, and a later correction updates it | pass | `test_fleet_publishes_one_plan_pr_only_when_the_plan_is_finished` |
| No item branch reaches the remote when a plan owns integration | pass | same test — the bare remote holds only `main` and the plan branch |
| A remote failure cannot fail a promoted item | pass | by construction: `_publish_if_ready` reports `plan_publication_failed` and returns |
| The harness merges, approves or marks ready | never | no such call exists in `plan_publication.py` |

## What this does not show

Named explicitly, because the tests above could otherwise be read as more than
they are:

- **No real remote was contacted.** The pull-request client is a fake and the
  Git remote is a bare repository in a temporary directory. `gh` was never
  run, and no GitHub API behaviour is evidence here.
- **The wiring has never run against a real remote.** The fleet test drives
  the real `direct_executor_factory` path, but its remote is a bare repository
  in a temporary directory and its pull-request client is a fake. Nothing here
  says how GitHub behaves, and P12 still forbids pushing between slices — a
  deployment only reaches this path by setting `push=True` on a project that
  has a plan branch and a repo.
- **The review source is installed but not deployed.** Nothing has polled a
  real pull request, so the marker convention has never been used by a real
  reviewer and the `since`/pagination behaviour is untested against GitHub.
- **The Stage 5 exit is not claimed**, and neither is Stage 2's live execution
  boundary, on which every later stage still depends.
