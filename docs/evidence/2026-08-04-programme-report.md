# Fit-for-purpose programme report — 2026-08-04

The §11 report the original proposal requires, over every stage that has been
delivered. It **summarises** the per-stage reports beside it in this directory
and replaces none of them; where this and a stage report disagree about a
detail, the stage report is the one written with the code in front of it.

**One sentence for a hurried reader:** every stage the programme could run has
been run and is verified together on one tree at 1063 tests, and **not one line
of it is evidence that the harness works against a real model or a real fleet**
— that is Stage 8, it is blocked, and nothing here substitutes for it.

## 1. Configuration under test

| | |
|---|---|
| Branch | `fix/validator-rejects-valid-patches` |
| Where the programme started this session | `01be448` |
| Result commit | `3dbb764` |
| `TMPDIR` | on the NVMe volume throughout, per risk R6 |
| Network, credentials, provider traffic | **none, at any point** |

```console
TMPDIR=/path/on/fast/volume uv run pytest
uv run ruff check .
uv run ruff format --check .
TMPDIR=/path/on/fast/volume uv run mypy
```

| Gate | Result on `3dbb764` |
|---|---|
| `pytest` | **1063 passed** |
| `ruff check .` | all checks passed |
| `ruff format --check .` | 97 files already formatted |
| `mypy` | success, no issues in 93 source files (full-project strict) |

**One number, derived on this tree.** The per-branch counts the earlier stages
reported — 676, 678, 708, 715, 821 — are history and are not additive. The
progression through this session, each verified with all four gates:

| After | Tests |
|---|---|
| Stage F (integration) | 895 |
| Stage D (first run) | 920 |
| Stage K (outcome taxonomy) | 950 |
| Stage H (resumable attempts) | 982 |
| Stage L (item budgets) | 1002 |
| Stage J (durable hold) | 1031 |
| Stage M (telemetry export) | 1063 |

## 2. Per stage

Every row is a **repository fact**. No row is a live observation.

| Stage | Criterion | Observed | Decision |
|---|---|---|---|
| 0 evidence package | a published package exists | `2026-08-03-04-ngms-first-sustained-run-v1.md` | complete |
| A deterministic e2e | a slice runs end to end with no network | within the 1063; full-project strict typing | complete |
| E2 context selection | the implementer sees what it patches | within the 1063; **#146 closed** | complete |
| E1 change protocol | a decision settled by comparison, no protocol removed first | within the 1063; zero lines under `src/`; D10 recorded | complete |
| C adoption | a project with existing work can be adopted | within the 1063; `adopt` reachable from the merged CLI | complete |
| G typed work graph | a required target the graph cannot resolve is a blocker | within the 1063; readiness re-checked in **both** executors | complete |
| B provider protocols | a route says *how* an endpoint is spoken to | within the 1063; four configurations, one conformance suite | complete |
| **F integration** | merged tree passes four gates; real count derived | 895 at the time; one semantic divergence found and recorded | complete |
| **D first run** | a clean checkout runs with no credentials or network | `init --demo` → `run --demo` completes an item; `doctor` spends nothing | complete |
| **K outcome taxonomy** | four gate outcomes and a non-failure stop distinguishable end to end | five check outcomes, five dispositions, twelve reason kinds, all through the API | complete |
| **H resumable attempts** | a killed worker resumes without re-paying for earlier stages | 3 model calls against 5 on the same induced crash | complete, **direct-API executor only** |
| **L item budgets** | an item cannot exceed a declared wall-clock or spend ceiling | both, stopping at a boundary, without parking an endpoint | complete, **direct-API executor only** |
| **J durable hold** | a held item survives worker death and is resumable from another process | held past a lease with the worker gone; answered from a fresh process | complete, **and see §5** |
| **M telemetry export** | spans exported without becoming a second source of truth | projection tested; **wire never run** | complete as specified |
| 8 validation | live runs, second repository, NGMS | **blocked** | blocked |

## 3. Decisions

**Resolved this session, by decision rather than by argument.** Both were put
to the owner and recorded in the proposal's §11.1 *before* the code that
depends on them was written.

- **D11 — a resumed attempt continues the existing one.** A crash is not a
  failure of the work, so `max_attempts` bounds genuine failures. The
  consequence was named at the time: an item that crashes in a loop is then
  bounded by a budget and nothing else — which is what made **Stage L stop
  being optional**.
- **D12 — a hold suspends the lease and keeps the claim.** The worktree and
  context survive. The cost was named at the time: a worker slot is tied up for
  the whole hold, which is why the maximum hold duration defaults to six hours
  rather than to unlimited.

**Answered by the proposal and recorded rather than re-decided:** D13
(telemetry is export-only) and D14 (budget ceilings default to unlimited so an
upgrade is a no-op).

**Still open, and untouched:** **D7**. **D8** — whether third-party gates get a
registration mechanism — became load-bearing for Stage K and was deliberately
not answered; a test fails if `outcomes.py` ever grows a registry. **D9** is
still blocked on #84 / T43, and no stage held the review prompt as a variable.

## 4. Defects found, and by what

Eight, all found by a test rather than by inspection, and all fixed:

| Where | What |
|---|---|
| F | Stages B and G stated the same generic-core rule and only one encoded it; a clean auto-merge failed a gate |
| D | `run --project X` set X running and claimed from `default`, so a full queue reported "nothing to do" |
| D | a relative `--work` failed mid-apply with a git error naming the wrong thing |
| K | a `verify:` timeout reached `adoption` as `failed`, meaning *this item is not done* — a materially wrong answer |
| K | so did a missing interpreter |
| H | a killed worker left its half-applied diff in the tree; the next attempt's patch failed against a tree already containing it |
| H | an operator's retry would have resumed into the verdict it was retrying |
| L | a response reporting no usage was skipped rather than counted — the #128 shape reading as zero cost |

The two in Stage K and the one in Stage H were caught by **pre-existing tests
failing**, which is the system working.

## 5. Costs

- **Model spend across the entire programme: zero.** No provider was contacted
  at any point, in any stage. No request left the process.
- **Unmeasured, and the list has not shortened:** everything about live
  behaviour. Model quality, cost per merged item, unattended reliability,
  behaviour against a real gateway, a real `gh`, a real collector, a real
  second repository.

## 6. What is delivered but only half-delivered

Named here because §2's "complete" column is true and would otherwise read as
more than it is.

- **Session mode has no resumable attempts and no budgets.** Stages H and L are
  the direct-API executor only. A session-mode fleet gets nothing from either,
  and the two gaps are the same gap in the same place.
- **Stage J's hold cannot deliver an answer to a blocked agent.** The session
  host reports *that* an agent is waiting, not what it asked, so the recorded
  question is a pointer to the terminal. And answering through the API records
  the answer; it does not type it into the session, deliberately. In practice a
  human answers in the terminal and the hold is the inbox and the record.
- **Stage M has never sent a span.** The projection is tested; the OTLP wire is
  twenty lines that have never executed, and nothing flushes the tracer on exit.
- **Stage D's local-provider path is documented and unexecuted.** Nobody has run
  this against Ollama or any other local server.
- **`waiting_for_input` still exists beside the new `held` state.** Two things
  now describe one situation; retiring the projection is owed.

## 7. Blind spots for the programme as a whole

- **Every stage is verified by tests the stage wrote.** 1063 is a count of
  assertions this programme chose to make. The four gates cannot distinguish
  that from coverage of what a fleet will do.
- **A merged tree that passes tests is not a tree that has run.** Stage F's R1
  — a merge that passes while silently reversing a stage's intent — is
  mitigated for the four files two stages both touched and was not searched for
  elsewhere.
- **No clean-machine test exists.** "A clean checkout runs this" is tested
  inside a process that already has the dependencies installed.
- **Nothing has been pushed, and no pull request exists.** The whole programme
  is local commits on one branch.
- **The `P0`–`P4` milestone scheme is still load-bearing** in
  `tests/test_backlog.py`. Stage F renamed the seed file and documented the
  situation; the finding in proposal §2 is labelled, not fixed.
- **`attempt_stages` and `holds` grow without bound.** Nothing prunes either,
  and `maintenance.py`'s rollups do not know they exist.
- **Timing is nowhere a measurement.** `TMPDIR` was on the NVMe volume
  throughout, per R6. No duration in any report this session is a benchmark.

## 8. Continue/stop

**Stop here, and the reason is not fatigue.** Every stage that can be run from
this repository has been run. What remains is Stage 8, and it needs
credentials, network, a real second repository and NGMS human decisions that §9
of the original proposal explicitly refuses to answer by assertion.

The honest position, unchanged from where the programme started and now
supported by considerably more work: **deterministic success is proven and live
success is not.**

Three things are ready for whoever picks this up:

1. **Push, open a pull request, and get the branch reviewed.** Twenty-nine
   commits on one local branch is the same liability the status document
   described in July, one order of magnitude larger.
2. **Close the session-mode gaps in H and L together.** They are one gap.
3. **Run #84 / T43.** It is the only open decision that a single deterministic
   run would settle, and D9 has been waiting on it.

**"No failures observed" is not equivalent to "the requirement was
exercised."**
