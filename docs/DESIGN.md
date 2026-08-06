# Design

How the harness works, and why it is shaped that way.

This is the design document. It describes the system as the code implements it
today — not a roadmap, not a phase order, not a record of how any part got
here. **Where a claim here disagrees with the code, the code is right and this
document is wrong**; that is rule 7 of [`AGENTS.md`](../AGENTS.md) and it
applies to this file first.

What is built, what is half-built, what has never run against a real fleet, and
what is blocked on what: [`docs/STATUS.md`](STATUS.md) owns all of it. Nothing
here is a status claim, and where a design element exists but is not yet
reachable, this says so in one clause and moves on.

---

## 1. What this is, and the one idea

A queue and a delivery pipeline for a fleet of coding agents.

```
PLAN.md ──▶ issues ──▶ claim ──▶ implement ──▶ checks ──▶ review ──▶ PR
                                                     │
                               every stage recorded, append-only
```

You supply the plan, the model provider and the checks. The harness supplies
the queue, the leases, the failure model, the gates and the record of what
happened. It writes no code itself and judges no code itself; it decides *what
runs next*, *whether the result may proceed*, and *what is true about it
afterwards*.

Three properties define it, and every design decision below serves one of them.

**It is generic.** Not tied to a project, a language, a workload or a vendor.
That is an owner ruling, and it is enforced rather than trusted: the
authoritative list of what core may not know lives in `EXECUTION_PATH` in
`tests/test_generic.py`, not in prose. Core holds no log paths, no directory
conventions, no numbers belonging to one workload, no import of anything under
`adapters/` — and **no dotted path naming one**, because a lazy import written
as a string is still core knowing what a particular vendor is called. See §9.

**It is provider-agnostic in the strong sense.** A call site names a *role*,
never a model. What speaks HTTP, what carries the credential, what reads the
answer and what a failure *means* are four separate, replaceable things bundled
under one name. Adding a vendor changes no core module and no core module
imports it. See §4.

**It is honest about what happened.** A gate that passed and a gate that was
never asked are different answers. An unpriced call is not a free call. A
merged pull request that was later reverted produces two facts in order, not
one fact that changed its mind. The audit database has no mutation surface at
all, so a number in it cannot be improved after the fact — including by us.

The unifying idea, from which most of the rest follows:

> **The gates are the product. Everything else is scaffolding around them.**
> A gate is never weakened, made cheaper, made skippable or made optional in
> order to make the queue, the dispatcher, the API or a dashboard easier to
> build. If a change does that, it is the wrong trade.

---

## 2. The execution pipeline

One item, from claim to pull request. Every stage below is a boundary at which
the harness can stop, and each one guarantees something the next stage relies
on.

```
       claim (a LEASE)
            │
     base resolution ──── depends on an item already delivered?
            │             yes → branch from ITS branch, not from main
      sync the checkout
            │
        planner ──────────── names the files, says what it cannot identify
            │
    context selection ────── primary target does not fit? ESCALATE, do not guess
            │
      implementer ────────── direct EDIT BLOCKS, or a selected tool-using loop
            │
     validate the patch ──── before git is touched at all
            │
       cut the branch
            │
        apply ────────────── tolerance ladder; the APPLIED diff is what proceeds
            │
        checks ───────────── the cheap gate, and it runs FIRST
            │
   readiness re-check ────── the same call admission made, at the last cheap point
            │
     CHECKPOINT ──────────── local commit, then push, then a DRAFT pull request
            │
        review ───────────── a different role, ideally a different vendor
            │
    mark ready ───────────── approval takes the draft out of draft. Never merges.
```

### What each stage guarantees

| Stage | Guarantee |
|---|---|
| **Claim** | Exactly one worker owns the item, for a bounded time, and losing the process releases it without anyone doing anything (§3.1). |
| **Base resolution** | An item written against another item's result is branched from that result. Applying such a change to a base missing the function it assumes either fails, or — with fuzzy matching on — succeeds in the wrong place. Only **one** dependency can be stacked on: with several unmerged dependency branches there is no single correct base without merging them, so the first is used and the fact is reported rather than hidden. |
| **Tree sync** | The tree is at the item's base *before* anything reads it, and before the branch is cut. The context selector reads the file at the base while the edit applier reads the working tree; when those disagreed, the second item to touch a file was told its own correctly-quoted text did not occur. The branch is cut late, so an item producing no usable diff leaves no branch behind. |
| **Planner** | Named targets, in importance order, plus an explicit `cannot_identify_target`. That last field is a first-class answer, not an error. |
| **Context selection** | The implementer is never asked to change a file it was not shown. If the primary target alone exceeds the budget the item escalates (`context_unavailable`) rather than proceeding — retrying cannot change the size of a file. |
| **Implementer** | In direct mode, a change expressed as text to find and text to put there (§2.1). With a role runner selected, a bounded loop works in the repository and the harness computes the complete candidate diff against the item base, including new files and local commits. Both paths then enter the same validator and gates. |
| **Patch validation** | A reply that is not a well-formed diff is diagnosed as a *model* failure before git is involved, because once it reaches `git apply: corrupt patch at line 549` it is indistinguishable from a patch written against the wrong base — and the two are fixed in completely different places. |
| **Apply** | Either the change is in the tree, or the branch is destroyed and the patch is kept on disk for whoever diagnoses it. |
| **Checks** | The project's own commands answered on the *applied* tree, with five distinct outcomes (§6.4) — not a boolean. |
| **Readiness re-check** | The item is still eligible *now*, at the last point before money and durability are spent. |
| **Checkpoint** | Work that passed every cheap gate survives the worker dying during the expensive one — and does not present itself as reviewed while it waits (§3.5). |
| **Review** | A verdict from a role that did not write the change, failing closed in two independent ways (§3.6). |
| **Mark ready** | Approval takes a draft out of draft. **Nothing is ever merged and nothing is ever committed to the default branch.** A wrong answer stays reviewable — a rejected item keeps its branch and its draft, with the verdict recorded on it, because a rejection somebody has to read is worth more than a tidy repository. |

### 2.1 The change protocol: edit blocks, not hunk headers

The implementer is asked for edit blocks and told, in as many words, not to
write a unified diff:

```
path/to/file.ext
<<<<<<< SEARCH
the exact existing text to find
=======
the exact text to put in its place
>>>>>>> REPLACE
```

A unified diff asks a model to do arithmetic it cannot check. `@@ -401,7
+401,12 @@` declares how many lines the hunk consumes and produces, and the
model has to get both right, blind, from having read the file once in a prompt.
Measured against a real repository on 2026-08-05, two items in one run, four
model calls all returning HTTP 200 against a healthy gateway, **zero delivered**
— `hunk ends 0 source and 7 result line(s) short of what its header declares`
and `the last hunk supplies 1 fewer source line`. Neither model misunderstood
its item. Both miscounted, and the arithmetic gets harder as a file grows.

An edit block has no line numbers, so there is nothing to miscount. The text
either occurs in the file or it does not, and that is a question the harness can
answer *before* changing anything. Three rules make the answer definite:

- a match must begin and end on **whole-line boundaries**, so an edit naming
  `foo` cannot rewrite the middle of `foobar` into something that still
  compiles and means something else;
- text occurring **more than once is not a location** — ambiguity is refused,
  never resolved by taking the first hit;
- the one tolerated fuzziness is a **uniform** indentation shift, applied only
  when the match is still unique, with the file's indentation winning.

`edits.py` then computes the unified diff from file content the harness has
read. That is the point of doing it here: **every gate downstream is
untouched** — the validator, the apply ladder, the checks, the reviewer and the
commit all still see a diff, and none of them learns a second way for changes to
arrive.

A unified diff is still *read*. Models that ignore the instruction, an
implementer route with its own habits, and every durable attempt recorded before
this change all still work; refusing them would turn a format preference into an
outage. **Whole-file replacement is rejected** and exists only in the
experiment that measured it — its result was a change placed in the wrong file,
which is the finding that boundary exists to keep out of core.

`edits.py` also owns the *file-write* boundary: a path resolving outside the
working tree is refused. That is deliberately not the command guard's job, and
neither is the other's fallback — a write that never goes through a command is
invisible to the guard, and a command that never writes a file is invisible
here.

### 2.2 The apply ladder

A patch that parses can still be refused by git for reasons that are worth
telling apart, so `apply_diff` walks rungs and reports which one worked:

1. `git apply`
2. `git apply --unidiff-zero` — understated hunk headers are the single most
   common model error, and this is what forgives them
3. `patch -p1 --fuzz=3` — **off by default**, opt-in per deployment

each tried against the diff as written and, if they differ, against a recounted
form. One refusal precedes the ladder entirely: a hunk header claiming
`@@ -0,0` against a file that exists with content is rejected outright, because
`--unidiff-zero` would "succeed" by inserting the whole thing at line 1.

The ladder is why the *applied* diff — re-read after the apply and again if a
declared fix touched the tree — is what the reviewer sees. The reader uses a
temporary Git index so untracked files are included without changing the real
index; a plain `git diff HEAD` would silently omit a newly created module.
Reviewing the model's text instead would reject good work for an artefact of
the plumbing and, worse, make the gate structurally unable to catch a diff that
claims more than it did.

### 2.3 Three ways to run an item

The pipeline above is the direct-API executor (`executor.py`). Two others share
its gates:

- **Hosted session** (`session_executor.py`) runs the item as a CLI agent in a
  terminal a person can attach to, in a real `git worktree` of its own — this
  is the executor the phrase "a worktree per item" describes; the direct
  executor branches inside one checkout, and running two of it against one
  checkout would be the data race that phrase warns about. It has
  no planner, no context assembly and no resumable attempts; completion is the
  process **exit code, never idleness**, because an agent that has printed its
  answer and one waiting for approval look identical from outside. A timeout
  does not kill the session — it holds the agent's context, which is the only
  thing that makes the item resumable by a human — so the session is recorded
  as abandoned and reaped later. From the checks onward it is the same
  sequence, and where the two executors once disagreed about a gate, they no
  longer do.

- **A selected role runner** replaces the direct implementer call without
  replacing the executor. Core defines `RoleRunner` and `RoleRunRequest` in
  `role_runners.py`, resolves the configured name through installed metadata,
  and knows no adapter module path. The shipped agent-loop adapter
  (`adapters/minisweagent.py`) supplies what the other two paths lack: turns.
  Its `Model` routes every call through `ModelClient` — so
  fallback chains, the retry ladder, per-endpoint parking, classification,
  pricing and the recorded answer all still apply, and the loop never learns
  what a provider is — and its `Environment` puts every command the agent runs
  through the same `CommandGuard` that screens check commands. A call is billed
  before its body is parsed, because a reply nobody could parse was still paid
  for and a ceiling that counts only the calls that went well is not a ceiling.
  `run --role-runner NAME` selects it before any item is claimed; the choice is
  stored for doctor and preflight. The loop may run declared checks for
  feedback, but its complete candidate tree is converted to a diff and enters
  the existing `_from_diff` path, where the harness runs those checks again as
  the authoritative gate, checkpoints, reviews and records the attempt.

  Whole-item wall-clock and spend bounds are translated to their remaining
  values before the loop starts, while the step ceiling remains an independent
  emergency control. Usage is folded into the item on every successful reply,
  including a reply whose body cannot be parsed. Once any call is unpriced, a
  dollar total is only a lower bound, so the dollar ceiling becomes
  unenforceable rather than stopping the item on a known subtotal; step and
  wall-clock bounds still hold.

Which files inside the repository an agent touches is deliberately *not*
constrained. An agent using the whole repository to reach an outcome is how
work gets done; "it changed something the item did not ask for" is a question
for the reviewer. The guard bounds what is **dangerous**, not what is untidy.

**The remaining direction.** Issue #195 reframes the single-shot model call as
the defect rather than any one prompt or format. The implementer now has a
selectable bounded loop; planner, reviewer, surveyor, assessor and scoper still
use one call over context assembled for them. A gate converted to a loop needs
a read-only environment rather than the writable implementation environment.

---

## 3. The invariants

Each of these is a rule, and each rule is a measured failure wearing a
different hat. They are the part of this document worth arguing with; the rest
is arrangement.

### 3.1 A claim is a LEASE, not a lock

A worker claims an item until `now + 900s` and extends that with a heartbeat
while it works. It does not hold a lock.

A lock held by a dead process is a lock nobody can release, and the usual
workaround — a human clearing stale state — is *exactly* the unattended failure
the queue exists to prevent. A worker killed mid-item releases its work by doing
nothing at all.

The contract has two halves and for a while only one was enforced:

- **`heartbeat` is owner-guarded.** A refused heartbeat means someone else owns
  the item now. The worker raises `ClaimLost`, stops, and **releases nothing** —
  reporting anything at that point would overwrite a live claim. The durable
  attempt buffer is discarded for the same reason: it belongs to an attempt
  somebody else now owns.
- **`release` is owner-guarded too.** It was not, and a stalled worker could
  surface late and mark an item done from work the new owner never did, leaving
  the new owner running with nothing to release.

Omitting the owner on release is an *administrative* override, and stays
possible on purpose: an operator retrying a wedged row through the API has no
worker identity, and guarding that would remove the one lever a human has.

Two independent detectors exist because they answer different questions: a
background thread beating at a third of the lease notices a loss *during* a
stage, and a synchronous check at the next boundary is what turns that into a
stop. Both are read. The heartbeat returning a value nobody read is how a worker
carried on after losing its claim and then reported a result for someone else's
item — measured once as fifteen minutes of a 915-second agent run. A database
error is deliberately *not* treated as a lost claim; only a heartbeat the queue
actually refused is.

The claim itself is one `BEGIN IMMEDIATE` transaction covering both the
selection and the update, so two workers racing cannot both win — the loser's
transaction sees the row already claimed. Two things happen deliberately
*before* that transaction opens: the project's control state is read (only
`running` claims at all), and expired holds are swept, because a claim scan is
the moment the queue's view of what is available has to be true.

Ordering is `attempts, item_id`, so an item that has failed twice sinks below
one never tried and a poison item cannot monopolise a worker; it gets a turn, at
the back. Candidates are read a page at a time rather than all at once, because
loading the whole eligible backlog inside the write transaction held the write
lock longer the larger the backlog got. **The page is keyset-paged on
`(attempts, item_id)`, and the scan walks pages until it finds something or runs
out** — with a single bounded query, "the queue has ready work" and "the first
page has ready work" become the same question, and the answer is a permanently
stalled fleet with a full queue.

Ineligible candidates are handled inside the same scan: an item at or over
`max_attempts` is retired to `exhausted` in place and the walk continues, and an
item whose required dependencies do not resolve is skipped (§5). `max_attempts`
is re-read on every claim rather than cached, so raising it rescues exhausted
items without a restart. Setting it to zero disables the ceiling.

A re-claim that finds a **resumable durable position keeps its attempt number**
(D11): a crash is not a failure of the work. The consequence is named rather
than hidden — an item that crash-loops is then bounded by the wall-clock and
spend budgets rather than by the attempt counter.

### 3.2 Checks run BEFORE the reviewer

Paying a model to tell you the build is broken is paying the dearest gate to
catch what the cheapest one already caught.

So the project's own check commands run on the applied tree first, and a
non-passing result ends the attempt without a reviewer call. The implementer is
*told* what those commands are — a diff refused by a formatter the model was
never shown costs an attempt and a model call to discover something the harness
knew before it asked. Naming the commands is not weakening the gate: the gate
still runs, and still refuses.

A check may also declare a **mechanical fix**. Running it is off by default and,
where an operator turns it on, it is fenced so that it cannot become a way for
the gate to pass itself: one fix per check, never in a loop, a structural change
to the tree escalates rather than proceeding, the fix argv is screened like any
other, **and the re-run is the verdict** — a fix that does not clear the gate is
still a failure. The reviewer is told in as many words that the harness itself
modified the tree and which lines are not the agent's work, because the
alternative is a gate judging changes whose author it has been misled about.

### 3.3 Never retry a spend cap

`429` is at least four different facts: *slow down* (retry in a moment), *your
5-hour budget is gone* (hours), *your weekly budget is gone* (days), and *we
refuse this* (never). Vendors bury the difference in a body field.

Retrying a spent cap is a busy-wait that burns quota checking whether quota
exists. So the harness classifies first, and `window_cap` and `terminal_cap`
park the endpoint rather than retrying it — ever. When every route in a role's
chain has answered with a cap, the item is released back to `pending`, not
`failed`: nothing was wrong with the work, the account ran out of money, and
the worker sleeps a poll and asks again rather than exiting.

The mirror of this rule matters just as much. A **deployment's own** ceiling on
one item is a different fact, it never parks an endpoint, and it never enters
the never-retry set — see §6.5.

### 3.4 No global state in the retry path

One worker's rejection must never pause another worker.

A *fleet-wide* cooldown in response to one worker's 429 does not merely stall
the fleet — it **phase-locks** it. Every worker wakes together, bursts together
and is limited together, which is precisely the shape a rate limiter exists to
reject.

So every reaction is scoped to the worker and the endpoint that saw it, and the
backoff is jittered so that workers which *did* collide do not re-collide. The
cap bounds the curve, not the result: capping after jittering re-synchronises
exactly the workers the jitter was spreading out.

Parks are keyed by `(endpoint, role)` rather than by endpoint alone, and the
reviewer and planner roles are ringfenced from endpoint-wide parks — an
implementer exhausting a gateway must not take the gate that judges its work
down with it.

### 3.5 Checkpoint before the expensive gate

Review is the slowest and most failure-prone call in the pipeline. Work that
has passed every cheap gate must be durable before it runs.

So the checkpoint is a real local commit — then a push, then a **draft** pull
request — all taken *before* the reviewer is asked, and each recorded as a
durable attempt artefact. The commit's own trailer says what it is:

```
Reviewed: not yet — this is a checkpoint taken after the cheap gates passed
and before review.
```

Both halves are load-bearing. Durable, so a worker dying during review does not
lose work that had already passed everything cheap. **And plainly unreviewed,
so nothing presents itself as reviewed until approval exists.** A checkpoint
that looked like a finished pull request would be this invariant paying for
durability with the reviewer's credibility.

The two effects that are not idempotent — the push and the draft PR — are
bracketed by intent records, so a crash in that window is discovered as
"began and did not confirm" rather than as a push that may or may not have
landed, found later by someone reading git.

### 3.6 The reviewer is independent, and fails closed twice

The reviewer is a separate role, and a call site can never name a model, so
pointing it at a different vendor is a data change.

It fails closed in two independent ways:

- **No reviewer configured returns `REJECTED — nothing reviewed this`.** Absence
  is not approval.
- **Any verdict not starting with `APPROVED` is a rejection**, so a truncated
  or malformed reply rejects.

The asymmetry is deliberate. Approving work that does not do what was asked
reaches a pull request carrying the word "reviewed" and costs someone much
later; an unnecessary rejection costs one retry.

Cynicism is constructed rather than asserted. The prompt opens by telling the
reviewer to assume the change is wrong, and requires two lists before the
verdict means anything: **what I verified** (naming nothing is itself a
rejection) and **what I could not verify**. The second list is the useful one —
most changes that fail review fail because they did something *adjacent* to what
was asked, or claimed more than they did, not because they were obviously
broken. The reviewer is given the touched files in full at their post-change
state, not only the diff, so "the diff does not show it" stops being an
available answer about a file it can read.

A verdict may also carry **follow-ups**, and those become proposed work items
for a person to accept or discard. This exists because the alternative is
worse: refusing work that did what it was asked, because the reviewer would
additionally have done something else, discards the work *and* the observation —
the item goes back to be rewritten identically and nothing records what was
noticed.

Independence is **reported, not enforced**. `reviewer_independence()` compares
the reviewer's route to the implementer that actually runs and says when they
are the same model, or merely the same vendor:

```
WARNING: reviewer and implementer are the same model (m):
         every review is a model grading its own work
```

Running a single model is a legitimate deliberate choice and blocking it would
be the harness overruling an operator about their own budget. What it must not
be is a surprise. In session mode the implementer is an agent process rather
than a routed model, and the report says so rather than comparing two routes
that never meet.

### 3.7 A refusal by policy is terminal

`guard.py` screens the argv the harness itself executes — a plan's `verify:`
line, the agent command, a project's check commands — all of which are read from
documents and configuration a model may have written. Two separate rules: a
deployment-configured refusal list matched against argv, and a path boundary
that refuses any argument naming a path outside the directory the harness chose.
The boundary is what makes `~/.ssh`, `/etc` and `rm -rf /` unreachable without
the guard having to enumerate them.

The working directory *is* the boundary, on purpose: one configured separately
from the directory the command runs in is one more thing that can be set wrong.
Matching is argv-aware rather than textual, so neither an absolute path nor a
reordered flag is an evasion. The built-in default list is deliberately tiny and
names nothing belonging to any workload — privilege escalation, host lifecycle,
force-push — because "what must never run here" is a property of a deployment
and not of a framework. A guard nobody configured is reported by `doctor` as
*not configured*, which is not the same as a pass.

It is **screening, not instruction**. Telling the implementer not to reach for
`sudo` asks the least reliable component in the system to enforce the
constraint; the refusal holds whether the model is right, wrong or adversarial.

In the executors a refusal is **terminal** (owner decision, 2026-08-05): the
item stops with the disposition `blocked_by_policy` and the rule that fired, and
is not handed back to the agent as a correction. Terminal is the safest of the
two answers — a guard that answers can be probed — the cheapest, and it cannot
loop. **The cost is real and is not hidden**: an agent that reaches for a
forbidden command when a permitted equivalent existed loses the whole item and
needs a person, and `doctor` reports the policy so whoever pays that can see
what it is.

Inside the agent loop the same refusal is returned to the agent as observation
text rather than raised. That is the opposite answer, and the reason is that the
loop's turn budget already bounds it: nothing is silently permitted, the refusal
is recorded, and a loop that keeps trying refused commands runs out of steps.
The message names the tree and says the rule is not transient, because the
measurement behind it was 40 model calls of which 15 turns were spent being
refused by a message that never said where the boundary was.

An agent's shell line is not one command, so the loop's environment splits it
into the simple commands a shell would actually run — quote-, substitution-,
wrapper- and heredoc-aware — and screens each. Its `execute` is one method, and
one method is the entire attack surface.

**It is not a sandbox**, and nothing here should be described as one. The
current direct runner still needs the OS-enforced item boundary specified in
`STATUS.md` Stage 2; an optional session host may provide its own isolation,
but that does not satisfy the harness-owned execution target. The guard cannot
see inside an inline program — `sh -c '…'` and
`python -c '…'` are one argv token — so a deployment that runs a shell as a
check has an unscreened shell. Pattern matching bounds the obvious reaches, not
the clever ones: it converts a class of catastrophic outcomes into a legible
refusal, not into an impossibility.

### 3.8 Unknown is never zero, and history is never rewritten

Zero tokens claims a call was free. A call whose price is unknown is recorded as
**unpriced** and counted separately, never folded into a total.

Every model call records the tokens **and the price applied to them**, because
applying today's rates to last year's usage is a projection rather than history
and it silently rewrites the past on every vendor repricing. A price change then
shows as a step in the series instead of an invisible retroactive edit.

The same rule at the top: rate limits from logs written before classification
existed are `unclassified` and never folded into `rpm` / `window_cap` /
`terminal_cap`. The baseline is a total, the successor is a breakdown, and the
panel says so. Removing that caveat to make a page tidier would present a delta
that does not exist.

---

## 4. Model routing

### 4.1 Roles, not models

A call site names a role. Roles are open strings, not an enum — `planner`,
`implementer` and `reviewer` in the pipeline; `scoper`, `surveyor` and
`assessor` elsewhere — and a role resolves to a route through a map stored in
the queue database.

Storing the map is what makes re-routing a data change rather than a code
change: `PUT /api/roles` moves the reviewer to a different vendor mid-run with
no restart. It lives in the database rather than in memory because the API
process and the worker process are different processes, and an in-memory value
could never be changed from outside the loop using it.

**The stored map wins over the command line** by default, per role. That is the
point of storing it, but silently ignoring flags someone just typed is its own
kind of lie, so a run names which roles the command line lost. `--reroute`
inverts the precedence.

**The transport is injected, not imported.** The retry logic is therefore
testable without a network, and a deployment keeps whatever HTTP client it
already has. The harness owns the policy; the caller owns the socket.

### 4.2 What a route is made of

A `Provider` classified failures. The CLI's transport separately assumed one
gateway's completion path, one authentication header and one response envelope.
Neither knew about the other, which had two consequences: "add a vendor" meant
editing the transport function, and changing the classifier changed nothing
about what went on the wire.

They are four separate parts now, and a **preset** is one name for all four:

| Part | What it decides | Core default |
|---|---|---|
| request adapter | URL, method, payload keys | `JsonChatRequest()` — POST the endpoint exactly as configured |
| authentication strategy | which header carries the credential | `BearerAuth()`, omitted entirely when there is no key |
| response reader | where the text and the token counts are | `JsonResponseReader()` — conservative; no text path, so it declines rather than guessing |
| failure classifier | what a rejection means for control flow | `GENERIC` — HTTP only |
| model, endpoint | on the route itself | — |
| price reference | what the price table calls this model | the model id |

The request adapter forwards any option it was not configured to consume
straight into the payload. That is why the agent loop can send `tools=` and
`tool_choice=` without a protocol change — and equally why nothing in
`model_client.py` or `protocols.py` knows what a tool call is.

The core preset is `generic` and makes no vendor-specific claim. Every other one
is an adapter or a plugin, and `protocols.py` never imports one — it resolves
them **by name**, loading only the name asked for (§9).

**Which classifier reads a failure is a property of the route.** A gateway that
states its reasons in a body brings the reader for them along with the wire
shape it speaks. The generic preset can only see HTTP, which means it calls a
spend cap `rpm`; that is a documented limit rather than a bug, and
`tests/test_route_conformance.py` asserts it as one.

**Host detection is a suggestion and nothing else.** `protocols.suggest()` will
look at a hostname and say which preset is probably meant, and the CLI prints
it. It never chooses one. Hosts are proxied, renamed, self-hosted and shared; a
protocol nobody configured is a request sent to a URL nobody chose, and the
first symptom would be a failure the classifier cannot explain.

**An unknown preset name is loud, not silently substituted.** A live-edited role
map naming one that does not resolve logs a warning and falls back to the
deployment default rather than taking down the readiness report that would show
the operator what they typed. The CLI is stricter: it resolves its default
preset before anything claims work, and refuses with the list of declared names
rather than discovering the problem on the first call.

### 4.3 Fallback chains

A role maps to a *chain* of routes, not one route. The whole chain is walked
before any backoff: a parked or capped route is skipped when there is somewhere
else to go, and a connection error moves to the next route rather than sleeping.
The preferred route is `chain[0]`, and everything that *reports* — readiness,
reviewer independence, pricing keys — speaks about it, with a non-preferred
answer recorded as such.

`CapExhausted` is raised only when a full pass finds every route capped and none
merely rate-limited. That is the difference between "this vendor is out of
budget" and "we are out of budget".

### 4.4 The retry ladder, and per-endpoint parking

```
attempt ──▶ 2xx ────────────▶ record usage + the price applied
   │
   └─ error ──▶ classify(status, headers, body)
                   │
                   ├── rpm / transient ──▶ backoff, jittered, THIS worker ──▶ retry
                   ├── window_cap ───────▶ park this (endpoint, role). Never retry.
                   ├── terminal_cap ─────▶ park it for much longer. Never retry.
                   └── non_retryable ────▶ RequestRefused. Stop.
```

`retryAfter` is read from the **body as well as the header**, taking whichever
is larger — some vendors put it only in the envelope, and believing the smaller
of the two is how you get limited again immediately. It raises the floor of the
backoff rather than replacing the curve.

Parks are a per-process, per-`(endpoint, role)` map, extended monotonically and
never shortened. Where a chain offers an alternative the parked route is
skipped; where it is the only route, the worker sleeps out the park rather than
hammering it. Sibling clients created from one parent share the park map, so
what one learned about an endpoint is not re-learned at cost by the next.

### 4.5 Failure classification

Six kinds, and the distinctions exist because each demands a different control
flow: `rpm`, `window_cap`, `terminal_cap`, `non_retryable`, `transient`,
`fatal`. Two are never retried under any circumstance and are what §3.3 is
about.

The generic classifier sees status and headers only — `429` is `rpm`, `401`/
`403` is a rejected credential, `5xx` is transient, everything else is fatal.
It **cannot** distinguish a burst limit from a spent window, because nothing in
HTTP can. A vendor classifier reads the body: which field names the error,
which categories mean quota, which mean authentication, and which marks
distinguish a short window from a terminal cap. Two traps are documented in the
code because both were paid for: the retry delay lives in the body, and "not
retryable" is not the same fact as "out of budget".

`outcomes.py` answers a different question and is deliberately not this module:
`providers.py` says what a *provider* answered, `outcomes.py` says what a *gate*
answered and what stopped an item.

---

## 5. The dependency graph

`depends_on` is a list of strings on the wire, and each string is a **token**
with a grammar. The token says what kind of thing is being waited for, and the
kind is what makes the answer decidable:

| Token | Target kind | Resolved against |
|---|---|---|
| `W1` | `local_work` | a work row in this project |
| `external:RESOLVER:ID` | `external_reference` | a stored outcome from `RESOLVER` |
| `decision:D9` | `human_decision` | a decision recorded as work here |
| `project:OTHER/W1` | `cross_project_work` | a work row in `OTHER` |
| `?W1` | any of the above, **advisory** | reported, never gating |

Each edge carries its source, its target kind and identity, required-versus-
advisory, its resolver when external, a resolution state (`unresolved`,
`blocked`, `satisfied`), the evidence for that state, its provenance, and the
graph revision it was written at. **`unresolved` is never a synonym for
satisfied**, and the three states are validated rather than assumed.

**Parsing a token never raises.** A malformed token — `external:` without a
resolver, `project:` without a slash, a decision naming nothing — becomes an
edge that resolves to `unresolved` carrying the parse message as its evidence.
That is a blocker which explains itself, rather than an exception thrown from
inside the transaction that hands out work.

An unsatisfied **advisory** edge is reported separately and never enters the
blocking reasons — but it is reported, because an advisory edge that silently
vanished would be indistinguishable from one that was never declared.

**A required target the graph cannot resolve is a blocker, not an assumption.**
This reverses an earlier rule and the reversal is the whole point. A dependency
absent from the queue used to be treated as satisfied, on the reasoning that
plans reference work tracked elsewhere — which is true, and which made a typo, an
omitted item and a genuine external reference indistinguishable. All three ran
immediately. An external reference is still perfectly legitimate; it just has to
say so and have an answer from a resolver.

**Dependencies resolve only inside a project** unless the token says otherwise.
An id means one thing here and another there, so crossing the boundary has to be
spelled out.

**Cycles are named as cycles, and a concrete path is printed.** Two items each
requiring the other were reported as "waiting", forever, one item at a time,
with nothing saying the wait could never end. A set says *these are tangled*; a
path says *how*. Only required local edges can close a cycle — an advisory loop
is not a deadlock, and a target in another project or outside the harness cannot
close a loop inside this one. The cycle set is computed once per claim scan, not
once per candidate.

**An item with dependencies and no edges is a blocker, not a free pass.** That
is the in-place-upgrade case: "no edges" must never read as "no dependencies",
so it blocks with the tokens named and the rebuild command to run.

**Local targets are derived on every read; external ones are stored.** A local
edge's state comes straight from the work row, so it cannot go stale and there
is no second copy to disagree with the queue. An external outcome is evidence
obtained by I/O and has to be kept — but it is obtained outside the claim
transaction, because I/O inside the write transaction that hands out work is how
one slow ticket system stalls a fleet.

### One revision, two checks

The graph revision moves when the declared graph changes or a resolver reports
something new — **not** when work merely finishes, which is work state.
Re-ingesting an unchanged plan therefore leaves the revision where it was, which
is what stops a routine re-sync invalidating live claims.

`claim` records the revision it admitted at on the work row. Both executors
re-run **the same `readiness()` call** at the last cheap point before review
spends money and the checkpoint makes anything durable. Two implementations of
"is it ready" would be two answers, and the one that disagreed would be the one
that let ineligible work commit.

When that second check fails the agent is **not killed** — it has already
reached a safe boundary. The branch is abandoned, the durable resume position is
thrown away (the plan the attempt was briefed with is no longer the plan, so its
diff answers a question nobody is asking), the item returns to `pending` without
consuming an attempt, and a `dependency_invalidated` event records both
revisions. An operator override is scoped to the revision it was granted at, so
the next correction re-blocks the item.

The edge table is derived from `depends_on`, so it can be dropped and rebuilt;
that is the whole of [`MIGRATION-graph.md`](MIGRATION-graph.md).

---

## 6. State and durability

### 6.1 Two databases, two fates

| | `harness.sqlite` | `audit.sqlite` |
|---|---|---|
| Contents | queue, claims, control, projects, settings, attempts, holds, **and the live event view** | events with cost, rollups, baselines |
| Mutability | mutable, migrated in place | **append-only; no UPDATE, no DELETE** |
| If lost | re-sync the plan and carry on | irreplaceable |
| Migrations | rewrites tables in place | additive columns only, forward-only |
| Safe to delete | yes, deliberately | never |

The queue is a reasonable thing to delete and rebuild from the plan. **Anything
sharing that file shares that fate**, so history does not. The test of whether
this works is a single property: *deleting `harness.sqlite` must not change one
audited answer.*

There are therefore **two event tables, on purpose**. `store.py` keeps the live
one, in the queue's own file, indexed one index per panel it feeds — it is a
spine for the current view and it shares the queue's fate. `audit.py` keeps the
durable one in its own database, with the columns the live table has no business
carrying: project, item and attempt, tokens, cost, and the price table applied.
A schema mismatch in the live store raises; the audit store instead degrades to
a no-op, because observation must never be the thing that wedges the fleet —
and that is exactly why `/api/audit/health` exists.

The consequence of degrading rather than raising is that audit writes are
dropped silently, so **nothing else will tell you that history is not being
kept**; a fleet running unaudited looks exactly like one running audited. Check
the health route deliberately.

The store is read-only in both directions: it never writes to the harness's own
logs, and nothing but the ingester writes to `events`. Append-only is enforced
rather than intended, and the two stores are enforced differently on purpose —
`store.py` by a test that greps its own source for the statements that would
break it, `audit.py` by a test that asserts no public method is named anything
like a mutation, with `thin` present by name so that the test stays honest. A
change that needs to mutate an event has to delete one of those tests first,
which is the point.

An event's identity is **derived from its content**, not from its position in a
file. Replay, backfill and log rotation therefore collapse onto the same rows
without the ingester tracking offsets, and a repeated identity is a duplicate,
never an amendment — taking the newer one would make history depend on write
order. Baselines are immutable for the same reason: re-recording under an
existing id is refused rather than overwriting.

### 6.2 Roll up, then thin — never thin alone

Raw events are retained for a bounded window; daily rollups are written once,
immutable thereafter, and kept forever. **Raw rows are only ever removed after
the rollup covering them exists.** That ordering is the whole discipline;
thinning first is silent data loss that leaves a tidy-looking database and a hole
in the series.

Two related rules. **Never rewrite history to fit a new schema** — add columns,
leave old rows alone, let readers tolerate absence. A backfill is
indistinguishable from falsification afterwards, and once done once, no number
in the series can be defended. And **store pointers, not payloads**: prompts,
briefs and diffs are large and contain the workload's content, so what is kept
is a hash, a size and a location. The audit layer answers *how much, how often,
how well*, not *what was in it*.

Events are stamped with their own time rather than the clock that read them. A
revert event uses the revert commit's timestamp and a closed event uses the
platform's `closedAt`, because `time.time()` changes an event's content-derived
identity on every pass and every reconciliation re-records the same revert —
wrong, and unbounded. That bug shipped and was caught by an idempotence test
that failed one run in six, because two `time.time()` calls in one fast run can
return the same float.

### 6.3 Redaction at the write boundary

The event store is append-only and the audit store has no mutation surface,
which is exactly *why* redaction runs before the first write: a credential
written into either cannot be deleted afterwards, only rotated. So the filter
sits at `store.append` and `audit.append` — **the only two ways into an events
table** — rather than at the read edge where the plaintext is already on disk.

That claim is structural, not a convention. One test greps every module for an
insert into the events table and asserts the set of writers is exactly those two
files; another walks the AST and fails any function that writes to that table
without redacting first; a third asserts each store defaults to redacting when
nobody passes a redactor, because a caller added later cannot then forget it.
The one other durable path into the audit database — adopting history out of the
queue's file — goes through the same filter and is named in the test as the
exception it is.

Two sources of knowledge, and the first is worth far more than the second:
values this deployment knows it holds (exact replacement, no guessing), and
credential *shapes* that are secrets wherever they appear (a bearer header, an
assignment to something named like a key). The replacement is visibly marked,
because a reader must be able to tell "a secret was removed here" from "the
model said nothing", and the event carries a flag saying redaction happened at
all. If redaction itself raises, the event is still written with its payload
dropped — an event that never lands is indistinguishable from a call that never
happened, which is a worse record than a lossy one.

What is *not* rewritten is as deliberate as what is: the fixed-vocabulary
columns — the kind, the outcome, the error class, the role — are left alone,
because rewriting them would put this module in a position to change a measured
number.

**This is a reduction in exposure, never a guarantee**, and nothing should
describe it as one. It cannot catch a credential whose shape it does not know
and whose value it was not given. Nothing in it names a vendor: a pattern keyed
to one provider's key prefix would be core knowing what a particular vendor is
called.

### 6.4 Vocabularies: what a gate answered, and what stopped an item

A boolean cannot carry the difference between work that is wrong and a gate that
could not run, and those want opposite responses from a person.

**Five check outcomes**, and exactly one of them is a pass:

| Outcome | Meaning |
|---|---|
| `pass` | the gate is satisfied |
| `retry` | the gate got no answer, for a reason nothing to do with the item |
| `fail` | the gate ran and the item's work is wrong — the only outcome that is the item's fault |
| `fix_available` | as `fail`, and a mechanical fix is derivable |
| `escalate` | the gate could not run, and no retry and no diff will clear it |

The satisfied set is spelled out as a set of one, so that adding a sixth outcome
forces a decision about which side of the line it falls on rather than
defaulting to "not a pass".

**Six dispositions** on the work row, answering *why* it is in the state it is
in: `completed`, `refused` (a gate said no about this work), `crashed` (the
harness broke; look at the harness, not the diff), `withheld` (never attempted,
or discarded through no fault of the item — the item goes back to the queue),
`escalated` (a person must resolve something), and `blocked_by_policy` (§3.7).
Empty means nobody has finished with it yet, which is not a seventh disposition.
Alongside them sit nineteen `reason_kind` tokens, so an API consumer branches on
a token rather than matching on English.

Two derived sets carry the operational meaning. `DECIDED` is everything except
`withheld` — an attempt that reached a decision is not resumable. `NEEDS_A_PERSON`
is `escalated` and `blocked_by_policy` only; `refused` is deliberately absent,
because a rejected diff is the system working.

Three distinctions in that vocabulary are load-bearing:

- `failed` versus `exhausted` — one attempt did not work, versus the harness
  will not try again without a human. Without the second, an item that reliably
  kills its worker is re-claimed forever, spending real money each cycle and
  looking identical to an item that is merely busy.
- `refused` versus `crashed` — a verdict about the work, versus nothing having
  been decided at all.
- `blocked_by_policy` versus both — the work was never judged and nothing broke;
  the harness declined, on purpose.

### 6.5 Attempts, holds and budgets

**Attempts are resumable.** Six fixed stages — `planned`, `implemented`,
`applied`, `checked`, `checkpointed`, `reviewed` — one durable row each, so a
killed worker resumes rather than re-paying for the planner and the implementer.
This is a fixed list, **not a workflow engine**: nothing here composes stages or
takes a graph of them.

A resumed attempt **continues** the existing one (D11), so `max_attempts` bounds
genuine failures rather than crashes. Recording a stage and being able to resume
*at* it are different things, and the difference is durability:

- `applied` and `checked` resume at **`implemented`**, because an uncommitted
  working tree is not durable. The stored diff is re-applied to a freshly cut
  branch, which costs no model call.
- A resumed **checkpoint** is a `git checkout` of the branch, because a commit
  is a stronger artefact than any diff; re-applying would conflict with itself.
- A resumed **verdict** is reused rather than re-asked. A model is not
  deterministic, so re-asking would make a crash a way to shop for a verdict.
- The **checks** are recorded and never skipped: re-running them is idempotent
  and costs no model call, so a resumed attempt runs them again rather than
  trusting a result from a tree that may have been rebuilt.

An attempt is **sealed** once a decision was reached, which is what makes it
non-resumable; a killed worker never decided, so its position survives. A
deliberate `retry` is not a resume — it forgets the position and resets the
counter, because a "retry" that returned an item straight back to `exhausted`
while reporting success is the failure that rule comes from.

Three durability modes exist because their costs differ: buffer in memory and
lose it on a crash, write at each stage boundary (the default), or additionally
record the *intent* to perform an external effect before it happens. Only the
third closes the window in which a push may have half-landed; the other two do
not close it, and saying so is the honest description of what they buy.

If the item's brief or dependencies moved while an attempt was in flight, its
durable position is discarded loudly and the attempt restarts from the planner
— a worker briefed from one revision must not be judged against another.

**A hold is a state of the item.** It is durable, it survives worker death, and
it is answerable from any process — not a projection over events, and not the
coordination plane. Per D12, **a hold suspends the lease and keeps the claim**:
the lease is zeroed but the owner stays on the row, so answering hands the item
back to the worker that asked with its context intact, and `claim` never selects
a held row, so no lease expiry can take it while a person is thinking.

Four rules bound it. **No model interprets the answer** into a routing decision;
the answer is recorded verbatim and is never a prompt. **No text is injected
into a live terminal** — the process may be at a shell, an approval prompt or
inside another program, and an answer becomes a command. **Being held is not
approval**: a hold that expires returns the item to `blocked`, never to ready.
And every hold has a maximum duration, because the named cost of D12 is that a
worker slot is tied up for the whole of it — which is why holding on planner
ambiguity is opt-in, and why an unwatched fleet would rather fail an item in
seconds.

**Budgets are the deployment's ceiling on one item** — wall-clock and spend —
and default to unlimited, so enabling them changes no behaviour until someone
sets one. They are checked at boundaries that already exist rather than from a
timer, because a budget stop must never kill work mid-stage: doing so destroys
the context and leaves a half-finished worktree. Spend accrues as it happens
rather than being reconstructed afterwards, so an item that blows its ceiling on
the implementer never reaches the reviewer, and the total is carried across
attempts because the ceiling bounds the item rather than one try at it.

A call that reports no usage is counted as **unpriced, not free**, and one
unpriced call makes the spend total unmeasurable. A declared spend ceiling over
an unmeasurable total is then reported as **unenforceable** rather than treated
as satisfied — unknown cost is not zero cost, and a ceiling that cannot be
checked is not a ceiling that was met. It is said once, as a fact, not as a
warning nobody reads.

Exceeding one is **not** a provider cap. It lands the item in `blocked` with the
ceiling named, consumes no attempt, and **never parks an endpoint** — parking a
shared endpoint because one item was expensive is exactly the conflation the
vocabulary exists to prevent. The two facts even have separate event names:
`budget_exhausted` is a *provider* saying our account is out, and belongs to the
never-retry set; `budget_exceeded` is *this deployment* saying one item has had
enough.

### 6.6 The only honest quality metric

Everything the pipeline produces is a proxy. A reviewer approved it; the checks
passed. None of that says the change was any *good* — only what happens to it
afterwards does, and that happens outside the harness.

| Metric | What it actually measures |
|---|---|
| Review approval rate | whether a reviewer agreed |
| Check pass rate | whether the cheap gate works |
| Merge rate | whether a human accepted it |
| **Revert rate** | whether accepting it was right |

So merges, closures and reverts are fetched from the platform and recorded, with
three properties. **Two facts, never one that changes its mind** — a PR merged
today and reverted next week produces `pr_merged` then `pr_reverted`, in order,
both true when recorded; rewriting the merge would make history depend on when
you looked. **Stamped with their own time** (§6.2). And **unattributed outcomes
are skipped, not counted** — repositories are full of pull requests the harness
never made, and an outcome belonging to no item inflates every rate it appears
in.

Approval rate and revert rate come apart exactly when it matters. A harness
tracking only the first reports improving quality right up until someone looks
at the repository.

### 6.7 The coordination plane

A third durable store, separate again, exists for a different kind of fact: what
participants *said*. It is designed around one distinction — **conversation,
proposals, commands and state are four different things, and a message never
becomes queue state merely because an agent asserted it.**

- **The ledger** is append-only with no update and no delete path at all,
  per-room gapless sequencing assigned inside the write transaction, a
  content-chained digest per message, and a closed vocabulary of message types.
  It scans for secrets *before* accepting, because permanent retention means an
  accidentally posted credential cannot be removed afterwards — only hidden
  behind an append-only access restriction.
- **The command service** is the single deterministic door for state change,
  whoever the caller is. A proposal pins the graph revision, item state, owner
  and attempt it expects; a stale one is rejected and the rejection is appended
  to the room it came from. Applying an accepted command twice has the effect of
  applying it once, which is what makes crash recovery possible.
- **The oversight actor** is one per project, selected by a lease with a fencing
  generation so a restart cannot produce two authorities that both believe they
  are current — and so a deposed coordinator's in-flight proposal is refused
  rather than applied. It holds a read-only view: no queue handle, no database,
  no platform credential, and no resume tokens. **Its worst possible output is a
  bad suggestion.** It may not mark a dependency complete without evidence,
  override a check or a verdict, retry a terminal cap, fabricate ownership, edit
  a message, or pause a project because its own route is unavailable.

Absence is safe by construction: if the coordinator is unavailable, unresolved
work stays blocked and everything whose deterministic gates are satisfied keeps
running. Worker-side use is wrapped so that a ledger failure cannot fail an item.

**Status, in one clause:** the ledger, rooms, command service and oversight
actor are implemented and tested but **not wired** — nothing in the API or the
CLI constructs a ledger, so in a running deployment no worker speaks into the
plane and no coordinator reads from it. `docs/COORDINATION-PLANE.md` still
describes these four as proposed; that is stale, and the code is what counts.
The typed work graph from the same document (§5 here) is built and live.

---

## 7. Project isolation

Two projects both have a `T1`. Before project scoping they were **the same
row** — loading a second project did not fail to isolate, it overwrote.

Items are keyed `(project_id, item_id)`. Claims and dependencies resolve only
within a project unless a token says otherwise (§5). Each project has its own
control state and its own concurrency budget, and `fleet.py` gives each **one
worker pool**, so no project starves another and a failed model route in one
cannot stall the rest.

```
                 one harness process
   ┌───────────────────────┬───────────────────────┐
   │  project: alpha       │  project: beta        │
   │  control: running     │  control: stopped     │
   │  workers: 3           │  workers: 0           │
   │  T1, T2, T3…          │  T1, T2…              │
   └───────────────────────┴───────────────────────┘
              never reaches across
```

Sharing threads across projects is deliberately not an option: it reintroduces
starvation through the back door, and the fair thing to share is nothing. Each
worker also carries its own stop signal rather than one per pool, because with
a single shared event the only available answers were "keep every worker" and
"stop the project".

**Nothing resumes on its own.** Boot sets every project to `stopped` and records
what it *was* doing, so a project deliberately drained before a restart does not
come back looking identical to one that was running happily. A project registers
itself `stopped` too, so registering one cannot begin spending — and an
**unregistered** project id reads as `stopped` rather than running, because
defaulting the other way would mean a typo in a project id silently granted
claims. Only an explicit start creates workers.

Four control states, and **none of them kills anything**. `paused` and
`draining` are identical to a worker; the difference between them is recorded
operator intent, which is exactly the thing a person restarting a service needs
and a worker does not. `stopped` is not a third pause: pausing instructs a
running fleet, and stopped means there is no fleet to instruct. Stopping never
interrupts work in flight.

A worker that dies takes its claims with it — and they are released as
**`failed`, not requeued**. The item that killed a worker is the likeliest item
to kill the next one, and a silent requeue turns that into a crash loop that
spends money.

---

## 8. The API surface

`api.py` + `schemas.py` serve a documented OpenAPI document with Swagger UI, and
it is treated as a contract rather than as a debugging aid:

- **Every route names a response model.** A route returning a bare dict produces
  a schema of `{}` — valid, and useless to anyone generating a client.
- **Every field carries a description. The schema is the documentation.**
- **The docs need no token; the data does.** Requiring a credential to read a
  schema makes an API undiscoverable for no benefit.
- Behind a proxy, the root path must be set, or the schema advertises URLs the
  client cannot call.

Tests assert these properties, not just status codes.

Two service modes, and neither starts anything on its own. Without a session
host, `serve` is **monitoring-only** and starting a project is refused rather
than setting a flag no worker acts on. With one, the same API gains a worker
pool its start action can use — and still nothing runs until someone starts a
project.

### The browser control plane is another client

The browser application is packaged and served by `agent-harness` from the
same process and origin as the JSON API. It uses server-rendered templates,
vendored static assets and bounded opaque browser sessions established by a
one-time exchange of the configured bearer token. The token is not rendered,
stored in frontend code or placed in a URL. Browser mutations require CSRF
validation and explicit review; HTML controllers delegate to the same typed
application services and gate checks as JSON routes.

This does not make the GUI part of execution. A monitoring-only service renders
the same views while refusing start actions it cannot fulfil, and execution
continues when the GUI is offline. A session host remains an optional executor
adapter for PTY-backed agents, not the owner of browser authentication, routes,
assets or deployment. The later in-repository chat/terminal subsystem described
in `GUI_PLAN.md` is not implemented.

---

## 9. Extension points

"Add a vendor without editing core" is only true if the ones this repository
ships are added the same way. They are.

**Route presets, by installed metadata.** `protocols.py` resolves a preset by
name, loading only the name asked for, in this order: built in this process via
`register()`; named in configuration through `HARNESS_ROUTE_PRESETS`; published
by an installed distribution through an `agent_harness.route_presets` entry
point. This distribution's own presets are reached by the third door — if the
shipped ones took a shortcut through an import, "addable without editing core"
would be true only for the vendors we happen to ship.

```python
# somepackage/presets.py — in anyone's package
from agent_harness.protocols import BearerAuth, JsonChatRequest, JsonResponseReader, RoutePreset
from agent_harness.providers import VendorEnvelopeProvider

PRESET = RoutePreset(
    name="somevendor",
    request=JsonChatRequest(path="/v2/generate", model_key="model_id", messages_key="turns"),
    auth=BearerAuth(header="x-api-key", scheme=""),      # no scheme word
    reader=JsonResponseReader(text_paths=("result.reply",), usage_key="counters"),
    classifier=VendorEnvelopeProvider(vendor_field="problem", quota_categories=("budget",)),
)
```

```toml
# ...declared once, in that package's pyproject
[project.entry-points."agent_harness.route_presets"]
somevendor = "somepackage.presets:PRESET"
```

```json
// ...and named by a route. PUT /api/roles
{"roles": {"implementer": {"model": "m", "endpoint": "https://…", "preset": "somevendor"}}}
```

Nothing in `model_client.py`, `providers.py` or `protocols.py` changes for that,
and the conformance suite runs a preset registered from outside to keep it that
way.

**Dependency resolvers, the same door.** A resolver that knows one tracker's
format lives in `adapters/` and is declared under
`agent_harness.dependency_resolvers`. A dotted module path written into
`graph.py` would still be core knowing what a particular tracker is called.

**Role runners, the same door again.** Core publishes the versioned contract
and resolves `agent_harness.role_runners` entry points by name. The distribution
declares its shipped loop in metadata; `executor.py` receives only a structural
runner and neither imports nor names its adapter. An incompatible contract is a
configuration failure before work is claimed, not a substitution.

**Adapters generally.** Log readers, telemetry export and the agent loop are all
opt-in and lazily loaded, and nothing in core imports any of them. Telemetry is
**export-only**: it projects the event stream outward, nothing reads back, and
the event store stays the source of truth.

**Checks are argv the deployment supplies.** There is deliberately **no
registration mechanism for third-party gates** — that decision is open, and a
test fails if `outcomes.py` ever grows a registry, so it cannot be settled by
accident.

**What is not an extension point.** The gates themselves. A preset can change
what a failure means; it cannot change whether checks run before the reviewer,
whether an unreviewed change can present itself as reviewed, or whether a spend
cap is retried.

---

## 10. Known limits of this design

Named here rather than left for a reader to discover.

- **The generic classifier cannot see a spend cap.** It calls one `rpm`,
  because nothing in HTTP distinguishes them. A deployment on the generic preset
  gets §3.3's protection only as far as HTTP status codes reach.
- **The command guard is screening, not isolation** (§3.7), and cannot read
  inside `sh -c`.
- **Redaction is a reduction in exposure, not a guarantee** (§6.3).
- **A hold ties up a worker for its whole duration** (§6.5), which is why the
  maximum is not decoration and why holding is opt-in.
- **Only one dependency can be stacked on**, and the ones that were not are
  named rather than hidden (§2).
- **The coordination plane is one-way, and unwired** (§6.7). Agent-to-agent
  messages and human participation over the API are designed and not built, and
  two modules currently spell an item's room differently — harmless only
  because nothing in production constructs a ledger.
- **The role-runner path still works in one shared checkout.** It is selectable
  from `run`, but it is not yet the isolated per-item worktree fleet described
  by the accepted product direction. Its subprocess inherits the controller's
  environment, and `CommandGuard` remains screening rather than an OS security
  boundary. No secret-bearing real workload should be run through it until the
  Stage 2 confinement boundary exists.

Where to go next: [`AGENTS.md`](../AGENTS.md) for the binding rules,
[`STATUS.md`](STATUS.md) for what is built and what has been proven,
[`USAGE.md`](USAGE.md) for a worked example, [`DEPLOYMENT.md`](DEPLOYMENT.md)
for running it as a service, and [`evidence/`](evidence/) for the packages that
each name their own blind spots.
