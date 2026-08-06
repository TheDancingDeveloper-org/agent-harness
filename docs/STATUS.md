# STATUS — where agent-harness actually stands

**Date:** 2026-08-06. **This is the only status document for this repository.**

It says where the project stands, what is left to do, what the first real
workload is, and how to run the harness against it. It does not describe how
the harness works — that is [`DESIGN.md`](DESIGN.md), which owns the design and
is being written in parallel with this document. Where DESIGN.md and this file
disagree about mechanism, DESIGN.md is right.

[`FIT-FOR-PURPOSE-STATUS.md`](FIT-FOR-PURPOSE-STATUS.md) is frozen at
2026-08-04 and is being deprecated. Read it for the history of the stage
programme; do not read it as current, and do not link to it as current.

Three words are used precisely throughout, exactly as `README.md` defines them:

| word | meaning |
|---|---|
| **tested** | a test in this repository fails if it stops being true |
| **observed** | seen in a real run, without preserved artefacts that would let anyone reproduce it |
| **proven** | measured against a stated criterion, with the denominator and the commands published |

---

## 1. Where it actually stands

**The harness has never delivered a work item against a real workload.**

Not once. Four passes of the direct executor and one standalone run of the
agent loop against `rdpapp` produced no merged work, and no other real workload
has been attempted. There is no delivery rate, no cost per merged item, and no
comparison against any baseline, because the numerator has never been greater
than zero.

What *is* true, and is worth stating alongside it:

- The deterministic paths are **tested** — the queue and leases, the dependency
  graph, holds, attempts, budgets, the outcome taxonomy, the patch-apply
  ladder, the checks gate, the reviewer gate, the API contract, the redactor,
  the command guard, and the first-run/demo path. `uv run pytest` fails if any
  of them stops being true.
- The service runs and is deployed inside AIDevEnv. That is **observed**.
- Real model calls have been made against a real gateway, against a real
  repository, and they exposed real defects — several of which now have
  regression tests (#216, #217, #218). The defects are tested; the runs that
  found them are observed.
- Nothing about live behaviour is **proven**. No column entry exists.

The diagnosis of why nothing has been delivered is recorded in **#195**: the
single-shot model call is the defect. Every role — planner, implementer,
reviewer, surveyor, assessor, inception — is a model asked to answer questions
about a repository from a snapshot it was handed, unable to look at anything it
was not given and unable to check its own answer. Four separate repairs to the
implementer's output format on 2026-08-05 (unified diffs → edit blocks →
indentation tolerance → quoting the file back on a failed match) each improved
the output and each delivered nothing. The format was never the problem.

The same item, same models, same gateway, run as a **loop** with tools reached
`cargo test` green in 31 turns. That run went through a standalone script: the
queue, gates, audit, attempt record, budgets and reviewer have never seen a
loop-executed item. That is **observed**, once, on one item — it is not
evidence that the harness works, and it is explicitly not a delivered item.

**The single thing standing between the work already done and an item landing
is #215.** Everything else in the backlog is either downstream of it, blocked
on a run that cannot happen without it, or independent of delivery entirely.

---

## 2. All pending work

Every open issue, organised by what a reader can act on. Issue state lives on
GitHub (D1); this section is a reading of it on 2026-08-06 and will drift.

### 2.1 What blocks everything

| # | what it is | why it is where it is |
|---|---|---|
| **#215** | Build the agentic role runner, and put the implementer through it. One runner: given a role, a task, an environment (read-only or writable, screened by `CommandGuard`), and bounds, run a loop and return the result. | Nothing else in #195 can be built until it exists, and nothing built so far can be used without it. `run` chooses between `SessionExecutor` and `Executor`; the loop is neither. It carries two decisions the issue refuses to guess at: who runs the check command (feedback vs gate), and how a per-item budget in `budgets.py` bounds a loop whose only boundary is the whole loop. |

### 2.2 The #195 programme

**#195** is the organising idea and should be read in full before any of its
parts. It says the interaction model is the defect, names what survives
(`ModelClient` and its routing/retry/spend-cap behaviour, `protocols.py`, the
queue, holds, the graph, budgets, the guard, the audit, and the gates
themselves) and what is revealed as scaffolding (the planner, context
pre-selection, edit blocks and `to_diff`, structured-text parsing). It also
states the cost being accepted: `mini-swe-agent` becomes a dependency of the
core execution path rather than an opt-in extra, and turn count rises from 1 to
roughly 30 per role per item.

Its parts, in the order the workload's own evidence puts them:

| # | what it is | why it is where it is |
|---|---|---|
| #215 | implementer through the role runner | first, and blocks the rest — see above |
| #226 | reviewer through the runner, with a **read-only** environment | highest value after delivery works. The gate that rejected rdpapp T1 was *inferring* from a diff; it was right and it was guessing. A false rejection costs an attempt and blames a model that was correct. Read-only is not a detail: a gate with write access to the tree it judges can be talked out of a rejection. |
| #224 | surveyor through the runner, read-only | a plan should be written by something that read the repository. Matters more for the *next* project than for rdpapp M2, whose plan already exists. |
| #225 | assessor (`adopt`) through the runner, read-only | `adopt` asks a model to find evidence it cannot go and find. Real, and on nobody's critical path today. |
| #227 | retire the planner and context pre-selection | **deliberately last, and gated on evidence rather than a date.** Deleting the only path that has tests in favour of one that has never run in-harness is the trade AGENTS.md rejects. It moves once the loop has delivered items *through the harness*. |

### 2.3 Defects with no blocker

Each of these can be picked up today. All were found by reading code or by
reviewing a real failure, and none is waiting on anything.

| # | what it is | why it is where it is |
|---|---|---|
| #219 | two edit blocks naming one file by different path strings (`a.txt` and `./a.txt`) render its diff twice; the second copy cannot apply | found during the #216 review and deliberately left out of it, because the fix changes `plan_edits`' public keying. Low severity and fails safely — but the message blames the model for an edit it got right, which is the class of bug this repository spent a day removing. |
| #220 | two API routes compare a lease against `time.time()`, not `queue.now()` | identical in production; the divergence matters because the queue's clock is injectable *so that* lease behaviour can be tested, and two routes silently opt out. Needs a one-line ruling that `queue.now()` is authoritative, applied everywhere. |
| #221 | two holds opened by one attempt in the same tick raise a bare `sqlite3.IntegrityError` instead of a `HoldError` | `asked_at` is a float used as part of an identity. Effectively unreachable against a real clock, immediately reachable with an injected one. The fix needs a small design call: may one attempt hold twice at all? |
| #223 | `_WIRE_ROLES` permits a `tool` message that `_for_the_wire` has already stripped the `tool_call_id` from | latent, not live: nothing currently emits a `tool` role. The allow-list contradicts the rule `format_observation_messages` was written to enforce. The failure mode when it is reached is a gateway refusal naming no message, which has already cost one live run. |
| #207 | `test_pausing_a_project_stops_claiming` asserts completions stop within 100 ms, which is a timing assumption about the host | a CI flake on an unrelated branch. The property worth protecting is that pausing stops *claiming*; the assertion instead measures how fast an in-flight item finishes. The resume half of the same test already waits on a condition and is not flaky. |
| #209 | a stored model answer is redacted, so it cannot be used to reproduce what the model actually said | two promises in tension — "what did the model say" and "no credential reaches an append-only store" — with the second silently winning. It bites hardest on rdpapp, a credential vault whose fixtures are full of credential-shaped source. It matters most for exact-match edit failures, which are questions about characters, in a record whose characters were changed. |
| #103 | silent-but-active CLI sessions are indistinguishable from hangs | session-host path: PTY output is the only activity signal, so a working agent that prints nothing reports `activity: idle`. Independent of the #195 programme; note that #195 also deprecates `--session-host` in help and docs, so weigh effort here against that. |

### 2.4 Blocked on a decision or a measurement

These are not waiting on effort. Each names what it is waiting for.

| # | what it is | blocked on |
|---|---|---|
| #184 | a generated phase heading is both a tracking umbrella and a claimable item, and it cannot be both | **a measurement**, from a deployment that has actually run a generated plan: are phase items ever claimed and completed, or do they sit `pending` while their children finish? `render_plan(..., phases_as_items=...)` currently splits the behaviour by caller, which the issue calls a holding position rather than a design. |
| #189 | a correction learned on one item is paid for again on every item after it | **a decision, and a measurement.** The mechanism is easy; whether a lesson store is compatible with this repository's measurement discipline is the question, because a store that mutates the implementer's prompt between items makes two runs incomparable. The issue's own recommendation is *do not build it* until a real multi-item run says how many check failures share a cause with an earlier item's — a number #33/#44/#51 would produce as a by-product. |
| #222 | an item the claim scan gives up on is left with no disposition, and empty means "not finished with yet" | **decision D8** — whether third-party gates get a registration mechanism in `outcomes.py`. Recording `exhausted` properly needs a new reason kind (probably `gave_up` under `DECIDED`), and adding one would answer part of D8 sideways. A test fails if `outcomes.py` grows a registry, precisely so D8 is not answered by accident. |

Open decisions generally: **D7**, **D8** (above), **D9** (blocked on #84 — and
no stage may hold the review prompt as a variable while it is). See AGENTS.md
§ Decision hygiene; D1–D6 and D10–D14 are settled and are not to be
re-litigated.

### 2.5 Blocked on a real run

These cannot be closed by writing code. They need the harness to run against a
real workload for a real duration — which needs #215 first.

| # | what it is | why it is where it is |
|---|---|---|
| #33 | 72-hour measurement run: rate-limit errors broken down by class, delivery rate, patch-apply rate, `review_rejected` rate, against the plan's §2.1/§2.5 baselines | **the P1 deliverable is this measurement, not the code.** Cannot start: no item has ever been delivered, so there is nothing to measure a rate over. |
| #44 | 48-hour ingester soak against live fleet traffic — no restarts, no dropped events, store growth within expectation | needs live fleet traffic, which needs a fleet that delivers. |
| #51 | 7-day unattended run — no manual restart, no human intervention, every failure diagnosable from the GUI alone | the top-level fit-for-purpose criterion. Furthest out; everything else is upstream of it. |
| #84 | A/B whether the reviewer seeing the planner's rationale changes its verdict | blocked on a **real backlog run twice over**. It is the experiment D9 deferred to, and the audit layer (`review_approved`/`review_rejected` per item, `GET /api/audit/cost`, `reconcile`'s merged/closed/reverted) now makes it measurable. The metric that matters is revert rate, not approval rate: a higher approval rate with a higher revert rate is anchoring, not insight. **Do not settle it by argument.** |

Note that #84 and #226 interact: whether a read-only environment is enough to
keep the reviewer honest is untested, and #195 says so explicitly — it could
still be argued into a pass by its own reading of the code.

---

## 3. rdpapp is the first application under test

agent-harness is being exercised against **`TheDancingDeveloper-org/rdpapp`**,
also hosted on Forgejo at `repo.indexarr.net/indexarr/rdpapp`. **The Forgejo
remote is authoritative; GitHub is a mirror** — rdpapp's own `plan.md` says so,
and both remotes are configured in the working checkout (`origin` → Forgejo,
`github` → GitHub).

It is the first real workload, and it was chosen deliberately at the hard end:

- **Rust**, so a check gate means a real compile and a real test run, not a
  linter;
- a **704 KB `main.rs`**, which is where the miscounted-hunk failures came from
  — the arithmetic a unified diff header demands gets harder as a file grows,
  and that evidence is what reopened decision D10;
- a **credential vault**, whose own test fixtures trip the harness's redactor,
  which is how #209 was found.

The workload's own running record is
[`evidence/2026-08-05-06-rdpapp-m2-status.md`](evidence/2026-08-05-06-rdpapp-m2-status.md)
(evidence package `rdpapp-m2-2026-08-05-06-v1`). Read it for the detail; it is
not duplicated here. In summary:

- **No item has been delivered.** Four executor passes and one standalone loop
  run.
- Pass 1 failed on miscounted hunk headers — a format defect, fixed, and the
  reason D10 was reopened in favour of edit blocks.
- Pass 2 reached `checks passed → commit → review` and was rejected on
  substance. That is the gate working correctly.
- Passes 3–4 failed with `SEARCH text does not occur in the file`. **That was
  the harness's fault**: the diff was computed against a working tree still
  holding the *previous item's* branch. The model was right every time and the
  harness blamed it — and the better the previous item did, the more certain
  the next was to fail. Fixed in #216.
- The standalone loop run hit `LimitsExceeded` at 40 turns, ~15 of them lost to
  a guard false positive. Guard defect fixed (#217).

Its decision — **do not start another delivery run until #215 exists** — is
this repository's operating instruction too. A rerun today uses the execution
model measured to deliver nothing.

**These numbers are rdpapp's.** They are one repository, one gateway, one model
family, and nothing in them is a universal measurement about the harness. A
second repository is the only thing that would make any of it general, and
none has been attempted.

Two limits of that evidence deserve repeating here because they qualify
everything above: the 31-turn loop run **cheated and was caught by hand, not by
a gate** — it appended tables to a tracked SQL fixture so its own registry
matched, and no reviewer ever saw it. And **cost is unmeasured**: turn counts
are recorded, spend is not, and `pricing` has never attributed a ~30-call role
to a single item.

---

## 4. Running the harness against rdpapp

This section is versioned here so that a reader can follow it without asking
anyone anything. Its content comes from the evidence package above and from an
**unversioned** file at `~/Working/Active/.harness-runs/rdpapp-m2/env.sh`; that
file remains the thing actually sourced, and this is the record of what it
contains.

Paths below assume the layout of the machine this was run on
(`~/Working/Active/...`). Adjust them; nothing in the harness requires them.

### 4.1 Preconditions, each verifiable

```bash
# 1. Base lineage is not stale. This is what invalidated the abandoned
#    2026-08-04 attempt: work was based on a branch 121 commits behind the
#    authoritative remote.
cd ~/Working/Active/rdpapp
git fetch --all
git rev-list --count harness/m2-base..origin/master     # must be 0

# 2. The tree is clean.
git status --porcelain                                   # must be empty

# 3. Both remotes agree. `origin` (Forgejo) is authoritative; GitHub is a
#    mirror. Confirm rather than assume.
git rev-parse origin/master github/main                  # must match

# 4. The gateway answers, and with what. Claw Bay is frequently and broadly
#    degraded: 8 of 42 models answered on 2026-08-05, all one family.
curl -s -H "Authorization: Bearer $THECLAWBAY_API_KEY" \
  https://api.theclawbay.com/v1/models | head -c 200
```

### 4.2 The environment

Run-scoped rather than written into a shell profile: the attempt is meant to be
discardable by deleting one directory, and a profile edit would outlive it.
Every model here is on Claw Bay; nothing routes to a local CLI agent.

```bash
export HARNESS_ENDPOINT="https://api.theclawbay.com/v1"
export HARNESS_ROUTE_PRESET="claw-bay"
export HARNESS_API_KEY="${THECLAWBAY_API_KEY:?THECLAWBAY_API_KEY is not set}"

# gpt only, by owner's decision 2026-08-05: measured 8 of 42 models answering
# and all 8 in this family. Chains are preference order, first that answers
# wins. gpt-5.6 leads because gpt-5.5 timed out the gateway origin (524) on
# long generations while 5.6 answered 200 throughout.
export HARNESS_PLANNER="gpt-5.6,gpt-5.4-mini"
export HARNESS_IMPLEMENTER="gpt-5.6,gpt-5.5,gpt-5.4"
export HARNESS_SURVEYOR="gpt-5.6,gpt-5.5"
export HARNESS_ASSESSOR="gpt-5.6"

# NOT independent: same vendor as the implementer, because no second vendor is
# reachable. Every approval taken under this configuration is weaker than one
# taken when two vendors answer. Check GET /api/routes/health before trusting a
# review; when independence_possible turns true, move this to another vendor.
export HARNESS_REVIEWER="gpt-5.5,gpt-5.4"

# No local CLI agent. Direct API mode is used, so this is belt and braces: if a
# session host is ever passed, the agent must still not be a
# subscription-backed local binary.
export HARNESS_AGENT_COMMAND=""

# Shared so each item does not pay a cold Rust build in a fresh worktree.
export CARGO_TARGET_DIR="$HOME/Working/Active/.harness-runs/rdpapp-m2/cargo-target"
```

### 4.3 The run

```bash
cd ~/Working/Active/apps/agent-harness
R=~/Working/Active/.harness-runs/rdpapp-m2
. $R/env.sh

uv run agent-harness --db $R/queue.sqlite run --project rdpapp-m2 \
  --work ~/Working/Active/rdpapp \
  --plan ~/Working/Active/rdpapp/docs/harness/M2-PLAN.md \
  --base harness/m2-base --no-push --reroute \
  --context-budget 300000 \
  --events $R/events.jsonl \
  --check 'cargo test -p rdpapp-models -p rdpapp-sessions -p rdpapp-gateway' \
  2>&1 | tee $R/run-$(date +%H%M).log
```

Flags that are not decoration:

- **`--no-push`** — rdpapp's `plan.md` calls GitHub a mirror and says the CI
  cutover is **not authorised**. Work stays on local branches, so a discarded
  attempt is branches to delete rather than a remote to clean.
- **`--reroute`** — without it the **stored** role map wins and the role-chain
  environment above silently does nothing. The harness warns, and the warning
  is emitted *before* the reroute applies, so it can read as a failure when it
  is not.
- **`--base harness/m2-base`** — cut from `4dff7e2`, equal to both remotes.
- **`--context-budget 300000`** — the implementer chain's head is `gpt-5.6`,
  which holds 372k. Anything above that silently exceeds the model.
- **`--check '…'`** — Rust only, and deliberately: `migration-tool` is
  workspace-`exclude`d so its FluentGUI path dependency does not affect the
  gated crates (do not add it), and a fresh worktree has no `node_modules`, so
  `tsc`/`vitest` cannot start. **Do not gate on `cargo fmt --check`** — it
  refused five otherwise-correct attempts in the abandoned attempt. #155 lets a
  declared formatter's fix run and re-checks, but it is off unless
  `apply_fixes` is set on the project.

### 4.4 Retrying failed items without destroying `last_error`

```bash
uv run python -c "
import sqlite3; c=sqlite3.connect('$R/queue.sqlite')
c.execute(\"update work set state='pending', owner=NULL, lease_until=0 where state='failed'\")
c.commit()"
```

**Keep `last_error`.** The harness feeds it into the next attempt's prompt, and
that is the only thing that makes a retry different from a repeat. Clear
`attempts` too **only** when the previous failure was the harness's fault
rather than the item's; otherwise the attempt ceiling stops meaning anything.

### 4.5 Monitoring

```bash
R=~/Working/Active/.harness-runs/rdpapp-m2

# What each item is doing. The stages are the whole story: an item that reached
# `checks` failed differently from one that died at `implement`.
grep -E "T[0-9]+ (started|edits_parsed|edits_rejected|applied|checks_|review_|committed|no_diff)" \
  $R/run-*.log | tail -30

# Queue state.
uv run python -c "
import sqlite3; c=sqlite3.connect('$R/queue.sqlite')
for r in c.execute('select item_id,state,attempts,disposition,reason_kind,substr(coalesce(last_error,\"\"),1,70) from work order by cast(substr(item_id,2) as int)'): print(r)"

# What the models actually said (#190).
python3 -c "
import json
for l in open('$R/events.jsonl'):
    e=json.loads(l)
    if e.get('outcome')=='ok' and e.get('answer'):
        print(e['model'], e['answer_chars'], 'redacted' if e['answer_redacted'] else '')"

# Gateway health, per model, from traffic already made (#192).
grep -c "fell back" $R/events.jsonl
```

**The caveat that matters (#209): stored answers are redacted on the way into
the store.** Redaction is applied in `store.append` and `audit.append` and
never to prompts, so the model was given the real file — but text containing
`password: "…"`, which rdpapp's fixtures are full of, is rewritten before it is
recorded. **Do not diff a stored answer against a file and conclude the model
was wrong.** The `answer_redacted` flag tells you it happened; it does not tell
you where or how much.

Symptoms and what they mean:

| symptom | meaning |
|---|---|
| `edits_rejected … does not occur` | the model named text that is not there. Since #216 this is genuinely the model, not a stale worktree; the error now quotes the file back. |
| `review_rejected` | the gate working. Read the objection — on T1 it was correct and the brief was sharpened in response. |
| `LimitsExceeded` (loop) | ran out of turns. Check the refusal count first: a guard false positive used to consume ~38% of them (#217). |
| 429 / 503 storms | the gateway, not the harness. `--implementer a,b,c` chains past it; `survey` could not until #193. |
| nothing claimed, queue full | was a real deadlock (#218) — the claim scan stopped after one page. Fixed; a recurrence is a regression worth reporting. |

### 4.6 Cleaning up an attempt

```bash
rm -rf ~/Working/Active/.harness-runs/rdpapp-m2   # queue, events, logs, cargo cache
cd ~/Working/Active/rdpapp
git worktree list                                 # remove any under .harness-work/
git branch -D $(git branch --list 'harness/t*' 'adapter/*' 'spike/*' | tr -d ' *+')
```

`harness/m2-base` is the base and must survive. **The `harness/r1`–`harness/r7`
and `harness/base*` branches are from the ABANDONED 2026-08-04 attempt** — the
one based 121 commits behind — and are not this work. Do not read them as
evidence and do not build on them.

---

## 5. What is proven, observed and tested

| claim | word | how to check it |
|---|---|---|
| Queue and leases, dependency graph, holds, attempts, budgets, outcome taxonomy, patch-apply ladder, checks gate, reviewer gate, API/OpenAPI contract, redaction on the only two write paths, command guard, first-run/demo path | **tested** | `uv run pytest` |
| The core stays generic — no workload-specific paths, numbers or adapter imports | **tested** | `tests/test_generic.py` (`EXECUTION_PATH` is the authoritative list) |
| The store has no UPDATE and no DELETE | **tested** | the source-level assertion in the store tests |
| The four rdpapp-derived defects: edit-block rendering, stale worktree, guard false positives, claim-scan page deadlock | **tested** | regression tests landed with #216, #217, #218 |
| The service runs and is deployed inside AIDevEnv | **observed** | no preserved artefacts |
| An earlier supervised NGMS attempt and later direct calls exercised real agents and providers | **observed** | [`evidence/2026-08-03-04-ngms-first-sustained-run-v1.md`](evidence/2026-08-03-04-ngms-first-sustained-run-v1.md) — lacks a common run ID, complete configuration, checksums and a comparable follow-up |
| Four executor passes against rdpapp delivered nothing, and why each failed | **observed** | [`evidence/2026-08-05-06-rdpapp-m2-status.md`](evidence/2026-08-05-06-rdpapp-m2-status.md); the pass 3–4 attribution is hindsight and has not been confirmed by re-running against the fix |
| A loop reached `cargo test` green on rdpapp in 31 turns | **observed** | one item, once, through a standalone script — never through the harness, and it cheated in a way caught by hand rather than by a gate |
| Claw Bay answered on 8 of 42 models on 2026-08-05, all one family | **observed** | a sweep on one day; not preserved |
| Delivery rate | **neither** | no item has ever been delivered |
| Cost per merged item | **neither** | spend is not recorded per item; `pricing` has never been checked against a multi-turn role |
| Unattended reliability | **neither** | #33, #44, #51 have not run |
| Second-repository portability | **neither** | one repository has been attempted |
| Whether a read-only reviewer stays honest | **neither** | #226 is unbuilt; #84 is the experiment |
| Whether the reviewer's verdicts in this window are trustworthy | **not known** | every verdict recorded was same-vendor, because no second vendor was reachable, and nothing attaches that caveat to the verdicts themselves |

Nothing is in the **proven** column. That is not modesty; it is the definition
— nothing about live behaviour has been measured against a stated criterion
with a published denominator.

"No failures observed" is not the same as "the requirement was exercised".
