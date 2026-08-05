"""What the harness retains about its own calls, and what it refuses to lose.

Four defects, all found in one session driving a real project, all of the same
family: the harness observed something, acted on it correctly, and then threw
the observation away.

- #190 the price of an answer was recorded and never the answer
- #191 two long model calls could write one output, last writer winning
- #192 a route's reachability was classified per call and forgotten
- #193 fallback chains existed for `run` and nowhere else
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_harness.model_client import Availability, ModelClient, Response, Route
from agent_harness.outputs import Claim, OutputBusy, claim_path, claiming
from agent_harness.providers import Classification
from agent_harness.redaction import MARK, Redactor, from_environment

# --------------------------------------------------------------- redaction


def test_a_known_credential_is_removed_wherever_it_appears() -> None:
    """Exact values first, because they cannot produce a false negative.

    The store is append-only. A credential that reaches it can be rotated but
    never deleted, which is why this runs before the first write rather than
    at the read edge.
    """
    redact = Redactor(["sekrit-value-1234"])
    assert redact("the key is sekrit-value-1234, ok?") == f"the key is {MARK}, ok?"
    assert redact("no secret here") == "no secret here"
    assert redact(None) is None
    assert redact("") == ""


def test_a_short_value_is_not_treated_as_a_secret() -> None:
    """Below a floor, exact replacement corrupts more than it protects.

    A three-character "key" occurs inside ordinary words, so redacting every
    occurrence would mangle the record while protecting nothing.
    """
    assert Redactor(["abc"])("abc appears in abcdef") == "abc appears in abcdef"


def test_credential_shapes_are_removed_even_when_the_value_is_unknown() -> None:
    """The deployment cannot enumerate what an agent might echo."""
    redact = Redactor([])
    assert redact("Authorization: Bearer abcdefghijklmnop") == f"Authorization: Bearer {MARK}"
    assert redact('{"api_key": "abcdefghijklmnop"}') == f'{{"api_key": "{MARK}"}}'
    assert redact("password=hunter2hunter2") == f"password={MARK}"
    # The label survives. A reader must be able to tell what kind of thing was
    # removed; replacing the whole assignment would lose that.
    assert "api_key" in (redact('{"api_key": "abcdefghijklmnop"}') or "")


def test_a_longer_secret_containing_a_shorter_one_is_fully_removed() -> None:
    """Ordering matters: shortest-first would leave the tail exposed."""
    redact = Redactor(["abcdefgh", "abcdefgh-ijklmnop"])
    assert redact("token abcdefgh-ijklmnop here") == f"token {MARK} here"


def test_redaction_reports_that_it_happened() -> None:
    """Silently shortening text makes the record lie indistinguishably."""
    redact = Redactor(["sekrit-value-1234"])
    assert redact.redacted("has sekrit-value-1234")
    assert not redact.redacted("has nothing")


def test_the_process_redacts_its_own_credentials_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The likeliest leak is the harness's own key, echoed back at it."""
    monkeypatch.setenv("HARNESS_API_KEY", "this-is-the-api-key")
    assert from_environment()("key=this-is-the-api-key") == f"key={MARK}"


# ------------------------------------------------------- #190 the answer


def _client(**kw: object) -> tuple[ModelClient, list[dict[str, object]]]:
    events: list[dict[str, object]] = []

    def transport(route: Route, messages: object, options: object) -> Response:
        return Response(
            status=200,
            headers={},
            body=json.dumps({"choices": [{"message": {"content": "the model said this"}}]}),
        )

    client = ModelClient(
        roles={"planner": Route("m", "https://e.example", preset="chat-completions")},
        transport=transport,
        on_event=events.append,
        now=lambda: 1000.0,
        **kw,  # type: ignore[arg-type]
    )
    return client, events


def test_the_event_carries_what_the_model_said() -> None:
    """The gap this closes: usage and latency were recorded, the answer never.

    So "why did this item produce that diff" and "what did the reviewer object
    to" were unanswerable once the process exited, and nothing else held it --
    a terminal's scrollback is not durable and does not exist for a direct
    API call at all.
    """
    client, events = _client()
    client.call("planner", [{"role": "user", "content": "hi"}])
    ok = [e for e in events if e["outcome"] == "ok"]
    assert ok, events
    assert ok[0]["answer"] == "the model said this"
    assert ok[0]["answer_chars"] == len("the model said this")
    assert ok[0]["answer_truncated"] is False
    assert ok[0]["answer_redacted"] is False


def test_a_secret_in_the_answer_never_reaches_the_event() -> None:
    """#190 must not land before #186, and this is why.

    Model output routinely quotes config, headers and error envelopes. Storing
    it raw into an append-only store served over HTTP is the largest possible
    increase in that exposure.
    """
    client, events = _client(redact=Redactor(["the model said this"]))
    client.call("planner", [{"role": "user", "content": "hi"}])
    ok = [e for e in events if e["outcome"] == "ok"][0]
    assert ok["answer"] == MARK
    assert ok["answer_redacted"] is True


def test_a_long_answer_is_truncated_and_says_so() -> None:
    """A record that quietly stops cannot be told from a model that stopped."""
    client, events = _client(answer_limit=4)
    client.call("planner", [{"role": "user", "content": "hi"}])
    ok = [e for e in events if e["outcome"] == "ok"][0]
    assert ok["answer"] == "the "
    assert ok["answer_truncated"] is True
    # The true length survives truncation, so the loss is measurable.
    assert ok["answer_chars"] == len("the model said this")


def test_capturing_the_answer_can_be_turned_off_entirely() -> None:
    """A deployment may have policy reasons to keep no bodies at all."""
    client, events = _client(answer_limit=0)
    client.call("planner", [{"role": "user", "content": "hi"}])
    ok = [e for e in events if e["outcome"] == "ok"][0]
    assert "answer" not in ok
    # And the accounting it never had a problem with is untouched.
    assert ok["outcome"] == "ok"


def test_an_unreadable_body_costs_the_call_nothing() -> None:
    """Telemetry is never load-bearing, exactly as usage already is."""
    events: list[dict[str, object]] = []

    def transport(route: Route, messages: object, options: object) -> Response:
        return Response(status=200, headers={}, body="not json at all")

    client = ModelClient(
        roles={"planner": Route("m", "https://e.example", preset="chat-completions")},
        transport=transport,
        on_event=events.append,
    )
    reply = client.call("planner", [{"role": "user", "content": "hi"}])
    assert reply.status == 200
    assert [e for e in events if e["outcome"] == "ok"]


# ------------------------------------------------ #192 route reachability


def _route(model: str = "m", endpoint: str = "https://e.example") -> Route:
    return Route(model, endpoint)


def test_reachability_is_learned_from_ordinary_traffic() -> None:
    seen = Availability()
    seen.record(_route(), "ok", None, now=10.0)
    (entry,) = seen.all()
    assert entry["answering"] is True
    assert entry["last_ok"] == 10.0
    assert entry["consecutive_failures"] == 0
    assert entry["calls"] == 1


def test_a_route_that_has_only_ever_failed_is_never_reported_healthy() -> None:
    """`last_ok` stays null. Never defaulted to now, which would invert it."""
    seen = Availability()
    seen.record(_route(), "error", Classification("transient", "boom"), now=10.0)
    (entry,) = seen.all()
    assert entry["answering"] is False
    assert entry["last_ok"] is None
    assert entry["last_error_class"] == "transient"


def test_a_success_clears_the_failure_run() -> None:
    seen = Availability()
    seen.record(_route(), "error", Classification("transient", "boom"), now=10.0)
    seen.record(_route(), "error", Classification("transient", "boom"), now=11.0)
    assert seen.all()[0]["consecutive_failures"] == 2
    seen.record(_route(), "ok", None, now=12.0)
    entry = seen.all()[0]
    assert entry["consecutive_failures"] == 0
    assert entry["last_error_class"] is None


def test_this_processs_own_waiting_is_not_evidence_about_a_route() -> None:
    """A wait is us pausing and a skip is us not asking.

    Counting either as a failure would defame a route nobody called.
    """
    seen = Availability()
    for outcome in ("retry_wait", "skipped", "parked"):
        seen.record(_route(), outcome, None, now=10.0)
    assert seen.all() == []


def test_the_worst_route_is_reported_first() -> None:
    """The reason to read this is that something is wrong."""
    seen = Availability()
    seen.record(_route("healthy"), "ok", None, now=10.0)
    for tick in range(3):
        seen.record(_route("broken"), "error", Classification("transient", "x"), now=10.0 + tick)
    assert [r["model"] for r in seen.all()] == ["broken", "healthy"]


def test_a_client_records_reachability_even_with_no_event_sink() -> None:
    """A deployment with no sink still needs to know what is answering."""

    def transport(route: Route, messages: object, options: object) -> Response:
        return Response(status=200, headers={}, body="{}")

    client = ModelClient(
        roles={"planner": Route("m", "https://e.example")},
        transport=transport,
        on_event=None,
    )
    client.call("planner", [{"role": "user", "content": "hi"}])
    assert client.availability.answering() == ["m"]


# ------------------------------------------------------- #191 output claim


def test_a_second_writer_is_refused_while_a_claim_is_live(tmp_path: Path) -> None:
    """Two surveyors ran ten minutes against one output and neither noticed."""
    out = tmp_path / "PLAN.md"
    # Entered left to right: the first claim is held when the second is
    # attempted, which is the collision this reproduces.
    with (
        claiming(out, now=1000.0),
        pytest.raises(OutputBusy) as raised,
        claiming(out, now=1001.0),
    ):
        pass
    assert str(out) in str(raised.value)


def test_a_claim_expires_so_a_dead_writer_releases_it(tmp_path: Path) -> None:
    """The queue's rule, for the queue's reason.

    A lock held by a process that died is a lock nobody can release, and
    needing a human to delete a stale file is the unattended-operation failure
    the lease design exists to reject.
    """
    out = tmp_path / "PLAN.md"
    marker = claim_path(out)
    marker.write_text(json.dumps({"pid": 1, "host": "gone", "started": 0.0, "expires": 500.0}))
    with claiming(out, now=1000.0):
        pass  # The stale claim did not block it.


def test_the_claim_is_released_even_when_the_call_fails(tmp_path: Path) -> None:
    out = tmp_path / "PLAN.md"
    with pytest.raises(RuntimeError), claiming(out, now=1000.0):
        raise RuntimeError("the model call blew up")
    assert not claim_path(out).exists()


def test_a_corrupt_claim_cannot_wedge_an_output_forever(tmp_path: Path) -> None:
    """Otherwise the stale-lock failure returns wearing a different hat."""
    out = tmp_path / "PLAN.md"
    claim_path(out).write_text("{ this is not json")
    with claiming(out, now=1000.0):
        pass


def test_nothing_durable_means_nothing_to_claim(tmp_path: Path) -> None:
    with claiming(None):
        pass


def test_a_claim_describes_who_holds_it(tmp_path: Path) -> None:
    """An operator has to be able to act on the refusal."""
    held = Claim(pid=42, host="somehost", started=0.0, expires=600.0)
    described = held.describe(now=300.0)
    assert "42" in described and "somehost" in described


# ---------------------------------------------------------- #193 chains


def test_every_command_that_names_a_role_can_name_a_chain() -> None:
    """`run` had fallbacks and the single-call commands did not.

    Measured cost: a surveyor retried one model returning 524 for fifteen
    minutes while another model on the same endpoint answered 200 throughout.
    """
    from agent_harness.__main__ import _role_chain

    chain = _role_chain("first,second , third", "https://e.example", "key", "claw-bay")
    assert [r.model for r in chain] == ["first", "second", "third"]
    assert {r.endpoint for r in chain} == {"https://e.example"}
    assert {r.preset for r in chain} == {"claw-bay"}


def test_one_model_is_still_a_chain_of_one() -> None:
    from agent_harness.__main__ import _role_chain

    assert [r.model for r in _role_chain("only", "https://e.example", "")] == ["only"]


def test_an_empty_role_names_nothing_rather_than_an_empty_model() -> None:
    """A `Route("")` would be a route to a model with no name."""
    from agent_harness.__main__ import _role_chain

    assert _role_chain("", "https://e.example", "") == []
    assert _role_chain(" , ", "https://e.example", "") == []


def test_a_chain_moves_on_when_the_first_model_is_down() -> None:
    """The whole point: one pass down the chain before any backoff."""
    tried: list[str] = []

    def transport(route: Route, messages: object, options: object) -> Response:
        tried.append(route.model)
        if route.model == "dead":
            return Response(status=503, headers={}, body="{}")
        return Response(status=200, headers={}, body="{}")

    client = ModelClient(
        roles={
            "planner": [
                Route("dead", "https://e.example"),
                Route("alive", "https://e.example"),
            ]
        },
        transport=transport,
        sleep=lambda _: None,
    )
    assert client.call("planner", [{"role": "user", "content": "hi"}]).status == 200
    assert tried == ["dead", "alive"]
