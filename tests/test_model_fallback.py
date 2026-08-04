"""A role can name more than one model, and the first that answers wins.

Measured against the endpoint this runs on: 34 of 42 advertised models were
unavailable simultaneously, including two of the three an operator had chosen
for implementation. A role with one name per model is a fleet that stops when
that name is down — so a role holds an ordered chain, and the alternatives are
tried before any backoff, because a dead provider answers instantly and
sleeping on it first would waste the fallback entirely.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from agent_harness import providers as P
from agent_harness.model_client import (
    CapExhausted,
    ModelClient,
    RequestRefused,
    Response,
    RetryExhausted,
    RetryPolicy,
    Route,
    chains_from_map,
)

ENDPOINT = "https://api.example/v1"


def chain(*models: str) -> tuple[Route, ...]:
    return tuple(Route(m, ENDPOINT, P.CLAW_BAY, options={"role": "implementer"}) for m in models)


class Scripted:
    """Answers per model, and records the order it was asked."""

    def __init__(self, answers: Mapping[str, int | Exception]) -> None:
        self.answers = dict(answers)
        self.asked: list[str] = []

    def __call__(
        self, route: Route, messages: Sequence[Mapping[str, Any]], options: Mapping[str, Any]
    ) -> Response:
        self.asked.append(route.model)
        status = self.answers.get(route.model, 200)
        if isinstance(status, Exception):
            raise status
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]})
        # A claw-bay upstream failure carries a JSON body naming the reason;
        # the classifier reads it, so the fixture has to produce one.
        if status >= 400:
            body = json.dumps({"error": "model service unavailable", "code": "upstream_rejected"})
        return Response(status, {}, body)


def build(transport: Scripted, *models: str, attempts: int = 2) -> ModelClient:
    return ModelClient(
        roles={"implementer": chain(*models)},
        transport=transport,
        policy=RetryPolicy(max_attempts=attempts, backoff_seconds=0.001),
        sleep=lambda _s: None,
    )


def call(client: ModelClient) -> Response:
    return client.call("implementer", [{"role": "user", "content": "hi"}])


def test_the_preferred_model_is_used_when_it_answers() -> None:
    transport = Scripted({})
    client = build(transport, "deepseek-v4-flash", "glm-5.2", "gpt-5.4")

    assert call(client).status == 200
    assert transport.asked == ["deepseek-v4-flash"], "a healthy first choice must not be skipped"


def test_an_unavailable_model_falls_through_to_the_next() -> None:
    """The live case: two of three down, one answering."""
    transport = Scripted({"deepseek-v4-flash": 499, "glm-5.2": 503})
    client = build(transport, "deepseek-v4-flash", "glm-5.2", "gpt-5.4")

    assert call(client).status == 200
    assert transport.asked == ["deepseek-v4-flash", "glm-5.2", "gpt-5.4"]


def test_the_whole_chain_is_tried_before_any_backoff() -> None:
    """A dead provider answers in milliseconds. Backing off against it before
    looking at the second choice would spend minutes to learn nothing."""
    slept: list[float] = []
    transport = Scripted({"a": 503})
    client = ModelClient(
        roles={"implementer": chain("a", "b")},
        transport=transport,
        policy=RetryPolicy(max_attempts=3, backoff_seconds=10.0),
        sleep=slept.append,
    )

    assert call(client).status == 200
    assert transport.asked == ["a", "b"]
    assert slept == [], "the fallback was reached only after a backoff"


def test_a_refusal_still_tries_the_next_model() -> None:
    """A refusal is about this request, and one vendor's refusal is routinely
    another's answer."""
    transport = Scripted({"a": 400})
    client = build(transport, "a", "b")

    assert call(client).status == 200
    assert transport.asked == ["a", "b"]


def test_a_refusal_does_not_park_the_model_it_came_from() -> None:
    """Falling back is not the same as declaring a model unhealthy. Parking a
    working endpoint over one bad prompt would be a self-inflicted outage."""
    transport = Scripted({"a": 400})
    client = build(transport, "a", "b")
    call(client)

    assert client.parks.remaining(ENDPOINT, client.now(), "implementer") == 0


def test_when_every_route_refuses_the_refusal_is_raised() -> None:
    transport = Scripted({"a": 400, "b": 400})
    client = build(transport, "a", "b")

    with pytest.raises(RequestRefused) as caught:
        call(client)
    assert "a" in str(caught.value) and "b" in str(caught.value)


def test_when_every_route_is_out_of_budget_the_cap_is_raised() -> None:
    """`CapExhausted` is what hands an item back untouched, so it must survive
    the chain rather than being flattened into a generic failure."""
    transport = Scripted({"a": 403, "b": 403})
    client = build(transport, "a", "b")

    with pytest.raises(CapExhausted):
        call(client)


def test_a_transient_failure_everywhere_is_retried_then_given_up_on() -> None:
    transport = Scripted({"a": 503, "b": 503})
    client = build(transport, "a", "b", attempts=2)

    with pytest.raises(RetryExhausted):
        call(client)
    # Two cycles over two routes.
    assert transport.asked == ["a", "b", "a", "b"]


def test_a_parked_route_is_skipped_when_another_endpoint_can_serve() -> None:
    """With somewhere else to go, sleeping out a park is time spent for no
    reason."""
    slept: list[float] = []
    transport = Scripted({})
    elsewhere = "https://other.example/v1"
    client = ModelClient(
        roles={
            "implementer": (
                Route("a", ENDPOINT, P.CLAW_BAY, options={"role": "implementer"}),
                Route("b", elsewhere, P.CLAW_BAY, options={"role": "implementer"}),
            )
        },
        transport=transport,
        policy=RetryPolicy(max_attempts=2, backoff_seconds=0.001),
        sleep=slept.append,
    )
    client.parks.park(ENDPOINT, 600.0, client.now(), "implementer")

    assert call(client).status == 200
    assert transport.asked == ["b"], "the parked route should have been skipped"
    assert slept == []


def test_models_on_one_endpoint_share_its_park() -> None:
    """Worth pinning, because it bounds what a fallback chain can do.

    A spend cap belongs to the account, not the model, so parking the endpoint
    parks every model behind it — which is right, and means a chain of models
    on a single provider is insurance against *that model* being unavailable,
    not against running out of budget.
    """
    transport = Scripted({})
    client = build(transport, "a", "b")
    client.parks.park(ENDPOINT, 600.0, client.now(), "implementer")

    with pytest.raises(CapExhausted):
        call(client)
    assert transport.asked == [], "nothing should have been called on a parked endpoint"


def test_falling_back_is_recorded() -> None:
    """A fleet quietly running on its third choice for a week is a fleet whose
    results nobody can explain."""
    events: list[dict[str, Any]] = []
    transport = Scripted({"a": 503})
    client = ModelClient(
        roles={"implementer": chain("a", "b")},
        transport=transport,
        policy=RetryPolicy(max_attempts=2, backoff_seconds=0.001),
        sleep=lambda _s: None,
        on_event=events.append,
    )
    call(client)

    ok = [e for e in events if e["outcome"] == "ok"]
    assert ok and ok[0]["model"] == "b"
    assert "fell back to b" in (ok[0].get("detail") or "")


def test_the_preferred_route_is_what_is_reported() -> None:
    """Readiness, the role map and independence all describe configuration,
    not whichever alternative happened to answer."""
    client = build(Scripted({}), "deepseek-v4-flash", "glm-5.2")

    assert client.route_for("implementer").model == "deepseek-v4-flash"
    assert [r.model for r in client.routes_for("implementer")] == ["deepseek-v4-flash", "glm-5.2"]


# ------------------------------------------------------------- the stored map


def test_a_stored_map_can_name_several_models() -> None:
    chains = chains_from_map(
        {
            "implementer": {
                "models": ["deepseek-v4-flash", "glm-5.2", "gpt-5.4"],
                "endpoint": ENDPOINT,
            }
        }
    )

    assert [r.model for r in chains["implementer"]] == [
        "deepseek-v4-flash",
        "glm-5.2",
        "gpt-5.4",
    ]


def test_a_map_written_before_fallbacks_existed_still_reads() -> None:
    """`model` as a single name is the one-element chain it always was."""
    chains = chains_from_map({"reviewer": {"model": "gpt-5.6", "endpoint": ENDPOINT}})

    assert [r.model for r in chains["reviewer"]] == ["gpt-5.6"]


def test_a_role_with_no_endpoint_is_dropped_rather_than_half_built() -> None:
    assert chains_from_map({"reviewer": {"models": ["gpt-5.6"]}}) == {}
