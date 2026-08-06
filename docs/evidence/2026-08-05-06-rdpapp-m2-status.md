# Evidence package: rdpapp M2 through the harness, 2026-08-05/06 (v1)

**Evidence package ID:** `rdpapp-m2-2026-08-05-06-v1`
**Report version:** 1
**Report date:** 2026-08-06
**Observation window:** 2026-08-05 through 2026-08-06
**Status:** live; the workload is mid-flight and this is its running record

## Decision

**Do not start another delivery run until the agentic role runner exists
(#215). Reruns before that exercise the execution model measured to deliver
nothing.**

Four passes of the direct executor and one standalone run of the agent loop
have produced no merged work. The loop reached `cargo test` green on this
repository in 31 turns using the same models and the same gateway, and cannot
currently be selected from `run`.

This report is one project's evidence. **Nothing in it is a universal
measurement**, and the numbers here are rdpapp's — a 704 KB `main.rs`, a Rust
workspace, a credential vault whose own fixtures trip the harness's redactor.
A second repository would produce different numbers and is the only thing that
would make any of this general.

## Evidence classification

Per the README's vocabulary:

- **tested** — the harness defects found here have regression tests in this
  repository (#216, #217, #218). They fail if the defect returns.
- **observed** — the pass-by-pass outcomes below, the 31-turn loop run, and the
  gateway availability sweep. Real runs, no preserved raw artefacts.
- **proven** — nothing. No item has been delivered, so there is no delivery
  rate, no cost per merged item, and no baseline comparison.

## Where this actually stands

**No item has been delivered.** Four passes of the direct executor and one
standalone run of the agent loop have produced no merged work. That is the
headline and everything below qualifies it.

What has changed is *why* it fails, and each move was progress:

| pass | failure | verdict |
|---|---|---|
| 1 | hunk headers miscounted lines | format defect, **fixed** (D10 reopened, edit blocks) |
| 2 | T1 reached `checks passed → commit → review`, rejected on substance | the gate working correctly |
| 3–4 | `SEARCH text does not occur in the file` | **the harness's fault** — see below |
| loop | `LimitsExceeded` at 40 turns, 15 lost to a guard defect | guard defect **fixed** |

The pass 3–4 failure deserves naming: the diff was computed against the working
tree, which still held the *previous item's* branch. The model was shown the
file from `main`, wrote an edit against it, and was told the text did not
exist. **The model was right every time and the harness blamed it** — and the
better the previous item did, the more certain the next was to fail. Fixed in
agent-harness #216.

## Do not start another run yet — and why

A rerun today uses the **direct executor**: one model call per item, over a
context the planner chose in advance, with no way to read a file it was not
given and no compiler feedback. That is the execution model measured to deliver
nothing here. The four rendering fixes in #216 improve it without changing its
shape.

The reason to expect a different outcome is the loop — same models, same
gateway, 31 turns, `cargo test` green on this repository — and it cannot
currently be selected from `run`.

### Priority, in order. Each blocks the next.

| # | agent-harness issue | why it is where it is |
|---|---|---|
| **1** | **#215 — the agentic role runner, implementer through it** | Nothing else can be built until it exists, and nothing built so far can be used without it. Every successful loop run to date went through a scratch script; the queue, gates, audit and reviewer have never seen a loop-executed item. **This is the only thing standing between the work already done and an item landing.** |
| **2** | **rerun rdpapp M2** | The first run worth doing. T1 and T2 are the direct comparison — both have failed repeatedly and both are now understood. |
| 3 | #226 — reviewer, read-only | The gate that rejected T1 was *inferring* from a diff. It was right, and it was guessing. Highest value after delivery works, because a false rejection costs an attempt and blames a model that was correct. |
| 4 | #224 — surveyor | Would have written T1's acceptance criterion correctly the first time instead of it being hand-repaired after a rejection. Matters for the *next* project more than for M2, whose plan already exists. |
| 5 | #225 — assessor (`adopt`) | Not on M2's path — `adopt` is for taking on a project already part-built. Real, and not blocking anything here. |
| 6 | #227 — retire the planner and context pre-selection | **Deliberately last.** Gated on evidence: items delivered through the loop, not a date. Deleting the only path with tests in favour of one that has never run in-harness is the trade AGENTS.md rejects. |

The framing behind all of it is agent-harness **#195**: the single-shot call is
the defect, not any one role's prompt. Four format repairs on 2026-08-05 —
diffs → edit blocks → indentation tolerance → quoting the file back — each
improved the output and delivered nothing.

### If you want to run something today anyway

It is defensible, and it is a measurement rather than a delivery attempt: the
direct executor now has the #216 fixes, and no pass has been run since them.
A pass would say whether the stale-worktree bug was the only thing standing
between T2 and an applied patch. Expect a substantive review rejection rather
than a delivery, and treat a green `cargo test` as the useful signal.

## Re-running it

### Preconditions, all verifiable

```bash
# 1. Base lineage. This is what invalidated the abandoned 2026-08-04 attempt:
#    work was based on a branch 121 commits behind the authoritative remote.
cd ~/Working/Active/rdpapp
git fetch --all
git rev-list --count harness/m2-base..origin/master     # must be 0
git status --porcelain                                   # must be empty

# 2. Both remotes agree. `origin` (Forgejo) is authoritative; GitHub is a
#    mirror. plan.md says so; confirm rather than assume.
git rev-parse origin/master github/main                  # must match

# 3. The gateway answers, and with what. Claw Bay is frequently and broadly
#    degraded — measured 8 of 42 models on 2026-08-05, all one family.
curl -s -H "Authorization: Bearer $THECLAWBAY_API_KEY" \
  https://api.theclawbay.com/v1/models | head -c 200
```

### The run

```bash
cd ~/Working/Active/apps/agent-harness
R=~/Working/Active/.harness-runs/rdpapp-m2
. $R/env.sh                      # endpoint, preset, gpt-only role chains, CARGO_TARGET_DIR

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

- `--no-push` — `plan.md` calls GitHub a mirror and says the CI cutover is not
  authorised. Work stays on local branches; a discarded attempt is branches to
  delete, not a remote to clean.
- `--reroute` — without it the *stored* role map wins and the flags do nothing.
  The harness warns, and the warning is emitted before the reroute applies, so
  it can read as a failure when it is not.
- `--base harness/m2-base` — cut from `4dff7e2`, equal to both remotes.
- `--context-budget 300000` — the implementer chain's head is `gpt-5.6` at 372k.
  Anything above that silently exceeds the model.

### Retrying failed items

```bash
uv run python -c "
import sqlite3; c=sqlite3.connect('$R/queue.sqlite')
c.execute(\"update work set state='pending', owner=NULL, lease_until=0 where state='failed'\")
c.commit()"
```

Keep `last_error` — the harness feeds it into the next attempt's prompt, which
is the only thing making a retry different from a repeat. Clear `attempts` too
**only** when the previous failure was the harness's fault rather than the
item's; otherwise the ceiling stops meaning anything.

## Monitoring it

```bash
R=~/Working/Active/.harness-runs/rdpapp-m2

# What each item is doing. The stages are the whole story: an item that reached
# `checks` failed differently from one that died at `implement`.
grep -E "T[0-9]+ (started|edits_parsed|edits_rejected|applied|checks_|review_|committed|no_diff)" $R/run-*.log | tail -30

# Queue state.
uv run python -c "
import sqlite3; c=sqlite3.connect('$R/queue.sqlite')
for r in c.execute('select item_id,state,attempts,disposition,reason_kind,substr(coalesce(last_error,\"\"),1,70) from work order by cast(substr(item_id,2) as int)'): print(r)"

# What the models actually said (agent-harness #190). NOTE #209: answers are
# redacted on the way into the store, so text containing `password: "..."` --
# which rdpapp's fixtures are full of -- is rewritten. Do not diff a stored
# answer against a file and conclude the model was wrong.
python3 -c "
import json
for l in open('$R/events.jsonl'):
    e=json.loads(l)
    if e.get('outcome')=='ok' and e.get('answer'):
        print(e['model'], e['answer_chars'], 'redacted' if e['answer_redacted'] else '')"

# Gateway health, per model, from traffic already made (agent-harness #192).
grep -c "fell back" $R/events.jsonl
```

### What to watch for, and what it means

| symptom | meaning |
|---|---|
| `edits_rejected … does not occur` | the model named text that is not there. Since #216 this is genuinely the model, not a stale worktree. The error now quotes the file back. |
| `review_rejected` | the gate working. Read the objection — on T1 it was correct and the brief was sharpened in response. |
| `LimitsExceeded` (loop) | ran out of turns. Check the refusal count first: a guard false positive used to consume ~38% of them. |
| 429 / 503 storms | the gateway, not the harness. `--implementer a,b,c` chains past it; `survey` could not until #193. |
| nothing claimed, queue full | was a real deadlock (#218) — the claim scan stopped after one page. Fixed; if it recurs, that is a regression worth reporting. |

## Decisions already taken — do not re-litigate

- **Q1** source profile: Royal plain XML + zipped (no protected docs); WinSCP
  `WinSCP.ini` only; Ansible INI **and** YAML static inventories.
- **Q2** Royal secrets: omit every credential payload, count `secret omitted`.
- **Q3** counters: `created`/`skipped`/`conflicted`/`failed` mutually exclusive
  per record; `unsupported`/`secret omitted` are field-level and may co-occur
  with `created`.
- **Q4** cross-version fixtures: current output **plus one prior revision** per
  format, plus one explicitly unsupported shape each.
- **Scope**: M2 only. M1 (production-target protocol proofs) is the owner's
  stated priority and is **not harness-executable** — it needs live RDP/VNC
  targets, Node B access and soak infrastructure.

## Known limits of this setup

- **Reviewer independence is unavailable.** Only the `gpt-*` family answers on
  Claw Bay, so the reviewer shares a vendor with the implementer. The harness
  says so on every run. Any approval taken now is weaker than one taken when
  two vendors answer; nothing records that against the verdict.
- **`migration-tool` is workspace-`exclude`d**, so the FluentGUI path
  dependency does not affect the gated crates. Do not add it to the check
  command.
- **Do not gate on `cargo fmt --check`** — it refused five otherwise-correct
  attempts in the abandoned attempt. agent-harness #155 now lets a declared
  formatter's fix run and re-checks, but it is off unless `apply_fixes` is set
  on the project.
- A fresh worktree has no `node_modules`, so `tsc`/`vitest` checks cannot
  start. Keep the gate Rust-only.

## Cleaning up an attempt

```bash
rm -rf ~/Working/Active/.harness-runs/rdpapp-m2          # queue, events, logs, cargo cache
cd ~/Working/Active/rdpapp
git worktree list                                        # remove any under .harness-work/
git branch -D $(git branch --list 'harness/t*' 'adapter/*' 'spike/*' | tr -d ' *+')
```

`harness/m2-base` is the base and should survive. The `harness/r1`–`r7` and
`harness/base*` branches are from the **abandoned 2026-08-04 attempt** and are
not this work.

## Known blind spots

Which of this report's own claims are untested.

- **No delivery, so no delivery evidence.** Every claim about the loop being
  better rests on one item, once, through a standalone script — not through
  the harness. `cargo test` green is a real result and is not a delivered item:
  it never met the reviewer, the queue, the audit or a budget.
- **The 31-turn loop run cheated and was caught by hand, not by a gate.** It
  appended tables to a tracked SQL fixture so its own registry matched. The
  reviewer never saw it because the run never reached a reviewer. Whether the
  gate would have caught it is **assumed, not observed** — and it is the
  argument for #226's read-only environment rather than evidence for it.
- **The refusal-count comparison in #217 is reconstructed.** The 15 refusals
  from the live run were not preserved; the before/after table was measured on
  a corpus built to match their described shapes. The agent said so, and it is
  repeated here rather than buried.
- **One repository, one gateway, one model family.** Claw Bay answered with
  `gpt-*` and nothing else on 2026-08-05 (8 of 42 models). Reviewer
  independence was unavailable throughout, so *every* verdict recorded in this
  window is same-vendor. Nothing attaches that caveat to the verdicts
  themselves.
- **The pass-by-pass table attributes causes with hindsight.** Passes 3 and 4
  were attributed to the stale-worktree defect (#216) after it was found. That
  attribution is consistent with the evidence and has **not** been confirmed by
  re-running those passes against the fix.
- **Cost is unmeasured.** Turn counts are recorded; spend is not. `pricing` has
  never attributed a ~30-call role to a single item, and nobody has checked
  whether it does so correctly.
- **The harness defects found here were found by reviewing code, not by
  running.** #218's page-scan deadlock, for instance, has never been observed
  in production — it was constructed. It is a real defect and its frequency in
  practice is unknown.
