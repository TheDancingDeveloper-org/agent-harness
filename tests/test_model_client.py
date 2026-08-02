"""ModelClient tests. No network, no sleeping — sleep, clock and jitter are
all injected."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from agent_harness import providers as P
from agent_harness.model_client import (
    CapExhausted,
    EndpointParks,
    ModelClient,
    RequestRefused,
    Response,
    RetryPolicy,
    Route,
)

MESSAGES = [{"role": "user", "content": "x"}]


def ok(body: str = '{"ok": true}') -> Response:
    return Response(200, {}, body)


def fail(status: int, body: object = None, headers: Mapping[str, str] | None = None) -> Response:
    import json

    return Response(status, headers or {}, json.dumps(body) if body is not None else "")


class Recorder:
    """A transport that replays a scripted sequence and records calls."""

    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.calls: list[Route] = []

    def __call__(
        self, route: Route, messages: Sequence[Mapping[str, Any]], options: Mapping[str, Any]
    ) -> Response:
        self.calls.append(route)
        if not self.responses:
            raise AssertionError("transport called more times than scripted")
        return self.responses.pop(0)


def build(
    transport: Recorder,
    *,
    roles: dict[str, Route] | None = None,
    policy: RetryPolicy | None = None,
    jitter: float = 0.0,
    events: list[dict[str, Any]] | None = None,
    parks: EndpointParks | None = None,
    sleeps: list[float] | None = None,
) -> ModelClient:
    clock = [1000.0]

    def now() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)
        clock[0] += seconds

    return ModelClient(
        roles=roles or {"implementer": Route("m", "https://a", P.CLAW_BAY)},
        transport=transport,
        policy=policy or RetryPolicy(max_attempts=4),
        on_event=(events.append if events is not None else None),
        sleep=sleep,
        now=now,
        jitter=lambda: jitter,
        parks=parks,
    )


# ------------------------------------------------------------ role routing


def test_the_call_site_names_a_role_never_a_model() -> None:
    transport = Recorder(ok())
    client = build(
        transport,
        roles={
            "planner": Route("strong-model", "https://a"),
            "implementer": Route("cheap-model", "https://a"),
        },
    )
    client.call("planner", MESSAGES)
    assert transport.calls[0].model == "strong-model"


def test_the_role_map_can_be_swapped_without_touching_the_call_site() -> None:
    """This is what makes the map changeable at runtime rather than at
    deploy time."""
    transport = Recorder(ok(), ok())
    client = build(transport, roles={"implementer": Route("v1", "https://a")})
    client.call("implementer", MESSAGES)
    client.roles["implementer"] = Route("v2", "https://a")
    client.call("implementer", MESSAGES)
    assert [c.model for c in transport.calls] == ["v1", "v2"]


def test_an_unknown_role_names_the_roles_that_do_exist() -> None:
    client = build(Recorder(ok()))
    with pytest.raises(KeyError, match="implementer"):
        client.call("reviewer", MESSAGES)


# ------------------------------------------------------------------ caps


def test_a_spend_cap_is_never_retried() -> None:
    """The whole point. Retrying cannot make budget appear."""
    transport = Recorder(
        fail(
            429,
            {
                "code": "weekly_cost_limit_reached",
                "theclawbayError": {
                    "category": "quota",
                    "code": "weekly_cost_limit_reached",
                    "retryable": False,
                },
            },
        )
    )
    client = build(transport, policy=RetryPolicy(max_attempts=100))
    with pytest.raises(CapExhausted) as excinfo:
        client.call("implementer", MESSAGES)
    assert excinfo.value.kind == P.TERMINAL_CAP
    assert len(transport.calls) == 1


def test_a_cap_parks_only_that_endpoint() -> None:
    parks = EndpointParks()
    transport = Recorder(
        fail(
            429,
            {
                "theclawbayError": {
                    "category": "quota",
                    "code": "weekly_cost_limit_reached",
                    "retryable": False,
                },
            },
        )
    )
    client = build(
        transport,
        parks=parks,
        roles={"implementer": Route("m", "https://capped", P.CLAW_BAY)},
        policy=RetryPolicy(terminal_cap_park_seconds=1800),
    )
    with pytest.raises(CapExhausted):
        client.call("implementer", MESSAGES)
    assert parks.remaining("https://capped", 1000.0) == pytest.approx(1800, abs=1)
    assert parks.remaining("https://other", 1000.0) == 0.0


def test_a_short_window_cap_parks_for_less_than_a_long_one() -> None:
    parks = EndpointParks()
    transport = Recorder(
        fail(
            429,
            {
                "theclawbayError": {
                    "category": "quota",
                    "code": "5h_cost_limit_reached",
                    "retryable": False,
                },
            },
        )
    )
    client = build(
        transport,
        parks=parks,
        policy=RetryPolicy(window_cap_park_seconds=300, terminal_cap_park_seconds=3600),
    )
    with pytest.raises(CapExhausted) as excinfo:
        client.call("implementer", MESSAGES)
    assert excinfo.value.kind == P.WINDOW_CAP
    assert parks.remaining("https://a", 1000.0) == pytest.approx(300, abs=1)


def test_a_parked_endpoint_is_waited_out_not_hammered() -> None:
    parks = EndpointParks()
    parks.park("https://a", 900, now=1000.0)
    sleeps: list[float] = []
    client = build(Recorder(ok()), parks=parks, sleeps=sleeps)
    client.call("implementer", MESSAGES)
    assert sleeps == [900.0]


# ---------------------------------------------------- refused, but healthy


def test_a_refused_request_does_not_park_the_endpoint() -> None:
    """Nothing is exhausted, so idling a healthy endpoint for an hour would
    be the wrong reaction to one bad request."""
    parks = EndpointParks()
    transport = Recorder(
        fail(
            429,
            {
                "theclawbayError": {
                    "category": "internal",
                    "code": "upstream_rejected",
                    "retryable": False,
                },
            },
        )
    )
    client = build(transport, parks=parks)
    with pytest.raises(RequestRefused) as excinfo:
        client.call("implementer", MESSAGES)
    assert excinfo.value.kind == P.NON_RETRYABLE
    assert parks.remaining("https://a", 1000.0) == 0.0


def test_a_fatal_request_error_is_not_retried() -> None:
    transport = Recorder(fail(400, {"error": "bad request"}))
    client = build(transport, policy=RetryPolicy(max_attempts=10))
    with pytest.raises(RequestRefused):
        client.call("implementer", MESSAGES)
    assert len(transport.calls) == 1


# -------------------------------------------------------------- retrying


def test_a_burst_limit_is_retried_then_succeeds() -> None:
    transport = Recorder(fail(429, {"error": {"message": "Rate limit reached"}}), ok())
    client = build(transport)
    assert client.call("implementer", MESSAGES).status == 200
    assert len(transport.calls) == 2


def test_5xx_is_retried() -> None:
    transport = Recorder(fail(503), fail(503), ok())
    client = build(transport)
    assert client.call("implementer", MESSAGES).status == 200


def test_the_backoff_curve_is_capped_but_the_jitter_is_not() -> None:
    """Capping the jittered value would put every capped worker back in
    lockstep — the phase-lock the jitter exists to break."""
    policy = RetryPolicy(backoff_seconds=10, max_backoff_seconds=25, max_attempts=5)
    assert policy.delay_for(1, None, 0.0) == 10
    assert policy.delay_for(2, None, 0.0) == 20
    assert policy.delay_for(3, None, 0.0) == 25  # curve capped
    assert policy.delay_for(3, None, 1.0) == 50  # jitter rides above the cap


def test_the_provider_retry_after_beats_the_computed_curve() -> None:
    policy = RetryPolicy(backoff_seconds=2)
    assert policy.delay_for(1, 240.0, 0.0) == 240.0


def test_a_smaller_retry_after_does_not_shorten_the_backoff() -> None:
    policy = RetryPolicy(backoff_seconds=10)
    assert policy.delay_for(2, 5.0, 0.0) == 20.0


def test_jitter_actually_reaches_the_sleep() -> None:
    sleeps: list[float] = []
    transport = Recorder(fail(429, {"error": "slow down"}), ok())
    client = build(
        transport, jitter=1.0, sleeps=sleeps, policy=RetryPolicy(backoff_seconds=2, max_attempts=3)
    )
    client.call("implementer", MESSAGES)
    assert sleeps == [4.0]  # base 2, doubled by a full-jitter draw of 1.0


def test_exhausting_the_ladder_names_the_last_failure() -> None:
    transport = Recorder(*[fail(503) for _ in range(4)])
    client = build(transport, policy=RetryPolicy(max_attempts=4))
    with pytest.raises(RuntimeError, match="transient"):
        client.call("implementer", MESSAGES)


# ---------------------------------------------------------------- locality


def test_two_clients_do_not_share_park_state() -> None:
    """§ locality: one worker's rejection must never pause another. Separate
    processes mean separate dicts; nothing is shared, persisted or locked."""
    a_parks, b_parks = EndpointParks(), EndpointParks()
    body = {
        "theclawbayError": {
            "category": "quota",
            "code": "weekly_cost_limit_reached",
            "retryable": False,
        }
    }
    client_a = build(Recorder(fail(429, body)), parks=a_parks)
    with pytest.raises(CapExhausted):
        client_a.call("implementer", MESSAGES)
    assert a_parks.remaining("https://a", 1000.0) > 0
    assert b_parks.remaining("https://a", 1000.0) == 0.0


def test_a_park_extends_but_never_shortens() -> None:
    parks = EndpointParks()
    parks.park("https://a", 3600, now=1000.0)
    parks.park("https://a", 60, now=1000.0)
    assert parks.remaining("https://a", 1000.0) == pytest.approx(3600)


# --------------------------------------------------------------- emission


def test_every_attempt_is_emitted_with_role_model_endpoint_and_class() -> None:
    events: list[dict[str, Any]] = []
    transport = Recorder(fail(429, {"error": "slow down"}), ok())
    client = build(transport, events=events)
    client.call("implementer", MESSAGES)
    outcomes = [e["outcome"] for e in events]
    assert "error" in outcomes and "ok" in outcomes
    error = next(e for e in events if e["outcome"] == "error")
    assert error["error_class"] == P.RPM
    assert error["role"] == "implementer"
    assert error["model"] == "m"
    assert error["endpoint"] == "https://a"
    assert error["provider"] == "claw-bay"


def test_a_cap_is_emitted_before_it_raises() -> None:
    """If the cap only surfaced as an exception, the dashboard could never
    show why the fleet stopped."""
    events: list[dict[str, Any]] = []
    transport = Recorder(
        fail(
            429,
            {
                "theclawbayError": {
                    "category": "quota",
                    "code": "weekly_cost_limit_reached",
                    "retryable": False,
                },
            },
        )
    )
    client = build(transport, events=events)
    with pytest.raises(CapExhausted):
        client.call("implementer", MESSAGES)
    assert [e["error_class"] for e in events if e["outcome"] == "error"] == [P.TERMINAL_CAP]


def test_a_broken_event_sink_never_fails_a_successful_call() -> None:
    def explode(_event: dict[str, Any]) -> None:
        raise OSError("disk full")

    client = ModelClient(
        roles={"implementer": Route("m", "https://a")},
        transport=Recorder(ok()),
        on_event=explode,
        sleep=lambda _s: None,
    )
    assert client.call("implementer", MESSAGES).status == 200


def test_every_emitted_event_carries_its_own_identity() -> None:
    """Two attempts can be identical in every observable field, including
    the timestamp. A consumer deduplicating a replayed stream must still be
    able to tell them apart, so identity is assigned by the writer rather
    than inferred from content."""
    events: list[dict[str, Any]] = []
    transport = Recorder(ok(), ok(), ok())
    client = build(transport, events=events)
    for _ in range(3):
        client.call("implementer", MESSAGES)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    assert len({e["run_id"] for e in events}) == 1


def test_two_clients_do_not_share_a_run_id() -> None:
    a = build(Recorder(ok()))
    b = build(Recorder(ok()))
    assert a.run_id != b.run_id


def test_the_role_map_can_be_changed_while_running() -> None:
    """The call site names a ROLE, never a model — which is exactly what
    makes re-routing it a data change rather than a redeploy."""
    live = {"implementer": Route("v1", "https://a")}
    transport = Recorder(ok(), ok())
    client = ModelClient(
        roles=dict(live),
        transport=transport,
        sleep=lambda _s: None,
        routes_provider=lambda: live,
    )
    client.call("implementer", MESSAGES)
    live["implementer"] = Route("v2", "https://a")  # changed from elsewhere
    client.call("implementer", MESSAGES)
    assert [c.model for c in transport.calls] == ["v1", "v2"]


def test_an_empty_live_map_keeps_the_last_known_routes() -> None:
    """A provider that briefly returns nothing — a half-written setting, a
    racing writer — must not take the fleet down with it."""
    transport = Recorder(ok())
    client = ModelClient(
        roles={"implementer": Route("m", "https://a")},
        transport=transport,
        sleep=lambda _s: None,
        routes_provider=dict,
    )
    assert client.call("implementer", MESSAGES).status == 200
