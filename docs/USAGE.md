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
POST /api/plan/parse        parse a plan, reporting what it could NOT read
POST /api/plan/sync         plan → GitHub issues, dry-run by default
GET  /api/errors            rate limits by class
GET  /api/events            paged by row id, not timestamp
GET  /api/summary           enough for a status line
GET  /api/control           is the fleet claiming work?
POST /api/control           pause, drain or resume — never interrupts work
GET  /api/roles             where each role's calls go
PUT  /api/roles             re-route a role, live
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

# Preview a plan sync without writing
curl -sH "Authorization: Bearer $TOKEN" -X POST localhost:8099/api/plan/sync \
  -H 'content-type: application/json' \
  -d '{"path":"/work/PLAN.md","repo":"owner/name","dry_run":true}'
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
| `HARNESS_API_KEY` | `run` | Key for the model provider. |
| `HARNESS_ENDPOINT` | `run` | Model API base URL. |
| `HARNESS_ROOT_PATH` | `serve` | Prefix when behind a proxy, e.g. `/api/harness`. |
| `AIDEVENV_URL` | `run` | Session host, enabling attachable agents. |
| `AIDEVENV_TOKEN` | `run` | Session host token. |
