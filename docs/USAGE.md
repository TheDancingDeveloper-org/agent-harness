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

## 0c. Or adopt a project that is already half-built

The common case is not a blank repository. It is a plan, a repository, some
issues, some branches and some work that is *already done* — and nobody
remembers exactly which. `adopt` is the command for that, and its first
principle is that **it does not guess**.

```bash
agent-harness adopt PLAN.md --project widgets --work ./widgets --repo owner/name
```

That reads the plan, the working tree's branches, the queue and — with
`--repo` — the repository's issues and pull requests, and prints a proposal.
It writes no queue rows, opens no branches, edits no issue and closes nothing.
`--dry-run` goes further and does not even store the proposal; `--report FILE`
also writes it as JSON.

Real output, from a three-item plan in a checkout with a `harness/W3` branch
and no `--repo` (line-wrapped here to fit the page):

```
project widgets: proposed
repository /home/you/widgets
3 plan item(s); 2 proposed as already delivered; 0 needing a human decision
  W1 -> done  [proposed done]
      explicit/done: the plan item is checked
      would create queue row item W1 in project widgets: insert as done if this
        drop is approved, otherwise pending
  W2 -> done  [proposed done]
      runnable/passed: `python -m unittest -q tests.test_serials` succeeded
      would create queue row item W2 in project widgets: insert as done if this
        drop is approved, otherwise pending
  W3 -> pending
      candidate branch harness/W3 (present, medium): a local branch is named for
        this item; a branch name is not proof that the harness created it, and
        carries no evidence that the work is finished
      would create queue row item W3 in project widgets: insert as pending
inspection only: no queue rows, issue edits or other external changes were made.
2 item(s) proposed as already delivered and NOT dropped: W1, W2
Use --approve --reconcile, and name every allowed drop with --approve-drop.
```

With `--repo owner/name` each item also lists its issue and pull-request
candidates, with the state (`open`, `closed`, `merged`), the confidence, and
the reason the match was made — and the exact issue edit that approving it
would cause.

### How it decides an item is already done

Three rungs, in this order, and every rung that ran stays in the report:

| Rung | What it is | What it can do |
|---|---|---|
| **explicit** | The plan item is checked, or a closed issue / merged PR names the item id exactly. | Propose a drop. |
| **runnable** | The item's own `verify:` command exits 0. | Propose a drop. |
| **judged** | The `assessor` role says `done`, `partial` or `not_started`, with citations. | Propose a drop, and only with citations. |

**A proposal is not a decision.** Nothing enters the queue as `done` unless a
human names it:

```bash
agent-harness adopt PLAN.md --project widgets --work ./widgets --repo owner/name \
    --approve --approve-drop W1 --approve-drop W2 --reconcile
```

Anything proposed and not named stays `pending` — it gets done again, which is
wasteful, rather than lost, which is not recoverable. Rejecting works the same
way and needs a reason: `--reject "W2 is not finished"` or `--revise "..."`.

Uncertainty always resolves downwards. Two equally-good candidates for one
item, an assessor that says `done` and cites nothing, an assessor whose route
is down, or a `verify:` command that ran and *failed* while the assessor said
`done` — all of them come back as work to do, flagged for a human.

### What it will and will not touch outside the harness

| | |
|---|---|
| Backfilling an id marker | Appends `<!-- harness:id=W1 -->` to an issue body and changes nothing else — not the title, labels, milestone, assignees or a single word of the prose. Only for a drop you approved. |
| Adopting an existing pull request | Only when the PR carries that item's harness marker *and* its head branch is in this repository. A branch called `harness/w1` that nobody can prove the harness opened is reported as a medium-confidence candidate and never recorded as the item's PR. |
| An existing local branch | Listed as a lead and nothing more. A branch has no body, so its name is the only evidence there is — which is never enough to say the work is finished, or that the harness cut it. |
| A fork's pull request | Reported, never adopted: this repository did not produce it. |
| Closing or deleting anything | Never. Adoption has no path that closes an issue, deletes a branch or removes a queue row. |

Re-running is safe. Adoption never creates a second issue, never resets an
item the fleet has already finished, and never repeats a marker backfill —
the second inspection sees the marker it wrote the first time.

An item the queue has already failed keeps its attempts, its error and its
event history; the report quotes the prior failure so you can decide what to
do about it, and rewrites none of it.

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

`labels:`, `milestone:`, `depends on:`, `size:`, `risk:` and `verify:` are
recognised and removed from the brief — an agent should read the
specification, not the bookkeeping.

### `verify:` — how an item proves it is already done

One metadata key is executable, and its syntax is deliberately narrow:

```markdown
### W2: Reject duplicate serials at the API

verify: ["python", "-m", "pytest", "-q", "tests/test_serials.py::test_duplicate"]
```

A **JSON array of argv strings**, never shell text. `adopt` runs it in the
repository under exactly the same rules as a project check — fixed argv, no
shell, a timeout (`--verify-timeout`, defaulting to the project-check
timeout) — and an exit code of 0 is evidence that this item's work already
exists.

| | |
|---|---|
| `verify: ["pytest", "-q", "tests/test_serials.py"]` | Fine. |
| `verify: pytest -q && ./deploy.sh` | **Refused.** A plan is a document people edit and paste into; reading one must not be equivalent to granting it a shell. |
| `verify: []` or `verify: "pytest -q"` | **Refused.** Not a non-empty array of non-empty strings. |

It is per item, and it is not the project's check command. The project's
checks say the tree is healthy; `verify:` says one specific item is delivered.
`run` does not execute it — only `adopt` does.

### Dependencies say what kind of thing they are waiting for

A dependency is not just an id. `depends on:` takes **tokens**, and the token
says what sort of target it is:

| Token | Means |
|---|---|
| `W1` | work in this project |
| `external:RESOLVER:IDENTITY` | something outside the harness; `RESOLVER` answers for it |
| `decision:D9` | a human decision, parked as work in this project |
| `project:OTHER/W1` | work in a different project |
| `?W1` | **advisory**: reported, never a blocker |

```markdown
depends on: W1, external:github-issue:owner/repo#42, decision:D9
```

**A required target the graph cannot resolve blocks the item.** This is the one
behaviour worth reading twice, because it used to be the opposite. A dependency
naming something absent from the queue was previously treated as satisfied, on
the grounds that plans reference work tracked elsewhere — which is true, and
which made a typo, an omitted item and a genuine external reference completely
indistinguishable. All three ran immediately.

So a genuine external reference now says so and gets a resolver, and everything
else stops with a reason you can read:

```bash
curl -sH "Authorization: Bearer $TOKEN" \
  'localhost:8099/api/work/W2/readiness?project_id=widgets' | jq -r .explanation
# not ready at graph revision 4: local_work target 'W1x' is unresolved:
# no item 'W1x' in project 'widgets'; a required target the graph cannot find
# is a blocker, not an assumed external dependency
```

### Arrow notation, when there are enough edges to draw

Repeating `depends on:` per item stops reading well past a handful of edges, so
a plan can state its graph in one place instead:

````markdown
```dependencies
W1 -> W2        # the arrow follows the work: W2 waits for W1
W1 -> W3
external:github-issue:owner/repo#42 -> W4
```
````

The left side is the prerequisite. Both notations produce exactly the same
edge, and the same token grammar applies on the left. An arrow naming an item
the plan does not define is **reported**, not discarded — an arrow that lands
nowhere is the one outcome worse than a refusal.

### Reading and repairing the graph

```bash
agent-harness --db harness.sqlite graph report       # who is ready, and why not
agent-harness --db harness.sqlite graph export --out graph.json
agent-harness --db harness.sqlite graph rebuild      # re-derive edges from depends_on
agent-harness --db harness.sqlite graph checkpoint   # before copying the file
```

`graph report` exits 4 when anything is held back, so it works as a gate in a
script without parsing its text. It names cycles explicitly: two items that
each wait for the other are invisible one at a time, because each merely looks
like it is waiting.

The export/rebuild pair is the supported backup and recovery procedure, and
upgrading an existing database has a procedure of its own —
[`docs/MIGRATION-graph.md`](MIGRATION-graph.md).

If an item is blocked and you know better, the block lifts by decision rather
than by editing the database:

```bash
curl -sH "Authorization: Bearer $TOKEN" -X POST \
  'localhost:8099/api/work/W2/dependency-override?project_id=widgets' \
  -d '{"reason": "tracked in the other repo", "who": "sam"}'
```

The edge keeps its real state; the override is recorded next to it, and it
applies to **that graph revision only** — a later correction re-blocks the
item rather than inheriting a judgement nobody made about it.

### What it could not read is part of the answer

```bash
$ agent-harness plan examples/PLAN.md --repo owner/name --dry-run
dependencies:
  W4: external target(s) external:github-issue:owner/name#42 — needs a resolver
4 work items, 3 headings skipped as narrative
would create missing labels: area:api, area:docs
would sync: created 4, updated 0, unchanged 0
```

Those 3 skipped headings are `Widget service`, `Background` and `Dependencies`
— narrative and the graph block, as expected. **A large skip count relative to
items means your plan does not use a recognised shape**, and the harness would
rather tell you than quietly find three items in a fifty-item plan.

The `dependencies:` block above it is the other half of the same idea: every
line there is something that *will* hold work back, said before the issues
exist rather than after the queue has stopped.

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

**A role may name several models, in preference order.** The first that
answers does the work; the others are tried only when it will not:

```bash
    --implementer deepseek-v4-flash,glm-5.2,gpt-5.4 --reviewer gpt-5.6
```

This is not load spreading, it is availability. Measured on one gateway, 34 of
42 advertised models were unavailable simultaneously — a role with a single
name is a fleet that stops when that name is down. The whole chain is tried
before any backoff, because a model that is down answers immediately and
sleeping on it first would waste the alternatives entirely; the event stream
records which model actually answered, so a fleet quietly running on its third
choice says so.

Two bounds worth knowing. A chain protects against a *model* being
unavailable, not against running out of budget: a spend cap belongs to the
account, so it parks every model behind that endpoint. And `/api/roles`,
readiness and the independence warning all report the *preferred* route — a
fallback that has not been needed is not what you configured.

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

The response says which of those roles this deployment actually calls. In
session mode the agent process plans and implements with its own credentials
and endpoint, so `planner` and `implementer` come back `"used": false` with the
command that does that work instead: they are stored, the non-session executor
uses them, and nothing here will. A project can override any role for itself
with `roles` on its registration; unnamed roles still come from this map.

Worth doing deliberately: a reviewer on the same vendor as the implementer
means some share of reviews is a model grading its own work. Nothing enforces
that; it is your call.

### Which protocol a route speaks

A route may name a **preset** — the wire protocol, the authentication header,
the response reader and the failure classifier, as one name:

```bash
curl -sH "Authorization: Bearer $TOKEN" -X PUT localhost:8099/api/roles \
  -H 'content-type: application/json' -d '{
    "roles": {
      "reviewer": {"model": "m", "endpoint": "https://api.example/v1",
                   "preset": "claw-bay", "price_ref": "tier-2"}
    }
  }'
```

`GET /api/roles` shows it back. Omit it and the route uses the deployment
default (`run --preset` / `serve --preset`, default `chat-completions`), which
is printed at startup along with any per-role override.

`agent-harness --help` will not list the presets you can name, because they are
not a fixed set: `generic` is built in, this distribution publishes
`chat-completions` and `claw-bay`, and any installed package or
`HARNESS_ROUTE_PRESETS` entry adds more. Name one that does not resolve and the
CLI refuses before anything claims work, listing the ones that do.

The older `provider` field still works and still means what it always meant —
the **classifier only**. `{"provider": "claw-bay"}` keeps the deployment's wire
protocol and reads failures with that gateway's envelope, which is exactly what
it did before presets existed. Writing a `preset` supersedes it.

Adding a vendor of your own is a preset registered by name — no fork, no change
to any harness module. See
[`INTERNALS.md`](INTERNALS.md#route-presets-adding-a-vendor-without-touching-core).

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
GET  /api/roles             where each role's calls go, and which are called
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
| `HARNESS_ROUTE_PRESET` | `run`, `serve` | Default route preset (`--preset`) for roles that name none: the wire protocol, the authentication header, the response reader and a failure classifier, as one name. Default `chat-completions`. |
| `HARNESS_ROUTE_PRESETS` | all | Extra presets to make resolvable, as `name=module:attribute` pairs. For a preset that lives in your own code rather than in an installed distribution's entry points. |
| `HARNESS_ROOT_PATH` | `serve` | Prefix when behind a proxy, e.g. `/api/harness`. |
| `AIDEVENV_URL` | `run`, `serve` | Session host, enabling attachable agents. In `serve` it is what makes the deployment supervised rather than monitoring-only. |
| `AIDEVENV_TOKEN` | `run`, `serve` | Session host token. |
| `HARNESS_AUDIT_DB` | `serve` | Audit database. Put it on a different volume so history does not share a fate with the queue. Defaults to `audit.sqlite` beside `--db`. |
| `HARNESS_AUDIT_REQUIRED` | `serve` | `1` refuses to start without a writable audit store. Off by default, because observation failing must not stop work. |
| `HARNESS_AUDIT_RETENTION_DAYS` | `serve` | How long raw events are kept once a rollup covers them. Default 90. |
| `HARNESS_PRICE_TABLE` | `run` | JSON price table, inline or a path. Without it, calls are recorded with tokens and **no** cost, and reported as `unpriced`. |
