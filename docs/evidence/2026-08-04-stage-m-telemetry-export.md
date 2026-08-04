# Stage M telemetry-export report — 2026-08-04

**Status:** delivered as specified, which is a narrower thing than "the harness
exports telemetry". The **projection** from events to spans exists and is
tested; the **OTLP wire** is a thin lazily-imported step that has never been
run, because the OpenTelemetry SDK is deliberately not installed.

Every claim below is a **repository fact** reproducible from `6c20eea`. There
are no live observations and no collector was ever contacted.

Specification: §10 of
[`PROPOSAL-2026-08-finish-then-extend.md`](../PROPOSAL-2026-08-finish-then-extend.md);
acceptance §10.3. Explicitly off the critical path: it depends only on Stage F
and blocks nothing.

## 1. Configuration under test

| | |
|---|---|
| Branch | `fix/validator-rejects-valid-patches` |
| Base | `8f3ebcc` (Stage J, 1031 tests) |
| Result commit | `6c20eea` |
| New module | `src/agent_harness/adapters/otlp.py` |
| New tests | `tests/test_stage_m_telemetry_export.py`, 32 tests |
| OpenTelemetry SDK | **not installed**, deliberately |
| `TMPDIR` | on the NVMe volume, per risk R6 |
| Network, credentials, collector | none |

| Gate | Result on `6c20eea` |
|---|---|
| `pytest` | **1063 passed** |
| `ruff check .` | all checks passed |
| `ruff format --check .` | 97 files already formatted |
| `mypy` | success, no issues in 93 source files |

1063 − 1031 = 32, this stage's test file exactly.

## 2. Acceptance against §10.3

| Criterion | Result |
|---|---|
| Spans for model calls, gates and item lifecycle, with a run and item identity that joins to the event rows | Passes. Three span kinds; every span carries `harness.run_id`, `harness.seq`, `harness.item_id`, `harness.project_id` and `harness.worker` where the event has them. |
| With the exporter absent or failing, every test still passes and no work stops | Passes, and is asserted by construction: **the entire suite runs with the SDK not installed.** A separate test drives the real demo end to end with `--otel` on and no endpoint, and the item completes. A collector that raises costs one counter increment. |
| The report states exactly what fraction of model traffic is represented, and names session-mode as excluded | §5, and `Coverage.describe()` says it at runtime rather than only here. |

## 3. The projection

Model calls use the OpenTelemetry **GenAI semantic conventions** as far as they
honestly reach — `gen_ai.operation.name`, `gen_ai.request.model`,
`gen_ai.system`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` —
and the ones this harness cannot fill are **absent rather than defaulted**. A
span claiming zero tokens is a measurement saying the call was free, which is
`pricing.py`'s rule applied one layer out, and there is a test for it.

Gates and lifecycle transitions are mapped from an **explicit list** of
outcomes rather than inferred from the name. "Anything containing `check`"
would silently reclassify the next outcome somebody adds.

An event that is none of the three produces **no span**. A trace in which every
log line is a span is a trace nobody reads.

Latency becomes a real interval (`start = ts − latency_s`) rather than a
zero-length span, so a model call has a duration in the trace.

## 4. Export only, and how that is held

**D13's safe answer, taken.** A span is a projection and never a source.
Nothing is written back to the event store, nothing is read back, and no code
path consults a span for anything.

Asserted rather than promised: a test greps `otlp.py` for `EventStore`,
`WorkQueue`, `AuditStore`, `UPDATE ` and `INSERT `. "We would never read it
back" is exactly the sort of thing that stops being true one convenient
afternoon.

Two ordering rules, both tested:

- **The event is written before the span is made.** The event store is the
  source of truth and telemetry must never come between an event and the record
  of it.
- **The downstream sink's exceptions propagate; the exporter's do not.** A
  telemetry wrapper that silently ate a failed write to the record of truth
  would be worse than no telemetry.

No core module reaches for OpenTelemetry. `__main__.py` names the environment
variable in a help string and imports the adapter lazily inside the branch that
asks for it — the same shape `test_generic.py` already blesses for `oxidex` —
and a test pins both halves.

## 5. What fraction of model traffic is represented

**Of traffic that reaches `ModelClient`: all of it.** Every `model_call` event
becomes a span, and `Coverage.exported` counts them.

**Of traffic that does not: none, and it cannot be counted either.**
Session-mode implementer traffic runs inside a hosted CLI session and never
passes through `ModelClient`. It produces no event, so it produces no span, so
it is absent from the numerator *and* the denominator. **Exporting what we have
does not create what we do not have.**

`record_agent_usage` is a door for a session-mode agent to report a call count
it made itself. A call reported that way is counted as `self_reported`, which
**lowers** the exported fraction rather than raising it — a count with no
per-call record is honestly less than a span, and the number should say so.
**Nobody calls that door yet.**

`Coverage.fraction` is `None` when nothing has been observed, not `1.0`.
"Nothing was observed" and "everything was captured" are different facts and
must not share a representation; there is a test.

**#128 is not closed and this stage does not claim it is.** The runtime note
says so in the same words.

## 6. Costs

- **Model cost: zero.** Nothing was contacted.
- **Runtime cost when off (the default):** none. The adapter is not imported.
- **Runtime cost when on with no SDK:** one dictionary build per exportable
  event, discarded. Measured on nothing.
- **Unmeasured:** the cost of a real exporter under load, which is the number
  that would actually matter to a deployment.

## 7. Blind spots

Ordered by how badly each could mislead someone reading §2 as good news.

- **No span has ever reached a collector.** The OTLP half is roughly twenty
  lines: import the SDK, build a `TracerProvider`, start a span, set attributes.
  It is lazily imported inside a `try`, it typechecks against no stubs, and it
  has **never executed**. If it is wrong — a misnamed exporter class, a changed
  constructor signature, a batch processor that needs shutting down — nothing
  here would know. **This is the largest gap in the stage** and it is
  structural: proving it needs the dependency installed, which would end the
  property the rest of the stage rests on.

- **Nothing shuts the tracer down.** `BatchSpanProcessor` buffers, and a process
  that exits without flushing loses whatever is in the buffer. There is no
  `shutdown()` call anywhere, because there is no lifecycle hook to hang one on
  and no way to test that there should be. A short `run` with `--otel` would
  very likely export nothing at all.

- **Spans are discrete, not nested.** Every span is a point or a short
  interval; there is no parent item span holding the model calls and gates
  underneath it, and no trace context propagated anywhere. In a real tracing UI
  this reads as a scatter of unrelated events joined only by attributes. Making
  it a tree needs an item's span held open across a worker death, and a span
  that never ends is worse than a flat one.

- **`--otel` is on `run` only.** `serve` has no equivalent, so the mode most
  likely to be run for days is the one that cannot export.

- **The event-to-span mapping is hand-maintained.** Two dictionaries name the
  outcomes that are gates and the ones that are lifecycle. An outcome added by a
  later stage silently produces no span until somebody adds it, and no test
  fails.

- **`gen_ai.system` is a guess.** It reports the route's classifier or preset
  name, which is what this harness knows. The convention expects a vendor
  identifier, and `generic` or `chat-completions` is not one.

- **The self-reporting door has no caller and no protocol.** `record_agent_usage`
  takes an item id and a count. Nothing tells a session-mode agent that it
  exists, there is no HTTP route to it, and its coverage arithmetic has never
  been exercised against a real agent.

- **Timing is not reported.** `TMPDIR` was on the NVMe volume per R6.

**"No failures observed" is not equivalent to "the requirement was
exercised."** The wire was not exercised, the shutdown path does not exist, and
the self-reporting door has never been opened.

## 8. Continue/stop

**Continue.** §10.3 is met for the projection, which is what the stage
specified, and the wire gap is named rather than implied. It blocks nothing and
should not be treated as making the harness observable until somebody has run
it against a real collector once.
