# GUI Milestones 0–1 evidence and Milestones 2–3 slices

Status: implementation slice complete; the full GUI program is not complete.

## Build under test

- Branch: `codex/gui-plan`
- Worktree: `/home/sprooty/Working/Active/apps/agent-harness-worktrees/gui-plan`
- Plan: `GUI_PLAN.md`, SHA-256 `b8a5aa97c79dafdcb90502903df82445d7bdc274eb8c81a0deb6bccd7a68bb3d`
- Python: CPython 3.14.4; package installed with `uv sync --all-extras`
- Temporary test volume: `/tmp` (separate ext4 filesystem, 361 GiB free at baseline)

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
(preflight/configuration/bulk actions/notifications and dependency-override controls are
not yet wired). Milestone 3 remains partial (adoption, plan dry-run/sync and richer graph
interactions are not yet wired). Milestones 4–8 (routing/operations panels, internal
sessions, extensions, automation, RBAC and recovery) remain explicitly incomplete. No
real fleet or external deployment was used.
