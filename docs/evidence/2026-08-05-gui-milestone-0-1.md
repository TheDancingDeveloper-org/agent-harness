# GUI Milestones 0–1 evidence and Milestones 2–4 slices

Status: implementation slice complete; the full GUI program is not complete.

## Build under test

- Branch: `codex/gui-plan`
- Worktree: `/home/sprooty/Working/Active/apps/agent-harness-worktrees/gui-plan`
- Continuation checkpoint: `4059d22`
- Integration base: `main` at `be7abe1`; merged after the checkpoint
- Plan: `GUI_PLAN.md` in the integration commit containing this evidence update
- Python: CPython 3.14.4; package installed with `uv sync --all-extras`
- Temporary test volume: `/tmp` (separate ext4 filesystem, 361 GiB free at baseline)

This evidence file was refreshed on 2026-08-06 after the worker-inventory, filtered-event
and typed-analytics slices, then refreshed again after current `main` was integrated.

## Commands and results

The pre-change baseline was recorded before feature edits:

| Command | Result |
|---|---|
| `TMPDIR=/tmp/agent-harness-gui-baseline.HoeEBo uv run pytest -q` | passed, 1 skipped |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed |
| `TMPDIR=/tmp/agent-harness-gui-baseline.HoeEBo uv run mypy` | passed, 106 source files |

The same gates were run after the browser shell and guarded action bridge:

| Command | Result |
|---|---|
| `TMPDIR=/tmp/agent-harness-gui-baseline.HoeEBo uv run pytest -q` | passed at 100%, 1 skipped |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed, 116 files formatted |
| `TMPDIR=/tmp/agent-harness-gui-baseline.HoeEBo uv run mypy` | passed, 111 source files |

After the inception, plan-parse review and dependency-graph slices:

| Command | Result |
|---|---|
| `TMPDIR=/tmp/agent-harness-gui-baseline.HoeEBo uv run pytest -q` | passed at 100%, 1 skipped (`/tmp/gui-pytest-m3-final.log`) |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed |
| `TMPDIR=/tmp/agent-harness-gui-baseline.HoeEBo uv run mypy` | passed, 111 source files |

After the project configuration, plan synchronization and global role-routing slices:

| Command | Result |
|---|---|
| `TMPDIR=/tmp/agent-harness-gui-full.zPtjTH uv run pytest -q` | passed at 100%, 1 skipped |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed, 119 files checked |
| `TMPDIR=/tmp/agent-harness-gui-full.zPtjTH uv run mypy` | passed, 114 source files |

After the worker-pool inventory and URL-backed event-filter slices:

| Check | Result |
|---|---|
| `TMPDIR=/tmp/agent-harness-gui-workers.XXXXXX uv run pytest -q` | passed at 100%, 1 skipped |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed, 119 files already formatted |
| `TMPDIR=/tmp/agent-harness-gui-workers-mypy.XXXXXX uv run mypy` | passed, 114 source files |

After the typed analytics projection and browser panels:

| Check | Result |
|---|---|
| `TMPDIR=<fast-temp> uv run pytest -q` | passed at 100%, 1 skipped |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed, 119 files already formatted |
| `TMPDIR=<fast-temp> uv run mypy` | passed, 114 source files |

At the 2026-08-06 documentation-first resume checkpoint, against the complete dirty
continuation:

| Check | Result |
|---|---|
| `TMPDIR=/tmp/agent-harness-gui-resume.vVPDQ5 uv run pytest -q` | passed at 100%, 1 skipped |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed, 119 files already formatted |
| `TMPDIR=/tmp/agent-harness-gui-resume-mypy.KCzFMT uv run mypy` | passed, 114 source files |

After checkpoint `4059d22`, integrating `main` at `be7abe1`, and synchronizing the newly
declared optional dependencies with `uv sync --all-extras`:

| Check | Result |
|---|---|
| `TMPDIR=/tmp/agent-harness-gui-merged-sync.jXkgCh uv run pytest -q` | passed at 100%, 1 skipped |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed, 131 files already formatted |
| `TMPDIR=/tmp/agent-harness-gui-merge-mypy.8cRBKW uv run mypy` | passed, 126 source files |

After the reviewed reconciliation and audit-maintenance controls:

| Check | Result |
|---|---|
| `TMPDIR=/tmp/agent-harness-gui-4-8-full.VVKT0C uv run pytest -q` | passed at 100%, 1 skipped |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed, 132 files already formatted |
| `TMPDIR=/tmp/agent-harness-gui-4-8-full-mypy.MdANch uv run mypy` | passed, 127 source files |

The first full merged pytest attempt is not passing evidence. This older worktree had not
yet installed `main`'s newly declared `agent-loop` extra, so 38 tests failed consistently
with `ModuleNotFoundError: minisweagent`. `uv sync --all-extras` installed the declared
dependency; the complete affected test files then passed before the successful full run
above. No application-code workaround was made for the missing package.

An earlier concurrent setup attempt and an earlier `/dev/shm` pytest attempt are
not evidence: the former raced virtualenv creation and the latter filled the
64 MiB tmpfs. They are retained here only to explain why the valid baseline uses
`/tmp`.

## Implemented and exercised

- Policy/documentation alignment removes the old “GUI belongs to a host” ruling
  and records the one-origin, no-host dependency boundary.
- Packaged Jinja templates, repository-owned CSS, vendored HTMX 2.0.9
  (`htmx.min.js` SHA-256 `57d9191515339922bd1356d7b2d80b1ee3b29f1b3a2c65a078bb8b2e8fd9ae5f`)
  and plain browser JavaScript are served from `/assets`.
- `/` redirects to `/login` or `/projects`; `/projects`, `/work`, item detail,
  `/holds`, `/events`, `/analytics`, `/plans`, `/sessions` and `/settings` are
  authenticated HTML pages.
- Browser login exchanges the configured bearer token for an opaque bounded
  `HttpOnly`, `SameSite=Strict` cookie. Login failures are rate-limited; restart
  and logout revoke sessions. State-changing browser requests require CSRF and
  same-origin checks.
- Monitoring-only mode is visible in the shell and disables controls that need a
  supervised worker pool. The initial control bridge adds only explicit,
  CSRF-protected pause/drain/stop, retry, block and hold-answer actions; each
  delegates the existing queue validation and appends an operator audit event.
- The Plans page now runs the typed inception draft, scope, question-resolution and
  approval gates with CSRF and authenticated operator attribution. It renders a generated
  `PLAN.md` preview/download without creating queue rows.
- Configured plans have a read-only parser review listing recognized items, skipped
  headings, duplicate IDs, malformed/unresolved/external/decision/cross-project
  dependencies, cycles and unattached arrows.
- The Dependency graph page renders the typed graph revision, edge state/evidence,
  ready items, cycles and per-item readiness explanations from the same graph report
  used by admission and the JSON API.
- Dependency overrides now have an explicit browser review/action path. The form
  requires a reason, records the authenticated operator and graph revision, delegates
  to the same revision-scoped graph override as the JSON API, and displays the
  resulting audit row without hiding the real edge state.
- Project configuration now has a typed, secret-safe editor. Review renders the changed
  fields and consequences without applying them; apply consumes a one-time server-held
  payload, records the authenticated operator, and uses an atomic `updated_at` predicate so
  a concurrent API/operator edit cannot be overwritten by a stale browser page.
- Plan sync now has a separate read-only remote preview and explicit apply. The apply is
  bound to the reviewed plan bytes, project repository/path/version and remote counts;
  local, project, remote-preview and GitHub refusals are audited without credentials.
  A stateful remote-drift journey proves a changed second preview performs zero writes.
  GitHub does not provide a transaction across the second preview and later issue writes,
  so the residual remote race remains explicit.
- JSON and browser plan sync use the same finding gate for duplicate, unresolved,
  malformed, cyclic and unattached dependencies. The browser control appears only when
  both the plan path and repository are configured.
- Settings now includes a generic global role-routing editor. It preserves fallback order,
  route preset and price reference as well as the original route fields, renders endpoints
  without URL credentials/query strings, identifies routes unused by the active executor,
  reports reviewer independence, and applies through a one-time atomic compare-and-set.
- `/api/workers` and the Workers page expose a typed read-only inventory. Live identities
  come from the attached fleet; project/item claims, leases, heartbeat timestamps and stage
  evidence come from durable queue/audit state; failures and project-scoped abandoned
  sessions remain distinct evidence. Monitoring-only mode returns no invented workers.
- The event explorer now accepts URL-backed project, item, worker, endpoint, role, model,
  outcome, error-class, reason-kind and Unix-time filters. Sparse filters scan ordered rows
  without skipping a later match, and filtered SSE reconnect uses the same exclusive
  monotonic cursor.
- `/api/analytics` and `/analytics` now share a typed read projection. Rate-limit panels
  retain the three classified classes plus `unclassified` and an explicit denominator;
  cost panels count model calls and keep known spend separate from unpriced calls; delivery
  panels show event and distinct-item denominators; baselines and daily rollups remain
  visible; and missing, degraded or partial audit history is called out rather than inferred
  away. Focused API/audit/browser journeys cover these caveats.
- Analytics now exposes reviewed GitHub reconciliation and audit-maintenance actions.
  Review performs no external request or audit mutation; apply consumes a one-time
  server-held payload. Reconciliation resolves the repository from a persisted project,
  scopes PR attribution to that project and refuses configuration drift before GitHub.
  Maintenance validates and binds the raw-event retention window. Neither underlying
  operation supports a dry run, which the review says explicitly. Success and replay/drift
  refusals record the authenticated operator and required reason in the healthy append-only
  audit store, while result pages retain returned counts and errors. Missing or degraded
  audit stores refuse the controls because the required operator record could not be kept.
- Milestone 4.9 is implemented against a fixed generic boundary: service-process
  metrics come from a portable in-process sampler, and gateway logs are a narrowed
  projection of `model_call` rows from the live append-only event source. Those rows have
  already crossed the store redaction boundary. The projection does not expose arbitrary
  model payloads, depend on session-host state, or establish a filesystem log-path
  convention. It re-redacts allowlisted display fields, strips endpoint userinfo/query/
  fragment, bounds detail, filters by project without breaking sparse cursor paging, and
  reports degraded live history instead of silently falling back to stale ingest data.
- Typed item evidence exposes append-only events, durable attempt stages and
  retained holds without fabricating absent history or cost.
- `/api/events/stream` resumes after a monotonic cursor and surfaces disconnects;
  the existing `/api/events` cursor contract remains unchanged.
- Security headers include CSP, frame-ancestor denial, MIME sniffing and
  referrer/permissions policy. User/model text is escaped by Jinja defaults.

Focused browser/API journeys and the wheel packaging checks pass in-process via `TestClient`, including auth,
cookie flags, CSRF, no-token fail-closed behavior, XSS escaping, packaged asset
delivery, JSON/API isolation, evidence and cursor validation.

## Not yet exercised or complete

This is not a release claim for the full `GUI_PLAN`: browser automation,
screen-reader checks, forced reconnect with replayed events, and all additional
accessibility/security/concurrency journeys remain to run. Milestone 2 remains partial
(bulk-action review, notifications and other controls are not yet wired). Milestone 3
remains partial (adoption and richer graph interactions are not yet wired). Milestone 4's
substantially owned control-plane surface is implemented: global routing, worker inventory,
filtered events, typed analytics, confirmed reconciliation and maintenance, portable
process metrics and structured gateway-call evidence. This is not a claim that core reads
an arbitrary gateway daemon's local log files. Milestones 5–8 (internal sessions,
extensions, automation, RBAC and recovery) remain explicitly incomplete. No real fleet,
GitHub repository or external deployment was used.

## Current slice verification

The Milestone 4.9 four-gate table above is the latest complete implementation evidence.
Earlier transient or partial runs are historical context only and are not substituted for
that complete pass.
