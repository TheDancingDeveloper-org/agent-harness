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

## Which way in?

Four routes, and they are not alternatives to each other so much as different
starting points. Find your row.

| Where you are | Start at | What you get |
|---|---|---|
| **You want to see whether this thing works at all** | [§0a](#0a-the-first-run-no-credentials-no-network-no-model) — `init --demo` | A real git repo, a plan and a queue, and one item taken end to end. No credentials, no network, no model. |
| **New project, and you have not written the plan yet** | [§0b](#0b-or-dont-write-a-plan--describe-it-and-argue) — inception, **over the API** | Describe it in a paragraph, argue with the proposed scope, get a `PLAN.md`. Nothing external exists until you approve. |
| **New project, and you already have a plan** | [§1](#1-write-a-plan) — `plan` | Your markdown parsed into work items, then synced to issues. |
| **Existing project, already part-built** | [§0c](#0c-or-adopt-a-project-that-is-already-half-built) — `adopt` | What is *already done* proposed rather than assumed, with the evidence for each claim. Nothing is dropped unless you name it. |

They converge. **Every route ends at a `PLAN.md` and a project in the queue**,
and from there [§3](#3-execute-it) is the same for all of them:

```
inception ──┐
            ├──▶ PLAN.md ──▶ plan (sync) ──▶ project ──▶ run
your plan ──┤                                  ▲
   adopt ───┴──────────────────────────────────┘
                (adopt also seeds what is already done)
```

Whichever you pick, run [`doctor`](#0a1-ask-what-a-real-run-would-need) before
you start anything real. It tells you what a run would need and contacts
nothing.

---

## 0a. The first run: no credentials, no network, no model

Before configuring anything, watch an item go all the way through.

```console
$ agent-harness init --demo --into ./demo
built the demo in /home/you/demo
  repository   /home/you/demo/repo   (a real git repo, one commit)
  plan         /home/you/demo/PLAN.md   (one item)
  queue        /home/you/demo/queue.sqlite   (project `demo`, stopped)

Nothing is running and nothing external has happened: no network call,
no credential read, no GitHub anything. Run it with:

  agent-harness --db /home/you/demo/queue.sqlite run --demo --project demo \
      --work /home/you/demo/repo --events /home/you/demo/events.jsonl \
      --no-push --limit 1 --check 'python -m unittest discover -s tests -q'
```

Run that second command and the item goes through every stage:

```console
T1 started
T1 calling — planner
T1 planner_targets — {"targets": [{"path": "calc/operations.py", …}], …}
T1 context_selected — {"files": ["calc/operations.py", "tests/test_operations.py", …], …}
T1 calling — implementer
T1 applied — git apply
T1 checks_passed
T1 checkpointed — harness/t1
T1 calling — reviewer
T1 review_approved — APPROVED
T1 done — harness/t1
  ok  T1: plan -> implement -> apply -> checks -> commit -> review
1/1 items completed
```

`git -C ./demo/repo log --oneline --all` shows the commit on its own branch,
with `main` untouched. `./demo/events.jsonl` has every step.

**What this proves.** The wiring: plan parses to work, the graph admits it, a
worktree is made, a diff is validated and applied, the configured checks run
*before* the reviewer, the reviewer's verdict is a gate, and every step is in
the event stream. Only the transport is replaced — everything above it is the
same code a real run uses.

**What it does not prove.** Anything about a model, because there is no model.
The three replies are fixed. A green demo and a working fleet are different
claims, and this is the first one only. The scripted reviewer says so in its
own approval text, so nobody reading the event stream mistakes it for a
verdict.

You will see one warning, and it is correct: all three roles are the same
scripted "model", so the reviewer is not independent. In a real run that
warning means a model is grading its own work.

### 0a.1 Ask what a real run would need

```console
$ agent-harness --db ./demo/queue.sqlite doctor
environment
  ok    git: git at /usr/bin/git
  ?     gh cli: gh at /usr/bin/gh; whether it can WRITE is not checked here …
  ok    route presets: resolvable by name: chat-completions, claw-bay, generic
  ok    dependency resolvers: declared: github-issue

project demo
  ok    checkout: /home/you/demo/repo
  ok    disk space: 518.5 GiB free of 1831.7 GiB …
  ok    checks: 1 check(s) run before review
  ok    routes: planner=…, implementer=…, reviewer=…
  ok    protocol and classifier: planner: generic / generic; …
  warn  cost-cap classification: … the generic HTTP classifier … cannot tell a
        spend cap from a burst limit …
  warn  reviewer independence: reviewer and implementer are the same model …
  ?     cost visibility: no price is known for: … so any total is a lower bound
  ok    github mutations: no repo configured: nothing here can create an issue,
        branch or pull request. Local work only.
  ?     model reachability: not asked — it needs a network and a credential.
        Pass --probe-models to ask. Not asking is not the same as answering.

nothing blocks a start. Warnings and unknowns above are not passes: an unknown
is a thing nobody has checked.
```

`doctor` exits `0` when nothing blocks and `1` when something does, so it is
usable in a script. `--json` gives the same report machine-readably.

**It contacts nothing.** Every line above is answered from the database, the
filesystem and installed package metadata. `--probe-models` opts into the one
question that needs a network, and needs `HARNESS_API_KEY` and `--endpoint` to
do it. A `?` is never a pass — it is a thing nobody has checked.

`doctor` and `preflight` overlap on purpose and share their probes, so the
report cannot disagree with the gate that actually refuses a start. What
`doctor` adds is the set of questions that do not block a start but change what
you can believe about a run.

---

### 0a.2 Running against a local model

Optional, and deliberately not part of the demo or of required CI: a local
model is a *your machine* dependency, and the no-network path must not acquire
one.

The harness speaks OpenAI-compatible chat completions, so any server that does
is usable. Using [Ollama](https://ollama.com) as the worked example:

**You supply the server and the weights.** The harness downloads nothing,
installs nothing and starts nothing. Install Ollama yourself, then pull a model
yourself:

```bash
ollama pull qwen2.5-coder:7b
ollama serve            # listens on 127.0.0.1:11434
```

**The endpoint shape is the base URL, without the path.** The
`chat-completions` preset appends `/chat/completions` itself, because that is
what every gateway's documentation prints:

```bash
export HARNESS_ENDPOINT=http://127.0.0.1:11434/v1
export HARNESS_ROUTE_PRESET=chat-completions
```

**Authentication is not disabled — it is sent and ignored.** The preset always
sends a bearer header, and Ollama does not check it. `HARNESS_API_KEY` must
still be set to something non-empty, because the harness refuses to run without
one rather than silently sending an empty credential to a server that might
have wanted a real one:

```bash
export HARNESS_API_KEY=unused-locally
```

Then run normally:

```bash
agent-harness run --work ./target --no-push --check 'pytest -q' \
    --planner qwen2.5-coder:7b \
    --implementer qwen2.5-coder:7b \
    --reviewer a-different-local-model
```

**What "offline" means here, exactly.** No traffic leaves your machine *for
model calls*. It does not mean the harness is offline: `--repo` still reaches
GitHub, and `reconcile` still does. For a genuinely airtight run use `--no-push`
and configure no repo, which is what the demo does.

**Two things to expect.** The reviewer should be a different model from the
implementer, and running one local model for both means every review is a model
grading its own work — the harness warns, and the warning is right. And the
generic HTTP classifier cannot tell a local server's `429` or `500` apart from
a spend cap, because nothing in HTTP can; on a local server there is no spend
cap, so this matters less than it does against a gateway.

None of this is covered by the no-network test suite, by design. It is an
opt-in smoke test you run against a server you supplied.

---

## 0b. Or don't write a plan — describe it and argue

If you do not already have a plan, scope one. Nothing external exists during
any of this: no repository, no issues, no branches, no queue rows.

> **This one is API-only — there is no `agent-harness inception` subcommand.**
> Start the service first and keep it running for the whole exchange:
>
> ```bash
> HARNESS_TOKEN=dev uv run agent-harness --db harness.sqlite serve --port 8099
> export TOKEN=dev
> ```
>
> It needs a model routed for the `scoper` role, because proposing a scope is a
> model call. `agent-harness --db harness.sqlite doctor` will tell you whether
> one is.

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

**What you have now is a `PLAN.md`, and nothing else.** No repository, no
issues, no queue rows — deliberately, so another round of questions costs a
conversation rather than a cleanup. Continue at [§1](#1-write-a-plan) to parse
and sync it, exactly as though you had written it by hand.

---

## 0bb. Or point it at a project and state an objective

`inception` scopes a **new** project from prose. `survey` does the same for one
that already exists: you say what you want done, and the first run works out
what the items should be instead of being handed them.

```console
$ agent-harness survey "review and generate a plan to upgrade to Node v22" \
    --work ./service --doc docs/roadmap.md \
    --surveyor claude-sonnet-4-6 --endpoint $HARNESS_ENDPOINT --out PLAN.md
read 3 source(s): docs/roadmap.md, 412 tracked path(s), recent history
9 work item(s), 4 heading(s) skipped as narrative
blocking question: is the native addon in vendor/ still maintained upstream?
wrote PLAN.md
Review it, then: agent-harness adopt PLAN.md --project NAME --work ./service
```

**Name your roadmap with `--doc`.** Without it the harness guesses from a short
list (`docs/current-state.md`, `ROADMAP.md`, `README.md`, …) and stops at two.
A named file that is missing is *reported*, not skipped — the failure this
command exists to prevent is a confident plan built without the document that
states the project's direction.

**The gate is the harness's own parser.** The generated plan is read back by
`parse_plan`, the same function a hand-written plan goes through, so a plan the
queue would read differently from how it looks is caught here rather than three
commands later. If it cannot be read — no items, or duplicate ids, which cannot
each become one issue — nothing is written. `--force` overrides that, and means
executing a plan the harness has told you it does not understand.

**Nothing external happens.** No queue rows, no issues, no branches. Without
`--out` it prints the plan and writes nothing at all, which is the right
default for output whose entire purpose is to be argued with.

Blocking questions are reported and do **not** stop the file being written.
They are questions for you, and your answer decides — the same rule
`inception` applies at its approval gate.

### 0bb.1 Items that produce an answer rather than a change

Some work has no diff. "Compare these three approaches", "is this feasible",
"which of these is the cause" — the answer *is* the deliverable, and an item
like that used to leave a clean worktree and be recorded as
`escalated / no_target`: indistinguishable from an agent that did nothing.

An item can now say what it produces:

```markdown
### T1 — Choose the Tailnet attachment architecture

Compare a host-managed daemon, a managed sidecar, and a tsnet bridge. Say
which fits this deployment and what rules the others out.

deliverable: findings
```

`deliverable: code` is the default and means a diff, judged exactly as before.
`deliverable: findings` tells the agent to write its answer to
`.harness-findings.md` and change nothing else. The answer becomes the item's
result, the item completes, and the file is never committed.

**The plan declares this; the agent never chooses it.** Otherwise the first
hard test failure becomes an essay about why the test was wrong.

A findings item can still refuse — "this question cannot be answered from this
repository" is an escalation whatever the item was asked to produce — and one
that answers nothing at all lands in the same clean-tree path as any other
agent that did nothing, which is where it belongs.

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
3 plan item(s); 2 proposed as already delivered (1 unconfirmed); 0 needing a
  human decision
  W1 -> done  [proposed done]
      explicit/done: the plan item is checked
      would create queue row item W1 in project widgets: insert as done if this
        drop is approved, otherwise pending
  W2 -> pending  [possible drop, unconfirmed: droppable only if a human names it]
      runnable/passed: `python -m unittest -q tests.test_serials` exited 0,
        which says the command did not fail; it does not say the command
        tested anything
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
| **runnable** | The item's own `verify:` command exits 0. | **Offer** a drop. On its own it proposes nothing — see below. |
| **judged** | The `assessor` role says `done`, `partial` or `not_started`, with citations. | Propose a drop, and only with citations. |

**An exit code is not a rung on its own.** A `verify:` that exits 0 has not
failed; that is not the same fact as "the work is there", because in most test
runners a name filter that matches nothing passes. So a passing `verify:`
proposes `done` only when a second rung agrees — explicit evidence, or an
assessor `done` with citations. Alone, the item is listed as a drop you *may*
approve, marked `unconfirmed`, and it stays `pending` if you say nothing.
[How to write one that fails when the work is absent](#verify--how-an-item-proves-it-is-already-done).

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
is down, a `verify:` command that ran and *failed* while the assessor said
`done`, or a `verify:` that passed with nothing to corroborate it — all of them
come back as work to do, flagged for a human.

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

### After adopting: you have a queue, not a running project

`--reconcile` writes the queue rows. It does **not** register a project's
configuration, and it does not start anything — a project starts `stopped`, and
registering one must never begin spending.

So the next two steps are the ordinary ones:

1. **Register the project** with its checkout, base branch and checks —
   [§4a](#4a-projects--running-more-than-one-thing-at-once). Set
   `max_item_seconds` and `max_item_spend_usd` here too if this is going to run
   unattended ([§6d](#6d-bounding-what-one-item-may-cost-you)).
2. **Check it could actually run**, then start it —
   [`doctor`](#0a1-ask-what-a-real-run-would-need) and
   [§3](#3-execute-it).

Re-running `adopt` later is safe and is the intended way to pick up work done
outside the harness since. It never resets an item the fleet has finished.

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
timeout) — and an exit code of 0 is *one* piece of evidence that this item's
work already exists. On its own it never drops the item: read the next
paragraph for why, because it is the reason the word "one" is doing work
there.

| | |
|---|---|
| `verify: ["pytest", "-q", "tests/test_serials.py"]` | Fine. |
| `verify: pytest -q && ./deploy.sh` | **Refused.** A plan is a document people edit and paste into; reading one must not be equivalent to granting it a shell. |
| `verify: []` or `verify: "pytest -q"` | **Refused.** Not a non-empty array of non-empty strings. |

It is per item, and it is not the project's check command. The project's
checks say the tree is healthy; `verify:` says one specific item is delivered.
`run` does not execute it — only `adopt` does.

**Write one that fails when the work is absent, and check that it does.** This
is the trap, and it is easy to fall into: in most test runners **a name filter
that matches nothing is a pass**.

```markdown
verify: ["cargo", "test", "-p", "gateway", "secure_cookies"]
```

That looks like "the test for this item passes". On a tree where the item has
not been done, the test does not exist, `cargo test` runs zero tests and exits
`0`. `pytest -k`, `go test -run` and `npm test -- -t` behave the same way;
`pytest path::name` is the exception, exiting 4 when the name is absent.

The harness cannot tell those apart, and deliberately will not try: reading
another ecosystem's output to guess how many tests it ran is exactly what an
adapter is for, and a wrong guess here **drops work that is then never done**.
What it does instead is refuse to decide on that one fact. Adoption reports:

```
R2 -> pending  [possible drop, unconfirmed: droppable only if a human names it]
    runnable/passed: `cargo test -p gateway secure_cookies` exited 0, which
      says the command did not fail; it does not say the command tested
      anything
```

The item is offered to `--approve-drop R2` — you may know the command is a
real one — but nothing proposes it, and approving the report without naming it
leaves the work to do. A second rung changes that: an assessor `done` with
citations, or a closed issue naming the item, and the drop is proposed
outright.

So before trusting a `verify:`, run it on a tree where the item is *not*
finished and confirm it fails. A command that asserts a fact about the tree —
`grep -q`, a file existing, a whole test file rather than a filtered name —
fails honestly when the work is missing.

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

### Headless works in your checkout, and clears it first

Worth its own heading, because the two modes differ and the diagram above is
the session one.

**With `--session-host`**, each item gets its own `git worktree` and your
checkout is untouched.

**Without it**, the harness works **in place** in the directory you passed as
`--work`, and every attempt starts by putting that directory on a known state:

```bash
git checkout -- .   # every uncommitted change to a tracked file: reverted
git clean -fd       # every untracked file and directory: DELETED
```

That is right for the harness's own leftovers — a worker killed mid-apply ends
nothing and leaves a half-applied diff — and it does not distinguish those from
yours.

So **a dirty checkout is refused before anything is claimed**:

```console
$ agent-harness run --work ./my-project …
refusing to start: 14 uncommitted change(s) and 3 untracked path(s) in
./my-project. A run discards both — tracked files are reverted and untracked
ones are DELETED — so this work would be lost and could not be recovered.
Commit or stash it, or pass --allow-dirty if it is genuinely disposable.
```

`doctor` reports the same thing before you get that far, and `--allow-dirty`
overrides it — loudly, and recorded in the preflight report, because the whole
point is that the loss is silent and irreversible.

One consequence of working in place: **one worker per checkout**. Two headless
workers on one directory would check branches out over each other.

### The role flags are a seed, not a setting

```bash
agent-harness run --implementer some-model …
```

On a **fresh** database that stores `some-model` and uses it. On a database
that has run before, the **stored role map wins** — because a stored route is
how `PUT /api/roles` re-routes a live deployment without a restart, and the
command line must not silently undo that.

The consequence surprises people, so the harness now says it in the log:

```
WARNING a stored role map overrides the command line: implementer=old-model
        (you asked for some-model). The flags seed the map on first use only;
        after that the stored map wins. Pass --reroute to make the command
        line win, or PUT /api/roles.
```

`--reroute` inverts it for that run and stores the result. Without it, changing
a model meant editing the `settings` table by hand or deleting the queue.

A complete stored map needs **no role flags at all** — `run` consults it before
deciding a role is unconfigured.

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

### What each role is shown

Worth knowing, because it is what the models are actually judged on, and
because each of these was once absent and cost real attempts to discover.

| Role | Sees |
|---|---|
| planner | the brief, and the repository's file listing |
| implementer | the brief, its own plan, a bounded slice of the repository **target file first**, the check commands that will run on its diff, and why the previous attempt was refused if there was one |
| reviewer | the brief, the diff, **the files that diff touched as they now stand**, and whether the checks passed |

Three of those are recent and are worth stating plainly:

- **The implementer is told the checks.** It is graded by them; keeping them
  secret from it costs an attempt and two model calls to discover a formatter.
  It does not weaken the gate — the command still runs and still refuses.
- **The reviewer is given the touched files.** Asked whether a change is wired
  in where it should be, and holding only the change, it must answer "the diff
  does not show" — and "the task cannot be judged from what you were given" is
  grounds to reject. A file too large for the budget is **named as absent**,
  because a reviewer that thinks a partial view is complete is worse than one
  that knows it is partial.
- **A retry is told why the last attempt was refused.** It is *not* a
  resumption: the item is re-planned against the current brief exactly as
  before, no prior diff is fed back, and nothing is treated as progress. It
  simply does not repeat the last mistake blind.

**A brief that does not bound its own scope will be rejected.** The reviewer is
told to assume the work is wrong, and a sufficiently sceptical model can always
name one more path it has not been shown. An item that says what "done" is —
finitely, and including what is *not* in scope — is judged; one that does not
collects rejections that are each individually reasonable. That cost lands as
retries, and it is the plan's to fix, not the reviewer's.

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

### 6a. Why an item is in the state it is in

The classes above are what a **provider** answered. They say nothing about what
a **gate** answered, and those are different questions — a gateway's opinion
about your budget and a test suite's opinion about a diff are not the same kind
of fact.

`failed` covers a reviewer's rejection and a crashed worker alike. One is the
system working and the other is the system broken, and they want opposite
responses. So every item also carries a `disposition` and a `reason_kind`:

```bash
curl -sH "Authorization: Bearer $TOKEN" 'localhost:8099/api/work/T4' \
  | jq '{state, disposition, reason_kind, attempts, last_error}'
```

```json
{
  "state": "failed",
  "disposition": "refused",
  "reason_kind": "review_rejected",
  "attempts": 1,
  "last_error": "review rejected: …"
}
```

| Disposition | Means | What you should look at |
|---|---|---|
| `completed` | It is done | Nothing |
| `refused` | A gate said no about **this item's work** | The diff, or the brief |
| `crashed` | The worker or harness broke; nothing judged the work | The harness |
| `withheld` | Never attempted, or discarded through no fault of the item | The provider, the budget, or the plan |
| `escalated` | A person has to resolve something | The machine |
| *(empty)* | Nobody has finished with it yet | Nothing. **Not a sixth disposition.** |

`reason_kind` is the specific one, as a token rather than English so a client
can branch on it: `checks_failed`, `check_escalated`, `check_transient`,
`review_rejected`, `patch_rejected`, `no_target`, `worker_error`,
`provider_exhausted`, `budget_exhausted`, `dependency_invalidated`,
`agent_timeout`, `claim_lost`, `item_wall_clock`, `item_spend`,
`hold_expired`, `context_unavailable`, `item_impossible`.

### 6a.0 When the agent says the item cannot be done

An agent in session mode is told, in its prompt, that if the item is ambiguous,
contradicts the code, or depends on something absent, it should write its
reasoning to `.harness-refusal.md` and change nothing else.

That is a correct outcome, and the harness records it as one:

```json
{"state": "blocked", "disposition": "escalated",
 "reason_kind": "item_impossible", "attempts": 0,
 "last_error": "The item asks for a snippet timestamp, but every route to one
                is forbidden by its own criteria: …"}
```

**It costs no attempt.** What is wrong is the brief, and no number of retries
rewrites a brief — so the item waits for you rather than spending its budget
proving the same point three more times. It appears wherever your deployment
surfaces `escalated`, which is the set of dispositions meaning *a person, not a
timer, is what this is waiting on*.

The note itself never reaches a commit, a diff or a reviewer: it is read and
deleted before the worktree is inspected. An agent that leaves the note *and*
makes real changes has not refused, and is judged on the changes as usual.

A session that ends with a clean tree and **no** note escalates too, with
`reason_kind: no_target` and the session id in `last_error` — an agent that
did nothing and an agent that could not say why both want a human, and the
session is where the explanation is.

### 6a.1 When the target does not fit in the prompt

The implementer is shown a bounded slice of the repository — 60,000 characters
by default, the file the planner named first and a relevance-ordered fallback
after it. A repository whose relevant file is *larger than the whole budget*
therefore has a target that cannot be supplied at all.

That used to proceed anyway: the target was dropped, the fallback filled the
space with whatever else was nearby, and the implementer was asked to change a
file it had never seen. It answers — models do not refuse for want of evidence
— and the diff then fails to apply, which reads in the log as a bad model and
is not one.

Now the item stops **before** the implementer is called:

```json
{"state": "blocked", "disposition": "escalated",
 "reason_kind": "context_unavailable", "attempts": 0,
 "last_error": "the planner's target(s) crates/gateway/src/main.rs (612334 bytes)
                do not fit the context budget of 60000 characters, …"}
```

It costs no attempt, because no attempt could have succeeded and retrying will
not make the file smaller. Two things fix it, and both are yours to choose:

```bash
agent-harness run --context-budget 400000 …    # or $HARNESS_CONTEXT_BUDGET
```

or split the file. The ceiling that actually matters is the model's context
window, which the harness does not know and will not guess — a budget large
enough to overflow it turns a working item into a provider error, so raise it
deliberately rather than to the maximum.

### 6b. A check has five answers, not two

`Checks` classifies **how the subprocess ended**, never what your project's
output said — guessing at another ecosystem's messages is how a generic harness
stops being one.

| Outcome | When | What happens |
|---|---|---|
| `pass` | exit 0 | On to the reviewer |
| `fail` | exit non-zero | The item is refused. Costs an attempt. |
| `fix_available` | exit non-zero, **and** you declared a fix for that command | The fix is recorded in the event stream. With `apply_fixes` off (the default) the item is refused there; with it on, the fix is run once and the check re-run |
| `retry` | the command did not finish in time | The item goes back to pending. **Costs no attempt** — the question was not answered, which is not the answer being no |
| `escalate` | the program is not installed, or the disk is full | The item is **blocked**. No diff fixes that and no retry clears it |

Declaring a fix, per project:

```json
{"checks": ["ruff format --check ."], "fixes": {"ruff format --check .": ["ruff", "format", "."]}}
```

It is recorded, not applied — unless you also say `"apply_fixes": true`.

#### Running a declared fix (#155)

A formatter in check mode is a gate no model reliably passes. It is not a
judgement about the work: it is column arithmetic, the model cannot compute what
`rustfmt` would have done, and a measured run of one item was refused four times
out of seven for brace placement while being substantively correct every time.

With `apply_fixes` on, a check that fails and has a declared fix has that fix run
**once, in the item's own worktree**, and the check **re-run**. The re-run is the
verdict:

```json
{
  "checks": ["cargo fmt --all -- --check", "cargo test"],
  "fixes": {"cargo fmt --all -- --check": ["cargo", "fmt", "--all"]},
  "apply_fixes": true
}
```

**This is allowed only because a formatter's fix is deterministic and
mechanical.** It changes where a brace goes, never what the program does, and
what it produces is the code that would have been merged anyway. **A failing
test must never be "fixed" and re-run** — a test failure is a statement about
behaviour, repairing it is a judgement, and re-running it after something edited
it launders the failure rather than reporting it. Declaring a fix for a test
suite, a type checker, or a linter whose autofix changes behaviour defeats every
guard below.

The gate is not weakened. What the harness enforces:

- the fix runs **once**, and never provokes another fix. A gate cannot be ground
  down to a pass;
- **the whole suite must pass on the post-fix tree** — checks that had already
  passed are re-run after a fix, so a fix cannot buy one gate by breaking
  another;
- the fix may only **rewrite files that already exist**. Adding, deleting or
  renaming a path is not something a formatter does, and it escalates to a
  person rather than passing the item;
- the fix runs with the **item's worktree** as its working directory, and an
  argv naming an absolute path or a `..` segment is refused before it runs;
- **nothing is silent.** Each fix that runs emits a `check_fix_applied` event
  naming the check, the fix and the exact paths it rewrote; those paths are in
  the committed diff; and the reviewer is told, in its prompt, that the harness
  modified the tree and which files it touched.

What the harness **cannot** enforce is that the command you declared is a
formatter. `apply_fixes` is off by default, is per project, and applies only to
commands you personally paired with a fix. That last step is your assertion, and
nothing checks it.

`escalate` is an **additional** outcome and never a softer `fail`. A check
cannot reach for it to avoid failing an item, and an escalating check still
stops the item before the reviewer is paid.

### 6c. What a killed worker costs

An attempt used to be all-or-nothing: kill a worker after the checks passed and
the next claim started again at the planner, re-paying for the plan, the
context selection and the implementer. It now resumes.

Six stages are recorded as an attempt reaches them — `planned`, `implemented`,
`applied`, `checked`, `checkpointed`, `reviewed` — each with the artefact that
makes it resumable. A re-claim reads the last one and continues.

**Recording a stage is not the same as being able to resume at it**, and the
difference is stated rather than implied:

| Reached | Resumes at | Because |
|---|---|---|
| `planned` | `planned` | the plan is durable; the planner is not re-asked |
| `implemented` | `implemented` | the diff is durable; the implementer is not re-asked |
| `applied` | `implemented` | an uncommitted working tree does not survive a crash, so the stored diff is re-applied to a fresh branch |
| `checked` | `implemented` | same, and re-running checks is cheap and idempotent |
| `checkpointed` | `checkpointed` | the commit is in git, which is as durable as it gets |
| `reviewed` | `reviewed` | the verdict is reused, not re-asked — a model is not deterministic, and re-asking would make a crash a way to shop for a different answer |

**A resumed attempt is the same attempt.** `max_attempts` bounds genuine
failures, not crashes (decision D11). The consequence is worth knowing: an item
that crashes in a loop is bounded by a budget rather than by the attempt count,
and per-item budgets are a later stage.

**A decision ends resumability.** A worker that reached a verdict decided; only
a worker that was *killed* leaves a position to continue from. So retrying a
rejected item re-plans it against the current brief rather than resuming into
its own rejection, and `POST /api/work/{id}/retry` forgets every attempt at it.

**A brief that moves discards the position, loudly.** Re-syncing a plan rewrites
`title`, `brief` and `depends_on` on live claimed rows. An attempt briefed
before that change is not resumed into work that answers a superseded question —
it emits `brief_moved` and starts again.

#### Durability is a policy

```json
{"durability": "boundary"}
```

| Mode | Writes | Use it for |
|---|---|---|
| `exit` | nothing until the attempt ends | the deterministic demo, and anywhere a crash costing a re-plan is cheaper than the writes |
| `boundary` | one row per stage boundary — **the default** | a fleet |
| `sync` | every boundary, plus the *intent* to perform each external effect before it happens | anywhere a push that may have half-happened must be a fact rather than a gap |

The **pre-review git checkpoint is unaffected by all three.** The mode governs
the attempt *record*; the commit before the expensive gate happens regardless,
and no mode can remove it.

### 6d. Bounding what one item may cost you

```json
{"max_item_seconds": 3600, "max_item_spend_usd": 2.50}
```

Both default to **zero, meaning unlimited**, so upgrading changes nothing. Per
item, `PATCH`-style overrides live on the work row; zero there means "take the
project's".

The lease bounds a worker's *absence*. These bound an item's *duration* and its
*spend*, which a heartbeat says nothing about — an item that heartbeats forever
is indistinguishable from one making progress, and it is the failure mode a
seven-day run is most likely to produce.

**Exceeding a ceiling stops the item at the next boundary**, never mid-stage.
Stopping in flight destroys the agent's context and leaves a half-finished
worktree. The item lands in `blocked`, with `disposition: escalated` and
`reason_kind: item_wall_clock` or `item_spend`, and **does not consume an
attempt** — a policy stopped it; it did not fail.

**A spend ceiling is not a provider cost cap.** `window_cap` and `terminal_cap`
are a provider saying your *account* is out of budget and are in the
never-retry set. This is you saying *one item* has had enough. A budget stop
therefore **does not park the endpoint**, and a test asserts that.

**Unknown cost is not zero cost.** A call whose price nobody knows — an unpriced
model, or session-mode traffic that never went through `ModelClient` (#128) —
is counted as *unpriced*, not as free. While any call is unpriced:

- `spend_usd` on the item is a **lower bound**, and says so;
- `unpriced_calls` is non-zero;
- the spend ceiling is reported as **unenforceable** in the event stream, not
  quietly treated as met — stopping an item on a number nobody can defend would
  be worse than not stopping it;
- the **wall-clock ceiling is still enforced**, because it can always be
  measured.

`doctor` reports the ceilings before anything runs, and warns when there are
none: unlimited is safe on upgrade and unsafe unattended.

### 6e. When an item is waiting on you

```bash
curl -sH "Authorization: Bearer $TOKEN" 'localhost:8099/api/holds' | jq
```

```json
{"open": [{"item_id": "T4",
           "question": "the agent is waiting for input in its terminal session — attach at https://…/t/abc",
           "age_seconds": 412.0,
           "expires_at": 1754000000,
           "session_url": "https://…/t/abc",
           "who_may_answer": "anyone"}]}
```

That is the inbox, oldest first. It is a **state of the work item**, not a
projection over recent events: it survives the worker dying, no other worker can
claim a held item, and the answer can come from anywhere.

**This is how a hang stops looking like a question** (issue #103). An item that
is `claimed`, making no progress and holding no question is a hang. One that is
`held` says what it is waiting for and how long it has waited.

Answer from anywhere — a phone, a second terminal, a script:

```bash
curl -XPOST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"resume_token":"…","text":"use postgres","who":"me"}' \
  'localhost:8099/api/work/T4/answer'
```

The item goes back to `claimed`, with a fresh lease for the worker that asked,
and its worktree and context intact. If that worker really did die, the lease
expires as it always did and another worker continues the attempt from its last
durable stage.

**Four things it deliberately does not do.**

- **Nothing interprets your answer.** It is structured data or a message,
  recorded verbatim. A model reading human feedback to decide what it meant is a
  gate decided by a model, and `AGENTS.md` rejects that.
- **Nothing is typed into the session.** The agent may be at a shell, where an
  answer becomes a command. Delivery is through the protocol.
- **Being held is not approval.** Answering returns the item to where it
  stopped; it does not move it past anything.
- **A hold that times out returns the item to `blocked`, never to `ready`**,
  with the question preserved in the blocked reason. `max_hold_seconds`
  (default six hours) is what stops one unanswered question tying up a worker
  for ever — a hold keeps its claim, so unlike the budgets its default is
  deliberately **not** unlimited.

#### Being told, instead of thinking to look

A hold used to be durable and completely silent (#188): every route to one was
a pull, so an item could sit on a thirty-second question overnight while every
dashboard read healthy.

Opening a hold now emits one notice — into the event stream the run already
writes, and to one URL you name:

```bash
uv run agent-harness --db harness.sqlite serve --hold-webhook https://your-host/holds
# or: HARNESS_HOLD_WEBHOOK=https://your-host/holds
```

```json
{"kind": "work", "outcome": "hold_opened", "ts": 1754000000.0,
 "project_id": "default", "item_id": "T4", "worker": "worker-1",
 "question": "Which database should this use?",
 "reason": "the schema is not decided",
 "who_may_answer": "anyone", "expires_at": 1754021600.0,
 "session_url": "https://…/t/abc",
 "answer_path": "/api/work/T4/answer?project_id=default",
 "detail": "T4 is waiting on a person: Which database should this use?"}
```

Three things about it are deliberate.

- **It is not a notification system.** One URL, one POST, no retries and no
  queue. What is on the other end — a session host that already has push
  notifications, a chat relay you wrote, a log file — is not this service's
  business, and adding a product here would be the coupling `AGENTS.md`
  forbids.
- **A failed delivery is dropped, never raised.** It cannot fail the item,
  stall it, or un-hold it. This is the rule telemetry already follows, for the
  same reason: the fleet must not depend on it.
- **It carries no resume token.** `answer_path` says where the answer goes;
  spending it is an authenticated call to the API, which looks the token up
  itself.

Configure nothing and nothing changes: the inbox is still `GET /api/holds`, and
`GET /api/summary` reports `holds_open` plus a `holds_overdue` entry for any
question still open past its own deadline — a status line that reads healthy
while one of those exists is exactly the failure this closes.

---

## Configuration reference

| Variable | Used by | Purpose |
|---|---|---|
| `HARNESS_TOKEN` | `serve` | Bearer token for the API. Without it every authenticated route refuses. |
| `HARNESS_DB` | all | SQLite path. Default `./harness.sqlite`. |
| `HARNESS_API_KEY` | `run`, `serve` | Key for the model provider. In `serve` it is the reviewer's. |
| `HARNESS_ENDPOINT` | `run`, `serve` | Model API base URL. |
| `HARNESS_HOLD_WEBHOOK` | `run`, `serve` | URL POSTed a JSON notice when an item stops to ask a person something (`--hold-webhook`). Unset means nothing is sent and the pull routes are unchanged. Delivery is best-effort: it can never fail or stall the item. |
| `HARNESS_ROUTE_PRESET` | `run`, `serve` | Default route preset (`--preset`) for roles that name none: the wire protocol, the authentication header, the response reader and a failure classifier, as one name. Default `chat-completions`. |
| `HARNESS_ROUTE_PRESETS` | all | Extra presets to make resolvable, as `name=module:attribute` pairs. For a preset that lives in your own code rather than in an installed distribution's entry points. |
| `HARNESS_CONTEXT_BUDGET` | `run` | How many characters of repository the implementer is shown (`--context-budget`, default 60000). A file bigger than this cannot be supplied at all — see [§6a.1](#6a1-when-the-target-does-not-fit-in-the-prompt). |
| `HARNESS_ROOT_PATH` | `serve` | Prefix when behind a proxy, e.g. `/api/harness`. |
| `AIDEVENV_URL` | `run`, `serve` | Session host, enabling attachable agents. In `serve` it is what makes the deployment supervised rather than monitoring-only. |
| `AIDEVENV_TOKEN` | `run`, `serve` | Session host token. |
| `HARNESS_AUDIT_DB` | `serve` | Audit database. Put it on a different volume so history does not share a fate with the queue. Defaults to `audit.sqlite` beside `--db`. |
| `HARNESS_AUDIT_REQUIRED` | `serve` | `1` refuses to start without a writable audit store. Off by default, because observation failing must not stop work. |
| `HARNESS_AUDIT_RETENTION_DAYS` | `serve` | How long raw events are kept once a rollup covers them. Default 90. |
| `HARNESS_PRICE_TABLE` | `run` | JSON price table, inline or a path. Without it, calls are recorded with tokens and **no** cost, and reported as `unpriced`. |
