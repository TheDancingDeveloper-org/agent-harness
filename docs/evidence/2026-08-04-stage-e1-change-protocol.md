# Stage E1 change-protocol experiment report — 2026-08-04

**Status:** the deterministic comparison ran and separates the three protocols;
provider tokens, latency, money and any real failure *frequency* remain
unmeasured. D9 is held constant, not answered.

## Configuration under test

- Implementation commit: `07d67f1c3b8f565f7977b8f007726b75bfdcd217`, on
  `codex/fit-stage-e1`.
- Base commit: `afdc3bc998cfc5f6b0e763782023acf3b860de43` — the Stage A and
  Stage E2 integration tip, itself based on
  `6a909a7962a8b9afb2750c61c81f9b1f6c5db4f0`.
- Test transport: the Stage A in-process `DeterministicTransport`. No network,
  provider credentials, remote push or GitHub mutation.
- Repository: the Stage A generated fixture (`generated_repository`) plus one
  file added by this experiment, `src/mathkit/repeated.py`, which carries a
  leading header and two byte-identical `marker = "same"` bodies.
- Executor surface: the production `extract_diff`, `validate_diff`,
  `apply_diff` and `Checks` from `agent_harness.executor`. The two
  alternatives are implemented **only** in the test module; a test asserts
  that `src/agent_harness/executor.py` still names a unified diff as its sole
  response format and contains neither alternative.
- Cheap gate: the fixture's own declared commands — `python -m compileall -q
  src extensions` and `python -m unittest discover -s tests -q`.

**Not covered here:** any provider, any real model output, any hosted session,
any measurement of how *often* a model produces one of these responses. This
report is repository-verifiable fact about what each encoding does when a
given failure occurs, and nothing more.

## Reproduction

One command per number. The measurement table is a single JSON line printed by
the run.

```console
uv run pytest tests/test_stage_e1_protocol_experiment.py -o addopts="" -q
STAGE_E1_REPORT=1 uv run pytest tests/test_stage_e1_protocol_experiment.py -o addopts="" -q -s
uv run pytest -o addopts="" -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

`STAGE_E1_REPORT=1` prints two lines: `STAGE_E1_ATTEMPTS=` (one record per
protocol per case, the source of every per-case claim below) and
`STAGE_E1_MEASUREMENTS=` (the per-protocol totals in the table below).

Observed on 2026-08-04:

- the Stage E1 module collected 2 tests and passed in 4.50 s;
- all 678 repository tests passed, in 496.6 s and then 589.7 s wall-clock on
  two runs of the same commit. Both were taken while other work ran on the
  same machine; neither is comparable with the 49.4 s recorded in the Stage A
  package, and the spread between them is why;
- lint, formatting and strict whole-project typing passed.

## The case mix

Ten cases, run against every protocol. The denominator for every rate below is
**10 attempts per protocol**, one per case.

| Case | What it asks | Why it is in the mix |
|---|---|---|
| `modify` | add `multiply` after `add` | ordinary change, header must stay first |
| `repeated-text` | change only `second()`'s marker | two byte-identical bodies in one file |
| `create` | create `RELEASE.txt` | file creation correctness |
| `delete` | delete `src/mathkit/deprecated.py` | file deletion correctness |
| `rename` | rename `docs/OLD.md` → `docs/ARCHIVE.md` | rename correctness |
| `repairable` | a change whose encoding is damaged but derivable | repair rate and repair cost |
| `unsafe-ambiguous` | an encoding that does not say where | must refuse, must not guess |
| `elided-context` | the reply elides the region it thinks unchanged | the protocols' one real divergence |
| `malformed` | a reply that is not this protocol at all | unusable-response rate |
| `reviewer-reject` | a clean application of the wrong change | reviewer rejection rate |

Intent is held constant across protocols; only the encoding varies. Each
protocol receives the damage its own encoding admits — for `unsafe-ambiguous`,
a `@@ -0,0` hunk against a file with content (diff), the same path written
twice in one reply (whole file), and a search string matching twice (search
and replace). A single shared failure shape would measure the fixture rather
than the protocol.

## Comparative measurements

Denominator is 10 attempts per protocol unless the cell says otherwise.

| §4 metric | unified diff | whole file | search/replace |
|---|---:|---:|---:|
| Clean application rate (no repair) | 6/10 | **7/10** | 6/10 |
| Applied at all | 7/10 | 8/10 | 7/10 |
| **Wrong-location rate** | **0/10** | **1/10** | **0/10** |
| Wrong location reaching the reviewer | 0/10 | 0/10 | 0/10 |
| Unusable-response rate | 1/10 | 1/10 | 1/10 |
| Validator refusal rate (parsed, then declined) | 2/10 | 1/10 | 2/10 |
| Cheap-check failures | 0/7 applied | 1/8 applied | 0/7 applied |
| Repair rate | 1/10 | 1/10 | 1/10 |
| Repair cost (steps) | 1 | 1 | 1 |
| Repair cost (bytes rescanned) | 195 | 110 | 145 |
| Implementer input tokens (mock tokenizer) | 312 | 542 | 502 |
| Implementer output tokens (mock tokenizer) | 698 | 727 | 693 |
| Reviewer input / output tokens | 789 / 7 | 789 / 7 | 789 / 7 |
| Time to cheap-gate completion, total | 504.7 ms | 573.2 ms | 503.4 ms |
| — of which apply/validate | 11.4 ms | 2.8 ms | 2.4 ms |
| — of which declared checks | 493.3 ms | 570.4 ms | 501.0 ms |
| Reviewer calls | 7 | 7 | 7 |
| Reviewer rejection rate | 1/7 reviewed | 1/7 reviewed | 1/7 reviewed |
| File creation correct | 1/1 | 1/1 | 1/1 |
| File deletion correct | 1/1 | 1/1 | 1/1 |
| File rename correct | 1/1 | 1/1 | 1/1 |

Three of these cells matter and the rest are ties.

**Whole-file replacement has the best clean-application rate and the only
wrong-location application.** They are the same cell read twice. On
`elided-context` the reply wrote `src/mathkit/operations.py` containing the
module docstring, `# ... unchanged ...`, and the new `multiply` — a complete,
well-formed, schema-valid answer that destroyed `add`. It applied cleanly, in
0.5 ms, with nothing for a validator to object to. The two located protocols
refused the same failure before touching the tree: the elided region was the
anchor a hunk needs (`git apply` and `git apply --unidiff-zero` both failed),
and it was the search text that then matched zero times rather than once.

**The cheap gate contained it; it did not prevent it.** `python -m unittest`
failed on the corrupted tree, so the reviewer was never called and no wrong
location reached paid review in any protocol (0/10 everywhere). The cost of
that containment was one written-then-discarded tree, one wasted item attempt,
and 67.3 ms of check time that the other two protocols did not spend.

**Timing is dominated by the declared checks, not the protocol.** Apply and
validate account for 11.4 ms of 504.7 ms for diffs and roughly 2.5 ms for the
two JSON protocols. The diff path is slower because it shells out to `git
apply`, up to twice per rung. At this fixture's size that difference is noise
next to a `unittest` run, and these are wall-clock figures from one run on a
loaded machine, not stable measurements.

## What the adversarial cases proved

`src/mathkit/repeated.py` exists so that "it applied" cannot be mistaken for
"it applied where intended". It carries a header that must remain first and
two byte-identical function bodies.

- On `repeated-text` all three protocols changed `second()` and left `first()`
  and the header untouched. Applying is not the finding; applying to the right
  one of two identical sites is.
- On `unsafe-ambiguous` the search/replace protocol was handed `marker =
  "same"`, which matches twice. It refused rather than taking the first match.
  The diff protocol was handed a `@@ -0,0 +1,2 @@` header against a file with
  content and refused it before the first ladder rung — no tolerance rung
  guessed at a location, as §4.1 requires. The whole-file protocol was handed
  the same path written twice in one reply and refused it.
- On `elided-context` the divergence above is the whole result.

The wrong-location detector reports three separable failures: a path the task
never named changed; a header that had to remain first no longer is; or text
the task did not authorise changing has gone, which is what an edit landing
*over* existing code looks like afterwards.

A zero is only evidence if the detector can say no.
`test_wrong_location_detector_reports_each_way_a_change_can_miss` builds five
trees and asserts the detector's verdict on each: the correct change (no), an
unrelated file also changed (yes), the new function inserted above the module
docstring (yes), the elided tree that loses `add` (yes, and the fixture's
checks fail on it), and the repeated-text change applied to `first()` instead
of `second()` (yes). The zeroes in the table are earned against a detector
that fires on all four wrong trees.

## D9 is held constant, not answered

§9 requires Stage E1 either to hold the review prompt constant or to record it
as a variable. It is held constant, and the test asserts it: for each of the
seven cases that reached review, the review prompt was byte-for-byte identical
across all three protocols, and each of those cases produced exactly three
prompts. The prompt contains the task text and the resulting diff, so equality
also confirms the three protocols produced identical trees wherever they all
applied.

D9 — whether the reviewer sees the planner's plan and rationale — remains open
and is untouched by this report. Nothing here is evidence for or against
either variant of that prompt.

## Costs and blind spots

- **Provider input/output tokens: unmeasured.** The token columns come from a
  local tokenizer in the test module (words plus individual punctuation). It
  makes relative response volume reproducible; it is not vendor billing usage
  and must not be quoted as one.
- **Input tokens here are the instruction wording only.** The experiment
  supplies no repository context to the implementer, so the 312/542/502 split
  measures how long each protocol's instructions are, not what a real prompt
  would cost. Real input cost is dominated by the context selected in Stage
  E2, which is identical across protocols.
- **The output-token tie is an artefact of fixture size.** The files here are
  five to nine lines, so resending a whole file costs about what a diff of it
  costs. On a file of a few hundred lines, whole-file replacement's output
  cost is the dominant term and this fixture cannot show it. That is a
  hypothesis this experiment did not test.
- **Latency and money: unmeasured.** The transport is in-process and returns
  immediately.
- **Frequency is not measured, and this is the largest gap.** Every rate above
  is an outcome over a scripted case mix chosen by the test author. It says
  what each encoding does when an elision, an ambiguity or a malformed reply
  occurs. It says nothing about how often any model produces one. A protocol
  choice made on these numbers is a choice about *containment*, not about
  expected success rate.
- **The elision case is one instance, not a rate.** That an unguarded
  whole-file protocol cannot detect it is a structural property of the
  encoding and is reproducible. That it happens often enough to matter is a
  live hypothesis from the earlier workload record, not established here.
- **A model refusal and a malformed reply are not separable at this
  boundary.** The `malformed` case's diff response is refusal-shaped prose,
  and `extract_diff` reports the same "no diff here" for both. The harness
  classifies refusals at the *transport* layer (Stage A's conformance matrix),
  where they never reach an apply step. There is no content-level refusal
  classifier and this report does not invent one, so §4's "malformed-response
  / refusal rate" is reported as a single unusable-response rate.
- **Session mode is unaffected and still unmeasured.** Its implementer edits
  files inside a hosted CLI session using whatever protocol that agent uses;
  that traffic does not pass through `ModelClient` (issue #128), so none of
  these measurements describe it. Nothing in this report changes what session
  mode does, and nothing in it should be read as evidence about session mode.
- **Reviewer rejection is scripted, not judged.** The fixture reviewer
  approves when the resulting diff equals the expected diff and rejects
  otherwise. The 1/7 rejection rate is therefore a property of the case mix.
  It shows the reviewer boundary is reached and its verdict recorded; it is
  not a measurement of reviewer quality.
- **Repair costs are not comparable in kind.** The diff repair is a hunk
  recount — a substantive derivation over the patch body. The two JSON repairs
  strip a markdown fence. Both are local and cost no model call, and the byte
  counts (195 / 110 / 145) are the bytes each rescanned, not equivalent work.

## Decision

**Continue. Retain the model-authored unified diff as the executor's change
protocol; keep search/replace as the named alternative if a second protocol is
ever configured; do not adopt whole-file replacement in its unguarded form.**

Search/replace matched the diff on every safety metric in the table — same
clean-application rate, same refusals, same zero wrong locations — and needs no
tolerance ladder to get there. It is the alternative to configure if a second
protocol is ever wanted; it is not a reason to change the default, because
changing a working default on a tie is a cost with no measured benefit.

The evidence for the ordering is one cell: whole-file replacement is the only
protocol that applied a destructive response, and it did so with the highest
clean-application rate in the table. §4 says a wrong-location rate must be
zero for an acceptable protocol, and unguarded whole-file replacement scored
1/10. Its structural advantage is real — it cannot misplace a change, because
it never states a location — and it is bought by making the payload and the
anchor the same text, so a reply that elides part of the file is
indistinguishable from a complete one until something else inspects the tree.

Nothing is removed. Per §4.1 both alternatives remain implemented in the test
module and neither is deleted; the API executor and session mode both remain
supported; no tolerance rung guesses at a patch's location. Unified diff wins
on evidence that is narrow — one adversarial case out of ten — and the
recommendation is correspondingly narrow: it is not a finding that diffs
produce better patches, only that the two located encodings refuse a class of
damage that the unlocated one accepts.

The decision record is `D10` in [`docs/backlog.json`](../backlog.json).

A later measurement against a real provider must append a new dated package
with its route, raw artifacts, costs and a single run denominator, rather than
editing this one. The frequency question this report could not answer is the
one that would change the ranking.
