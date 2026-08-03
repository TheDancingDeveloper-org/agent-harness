# Using agent-harness

A worked example, end to end, with real output. Everything below was run
against [`examples/PLAN.md`](../examples/PLAN.md) in this repository.

If you only read one thing: **every destructive step has a `--dry-run`, and
the sync defaults to one.** Run those first. They tell you exactly what would
happen without doing any of it.

---

## 0. Install

```bash
pip install git+https://github.com/TheDancingDeveloper-org/agent-harness
agent-harness --help
```

Inside [AIDevEnv](https://github.com/TheDancingDeveloper-org/aidevenv) it is
already there, already running, and already behind the Work tab.

---

## 0b. Or don't write a plan — describe it and argue

If you do not already have a plan, scope one. Nothing external exists during
any of this: no repository, no issues, no branches, no queue rows.

```bash
# A paragraph, not a plan.
curl -sH "Authorization: Bearer $TOKEN" -X POST localhost:8099/api/inception \
  -H 'content-type: application/json' -d '{
    "project_id": "widgets",
    "overview": "A service that reconciles widgets from the upstream feed and
                 exposes them over a small read API. It has to cope with the
                 feed being late or wrong."
  }'

# Propose a scope.
curl -sH "Authorization: Bearer $TOKEN" -X POST \
  localhost:8099/api/inception/widgets/scope -d '{}' -H 'content-type: application/json'
```

```json
{"revision": 1, "item_count": 14, "blocking_open": 2,
 "goal": "…", "non_goals": ["A user interface"], "risks": ["The feed is undocumented"],
 "questions": [
   {"id": "Q1", "severity": "blocking", "question": "Which database?",
    "why_it_matters": "The schema is written against it"},
   {"id": "Q2", "severity": "deferrable", "question": "Metric or imperial units?"}]}
```

**Argue with it.** Feedback revises the previous proposal rather than starting
over, so points you already settled are not re-argued:

```bash
curl -sH "Authorization: Bearer $TOKEN" -X POST \
  localhost:8099/api/inception/widgets/scope -H 'content-type: application/json' \
  -d '{"feedback": "drop the importer, we already have one; and this needs to
                    survive the feed being unavailable for a day"}'
```

**Resolve the questions.** Answer, defer with a reason, or overrule the
severity — the model proposes it so you are not triaging a flat list, but you
decide what matters:

```bash
# Answer a blocking one.
curl -sH "Authorization: Bearer $TOKEN" -X POST \
  localhost:8099/api/inception/widgets/questions/Q1 \
  -H 'content-type: application/json' -d '{"answer": "Postgres 16"}'

# Defer a cosmetic one. A reason is required.
curl -sH "Authorization: Bearer $TOKEN" -X POST \
  localhost:8099/api/inception/widgets/questions/Q2 \
  -H 'content-type: application/json' \
  -d '{"defer_reason": "cosmetic, revisit at P2", "who": "sprooty"}'

# Or decide the model over-weighted one.
curl -sH "Authorization: Bearer $TOKEN" -X POST \
  localhost:8099/api/inception/widgets/questions/Q1 \
  -H 'content-type: application/json' -d '{"severity": "deferrable"}'
```

| | |
|---|---|
| `blocking` | The answer changes what gets built. **Approval is refused** until it is answered. |
| `deferrable` | Worth knowing; a reasonable default holds. Approval proceeds. |

Blocking on *every* question would be worse than no gate: one cosmetic
question stalls the project, and the predictable adaptation is answering
carelessly to get past it — which turns a real signal into noise while looking
like diligence.

Deferring is answering "not now", which is different from unasked. It records
who and when, and **survives approval** into the plan rather than being cleared
at the gate. Silence never resolves anything.

```bash
# The gate. 409 while a blocking question is open.
curl -sH "Authorization: Bearer $TOKEN" -X POST localhost:8099/api/inception/widgets/approve

# The result is a PLAN.md, not database rows.
curl -sH "Authorization: Bearer $TOKEN" \
  'localhost:8099/api/inception/widgets/plan?name=Widgets' | jq -r .markdown > PLAN.md
```

That last point is the load-bearing one: writing straight to the queue would
fork the pipeline into a generated path and a hand-written path that diverge
forever. A plan document goes through the machinery below unchanged — including
the parser that reports what it could **not** read, so a proposal the harness
cannot consume is caught before it creates a single issue.

---

## 1. Write a plan

Keep writing plans the way you already do. Three shapes are recognised, all of
which occur naturally:

```markdown
### W1: Add a serial-number column     ← id + title heading
- [ ] W2 Reject duplicate serials      ← checkbox, optional leading id
| W3 | Show serials in the listing |   ← table row with an id column
```

The prose under an item becomes the **brief** — the specification an agent is
given, so it is worth writing properly. Metadata is picked out of it:

```markdown
### W2: Reject duplicate serials at the API

Return 409 with a useful message when a widget is created with a serial that
already exists, rather than surfacing a database error.

depends on: W1
labels: area:api
```

`labels:`, `milestone:`, `depends on:`, `size:` and `risk:` are recognised and
removed from the brief — an agent should read the specification, not the
bookkeeping.

### What it could not read is part of the answer

```bash
$ agent-harness plan examples/PLAN.md --repo owner/name --dry-run
3 work items, 2 headings skipped as narrative
would create missing labels: area:api
would sync: created 3, updated 0, unchanged 0
```

Those 2 skipped headings are `Widget service` and `Background` — narrative, as
expected. **A large skip count relative to items means your plan does not use
a recognised shape**, and the harness would rather tell you than quietly find
three items in a fifty-item plan.

---

## 2. Sync it to GitHub

```bash
agent-harness plan examples/PLAN.md --repo owner/name
```

Creates one issue per item, and any labels or milestones the plan names that
the repo lacks — `gh issue create --label` fails outright on an unknown label,
so the first sync of any plan would otherwise die on its first item.

**Re-running after editing the plan updates those issues rather than
duplicating them.** Matching is by a marker in the issue body:

```html
<!-- harness:id=W1 -->
```

not by title — so improving the wording of an item does not fork it into two.
A real run, editing one title and one body and adding one item:

```
synced: created 1, updated 1, unchanged 2
```

Four issues, not five.

Three things it will not do:

| | |
|---|---|
| Sync a plan with duplicate ids | Each id becomes one issue, so two would be created. Fix the plan, or pass `--allow-duplicates` to keep the richest description of each. |
| Close or reopen anything | The plan says what work *is*; the issue says where it *got to*. An item vanishing from a document is usually an edit and sometimes a mistake, never grounds to close work. |
| Strip labels you added on GitHub | The check is a subset, not equality. A sync that removed them would make the backlog hostile to use. |

---

## 3. Execute it

### With a session host — agents you can watch

This is the mode worth using. Each agent runs as a **terminal session** in the
host, so you can attach to it from any device, read its scrollback, and answer
it when it asks something.

```bash
agent-harness --db harness.sqlite run \
    --repo owner/name \
    --work ./target-repo \
    --plan PLAN.md \
    --check 'pytest -q' \
    --session-host https://your-devenv.example \
    --reviewer claude-sonnet-4-6 \
    --endpoint https://api.your-gateway.example \
    --dry-run
```

```
loaded 3 new items from PLAN.md
queue: {'pending': 3}
repo: ./target-repo   base: main   push: True
agents: `claude -p {prompt_file}` as sessions on https://your-devenv.example
reviewer: claude-sonnet-4-6
checks before review: ['pytest -q']

dry run: no model calls, no commits, no pull requests.
```

Add `--serve` to keep it running when the queue empties, waiting for work
rather than exiting — without it, a plan synced an hour later is never picked
up. `--project` selects which project's queue to work.

Drop `--dry-run` to actually run it. Per item:

```
claim ──▶ git worktree on the item's base
      ──▶ write the brief to a prompt file
      ──▶ start `claude -p <prompt>` as a session      ← attach here
      ──▶ wait (a prompt is a human's turn, not a failure)
      ──▶ your checks
      ──▶ review by a different model
      ──▶ commit, push, pull request
```

`AIDEVENV_TOKEN` authenticates to the session host; `HARNESS_API_KEY`
authenticates the reviewer's model calls.

### Without one — headless

Omit `--session-host` and the harness calls the model API directly, doing the
implementing itself. Fully deterministic, and there is nothing to attach to:

```bash
agent-harness run --repo owner/name --work ./target-repo \
    --planner gpt-5.6 --implementer gpt-5.6-terra --reviewer claude-sonnet-4-6 \
    --endpoint https://api.your-gateway.example --check 'pytest -q'
```

### What it guarantees either way

- **Cheap checks run before the reviewer.** Paying a model to tell you the
  build is broken is paying the dearest gate to catch what the cheapest one
  already caught.
- **Nothing is committed to your default branch.** Every item is a proposal.
- **A failed attempt cleans up after itself.** Otherwise one bad diff quietly
  contaminates every item after it.
- **Dependent work is stacked.** An item written against its dependency's tree
  is branched from that dependency, not from `main` — otherwise its diff is
  applied to a tree missing the very change it assumes.
- **No reviewer configured is a rejection, not an approval.** Unreviewed work
  never passes as reviewed.
- **A patch that fails is kept.** The implementer's diff is parsed before git
  sees it, so a truncated or mis-prefixed reply is reported as a *model*
  failure rather than as `corrupt patch at line 549` — and the patch itself is
  written to `--artifacts` (an `artifacts/` directory beside `--events` by
  default) so it can be read instead of paid for again. Pass `--artifacts ''`
  to keep nothing.

---

## 4. Resume after anything

Kill it mid-run — Ctrl-C, a crash, a reboot — and just run it again.

Claims are **leases, not locks**. A lock held by a dead process is a lock
nobody can release, and the usual workaround (a human clearing stale state) is
exactly the unattended failure this exists to prevent. A worker that dies
releases its item by doing nothing; the lease simply expires.

```bash
curl -H "Authorization: Bearer $TOKEN" localhost:8099/api/work | jq '.stale'
```

A non-empty `stale` list is not an error — those items are re-claimed
automatically. A *rising* count means something is killing workers.

---

## 4a. Projects — running more than one thing at once

Work is keyed by **(project, item id)**, so two plans that both name `T1` are
two items rather than one row quietly overwriting the other.

```bash
# Register a project. It starts STOPPED: registering must not begin spending.
curl -sH "Authorization: Bearer $TOKEN" -X POST localhost:8099/api/projects \
  -H 'content-type: application/json' -d '{
    "project_id": "ngms", "name": "NGMS",
    "repo": "owner/NGMS", "work_dir": "/work/ngms",
    "base_branch": "main", "checks": ["cargo test"],
    "max_workers": 3, "max_attempts": 5, "min_free_disk_gb": 48
  }'

# Check the base branch before paying for an agent. Check entries are argv,
# not shell: use separate list entries instead of `cmd1 && cmd2`.
#
# The run is a whole build, so it happens in the background: start it, then
# poll. `check_base=true` on readiness reports the latest run and never starts
# one.
curl -sH "Authorization: Bearer $TOKEN" -X POST \
  localhost:8099/api/projects/ngms/preflight/base
curl -sH "Authorization: Bearer $TOKEN" \
  localhost:8099/api/projects/ngms/preflight/base | jq
curl -sH "Authorization: Bearer $TOKEN" \
  'localhost:8099/api/readiness?project_id=ngms&check_base=true' | jq

# Everything about it, in one call — counts, control state, live worker count.
curl -sH "Authorization: Bearer $TOKEN" localhost:8099/api/projects | jq

# Continue execution. This is the ONLY thing that creates workers.
curl -sH "Authorization: Bearer $TOKEN" -X POST localhost:8099/api/projects/ngms/start
```

**Nothing resumes on its own after a restart.** Every project comes back
`stopped`, carrying what it *was* doing:

```json
{"state": "stopped", "reason": "process started (was running)",
 "previous_state": "running"}
```

So a project you deliberately drained before a deploy does not come back
looking identical to one that was running happily — and a crash-looping pod
cannot restart the fleet on every loop.

`workers` is reported separately from `control.state` on purpose. `running` is
an instruction; `workers` is whether anything is carrying it out, and a project
marked running with zero workers is the failure that otherwise reads as
success.

**Changing capacity without stopping.** Re-registering a running project with a
different `max_workers` resizes its pool in place — POST the same body with the
new number. Extra workers start immediately; surplus ones stop claiming and are
joined once the item they are holding finishes, so no agent is interrupted and
the project never leaves `running`. That means `workers` stays at the old count
for as long as those items take, which is the honest answer: `max_workers` is
what you asked for, `workers` is what is alive. On a stopped project it changes
nothing until the next start, as registering anything does.

**Giving up.** An item that reliably kills its worker is never released, so its
lease lapses and it would be re-claimed forever — spending money each cycle
while looking exactly like an item that is busy. Past `max_attempts` it becomes
`exhausted`, which is different from `failed`: failed is one attempt that did
not work, exhausted says the harness will not try again without you. Raise the
limit and retry to rescue it; `0` disables it.

---

## 4b. Stop it without breaking anything

```bash
# Stop taking new work. Anything in flight finishes.
curl -sH "Authorization: Bearer $TOKEN" -X POST localhost:8099/api/control \
     -H 'content-type: application/json' \
     -d '{"state":"paused","reason":"deploying"}'

curl -sH "Authorization: Bearer $TOKEN" -X POST localhost:8099/api/control \
     -d '{"state":"running"}' -H 'content-type: application/json'
```

**Nothing in flight is ever interrupted.** Killing an agent mid-item destroys
the context that makes its work resumable and leaves a half-finished worktree
behind; stopping at the next item boundary is strictly better.

`draining` behaves identically to `paused` for a worker. The difference is
what you meant, which matters to whoever finds the fleet stopped and has to
decide whether to resume it — so set a `reason`.

## 4c. Re-route a role while it runs

```bash
curl -sH "Authorization: Bearer $TOKEN" -X PUT localhost:8099/api/roles \
  -H 'content-type: application/json' -d '{
    "roles": {
      "implementer": {"model": "a-cheaper-tier", "endpoint": "https://api.example"},
      "reviewer":    {"model": "a-different-vendor", "endpoint": "https://api.example"}
    }
  }'
```

Takes effect on the next call, no restart. This is possible only because a
call site names a **role**, never a model — so re-routing one is a data change
rather than a code change.

Worth doing deliberately: a reviewer on the same vendor as the implementer
means some share of reviews is a model grading its own work. Nothing enforces
that; it is your call.

---

## 4d. Serve the API *and* the workers

`serve` on its own is monitoring only — it exposes the API and has no workers,
so starting a project is refused rather than marking it running with nothing
able to claim. Give it a session host and it owns both:

```bash
HARNESS_TOKEN=… HARNESS_API_KEY=… AIDEVENV_TOKEN=… \
agent-harness --db harness.sqlite serve --port 8099 \
    --session-host https://your-devenv.example \
    --agent 'claude -p {prompt_file}' \
    --reviewer claude-sonnet-4-6 \
    --endpoint https://api.your-gateway.example
```

Everything project-shaped — the checkout, the repo, the checks, the base
branch — comes from the **registered project**, not from a flag, because one
deployment serves several projects and cannot have one checkout on its command
line. Register them with `POST /api/projects`.

**Nothing starts on its own.** Booting registers no workers and resumes no
project; `POST /api/projects/{id}/start` is the only thing that creates a
worker, and only after preflight passes. Stopping drains: no new claims, and
in-flight items are joined rather than killed. The stop request returns while
that happens; read `control.state` and `draining_items` from the project until
it changes from `draining` to `stopped`.

Monitoring-only deployments stay supported — a dashboard over someone else's
harness should not need a session host, a model key or a checkout.

The full deployment contract for both modes, including what the *agent's*
environment must hold and a non-destructive post-deploy smoke test, is in
[`DEPLOYMENT.md`](DEPLOYMENT.md).

---

## 5. Drive it from the API

The harness serves a full OpenAPI document with Swagger UI. Inside a session
host, the token that reaches the GUI reaches this too.

```bash
# Directly
curl -H "Authorization: Bearer $HARNESS_TOKEN" localhost:8099/api/work

# Through the session host, with your normal token
curl -H "Authorization: Bearer $AIDEVENV_TOKEN" \
     http://localhost:8910/api/harness/api/work
```

Swagger UI: `/docs` directly, or `/api/harness/docs` through the host.

```
GET  /api/work              backlog, counts and stale claims in one call
GET  /api/work/{id}         one item
POST /api/work              add items directly, without a plan document
POST /api/work/{id}/retry   re-queue; refuses while a claim is live
POST /api/work/{id}/block   park a decision, with a required reason
POST /api/plan/parse        parse a plan, reporting what it could NOT read
POST /api/plan/sync         plan → GitHub issues, dry-run by default
GET  /api/errors            rate limits by class
GET  /api/events            paged by row id, not timestamp
GET  /api/summary           enough for a status line
GET  /api/control           is the fleet claiming work?
POST /api/control           pause, drain or resume — never interrupts work
GET  /api/roles             where each role's calls go
PUT  /api/roles             re-route a role, live
GET  /api/readiness         can anything actually run, and why not
GET  /healthz               open, cheap, needs no credential
```

Some worked calls:

```bash
# What needs my attention right now?
curl -sH "Authorization: Bearer $TOKEN" localhost:8099/api/summary | jq
# {"running":1,"pending":2,"done":4,"failed":0,"stale":0,
#  "waiting_for_input":[{"item_id":"W2","session_url":"https://…/t/abc"}]}

# Add work without a plan document
curl -sH "Authorization: Bearer $TOKEN" -X POST localhost:8099/api/work \
  -H 'content-type: application/json' \
  -d '{"items":[{"item_id":"X1","title":"Fix the flaky test",
                 "brief":"tests/test_sync.py::test_retry is flaky under load."}]}'

# Retry a failed item
curl -sH "Authorization: Bearer $TOKEN" -X POST localhost:8099/api/work/W2/retry
# 409 if its claim is still live — an agent is working on it right now.

# Park a plan item that is a DECISION, not a task, so nothing claims it
curl -sH "Authorization: Bearer $TOKEN" -X POST localhost:8099/api/work/D8/block \
  -H 'content-type: application/json' \
  -d '{"reason":"needs a human: which database?","who":"sprooty"}'
# Anything that depends on D8 waits with it. The reason comes back as
# `blocked_reason` on the item, and retry is the way back once it is decided.

# Could anything actually run? One read-only call, before starting anything.
curl -sH "Authorization: Bearer $TOKEN" localhost:8099/api/readiness | jq
# {"mode":"supervised","ready_to_start":true,
#  "workers":{"configured":true,"ok":true,"detail":"2 worker(s) running"},
#  "session_host":{"configured":true,"ok":true,"detail":"reachable and authenticated, …"},
#  "reviewer":{"configured":true,"ok":true,"detail":"reviewer routed to …"},
#  "projects":[{"project_id":"ngms","ready_to_start":true,"summary":"ready", …}]}
#
# `/healthz` cannot answer this and does not try: a monitoring-only
# deployment is perfectly healthy and cannot run a single item.

# Preview a plan sync without writing
curl -sH "Authorization: Bearer $TOKEN" -X POST localhost:8099/api/plan/sync \
  -H 'content-type: application/json' \
  -d '{"path":"/work/PLAN.md","repo":"owner/name","dry_run":true}'
```

---

## 5b. Measure it over months

The audit log lives in its **own database**. The queue is mutable, migrated in
place, and a reasonable thing to delete and rebuild from the plan — anything
sharing that file shares that fate.

```bash
export HARNESS_AUDIT_DB=/var/lib/aidevenv/audit.sqlite   # a different volume
export HARNESS_AUDIT_REQUIRED=1                          # refuse to start without it
```

```bash
# Is anything actually being recorded?
curl -sH "Authorization: Bearer $TOKEN" localhost:8099/api/audit/health | jq
# {"configured":true,"degraded":false,"events":48213,"oldest":1754...}

# What did it cost?
curl -sH "Authorization: Bearer $TOKEN" 'localhost:8099/api/audit/cost?window=7d' | jq
```

```json
{"window": "7d", "total_cost_usd": 42.18, "total_unpriced": 61, "partial": false,
 "rows": [{"project_id": "ngms", "role": "implementer", "model": "a-model",
           "calls": 312, "tokens_in": 8100000, "cost_usd": 31.4, "unpriced": 0}]}
```

Three things in that response are deliberate:

| | |
|---|---|
| `total_unpriced` | Calls whose price was unknown, counted **separately**. A total that silently omits them reads as complete and is not. |
| `partial` | True when the window starts before the earliest recorded event. A chart labelled "7 days" drawn from one hour is not wrong about the data, it is wrong about the question. |
| `cost_usd: null` | Never `0`. Zero is a measurement claiming the call was free. |

**Give it prices.** The table ships pricing nothing — this harness is not tied
to a vendor and guessed rates produce confident, wrong money.

```bash
export HARNESS_PRICE_TABLE='{"version":"2026-08-01",
  "prices":{"a-model":{"in_per_mtok":3.0,"out_per_mtok":15.0}}}'
```

The price is stored **on each event**, not applied at read time. Applying
today's rates to last year's tokens is a projection, and it rewrites the past
every time a vendor reprices; recording the applied rate makes a repricing a
visible step in the series instead.

**Ground truth comes from GitHub.** Everything the harness knows about quality
is a proxy — a reviewer approved it, the checks passed. Whether it was merged,
rejected or reverted happens outside:

```bash
curl -sH "Authorization: Bearer $TOKEN" -X POST \
  'localhost:8099/api/audit/reconcile?repo=owner/name'
# {"merged":14,"closed_unmerged":2,"reverted":1,"skipped":37}
```

`skipped` is the pull requests the harness did not create — dependabot, humans.
Counted, never attributed: an outcome belonging to no item inflates every rate
it appears in.

**Retention runs itself.** Complete days roll up into immutable rows kept
forever; raw events are thinned after 90 days and **only** once the rollup
covering them exists. Thinning first is silent data loss that leaves a tidy
database and a hole in the series.

```bash
curl -sH "Authorization: Bearer $TOKEN" localhost:8099/api/audit/rollups | jq '.rolled_up_through'
```

---

## 6. Read the failures honestly

```bash
curl -sH "Authorization: Bearer $TOKEN" 'localhost:8099/api/errors?window=24h' | jq
```

```json
{
  "classified": {"rpm": 314, "window_cap": 9, "terminal_cap": 0},
  "unclassified": 0,
  "total": 323
}
```

| Class | Means | What the harness does |
|---|---|---|
| `rpm` | Going too fast | Retries, per-worker, with full jitter |
| `window_cap` | Short spend window exhausted | **Never retries.** Parks that endpoint, in that worker only |
| `terminal_cap` | Spend cap or rejected credential | **Never retries.** Parks for longer |
| `unclassified` | Recorded before classification existed | Counted separately and **never folded into a class** |

That last row matters. A pre-classification total has no per-class breakdown,
and none can be recovered by re-parsing — so the harness compares totals and
refuses to imply a per-class delta that does not exist.

`rpm` dominating means the fleet is asking for more than the account's
per-minute ceiling allows. Retry tuning will not fix that; fleet concurrency
is the lever.

---

## Configuration reference

| Variable | Used by | Purpose |
|---|---|---|
| `HARNESS_TOKEN` | `serve` | Bearer token for the API. Without it every authenticated route refuses. |
| `HARNESS_DB` | all | SQLite path. Default `./harness.sqlite`. |
| `HARNESS_API_KEY` | `run`, `serve` | Key for the model provider. In `serve` it is the reviewer's. |
| `HARNESS_ENDPOINT` | `run`, `serve` | Model API base URL. |
| `HARNESS_ROOT_PATH` | `serve` | Prefix when behind a proxy, e.g. `/api/harness`. |
| `AIDEVENV_URL` | `run`, `serve` | Session host, enabling attachable agents. In `serve` it is what makes the deployment supervised rather than monitoring-only. |
| `AIDEVENV_TOKEN` | `run`, `serve` | Session host token. |
| `HARNESS_AUDIT_DB` | `serve` | Audit database. Put it on a different volume so history does not share a fate with the queue. Defaults to `audit.sqlite` beside `--db`. |
| `HARNESS_AUDIT_REQUIRED` | `serve` | `1` refuses to start without a writable audit store. Off by default, because observation failing must not stop work. |
| `HARNESS_AUDIT_RETENTION_DAYS` | `serve` | How long raw events are kept once a rollup covers them. Default 90. |
| `HARNESS_PRICE_TABLE` | `run` | JSON price table, inline or a path. Without it, calls are recorded with tokens and **no** cost, and reported as `unpriced`. |
