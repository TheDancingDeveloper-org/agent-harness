# Stage D first-run report — 2026-08-04

**Status:** delivered. A clean checkout runs an item end to end with no
credentials and no network, and `doctor` reports the deployment without
contacting anything. Every claim below is a **repository fact** reproducible
from `91714f9`. There is not one live observation in this report.

Specification: §8 of
[`PROPOSAL-2026-08-fit-for-purpose.md`](../PROPOSAL-2026-08-fit-for-purpose.md);
acceptance §8.4. Un-deferred by explicit go-ahead on 2026-08-04, recorded in
[`PROPOSAL-2026-08-finish-then-extend.md`](../PROPOSAL-2026-08-finish-then-extend.md)
§11.1. It is the ninth stage of the programme and the first after integration
(Stage F).

## 1. Configuration under test

| | |
|---|---|
| Branch | `fix/validator-rejects-valid-patches` |
| Base | `29d1f46` (Stage F integration, 895 tests) |
| Result commit | `91714f9` |
| New modules | `src/agent_harness/demo.py`, `src/agent_harness/doctor.py` |
| New commands | `agent-harness init --demo`, `agent-harness doctor`, `run --demo` |
| New tests | `tests/test_stage_d_first_run.py`, 25 tests |
| `TMPDIR` | on the NVMe volume, per risk R6 |
| Network, credentials, provider traffic | none |

## 2. Commands run, and the result

```console
TMPDIR=/path/on/fast/volume uv run pytest
uv run ruff check .
uv run ruff format --check .
TMPDIR=/path/on/fast/volume uv run mypy
```

| Gate | Result on `91714f9` |
|---|---|
| `pytest` | **920 passed** |
| `ruff check .` | all checks passed |
| `ruff format --check .` | 217 files already formatted |
| `mypy` | success, no issues in 83 source files |

920 − 895 = 25, which is exactly this stage's test file. No pre-existing test
was modified or deleted.

## 3. §8.1 — the deterministic no-network demo

`agent-harness init --demo --into DIR` creates, in one directory:

- a **real git repository** with one commit — a small Python package with a
  test suite, generated from nothing, committed with an explicit identity and
  `--no-gpg-sign` so it works on a machine whose git is configured for
  something else or not at all;
- a **one-item plan**, parsed by the ordinary plan parser;
- a **queue** holding one project and one pending item;
- a **stored role map** pointing all three roles at a demo model.

The project is left **stopped** and no event file exists yet. It prints one
command, which runs the item to completion.

### 3.1 The seam, and why it is that one

The **transport** is the only thing replaced. `ModelClient`, its retry ladder,
the preset that shapes the request, the reader that parses the reply, the
executor, the queue, the graph, the worktree handling, the patch validator, the
checks and the reviewer gate are the same code a real run uses.

The replies come back in the `chat-completions` body shape and are read back
through the same `JsonResponseReader` a real reply goes through. A transport
returning plain text would have skipped the reader and quietly stopped testing
it.

The implementer's diff is computed with `difflib` **against the tree on disk**,
not stored as a patch. A hardcoded patch would rot the first moment the fixture
changed, and would rot silently — as a demo that stopped applying. There is a
test that changing the fixture changes the diff.

The transport dispatches on **what the prompt asks for**, not on a role it was
told, because that is what a model has to do.

### 3.2 Observed, on the merged tree

```
T1 started → calling planner → planner_targets → context_selected
  → calling implementer → applied (git apply) → checks_passed
  → checkpointed (harness/t1) → calling reviewer → review_approved → done
  ok  T1: plan -> implement -> apply -> checks -> commit -> review
1/1 items completed
```

`git log --oneline --all` shows two commits; `main` does not contain the
change. Nothing was pushed and no repository is configured, so nothing *could*
be pushed.

Asserted rather than described, in `tests/test_stage_d_first_run.py`:

- the item reaches `done`, on a branch, with `multiply` in the commit and
  **not** on `main`;
- every stage appears in the event stream by name, and `checkpointed` precedes
  `review_approved` — the "checkpoint before the expensive gate" rule, proved
  again here rather than assumed;
- **`_http_transport` is replaced with a function that fails the test if it is
  called at all.** The no-network claim is adversarial, not incidental;
- the demo's project has **no repo configured**, so the GitHub path is closed
  by construction rather than by flag;
- breaking the fixture's test suite makes the item fail at `checks_failed` and
  **never reach the reviewer** — without this, a check that silently no-oped
  would look identical to one that ran;
- the run exits non-zero when the item does not complete, so the path is usable
  in CI, which §8.1 says is one of its two purposes;
- the command `init` **prints** is parsed out of its own output with `shlex`
  and executed. Not the argv a test would have written — the text a human
  copies.

### 3.3 Cost is reported as zero, not omitted

The demo reports zero tokens and no cost. A fabricated token count would appear
in the audit rollup as spend that never happened. Asserted.

## 4. §8.3 — `doctor`

Reports, per §8.3's list, and a test names each one individually so that
removing one is a failing test rather than a quietly shorter report:

| §8.3 asks for | Finding |
|---|---|
| configuration and route completeness | `routes` |
| protocol/classifier selection | `protocol and classifier`, plus `preset not stored` |
| provider reachability | `model reachability` |
| git/worktree availability | `git`, `checkout` |
| check-command validity | `checks` |
| reviewer independence | `reviewer independence` |
| session-mode traffic observable | `cost visibility` |
| GitHub mutations enabled | `github mutations` |

Beyond the list: `disk space`, `gh cli`, `route presets` and
`dependency resolvers`.

**`unknown` is a first-class state.** A check that was not run reports
`unknown`, which is neither a pass nor a failure, and the rendered report says
so in as many words: *"an unknown is a thing nobody has checked."* Model
reachability with no `--probe-models` reads *"not asked … Not asking is not the
same as answering."*

**Two findings exist because a true statement would otherwise mislead.**
`check-command validity` looks the program up on `PATH` rather than merely
confirming the string parses — a check naming a program that is not installed
passes every configuration test there is and then fails after the implementer
has been paid for. And `preset not stored` says out loud that the reported
protocol is what the *database* resolves to, because `run --preset` can
override it for a run and reporting `generic` for a role that will speak
chat-completions would be true about the database and false about the run.

Exit code is `0` when nothing blocks and `1` when something does, so it is
usable in a script. `--json` gives the same report machine-readably.

`doctor` and `preflight` share probes where they overlap, deliberately, so the
report cannot disagree with the gate that actually refuses a start.

### 4.1 It spends nothing, and that is tested adversarially

- `diagnose(..., ask=None)` produces exactly one `unknown` reachability
  finding and calls nothing.
- Running `diagnose` twice leaves the database **byte-identical** and the
  directory listing unchanged.
- `gh`'s *presence* is reported; its *write permission* is not, because asking
  is a network call against a real account. Preflight asks it at start, where
  the cost is justified.

## 5. §8.2 — the local-provider path

Documented in `docs/USAGE.md` §0a.2, using Ollama as the worked example, and
deliberately **not** part of required CI. It states each thing §8.2 asks for:

- **who supplies the model:** you do. The harness downloads nothing, installs
  nothing and starts nothing.
- **the endpoint shape:** the base URL without the path — the
  `chat-completions` preset appends `/chat/completions` itself.
- **authentication:** not disabled. It is *sent and ignored*. `HARNESS_API_KEY`
  must still be non-empty because the harness refuses to run without one rather
  than silently sending an empty credential to a server that may have wanted a
  real one.
- **what "offline" means:** no traffic leaves the machine *for model calls*. It
  does **not** mean the harness is offline — `--repo` still reaches GitHub and
  so does `reconcile`. An airtight run needs `--no-push` and no configured
  repo, which is what the demo does.

**This path is documented and not tested.** It needs a server this repository
cannot supply. It is named in §8 below.

## 6. §8.4 — README claims

`README.md` now defines three words and says which claims are which:

| | meaning | how to check |
|---|---|---|
| tested | a test here fails if it stops being true | `uv run pytest` |
| observed | seen in a real run, without a reproducible artefact | `docs/evidence/` |
| proven | measured against a stated criterion, denominator published | **nothing about live behaviour is in this column** |

A test asserts all three words are present and that *"No failures observed"* is
still qualified.

## 7. Two defects this stage found, both pre-existing

Neither is in Stage D's specification. Both were found by running the demo and
would have been found by any first user.

**`run --project X` set X running and then claimed from `default`.** `_run`
never passed `project_id` to either executor, nor to the `--plan` loader, nor
to the queue counts it printed. The demo's first run reported *"nothing to do"*
over a queue holding exactly one ready item. On a single-project deployment
this is invisible, because everything is `default`. It has its own test, kept
separate from the demo's, because the demo would pass again the moment anyone
put its item in the default project for an unrelated reason.

**A relative `--work` failed mid-apply.** Worktrees are made beside the
repository and git is invoked with `-C`, so a relative path resolved against
whatever directory each subprocess happened to be in. It worked from the
repository's own parent and failed everywhere else, as
`cannot change to 'x/y': No such file or directory` — a message that points at
git rather than at the flag, which is the worst way for a first run to fail.
`--work` is now resolved once, at the top of `_run`.

## 8. Blind spots

Ordered by how badly each could mislead someone reading §3 as good news.

- **A green demo is not a working harness, and this is the whole point.** The
  three replies are fixed and written to succeed. The demo cannot fail for any
  reason a *model* would cause, which is the majority of the reasons a real run
  fails. Its own scripted reviewer says in its approval text that the verdict is
  fixed and that it would say the same about a diff that did none of what it
  claims — because somebody will read that text in an event stream.

- **The demo exercises one item, one shape of change, one pass.** No
  dependency, no retry, no crash, no concurrency, no failure to apply, no
  reviewer rejection, no attempt exhaustion. The check-gate test is the only
  negative path covered. A first-run path that only ever demonstrates the happy
  path is being honest about what it is, but it is one path.

- **The local-provider path (§5) is documented and unexecuted.** Nobody has run
  this harness against Ollama or any other local server. The endpoint shape and
  the authentication behaviour are read off the preset's code, which is
  reliable, and the rest is untested prose. The first person to try it is the
  first person testing it.

- **`doctor` reports; it does not predict.** Every finding is a fact about
  configuration. A project it reports as unblocked can still fail every item,
  for every reason a model or a network supplies. "Nothing blocks a start" is
  not "this will work", and the rendered footer says so.

- **`--probe-models` has no test that a real probe works.** It is covered by an
  injected `ask`, which proves the wiring and the reporting. Whether the wired
  `ModelClient.answers` path behaves against a real endpoint is untested here,
  as everything requiring a network is.

- **`doctor` reports the routes as *stored*, not as a run will resolve them.**
  `run --preset` and the role flags can change what is actually used. The
  `preset not stored` finding names this; it does not fix it, and there is no
  way to ask doctor "what would *this* command do".

- **Cost visibility is a statement about prices, not about coverage.** It
  reports models with no known price. It does **not** and cannot report the
  #128 hole — session-mode implementer traffic bypassing `ModelClient`
  entirely — because a route map cannot tell you about calls that never went
  through it. That remains open and remains unquantified.

- **The demo's warning that its reviewer is not independent is correct and will
  look like a defect.** All three roles are the same scripted transport, so the
  independence warning fires. It was left firing rather than suppressed, on the
  grounds that suppressing a true warning to make a demo look tidy is exactly
  the habit the warning exists to prevent.

- **`init` refuses a non-empty directory and does nothing else about
  concurrency.** Two `init --demo` runs into the same path race; the second
  gets a partially built tree if it wins. Not defended against.

- **No clean-machine test exists.** The claim "a clean checkout runs this with
  no credentials" is tested by deleting the relevant environment variables and
  poisoning the HTTP transport inside a process that has the dependencies
  installed. It is not tested on a machine that has never seen this repository.
  A container test would close this and was not built.

- **Timing is not reported.** `TMPDIR` was on the NVMe volume per R6. No
  duration in this report is a measurement.

**"No failures observed" is not equivalent to "the requirement was
exercised."** §8.2's local-provider path was not exercised at all.

## 9. Continue/stop

**Continue.** §8.4's acceptance is met for the deterministic path — a clean
checkout runs the demo from documented commands with no credentials and no
network — and the local-provider path is documented as the opt-in it is
specified to be. The stranger-on-a-laptop gap the status document named is
closed for the deterministic case and open for the live one.

Next per §3 of the extension proposal: **Stage K — outcome and check
taxonomy.** `doctor` now exists, which is where later stages' checks were going
to have to be retrofitted otherwise.
