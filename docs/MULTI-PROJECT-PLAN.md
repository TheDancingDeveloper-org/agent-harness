# Multi-project harness and a real Work GUI

Status: proposed, not started. Written 2026-08-02.

This supersedes nothing. It adds the concept the harness has never had — a
**project** — and builds the GUI that concept makes possible.

---

## 0. Why, with evidence

Three findings from the deployed stack, all verified rather than assumed.

### 0.1 The GUI renders 2 of ~14 endpoints

The Work tab calls `GET /api/work` and `POST /api/work/{id}/retry` on a
polling timer. Nothing else. The event trail, the rate-limit classification,
fleet control, role routing, plan parse and plan sync all have no UI.

The proxy already forwards every route, including a catch-all passthrough, so
this is missing interface — not missing plumbing.

### 0.2 The harness has no concept of a project

```sql
CREATE TABLE work (
    item_id TEXT PRIMARY KEY,   -- global, not (project, item_id)
    ...
);
CREATE TABLE control (id INTEGER PRIMARY KEY CHECK (id = 1), ...);  -- ONE row
```

Consequences, in order of severity:

| | |
|---|---|
| **Id collision corrupts** | Two plans that both name `T1` are the same row. NGMS has `T1`. This is not merely unsupported — it silently destroys work. |
| One pause for everything | `control` is a single row. You cannot drain one project and leave another running. |
| One worker pool | No per-project budget, so a large project starves a small one. |
| One repo, one branch, one check command | All CLI flags, applying to whatever the runner was invoked with. |

### 0.3 Restart durability is half-present

The database **does** survive restarts — it sits on a named volume
(`/dev/sdg3[/aidevenv-feat/home]` → `/home/dev`) and files there predate three
image redeploys.

What does not survive is everything that makes the queue *usable*:

- **No project configuration is stored anywhere.** Repo, base branch, checks,
  plan path, reviewer, endpoint and session host are CLI flags to
  `agent-harness run`. The DB has nowhere to put them. Re-init after a restart
  is not a bug in the volume; there is simply nothing persisted to re-read.
- **The entrypoint starts `serve`, never `run`.** After a restart the API is
  up and no worker exists, so the queue sits with items nobody claims.

A footgun found alongside: `harness.sqlite` is 4 KB while `harness.sqlite-wal`
is 754 KB. Any backup or volume migration copying only the `.sqlite` file
loses nearly everything.

---

## 1. Decisions taken

| Decision | Choice |
|---|---|
| Isolation | **One process, one DB, project as a first-class scope.** Composite keys, per-project control, per-project worker pools. |
| First GUI deliverable | **Board + item detail + live events.** Controls, role editor and plan import follow. |
| Resume after restart | **Never automatic.** Each project requires an explicit *Continue execution* in the GUI. |

### 1.1 Why resume is human-gated

An auto-resuming fleet turns a routine pod restart into unattended spend
against a stack nobody has looked at yet. The failure it guards against is not
hypothetical: a bad deploy that restarts repeatedly would restart the fleet
repeatedly with it.

The cost is that work does not continue until someone says so. That is the
intent, and it is cheap to pay because claims are leases — nothing is lost by
waiting, and in-flight items recover on their own once a worker returns.

### 1.2 "Paused" and "not running" are different states

Conflating them loses the operator's intent across a restart. A project
deliberately drained before a reboot must not come back looking identical to
one that was running happily.

So `control.state` gains `stopped`, and on boot **every** project is set to
`stopped` regardless of prior state, recording what it was and why:

```json
{"state": "stopped", "previous_state": "running",
 "reason": "process started 2026-08-02T21:55:31Z"}
```

The GUI then distinguishes:

- *Stopped — was running before restart* → offer **Continue execution**
- *Stopped — was drained: "deploying"* → show the reason, and make resuming a
  deliberate act rather than a reflex

Boot never starts a worker. Only an explicit API call does.

---

## 2. Phase 1 — project as a scope

**Schema.** A `projects` table holding everything currently passed as a flag:

```sql
CREATE TABLE projects (
    project_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    repo         TEXT,              -- owner/name
    work_dir     TEXT,              -- checkout the worktrees branch from
    base_branch  TEXT NOT NULL DEFAULT 'main',
    checks       TEXT NOT NULL DEFAULT '[]',   -- JSON list of commands
    plan_path    TEXT,
    roles        TEXT,              -- JSON role->route override, null = global
    max_workers  INTEGER NOT NULL DEFAULT 1,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
```

`work` becomes `PRIMARY KEY (project_id, item_id)`; `control` gains
`project_id` as its key and the `stopped` state. Existing rows migrate to a
`default` project so nothing in flight is lost.

**Claims become project-scoped.** A worker claims *within* one project. This
is the change that stops co-mingling at the queue level rather than in the UI.

**API.** `POST /api/projects` registers one, durably. `GET /api/projects`
lists them with rollup counts. Existing routes gain a project scope; list
endpoints keep a cross-project view for the overview screen.

**Definition of done:** register two projects that both contain `T1`, sync
both, and confirm four distinct rows and two independent control states.

## 3. Phase 2 — separate streams, explicitly resumed

Per-project worker pools honouring `max_workers`, so one project cannot starve
another. `POST /api/projects/{id}/start` and `/stop` — start is the only thing
that ever creates workers, and boot does not call it.

WAL checkpoint on clean shutdown, so the `.sqlite` file is self-contained for
backup.

**Definition of done:** restart the pod; every project reports `stopped` with
its previous state intact; no agent runs until a human clicks; clicking
resumes exactly one project.

## 4. Phase 3 — the GUI worth having

Board and item detail and live events, per the decision above:

- **Overview** — every project, its counts, its control state and why
- **Board** — one project's backlog by state, waiting-for-input first
- **Item detail** — full event history, attempts, diff, PR, deep link to the
  agent's terminal
- **Live feed** — `/api/events` paged by row id, not timestamp
- **Continue execution** — per project, showing what it was doing before

Then, in order: fleet controls, plan-import wizard (parse → show what it could
*not* read → dry-run → sync), role-routing editor, rate-limit dashboard.

## 5. Phase 4 — operational depth

Per-project attempt and cost metrics, baseline comparison, failure triage.

---

## 6. What this deliberately does not do

- **No physical isolation between projects.** One process, one file. A
  corrupted DB affects everything. Accepted because cross-project views are
  the point of the overview screen, and N processes means N ports, N
  supervisors and N tokens.
- **No auto-resume**, per §1.1 — including after a crash.
- **No second web UI.** The session host keeps owning tabs, auth and
  terminals. The harness serves JSON.
