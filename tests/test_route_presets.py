"""How a route is configured, and how a vendor is added without editing core.

Stage B's claim has two halves. The first is that a route names four separable
things — a wire protocol, an authentication strategy, a response reader and a
failure classifier — rather than conflating "which vendor" with "how do its
failures read". The second is that adding one requires no change to
`model_client.py`.

The second half is the one that is easy to believe and hard to keep, so it is
asserted three ways: by registering a preset in this process, by naming one in
configuration that lives outside the package, and by resolving the two the
distribution itself declares through entry-point metadata that no core module
imports.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from agent_harness import protocols
from agent_harness import providers as P
from agent_harness.model_client import Route, chains_from_map, routes_from_map
from agent_harness.protocols import (
    BearerAuth,
    JsonChatRequest,
    JsonResponseReader,
    RoutePreset,
    UnknownPreset,
)

MESSAGES = [{"role": "user", "content": "hello"}]


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """Registrations are process-global; a test must not leak one."""
    saved = dict(protocols._REGISTERED)
    try:
        yield
    finally:
        protocols._REGISTERED.clear()
        protocols._REGISTERED.update(saved)


def a_preset(name: str = "invented") -> RoutePreset:
    return RoutePreset(
        name=name,
        request=JsonChatRequest(name="invented-wire", path="/invented"),
        auth=BearerAuth(name="invented-key", header="x-invented", scheme=""),
        reader=JsonResponseReader(name="invented", text_paths=("said",)),
        classifier=P.VendorEnvelopeProvider(name=name, vendor_field="oops"),
    )


# ------------------------------------------------------------- the default


def test_the_default_route_is_generic_and_claims_nothing() -> None:
    """A route that names no preset gets one that cannot be wrong about a
    vendor, because it makes no claim about one."""
    route = Route("a-model", "https://api.example/v1/chat")

    preset = route.resolve()
    assert preset is protocols.GENERIC_PRESET
    assert route.classifier is P.GENERIC
    # The endpoint as configured, with nothing appended to it.
    request = preset.request.render(route, MESSAGES, {})
    assert request.url == "https://api.example/v1/chat"
    assert request.payload == {"model": "a-model", "messages": MESSAGES}


def test_the_generic_reader_says_nothing_rather_than_guessing() -> None:
    """There is no vendor-neutral place for assistant text to be. Reporting an
    empty answer from a shape we have never seen would turn a parsing gap into
    a silent refusal, so the generic reader declines and the caller keeps the
    raw body."""
    body = '{"choices": [{"message": {"content": "hi"}}]}'
    assert protocols.GENERIC_PRESET.reader.text(body) is None
    assert protocols.resolve("chat-completions").reader.text(body) == "hi"


def test_a_credential_is_sent_only_when_there_is_one() -> None:
    """`Authorization: Bearer` with nothing after it is a malformed request,
    not an anonymous one — and a local server wanting no auth is a supported
    configuration."""
    route = Route("m", "https://local.invalid")
    auth = protocols.GENERIC_PRESET.auth
    assert auth.headers(route, None) == {}
    assert auth.headers(route, "secret") == {"Authorization": "Bearer secret"}
    assert auth.headers(Route("m", "e", api_key="own"), "shared") == {"Authorization": "Bearer own"}


# ------------------------------------------------------------ registration


def test_a_preset_registered_in_process_is_resolvable_by_name() -> None:
    protocols.register(a_preset())
    assert protocols.resolve("invented").request.name == "invented-wire"
    assert "invented" in protocols.names()


def test_two_packages_cannot_quietly_claim_one_name() -> None:
    """Import order deciding a fleet's wire shape is not a thing anybody can
    debug."""
    protocols.register(a_preset())
    with pytest.raises(ValueError, match="already registered"):
        protocols.register(a_preset().__class__(**{**a_preset().__dict__, "summary": "different"}))
    # Shadowing is allowed, but only when it is asked for.
    protocols.register(a_preset(), replace=True)


def test_the_generic_preset_cannot_be_shadowed_by_accident() -> None:
    with pytest.raises(ValueError, match="built-in"):
        protocols.register(a_preset(name="generic"))


def test_a_preset_named_in_configuration_is_loaded_from_outside_the_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tests/preset_plugin.py` stands in for a third party's module. Nothing
    in `src/` names it; only this environment variable does."""
    monkeypatch.setenv(protocols.PRESET_PATH_ENV, "fixture-plugin=preset_plugin:PRESET")

    preset = protocols.resolve("fixture-plugin")

    assert preset.request.name == "fixture-generate"
    assert preset.auth.headers(Route("m", "e"), "k") == {"x-api-key": "k"}
    assert "fixture-plugin" in protocols.names()


def test_a_malformed_configuration_entry_is_ignored_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(protocols.PRESET_PATH_ENV, "no-target,fixture=preset_plugin:PRESET")
    assert protocols.resolve("fixture").name == "fixture-plugin"


def test_a_plugin_that_will_not_load_is_a_named_failure_not_a_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quietly substituting a different wire shape would send requests to a URL
    nobody configured, and the only symptom would be failures the classifier
    cannot explain."""
    monkeypatch.setenv(protocols.PRESET_PATH_ENV, "broken=preset_plugin:NOT_THERE")
    with pytest.raises(UnknownPreset, match="broken"):
        protocols.resolve("broken")


def test_an_unknown_preset_names_the_ones_that_exist() -> None:
    with pytest.raises(UnknownPreset) as excinfo:
        protocols.resolve("typo")
    assert "generic" in str(excinfo.value)
    assert protocols.PRESET_PATH_ENV in str(excinfo.value)


def test_the_shipped_adapters_are_reached_by_metadata_not_by_import() -> None:
    """The distribution declares them as entry points. Core resolves the name;
    the module is imported at that moment and not before."""
    assert {"chat-completions", "claw-bay"} <= set(protocols.names())
    assert protocols.resolve("chat-completions").request.name == "chat-completions"
    assert protocols.resolve("claw-bay").classifier.name == "claw-bay"


def test_resolution_loads_only_the_preset_that_was_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build with twenty adapters installed imports the one a route asks
    for. Reporting is what loads everything, and reporting is not routing."""
    loaded: list[str] = []
    real = protocols._load_target

    def spy(name: str, target: str) -> RoutePreset:
        loaded.append(name)
        return real(name, target)

    monkeypatch.setattr(protocols, "_load_target", spy)
    # A resolved preset is remembered, so a role called on every item does not
    # re-import. Forget it first, or this asserts nothing about the first call.
    protocols._REGISTERED.pop("claw-bay", None)
    protocols.resolve("claw-bay")
    assert loaded == ["claw-bay"]


# -------------------------------------------------------------- suggestion


def test_an_endpoint_host_produces_a_suggestion_and_nothing_else() -> None:
    hint = protocols.suggest("https://api.theclawbay.com/v1")
    assert hint is not None
    assert hint.preset == "claw-bay"
    assert "Nothing has been chosen" in hint.why
    # ...and the route built from that endpoint is still the generic one.
    assert Route("m", "https://api.theclawbay.com/v1").resolve() is protocols.GENERIC_PRESET


def test_an_unrecognised_host_suggests_nothing() -> None:
    assert protocols.suggest("https://models.example/v1") is None
    assert protocols.suggest("not-a-url") is None


def test_routing_never_consults_the_suggestion(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hard rule. Detection from a hostname must never choose a protocol or
    a classifier: hosts are proxied, renamed, self-hosted and shared, and a
    protocol nobody configured is one nobody can audit."""

    def explode(endpoint: str) -> Any:
        raise AssertionError(f"routing consulted the host suggestion for {endpoint}")

    monkeypatch.setattr(protocols, "suggest", explode)
    routes = routes_from_map(
        {"reviewer": {"model": "m", "endpoint": "https://api.theclawbay.com/v1"}}
    )
    assert routes["reviewer"].resolve() is protocols.GENERIC_PRESET
    assert routes["reviewer"].classifier is P.GENERIC


# ------------------------------------------------- configuration, and its past


def test_a_stored_route_names_a_whole_preset() -> None:
    routes = routes_from_map(
        {"planner": {"model": "m", "endpoint": "https://e", "preset": "claw-bay"}}
    )
    route = routes["planner"]
    assert route.preset_name == "claw-bay"
    assert route.classifier.name == "claw-bay"
    assert route.resolve().request.render(route, MESSAGES, {}).url == "https://e/chat/completions"


def test_the_older_provider_field_still_selects_only_a_classifier() -> None:
    """It never chose a wire shape — the transport did — so it still does not.
    A map written before this stage calls the same URL with the same credential
    and classifies its failures the same way."""
    routes = routes_from_map(
        {"planner": {"model": "m", "endpoint": "https://e", "provider": "claw-bay"}},
        default_preset="chat-completions",
    )
    route = routes["planner"]

    assert route.classifier.name == "claw-bay"
    assert route.preset_name == "chat-completions"
    assert route.resolve().request.render(route, MESSAGES, {}).url == "https://e/chat/completions"


def test_a_preset_field_wins_over_the_older_provider_field() -> None:
    routes = routes_from_map(
        {
            "planner": {
                "model": "m",
                "endpoint": "https://e",
                "provider": "generic",
                "preset": "claw-bay",
            }
        }
    )
    assert routes["planner"].classifier.name == "claw-bay"


def test_an_unknown_preset_name_in_a_stored_map_is_loud_but_not_fatal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A role map is edited live through the API. One misspelling must not take
    down the readiness report that would show the operator what they typed."""
    with caplog.at_level("WARNING"):
        routes = routes_from_map(
            {"planner": {"model": "m", "endpoint": "https://e", "provider": "claw-by"}}
        )

    assert routes["planner"].classifier is P.GENERIC
    assert "claw-by" in caplog.text


def test_a_price_reference_is_carried_from_configuration() -> None:
    routes = routes_from_map(
        {
            "planner": {
                "model": "vendor-name-2026-08",
                "endpoint": "https://e",
                "price_ref": "family",
            }
        }
    )
    assert routes["planner"].pricing_key == "family"
    assert Route("m", "e").pricing_key == "m"


def test_every_route_in_a_chain_carries_the_same_preset() -> None:
    chains = chains_from_map(
        {"implementer": {"models": ["a", "b"], "endpoint": "https://e", "preset": "claw-bay"}}
    )
    assert [r.preset_name for r in chains["implementer"]] == ["claw-bay", "claw-bay"]


def test_a_deployment_default_applies_only_where_a_route_is_silent() -> None:
    stored = {
        "planner": {"model": "p", "endpoint": "https://e"},
        "reviewer": {"model": "r", "endpoint": "https://e", "preset": "generic"},
    }
    routes = routes_from_map(stored, default_preset="chat-completions")
    assert routes["planner"].preset_name == "chat-completions"
    assert routes["reviewer"].preset_name == "generic"
