# Stage B provider-protocol separation report — 2026-08-04

**Status:** Stage B's four §7.3 acceptance criteria are met against a local
scripted transport. No real gateway was contacted; every protocol claim in this
report is a claim about shape, not about a vendor's live behaviour.

## Configuration under test

- Implementation branch: `codex/fit-stage-b`, four commits based on
  `afdc3bc998cfc5f6b0e763782023acf3b860de43`; this report is the fifth. Every
  measurement below was taken against the fourth,
  `06738108e3915934388f7b71d8765a9c44d8a565`.
- Preceding stage reports: `2026-08-04-stage-a-deterministic-slice.md` and
  `2026-08-04-stage-e2-context-selection.md`. This one reuses Stage A's
  transport fixture rather than building a second.
- Test transport: Stage A's in-process `DeterministicTransport`
  (`tests/stage_a_support.py`), with no network call, provider credential,
  remote push or GitHub mutation.
- Route configurations exercised: four, listed below.
- Sleep, clock and jitter: injected. Nothing in the Stage B tests waits on
  provider time or wall-clock time.
- Runtime: Python 3.14, pytest 9.1.1, `uv run` in the worktree's own
  environment.

This is repository-verifiable deterministic evidence. It is not a live gateway
measurement, a cost measurement, or evidence that any named vendor behaves the
way its preset describes.

### The four configurations

| Name | Where it comes from | Wire protocol | Auth | Classifier |
|---|---|---|---|---|
| `generic` | core `protocols.py` | POST the endpoint as configured | `Authorization: Bearer` when a key exists | HTTP only |
| `chat-completions` | `adapters/chat_completions.py`, via entry point | `POST {endpoint}/chat/completions` | bearer | HTTP only |
| `claw-bay` | `adapters/claw_bay.py`, via entry point | chat completions | bearer | vendor envelope |
| `fixture-plugin` | `tests/preset_plugin.py`, via `$HARNESS_ROUTE_PRESETS` | `POST {endpoint}/v2/generate`, `model_id`/`turns` | `x-api-key`, no scheme word | vendor envelope, different key |

`fixture-plugin` is deliberately outside the package. Nothing in `src/` names
it; only an environment variable does.

## Reproduction and result

```console
uv run pytest tests/test_route_conformance.py tests/test_route_presets.py \
              tests/test_adapter_claw_bay.py tests/test_providers.py tests/test_generic.py
uv run pytest tests/test_executor.py tests/test_session_executor.py \
              tests/test_stage_a_e2e.py tests/test_pricing.py
uv run pytest tests/test_cli_roles.py
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Observed on 2026-08-04, at commit `06738108e3915934388f7b71d8765a9c44d8a565`:

- the conformance suite collected **102** cases; the preset-registry suite
  **22**; the moved adapter tests, the core classifier tests and the
  genericness guards **43** between them. All 167 passed in 0.28 seconds: none
  sleeps, opens a socket or starts a subprocess;
- the executor, session-executor, Stage A end-to-end and pricing suites — the
  ones the response-reader change touches — collected **145** and passed in
  112.42 seconds;
- the CLI role/transport suite collected **11** and passed;
- the full repository suite collected **821** tests and passed in **116.37
  seconds** — up from the 676 recorded in the Stage A report;
- lint, formatting and full-project strict typing passed.

One environment note, because it affects reproduction rather than the result.
This machine's `/tmp` sits on a slow shared disk that several other test suites
were hammering, and the harness's tests create a SQLite database per test; a
full run against `/tmp` spent almost all of its time blocked in
`jbd2_log_wait_commit` and did not finish. The run above set `TMPDIR` to a
directory on the local NVMe volume, which is why 116 seconds is comparable with
the Stage A report's 49 rather than with the hour it took not to finish. Nothing
about the code or the tests changed; only where their temporary files went.

## How each §7.3 criterion is proven

**"Core tests pass with only the generic route."**
`test_route_conformance.py::test_the_generic_route_works_with_no_preset_declared_at_all`
empties the registry and stubs entry-point discovery to return nothing, asserts
that `protocols.names() == ["generic"]`, and then runs a call through the retry
ladder: classification, retry count, park state and the `preset`/`protocol`
fields on the events. Separately, no core test module imports an adapter — the
vendor payloads that used to live in `tests/test_providers.py` moved to
`tests/test_adapter_claw_bay.py`, and the core tests that needed a
body-reading classifier now construct `VendorEnvelopeProvider` with neutral
field names.

**"At least two protocol/configuration implementations run through the same
conformance suite."**
Four do. `tests/test_route_conformance.py` parametrises every test over the
table above, so each configuration is asserted against the same six documented
failure shapes and the same usage, cost, wire and event assertions. Three of
the four differ from each other on the wire: `generic` posts to the endpoint as
given, `chat-completions` appends a path, and `fixture-plugin` changes the path,
the payload keys, the credential header, where the reply is and what the token
counts are called.

**"Adding a new configured classifier or protocol does not require a
`model_client.py` code change — prove this with a test that registers a new one
from outside."**
Three tests, three mechanisms:

- `test_route_conformance.py::test_adding_a_configured_protocol_requires_no_core_change`
  builds a preset in the test process, registers it with `protocols.register()`
  and runs a configured route through the call path — classification, park,
  event identity and usage reading all come from the new preset.
- `test_route_presets.py::test_a_preset_named_in_configuration_is_loaded_from_outside_the_package`
  resolves `tests/preset_plugin.py` through `$HARNESS_ROUTE_PRESETS`; the
  `fixture-plugin` row of the conformance table is that same module, so the
  whole suite runs against it.
- `test_route_presets.py::test_the_shipped_adapters_are_reached_by_metadata_not_by_import`
  and `test_resolution_loads_only_the_preset_that_was_named` prove the shipped
  adapters use the same door a third party would: entry-point metadata, with
  only the named preset's module imported.

**"No vendor-specific import is added to core modules."**
`tests/test_generic.py` already asserted that nothing on the execution path
imports `adapters`. Stage B adds `protocols.py` to that list and adds
`test_no_module_on_the_path_names_a_vendor_preset`, which fails on any dotted
adapter module path appearing in a core module — a lazily-imported string being
an import in different clothes. `providers.py` no longer defines a vendor
instance at all: `CLAW_BAY` moved to `adapters/claw_bay.py`, and
`VendorEnvelopeProvider`'s defaults are now empty rather than one gateway's
field names.

### The conformance table, and what it admits

The suite asserts classification *and* reaction for each shape: what was
retried, with what injected backoff and jitter; what was refused; what parked an
endpoint; and what the event said.

| Shape | `generic` / `chat-completions` | `claw-bay` / `fixture-plugin` |
|---|---|---|
| burst limit | `rpm` — retried | `rpm` — retried |
| short spend window | **`rpm` — retried** | `window_cap` — not retried, endpoint parked |
| spend cap | **`rpm` — retried** | `terminal_cap` — not retried, parked longer |
| refusal | **`rpm` — retried** | `non_retryable` — not retried, endpoint kept |
| transient upstream | `transient` — retried | `transient` — retried |
| credential rejected | `terminal_cap` — not retried | `terminal_cap` — not retried |

The bold cells are a limitation, recorded rather than excused. A classifier with
only HTTP to read cannot distinguish a spend cap from going too fast, and the
core preset therefore collapses three shapes into one. The collapse is
deliberate and asymmetric — wrongly retrying a permanent condition costs one
backoff, wrongly giving up on a transient one costs the work — but a deployment
running the generic preset against a metered API inherits exactly the blindness
`providers.py` exists to remove. The table is asserted, so a build cannot claim
a distinction it does not make.

`AGENTS.md`'s "never retry a cost cap" is therefore proven for the two
configurations that can *see* a cost cap, and the two that cannot are proven to
say so.

Pricing and usage, asserted for every configuration: an unknown model keeps its
token counts and gets no price keys at all; a known one records the rate and the
table version alongside them; an unrecognised body produces no usage keys rather
than zeros; and `price_ref` prices a model the table does not name without
inventing a number for one it does not.

## Backward compatibility and migration

`Route.provider` changed from `Provider` to `Provider | None`, and `Route`
gained `preset` and `price_ref`. Every existing construction still works: the
third positional argument is still the classifier.

The stored role map and the `PUT /api/roles` body are unchanged and keep
working. The `provider` field only ever selected a *classifier* — the wire shape
came from whichever transport the deployment had wired in — so it still does
exactly that, and the protocol comes from the deployment default. A map written
before this stage calls the same URL with the same credential and classifies the
same way; `tests/test_route_presets.py::test_the_older_provider_field_still_selects_only_a_classifier`
is that assertion. `preset` is the new spelling and wins where both are given.

Two changes are visible to a caller and are stated here rather than buried:

1. `routes_from_map` / `chains_from_map` no longer default to the vendor
   classifier. A stored route with **no** `provider` and no `preset` now gets
   the generic classifier where it previously got the claw-bay one. The CLI is
   unaffected — it has always written `provider` explicitly — but a hand-written
   map that omitted the field will classify differently. The fix is one field.
2. `providers.CLAW_BAY` and the two-entry `providers.PROVIDERS` mapping no
   longer exist. `PROVIDERS` remains, holding only `generic`; the vendor
   classifier is `agent_harness.adapters.claw_bay.CLASSIFIER`, or the
   `claw-bay` preset by name.

`run` and `serve` gained `--preset` (`$HARNESS_ROUTE_PRESET`), defaulting to
`chat-completions` — the wire shape the CLI transport previously hardcoded. Both
resolve it before anything claims work and refuse with the list of declared
names if it cannot be found, rather than discovering the problem on the first
model call after an item is claimed.

## Costs and blind spots

- **Real vendor behaviour is unmeasured.** The transport is local and scripted.
  No request left the process, no credential was used, and no gateway confirmed
  that the `chat-completions` or `claw-bay` presets describe it correctly. The
  claw-bay classifier's payloads are the ones captured live on 2026-08-02 and
  recorded in the first sustained-run evidence package; the *wire* half of that
  preset — path, headers, reply location — is asserted only against this
  repository's own fixture.
- **Provider tokens, latency and monetary cost: unmeasured.** Every token count
  in these tests is a number the fixture wrote. No money was spent.
- **Session-mode implementer traffic remains invisible (issue #128).** Stage B
  does not change that and does not claim to. A session agent plans and
  implements inside a hosted CLI process with its own credentials and endpoint;
  none of it passes through `ModelClient`, so no route, preset, protocol,
  classifier, token count or cost is recorded for it. Everything in this report
  concerns the direct API path and the reviewer call. Resolving #128 is a
  separate telemetry work item and is **not** resolved here.
- **The entry-point mechanism depends on installed distribution metadata.**
  Verified under `uv run` in this worktree. A stale editable install whose
  `dist-info` predates the `agent_harness.route_presets` group would fail to
  resolve `chat-completions` and `claw-bay`; the CLI refuses at startup with the
  declared names in that case, but no test covers a stale-metadata environment,
  because a test cannot uninstall the package it is running from.
- **The generic preset's request adapter is untested against any real server.**
  It posts to the endpoint exactly as configured, appending no path. That is the
  only assumption available to a preset that claims nothing, and it means a
  generic route configured with a base URL will 404 until an operator gives it a
  path or names a protocol preset. Documented in `INTERNALS.md`; not exercised
  against a live server.
- **Protocol comparison is not attempted.** §7.2 asks the event stream to carry
  enough to compare two protocols, and it now carries `preset`, `protocol` and
  `classifier` on every event. Whether one protocol *performs* better than
  another is Stage E1's question and is unmeasured here.
- The wider suite contains one timing-sensitive session-executor test
  (`test_an_agent_slower_than_the_lease_keeps_its_item`) that failed once during
  this work while several test runs shared the machine, and passed on its own
  and in every subsequent run. It sleeps 1.2s against a 0.3s lease and asserts
  that a second worker finds nothing to claim, so a machine that stalls the
  heartbeat thread long enough will fail it. It is unrelated to Stage B — no
  Stage B code is on its path — and it is recorded here rather than left
  unmentioned.
- **`AGENTS.md` was edited.** Its enumeration of core modules now includes
  `protocols`, and its "where things are" table names the file. That is a
  factual correction under its own rule 7, not a change to any rule.

## Decision

**Stage B's acceptance gate is met. Continue.** All four §7.3 criteria are
proven by named tests, and the existing route configuration keeps working with
the two documented exceptions above.

One honesty note about the test count, since the suite was reorganised as well
as extended. `tests/test_providers.py` went from 22 test functions to 21: the
vendor-envelope assertions moved to `tests/test_adapter_claw_bay.py` (16
functions, 14 of them moved verbatim with the same live-captured payloads and
expectations, 2 new ones about the preset), and the ones that stayed were
rewritten to use a neutrally-configured classifier and joined by two new cases
for the configurable envelope. Nothing was deleted to make a number move. Four
other existing modules changed only their *fixtures* — a configured
`VendorEnvelopeProvider` in place of the vendor instance that no longer lives in
core — without changing what they assert.

What has been earned is a *shape*: a route now names a protocol, an
authentication strategy, a reader and a classifier separately, and a vendor is
addable without editing core. What has not been earned is any claim about a
vendor. Before a preset shipped here is described as working against the gateway
it names, a live run must append a new package recording the endpoint, the
request actually sent, the response actually received, and the classification it
produced — with an immutable artifact for each. This report is not that, and
does not become that by being cited.
