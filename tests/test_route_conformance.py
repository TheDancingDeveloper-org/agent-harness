"""One conformance suite, run against every route configuration.

Stage A built a deterministic transport that scripts the failure shapes the
evidence package recorded. Stage B turns it into a **classifier conformance
fixture**: the same scripted shapes, run through four different route
configurations, asserting for each one both the classification and the
*reaction* — what was retried, what was refused, what parked an endpoint, and
what the event stream said about it.

The four configurations, deliberately spanning every way a route can be
supplied:

1. **generic** — the core preset. No adapter is loaded. It is included because
   §7.3 requires the core tests to pass with only the generic route, and
   because its *limits* are part of its contract: it collapses spend caps and
   refusals into "going too fast", since nothing in HTTP distinguishes them.
2. **chat-completions** — an adapter preset. Same generic classifier, a
   different wire shape and reader.
3. **claw-bay** — an adapter preset whose classifier reads an error envelope
   and can therefore tell a burst limit from a weekly spend cap.
4. **fixture-plugin** — declared outside the package entirely and named through
   `$HARNESS_ROUTE_PRESETS`. It exists to prove that adding a vendor needs no
   change to `model_client.py`: if this configuration passes the same suite,
   the claim holds.

Each configuration declares what classification it produces for each shape.
That is not a weakening — it *is* the finding. A configuration that cannot
distinguish a cap says so here, in the same table, rather than being quietly
excused.

No test sleeps. Sleep, clock and jitter are injected, so a backoff is a
recorded number rather than a delay.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_harness import protocols
from agent_harness import providers as P
from agent_harness.model_client import (
    CapExhausted,
    EndpointParks,
    ModelClient,
    RequestRefused,
    RetryExhausted,
    RetryPolicy,
    Route,
    routes_from_map,
)
from agent_harness.pricing import Price, PriceTable
from stage_a_support import DeterministicTransport, Reply

MESSAGES = [{"role": "user", "content": "implement it"}]
ENDPOINT = "https://gateway.example/v1"

#: The documented failure shapes, named by what happened rather than by how any
#: one vendor spells it.
BURST = "burst limit"
SHORT_CAP = "short spend window exhausted"
LONG_CAP = "spend cap exhausted"
REFUSAL = "request refused"
UPSTREAM = "transient upstream failure"
CREDENTIAL = "credential rejected"

SHAPES = (BURST, SHORT_CAP, LONG_CAP, REFUSAL, UPSTREAM, CREDENTIAL)


@dataclass(frozen=True)
class Configuration:
    """One route configuration, and how to script a failure in its own shape.

    `produces` is this configuration's honest answer for each documented shape.
    Two of them collapse under a classifier that has only HTTP to read, and
    saying so in the table is the point: the suite then asserts the reaction
    that *follows from the classification*, so a build cannot claim a
    distinction it does not make.
    """

    name: str
    spec: Mapping[str, Any]
    produces: Mapping[str, str]
    body: Callable[[str], tuple[int, str]]
    success_body: Callable[[str, Mapping[str, int] | None], str]
    text_key: str
    env: Mapping[str, str] = field(default_factory=dict)


def _http_only(shape: str) -> tuple[int, str]:
    """A gateway that says nothing beyond a status code.

    Every rejection that is not an authentication failure or a 5xx arrives as a
    bare 429, which is exactly the situation `GenericProvider` exists for and
    exactly the blindness the evidence package recorded.
    """
    if shape == CREDENTIAL:
        return (401, "")
    if shape == UPSTREAM:
        return (503, "")
    return (429, "")


def _envelope(shape: str) -> tuple[int, str]:
    """The claw-bay envelope, in the shapes captured live on 2026-08-02."""
    bodies = {
        BURST: {"theclawbayError": {"category": "rate", "code": "rate_limited", "retryable": True}},
        SHORT_CAP: {
            "theclawbayError": {
                "category": "quota",
                "code": "5h_cost_limit_reached",
                "retryable": False,
            }
        },
        LONG_CAP: {
            "theclawbayError": {
                "category": "quota",
                "code": "weekly_cost_limit_reached",
                "retryable": False,
            }
        },
        REFUSAL: {
            "theclawbayError": {
                "category": "internal",
                "code": "upstream_rejected",
                "retryable": False,
            }
        },
        CREDENTIAL: {"theclawbayError": {"code": "invalid_api_key", "retryable": False}},
    }
    if shape == UPSTREAM:
        return (503, json.dumps({"error": "bad gateway"}))
    return (429, json.dumps(bodies[shape]))


def _plugin_envelope(shape: str) -> tuple[int, str]:
    """A third party's envelope: different key, different vocabulary."""
    bodies = {
        BURST: {"problem": {"category": "rate", "code": "slow_down", "retryable": True}},
        SHORT_CAP: {"problem": {"category": "budget", "code": "daily_budget_spent"}},
        LONG_CAP: {"problem": {"category": "budget", "code": "budget_spent"}},
        REFUSAL: {"problem": {"category": "upstream", "code": "declined", "retryable": False}},
        CREDENTIAL: {"problem": {"category": "identity", "code": "bad_token"}},
    }
    if shape == UPSTREAM:
        return (502, json.dumps({"problem": {"code": "upstream_down"}}))
    return (429, json.dumps(bodies[shape]))


def _chat_success(text: str, usage: Mapping[str, int] | None) -> str:
    payload: dict[str, Any] = {"choices": [{"message": {"content": text}}]}
    if usage is not None:
        payload["usage"] = {"prompt_tokens": usage["in"], "completion_tokens": usage["out"]}
    return json.dumps(payload)


def _plugin_success(text: str, usage: Mapping[str, int] | None) -> str:
    payload: dict[str, Any] = {"result": {"reply": text}}
    if usage is not None:
        payload["counters"] = {"in_units": usage["in"], "out_units": usage["out"]}
    return json.dumps(payload)


HTTP_ONLY_PRODUCES = {
    BURST: P.RPM,
    # Collapsed, and documented as collapsed: nothing in HTTP distinguishes a
    # spend window from going too fast. Retrying a cap wastes one backoff;
    # giving up on a burst limit loses the work, so the safe collapse is `rpm`.
    SHORT_CAP: P.RPM,
    LONG_CAP: P.RPM,
    REFUSAL: P.RPM,
    UPSTREAM: P.TRANSIENT,
    CREDENTIAL: P.TERMINAL_CAP,
}

ENVELOPE_PRODUCES = {
    BURST: P.RPM,
    SHORT_CAP: P.WINDOW_CAP,
    LONG_CAP: P.TERMINAL_CAP,
    REFUSAL: P.NON_RETRYABLE,
    UPSTREAM: P.TRANSIENT,
    CREDENTIAL: P.TERMINAL_CAP,
}

CONFIGURATIONS = (
    Configuration(
        name="generic",
        spec={"model": "a-model", "endpoint": ENDPOINT},
        produces=HTTP_ONLY_PRODUCES,
        body=_http_only,
        success_body=_chat_success,
        # The generic reader knows no text path, so it reads no text. It still
        # reads usage, which is reported in the same place by everything that
        # reports it at all.
        text_key="",
    ),
    Configuration(
        name="chat-completions",
        spec={"model": "a-model", "endpoint": ENDPOINT, "preset": "chat-completions"},
        produces=HTTP_ONLY_PRODUCES,
        body=_http_only,
        success_body=_chat_success,
        text_key="choices",
    ),
    Configuration(
        name="claw-bay",
        spec={"model": "a-model", "endpoint": ENDPOINT, "preset": "claw-bay"},
        produces=ENVELOPE_PRODUCES,
        body=_envelope,
        success_body=_chat_success,
        text_key="choices",
    ),
    Configuration(
        name="fixture-plugin",
        spec={"model": "a-model", "endpoint": ENDPOINT, "preset": "fixture-plugin"},
        produces=ENVELOPE_PRODUCES,
        body=_plugin_envelope,
        success_body=_plugin_success,
        text_key="result",
        env={protocols.PRESET_PATH_ENV: "fixture-plugin=preset_plugin:PRESET"},
    ),
)

IDS = [c.name for c in CONFIGURATIONS]


@pytest.fixture(params=CONFIGURATIONS, ids=IDS)
def config(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """One configuration, with its plugin path set and its registration undone.

    The plugin is named in the environment for exactly as long as the tests
    that use it run, so nothing here can accidentally rely on a preset having
    been left behind by an earlier test.
    """
    configuration: Configuration = request.param
    saved = dict(protocols._REGISTERED)
    for key, value in configuration.env.items():
        monkeypatch.setenv(key, value)
    try:
        yield configuration
    finally:
        protocols._REGISTERED.clear()
        protocols._REGISTERED.update(saved)


@dataclass
class Harness:
    """A client whose clock, sleeping and jitter are all recorded."""

    client: ModelClient
    transport: DeterministicTransport
    events: list[dict[str, Any]]
    sleeps: list[float]
    parks: EndpointParks


def build(
    configuration: Configuration,
    steps: list[Reply],
    *,
    jitter: float = 0.0,
    attempts: int = 3,
    prices: PriceTable | None = None,
) -> Harness:
    route = routes_from_map({"implementer": dict(configuration.spec)})["implementer"]
    transport = DeterministicTransport({"a-model": steps})
    events: list[dict[str, Any]] = []
    sleeps: list[float] = []
    parks = EndpointParks()
    clock = [1000.0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    client = ModelClient(
        roles={"implementer": route, "reviewer": route},
        transport=transport,
        policy=RetryPolicy(max_attempts=attempts, backoff_seconds=2.0),
        on_event=events.append,
        prices=prices if prices is not None else PriceTable(),
        sleep=sleep,
        now=lambda: clock[0],
        jitter=lambda: jitter,
        parks=parks,
    )
    return Harness(client=client, transport=transport, events=events, sleeps=sleeps, parks=parks)


def failure_steps(configuration: Configuration, shape: str, count: int) -> list[Reply]:
    status, body = configuration.body(shape)
    return [Reply(status=status, body=body) for _ in range(count)]


def errors(harness: Harness) -> list[dict[str, Any]]:
    return [e for e in harness.events if e["outcome"] == "error"]


# --------------------------------------------------------- the failure matrix


@pytest.mark.parametrize("shape", SHAPES)
def test_a_failure_shape_is_classified_and_reacted_to(config: Any, shape: str) -> None:
    """The conformance assertion. For each documented failure shape: what did
    this configuration call it, and what did the harness then do about it?"""
    expected = config.produces[shape]
    harness = build(config, failure_steps(config, shape, 6), jitter=0.25)

    with pytest.raises((CapExhausted, RequestRefused, RetryExhausted)) as excinfo:
        harness.client.call("implementer", MESSAGES)

    recorded = errors(harness)
    assert recorded, "a rejection produced no event"
    assert {e["error_class"] for e in recorded} == {expected}

    retried = expected in (P.RPM, P.TRANSIENT)
    if retried:
        # Retried within policy, and every wait went through the injected
        # sleep function rather than the clock.
        assert len(harness.transport.calls) == 3
        assert isinstance(excinfo.value, RetryExhausted)
        # Jitter is applied to the backoff, and it is what stops N workers
        # rejected in the same instant from retrying in the same instant.
        assert harness.sleeps == [2.0 * 1.25, 4.0 * 1.25]
    else:
        # Nothing another attempt could fix. One call, no sleeping at all.
        assert len(harness.transport.calls) == 1
        assert harness.sleeps == []

    if expected in P.CAPS:
        assert isinstance(excinfo.value, CapExhausted)
        assert excinfo.value.kind == expected
        assert harness.parks.remaining(ENDPOINT, 1000.0, "implementer") > 0
    elif expected == P.NON_RETRYABLE:
        assert isinstance(excinfo.value, RequestRefused)
        # A refusal says something about the request, not the endpoint's
        # health. Idling a healthy endpoint over one bad prompt would be a
        # self-inflicted outage.
        assert harness.parks.remaining(ENDPOINT, 1000.0, "implementer") == 0.0
    else:
        assert harness.parks.remaining(ENDPOINT, 1000.0, "implementer") == 0.0


def test_a_spend_cap_is_never_retried(config: Any) -> None:
    """The rule the whole module exists for, asserted separately from the
    matrix because it is the one that must never regress. Retrying cannot make
    budget appear; a ladder that tries is a busy-wait burning quota to check
    whether quota exists.

    Where a configuration cannot *see* the cap, this is honest about it: the
    documented reading is asserted, and the collapse is what the table says.
    """
    harness = build(config, failure_steps(config, LONG_CAP, 20), attempts=20)

    with pytest.raises((CapExhausted, RetryExhausted)):
        harness.client.call("implementer", MESSAGES)

    if config.produces[LONG_CAP] in P.CAPS:
        assert len(harness.transport.calls) == 1
        assert harness.sleeps == []
    else:
        assert config.produces[LONG_CAP] == P.RPM
        assert len(harness.transport.calls) == 20


def test_a_refusal_leaves_a_healthy_endpoint_in_service(config: Any) -> None:
    """Whatever the refusal was called, it did not idle the endpoint.

    A refusal says something about the request, not about the model's health.
    Parking a healthy endpoint over one bad prompt would be a self-inflicted
    outage, so the next role's call goes straight through.
    """
    status, body = config.body(REFUSAL)
    harness = build(
        config,
        [Reply(status=status, body=body), Reply(body=config.success_body("done", None))],
    )

    if config.produces[REFUSAL] == P.NON_RETRYABLE:
        with pytest.raises(RequestRefused):
            harness.client.call("implementer", MESSAGES)
        # Nothing another attempt could fix, so nothing waited for one.
        assert harness.sleeps == []
    else:
        # This configuration cannot see a refusal; it reads as a burst limit
        # and is retried, which costs one backoff and then answers.
        assert config.produces[REFUSAL] == P.RPM
        assert harness.client.call("implementer", MESSAGES).status == 200

    assert harness.parks.remaining(ENDPOINT, 1000.0, "reviewer") == 0.0
    assert harness.client.call("reviewer", MESSAGES).status == 200


def test_a_transient_upstream_failure_is_retried_and_then_succeeds(config: Any) -> None:
    status, body = config.body(UPSTREAM)
    harness = build(
        config,
        [Reply(status=status, body=body), Reply(body=config.success_body("done", None))],
        jitter=0.5,
    )

    assert harness.client.call("implementer", MESSAGES).status == 200
    assert harness.sleeps == [3.0]
    assert [e["outcome"] for e in harness.events] == ["error", "retry_wait", "ok"]


def test_a_rejected_credential_is_terminal(config: Any) -> None:
    """Every later call fails identically until a human replaces the key, so
    the endpoint is parked rather than hammered."""
    harness = build(config, failure_steps(config, CREDENTIAL, 4))

    with pytest.raises(CapExhausted) as excinfo:
        harness.client.call("implementer", MESSAGES)

    assert excinfo.value.kind == P.TERMINAL_CAP
    assert len(harness.transport.calls) == 1
    assert harness.parks.remaining(ENDPOINT, 1000.0, "implementer") > 0


# ------------------------------------------------------------------- events


@pytest.mark.parametrize("shape", SHAPES)
def test_every_event_identifies_the_route_the_protocol_and_the_role(
    config: Any, shape: str
) -> None:
    """A stream that cannot say which protocol produced a failure cannot be
    used to compare two of them, which is the job §7.2 gives it. `provider`
    keeps its original meaning — what classified this — so nothing reading the
    stream today has to change."""
    harness = build(config, failure_steps(config, shape, 6))
    with pytest.raises((CapExhausted, RequestRefused, RetryExhausted)):
        harness.client.call("implementer", MESSAGES)

    preset = protocols.resolve(config.spec.get("preset", ""))
    for event in harness.events:
        assert event["role"] == "implementer"
        assert event["model"] == "a-model"
        assert event["endpoint"] == ENDPOINT
        assert event["preset"] == preset.name
        assert event["protocol"] == preset.request.name
        assert event["classifier"] == preset.classifier.name
        assert event["provider"] == preset.classifier.name
        assert event["outcome"] in {"error", "retry_wait", "ok", "parked", "skipped"}


def test_a_successful_call_records_the_protocol_too(config: Any) -> None:
    harness = build(config, [Reply(body=config.success_body("done", {"in": 11, "out": 3}))])
    harness.client.call("implementer", MESSAGES)

    ok = harness.events[-1]
    assert ok["outcome"] == "ok"
    assert ok["protocol"] == protocols.resolve(config.spec.get("preset", "")).request.name
    assert ok["error_class"] is None


# ------------------------------------------------------------ usage and cost


def test_usage_is_read_by_the_route_and_stays_conservative(config: Any) -> None:
    """Each configuration reads its own field names. A body that reports
    nothing produces no usage keys at all — an event carrying zeros is a claim
    that the call was free, and an event with no usage keys is honestly
    silent."""
    harness = build(config, [Reply(body=config.success_body("done", {"in": 120, "out": 7}))])
    harness.client.call("implementer", MESSAGES)
    counted = harness.events[-1]
    assert counted["tokens_in"] == 120
    assert counted["tokens_out"] == 7

    quiet = build(config, [Reply(body=config.success_body("done", None))])
    quiet.client.call("implementer", MESSAGES)
    silent = quiet.events[-1]
    assert "tokens_in" not in silent
    assert "tokens_out" not in silent


def test_an_unrecognised_body_reports_no_usage_rather_than_zero(config: Any) -> None:
    """Inventing zeros here would understate every total downstream, and
    nothing would ever flag it."""
    for body in ('{"something": "else"}', "not json at all", '{"usage": {"widgets": 4}}'):
        harness = build(config, [Reply(body=body)])
        harness.client.call("implementer", MESSAGES)
        recorded = harness.events[-1]
        assert "tokens_in" not in recorded
        assert "tokens_out" not in recorded


def test_an_unknown_price_stays_unknown_and_never_becomes_zero(config: Any) -> None:
    """A missing price is not a price of zero. The event carries tokens with no
    cost at all, so the API counts it as `unpriced` rather than adding nothing
    to a total that then looks complete."""
    harness = build(config, [Reply(body=config.success_body("done", {"in": 100, "out": 10}))])
    harness.client.call("implementer", MESSAGES)

    recorded = harness.events[-1]
    assert recorded["tokens_in"] == 100
    assert "price_in_per_mtok" not in recorded
    assert "price_out_per_mtok" not in recorded
    assert "price_table" not in recorded


def test_a_known_price_is_recorded_with_the_table_that_produced_it(config: Any) -> None:
    """Prices change. Recording the rate that was applied turns a repricing
    into a visible step in the series rather than an invisible retroactive
    edit."""
    table = PriceTable(version="2026-08-01", prices={"a-model": Price(3.0, 15.0)})
    harness = build(
        config, [Reply(body=config.success_body("done", {"in": 100, "out": 10}))], prices=table
    )
    harness.client.call("implementer", MESSAGES)

    recorded = harness.events[-1]
    assert recorded["price_in_per_mtok"] == 3.0
    assert recorded["price_table"] == "2026-08-01"


def test_a_price_reference_prices_a_model_the_table_does_not_name(config: Any) -> None:
    """A model is priced under the name configuration gives it, and left
    unpriced when nothing names it. Neither case invents a number."""
    table = PriceTable(version="2026-08-01", prices={"catalogue/tier-2": Price(1.0, 2.0)})
    spec = {**config.spec, "model": "a-model", "price_ref": "catalogue/tier-2"}
    route = routes_from_map({"implementer": spec})["implementer"]

    assert route.pricing_key == "catalogue/tier-2"
    assert table.price_for(route.pricing_key) is not None
    # ...and the same model, with nothing naming it, stays unpriced rather
    # than picking up a price that was never meant for it.
    assert table.price_for(Route("a-model", ENDPOINT).pricing_key) is None


# --------------------------------------------------------------- the wire


def test_each_configuration_renders_its_own_request(config: Any) -> None:
    """The request shape is the preset's, not the transport's. This is the half
    of Stage B that was previously hardcoded in one CLI function."""
    route = routes_from_map({"implementer": dict(config.spec)})["implementer"]
    preset = route.resolve()
    request = preset.request.render(route, MESSAGES, {"temperature": 0.2, "role": "implementer"})

    assert request.url.startswith(ENDPOINT)
    # Transport instructions never reach the model as completion parameters.
    assert "role" not in request.payload
    assert request.payload["temperature"] == 0.2
    assert list(MESSAGES) in request.payload.values()
    assert "a-model" in request.payload.values()

    headers = preset.auth.headers(route, "a-credential")
    assert any("a-credential" in value for value in headers.values())


def test_the_reader_finds_the_text_where_that_protocol_puts_it(config: Any) -> None:
    preset = protocols.resolve(config.spec.get("preset", ""))
    body = config.success_body("the answer", None)

    if config.text_key:
        assert preset.reader.text(body) == "the answer"
    else:
        # The generic preset declines rather than guessing, and says so.
        assert preset.reader.text(body) is None


def test_the_executor_reads_the_reply_through_the_route(config: Any) -> None:
    """The reader is load-bearing, not decorative: what the executor hands to
    its diff-apply ladder comes from the route's own reader. A preset whose
    gateway puts the reply somewhere unusual therefore works end to end without
    a change to the executor."""
    from agent_harness.executor import _reader_for, _text_of

    harness = build(config, [Reply(body=config.success_body("the answer", None))])
    response = harness.client.call("implementer", MESSAGES)

    assert _text_of(response.body, _reader_for(harness.client, "implementer")) == "the answer"


# ------------------------------------------------- the generic route, alone


def test_the_generic_route_works_with_no_preset_declared_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7.3's first criterion, asserted rather than assumed: with discovery
    returning nothing and no registration in this process, the generic route
    still classifies, retries, parks and reports."""
    monkeypatch.setattr(protocols, "_declared_targets", dict)
    monkeypatch.delenv(protocols.PRESET_PATH_ENV, raising=False)
    saved = dict(protocols._REGISTERED)
    protocols._REGISTERED.clear()
    try:
        assert protocols.names() == ["generic"]
        harness = build(CONFIGURATIONS[0], failure_steps(CONFIGURATIONS[0], BURST, 4))

        with pytest.raises(RetryExhausted):
            harness.client.call("implementer", MESSAGES)

        assert len(harness.transport.calls) == 3
        assert {e["error_class"] for e in errors(harness)} == {P.RPM}
        assert harness.events[0]["preset"] == "generic"
        assert harness.events[0]["protocol"] == "json-chat"
    finally:
        protocols._REGISTERED.update(saved)


def test_adding_a_configured_protocol_requires_no_core_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7.3's third criterion. The preset below is registered from outside any
    core module, by a caller that could equally be a third party's package, and
    a route configured with it runs the same call path. Nothing in
    `model_client.py` knows it exists.
    """
    invented = protocols.RoutePreset(
        name="invented-at-runtime",
        request=protocols.JsonChatRequest(name="invented", path="/answer", model_key="engine"),
        auth=protocols.BearerAuth(name="token-header", header="x-token", scheme=""),
        reader=protocols.JsonResponseReader(
            name="invented", text_paths=("out",), usage_key="spend", tokens_in_keys=("read",)
        ),
        classifier=P.VendorEnvelopeProvider(name="invented", vendor_field="why"),
    )
    saved = dict(protocols._REGISTERED)
    protocols.register(invented)
    try:
        configuration = Configuration(
            name="invented-at-runtime",
            spec={"model": "a-model", "endpoint": ENDPOINT, "preset": "invented-at-runtime"},
            produces=ENVELOPE_PRODUCES,
            body=lambda shape: (
                429,
                json.dumps({"why": {"category": "quota", "code": "weekly_cost_limit_reached"}}),
            ),
            success_body=lambda text, usage: json.dumps({"out": text, "spend": {"read": 5}}),
            text_key="out",
        )
        harness = build(configuration, failure_steps(configuration, LONG_CAP, 4))

        with pytest.raises(CapExhausted):
            harness.client.call("implementer", MESSAGES)

        assert len(harness.transport.calls) == 1
        assert harness.events[0]["preset"] == "invented-at-runtime"
        assert harness.events[0]["protocol"] == "invented"
        assert harness.events[0]["classifier"] == "invented"

        answered = build(configuration, [Reply(body='{"out": "hi", "spend": {"read": 5}}')])
        answered.client.call("implementer", MESSAGES)
        assert answered.events[-1]["tokens_in"] == 5
    finally:
        protocols._REGISTERED.clear()
        protocols._REGISTERED.update(saved)
