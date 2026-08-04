# Stage C adoption report — 2026-08-04

**Status:** Stage C deterministic acceptance met, with two named exceptions —
real GitHub service behaviour and assessor model quality are both unmeasured,
and the CLI's model-backed assessor route has no offline test.

## Configuration under test

- Implementation commit: `bc36e1c`, documentation `be5186f`, on branch
  `codex/fit-stage-c`.
- Base commit: `afdc3bc998cfc5f6b0e763782023acf3b860de43` (the Stage A/E2
  integration tip).
- External boundary: an in-process fake `gh` runner injected into the real
  `GitHub` client. It records every argv and applies the issue-body edits it
  is given, so a second inspection reads what the first one wrote. No network,
  no credential, no `gh` binary.
- Repository under inspection: temporary directories, one of which is a real
  `git init` checkout with a `harness/T5` branch and an unrelated branch.
- Assessor: an injected deterministic stand-in, plus `parse_judgement` and
  `ModelAssessor` exercised over a scripted reply. No provider is called.
- Event storage: the real append-only `AuditStore`, written through the same
  `event_sink` translation the Stage A slice uses.
- Verification execution: the real `executor.Checks`, with real subprocesses.

This is repository-verifiable deterministic evidence. It is not a live GitHub,
provider-quality, or real-backlog measurement.

## Reproduction and result

```console
uv run pytest tests/test_adoption.py -q
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Observed on 2026-08-04:

- `tests/test_adoption.py` collected 32 test cases; all passed, in 4.35s.
- The whole suite is 708 test cases (676 before this stage); all passed. Three
  runs of it on this machine took 276.32s, 587.10s and 617.44s, so **the total
  is not a stable measurement here and should not be quoted as one.** The
  slowest cases were all pre-existing concurrency and fleet tests
  (22.69s, 20.81s, 12.91s); no adoption case appears in the slowest twelve.
  The preceding Stage A report recorded 49.418s for its 676 cases on its own
  machine; that number and these are not comparable, and no like-for-like
  measurement was taken.
- Lint, formatting and strict full-project typing passed.

No test sleeps to wait for time. One case runs a real subprocess that would
sleep for 30s and asserts that a 0.5s verification timeout ends it; the
assertion is on the recorded evidence, not on elapsed time.

## What each Stage C acceptance criterion is proven by

§5.4 requires four things on a fixture repository with existing work.

**1. Repeated adoption is idempotent and produces the same report.** Proven in
two parts, and the split matters.

- `test_repeated_inspection_produces_the_same_report` — two inspections with
  nothing changed in between produce identical `to_dict()` output and an
  identical content digest, including the retained `created_at`.
- `test_repeated_adoption_reaches_a_fixed_point_without_losing_progress` — three
  complete `inspect → approve → reconcile` cycles, with the fleet marking an
  unrelated item done between the first and second. The second and third
  reports are byte-identical.

**The first report is not identical to the later ones, and this is reported
rather than hidden.** Before the first reconciliation the queue holds no rows,
so the first report says "would create queue row … insert as done if this drop
is approved"; afterwards it says "would refresh queue row … state stays done".
Both statements are true of the moment they were made. Making them identical
would mean the report either suppressing what the queue already knows or
asserting a queue state that does not exist. Idempotence is proven at the level
that matters — the effects, below — and as a fixed point from the second
adoption onwards.

**2. It does not duplicate issues, reset queue progress, or drop completed
work.** Same three-cycle test. Across all three cycles the recorded `gh` argv
contains exactly one mutating call (`gh issue edit 17`) and no `gh issue
create` at any point: the second inspection sees the marker the first one
appended and proposes nothing further. The item released `done` by the fleet
between cycles is still `done` at the end, the queue holds exactly 5 rows, and
no item's state moves backwards. `test_prior_harness_attempts_are_evidence_and_history_is_retained`
adds the attempt counter: an item the queue failed once keeps `attempts == 1`
and its `last_error` through reconciliation, the earlier audit rows are
byte-identical afterwards, and the prior failure appears in the report as
`prior_attempt` evidence rather than as a reason to do anything.

**3. Ambiguous matches and proposed drops are surfaced for human approval.**

- `test_competing_candidates_are_reported_and_never_guessed` — an item with a
  high-confidence open issue and a high-confidence merged pull request is
  reported as `2 competing high-confidence candidates`, stays `pending`, and
  cannot be approved as a drop at all: `approve` raises rather than accepting
  an id nothing proposed.
- `test_a_judgement_alone_cannot_drop_work` — the assessor's `done` verdict,
  with citations, reconciles to a `pending` queue row when nobody names it.
- `test_uncertainty_biases_towards_not_started` — `done` with no citations, an
  unrecognised disposition, and `partial` all resolve to `pending`.
- `test_an_assessor_that_fails_does_not_drop_or_abort` — a role that raises
  produces `not_started` evidence quoting the failure, and the inspection
  finishes.
- `test_a_failed_verification_outranks_a_judged_done` — a `verify:` command
  that ran and failed, followed by an assessor saying `done`, produces an
  ambiguity flag and no proposed drop.
- `test_approval_is_exact_and_reconciliation_needs_it` — `reconcile` before
  `approve` raises; the recorded lifecycle is
  `draft → inspecting → proposed → approved → reconciled → stopped`.
- `test_a_rejected_proposal_cannot_reconcile` — the `↘ rejected/revise` branch;
  a reason is required, and afterwards neither approval nor reconciliation is
  possible, with the queue and the external boundary both untouched.

**4. A dry run performs no external mutation.** Three tests, at three levels:
`test_dry_run_reconciliation_performs_no_mutation` (approved proposal,
`reconcile(dry_run=True)` — no queue rows, no projects, no `gh` mutations),
`test_a_dry_run_stores_nothing_at_all` (inspection with `persist=False` leaves
no stored proposal either), and `test_adopt_cli_dry_run_leaves_no_trace` (the
`--dry-run` flag through `main()`).

## Other §5 requirements and their evidence

- **Read-only inspection (§5.1):** `test_inspection_is_read_only_and_ranks_its_evidence`
  asserts an empty queue, no projects and zero mutating `gh` calls after a full
  inspection that ran a real verification subprocess and called the assessor.
- **Report is storable and reviewable (§5.1):** `test_report_is_storable_and_reviewable`
  round-trips it through JSON and asserts the rendered text;
  `test_adopt_cli_writes_a_report_and_reconciles_only_what_was_approved` writes
  it to a file through `--report`.
- **Plan syntax specified before use (§5.2):** `verify:` is documented in
  `docs/USAGE.md` §1 and in the `plan.py` module docstring, and
  `test_item_verification_is_json_argv_and_never_shell_text` proves that shell
  text, an empty array, a bare string and an empty argument are all refused.
- **Same shell-free and timeout rules as project checks (§5.2):** the runnable
  rung constructs `executor.Checks` — the same class the executor runs before
  the reviewer — rather than a second execution path, and
  `DEFAULT_VERIFY_TIMEOUT` is read from `Checks.timeout` so the two cannot
  drift. `test_verification_runs_under_the_project_check_rules` runs an argv
  containing `;`, `touch` and a filename, and asserts the file does not exist.
- **The exact external mutation is shown (§5.3):** every item carries a
  `mutations` list. The issue-marker entry names the issue, the repository, the
  marker text, the existing body length, and the fields that are not touched.
  After reconciliation the same report distinguishes `applied` from
  `did NOT (unapproved)`, proven by `test_a_reconciled_report_says_what_happened`.
- **Backfilling preserves human content (§5.3):**
  `test_marker_backfill_preserves_human_issue_content` asserts the resulting
  body byte for byte and that the argv contains `--body-file` and neither
  `--title` nor `--add-label`. `GitHub.update_issue_body` exists precisely so
  that adoption has no path that can overwrite a title, label, milestone or
  assignee. `test_a_title_lookalike_issue_is_never_edited` proves the narrower
  rule: an issue is edited only when it already names the item id, so a
  same-title issue nobody linked is left alone even when the item is approved
  as a drop — and the report promises no edit either, because the report and
  reconciliation share one predicate.
- **Pull-request adoption needs explicit evidence (§5.3):**
  `test_a_marked_pull_request_is_adopted_with_its_branch` adopts a PR that
  carries the item's marker and whose head branch is in the repository.
  `test_a_lookalike_branch_is_never_claimed_as_harness_work` proves both
  refusals: a `harness/T5` branch on a PR with no id reference, and a fork's PR
  that does reference the id. Both are medium confidence, neither is
  `harness_created`, and after reconciliation both items' queue rows have
  `branch is None` and `pr_url is None`.
- **Branches (§5.3):** `test_an_existing_branch_is_a_lead_and_never_a_completion`
  reads a real `git` checkout. Only an exact `harness/<id>` name produces a
  candidate, it is always medium confidence, never `harness_created`, and the
  queue row it reconciles to records no branch.
- **Events (§1.2, §3.5):** `test_adoption_appends_events_a_projection_can_read`
  asserts the per-item proposal outcomes, the ambiguity event, the ordering of
  approval before the marker backfill, and the terminal `adoption_stopped`.
  Adoption writes no event twice and rewrites none; the stored proposal lives
  in the queue's settings, as inception's does, and is not a second source of
  truth for anything the event stream records.
- **No automatic deletion or closure of external work (§12):** there is no code
  path in `adoption.py` that closes an issue, deletes a branch, closes a pull
  request or removes a queue row. The only external call it can make is
  `update_issue_body`.

## Costs and unmeasured costs

- Model tokens, latency and monetary cost: **zero spent, and unmeasured**. No
  provider is contacted anywhere in this stage's tests.
- Assessor quality — how often a real model's `done`, `partial` and
  `not_started` verdicts are correct — is **entirely unmeasured**. What is
  tested is the contract around it: that its output is structured, retained
  with citations, incapable of dropping work on its own, and that every
  malformed or hedged answer becomes `not_started`.
- Wall-clock cost of the new tests: 4.35s for 32 cases, dominated by real
  subprocess spawns for verification commands and one real `git init`.

## Blind spots

- **Real GitHub service behaviour is unmeasured.** The `gh` runner is a fake.
  In particular `gh pr list --json number,title,body,state,headRefName,url,isCrossRepository`
  has not been run against a live `gh`: the field names and the case of the
  `state` values are taken from the documented schema and normalised
  defensively, but nothing here proves the invocation succeeds. The first real
  `--repo` run must confirm it, and a new report must record the result.
- **The CLI's model-backed assessor path is untested.** `--assessor-model`
  builds a `ModelClient` over the same HTTP transport `run` uses. Only the
  refusal when `--endpoint` is absent is covered; the request itself is not.
  `ModelAssessor` and `parse_judgement` are tested through an injected
  callable.
- **No real backlog has been adopted.** The "fixture repository with existing
  work" of §5.4 is a temporary directory of a handful of items, not a project
  anyone has been working in for months. Item-id collisions in prose, plans
  whose items were renamed, issues in another repository, and the sheer volume
  of candidates a real repository produces are all unexercised.
- **The first report differs from later ones** for the reason given above. If a
  reader's definition of "the same report" includes the first, that criterion
  is met only in effect and not in bytes.
- **Assessor cost control is absent.** Inspection calls the assessor once per
  unresolved item with no budget, no cap and no batching. On a large plan
  against a real route that is real money, and nothing in this stage measures
  or limits it.
- **There is no HTTP surface.** Inception is drivable through the API;
  adoption is CLI and library only. §5 specifies a command, so this is not an
  unmet criterion, but a session host cannot offer adoption as a screen today.
- **The `partial` disposition is recorded and then ignored.** It never proposes
  a drop and never proposes anything else either. A partially-delivered item is
  simply queued as work; the report says why, but the harness does nothing with
  the distinction.

## Open decisions

- **D7 (service deployment)** — not touched. `adopt` is a local CLI command and
  a library; nothing here depends on how the service is deployed.
- **D8 (gate plugin interface)** — not settled and not assumed. `verify:` is a
  plan-declared argv run through the existing `Checks`, not a new gate type and
  not a plugin boundary. Whether item verification should become a plugin is
  exactly the D8 question, and this stage deliberately does not answer it.
- **D9 (reviewer sees plan/rationale)** — unaffected; adoption runs no reviewer.

The assessor introduces a new role name, `assessor`. That is a routing entry,
not a decision about gates, and it can be removed without touching anything
else.

## Decision

**Stage C's deterministic acceptance gate is met. Continue to Stage G.**

The four §5.4 criteria are proven by observable assertions on the report, the
queue, the recorded `gh` argv and the event stream, with the one honest
deviation on "the same report" stated above. What is *not* established is that
this behaves correctly against the GitHub service, that a real assessor model
is worth its cost, or that adoption survives contact with a backlog someone has
actually been living in. None of those may be claimed until a later dated
report appends the run, its configuration, its raw artifacts and its
denominator — rather than editing this one.
