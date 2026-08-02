"""Classification tests.

This is the highest-risk logic in the project: it decides whether a fleet
waits half a second or a week. It is also trivially testable — a status, a
header map and a body.
"""

from __future__ import annotations

import datetime
import email.utils
import json

from agent_harness import providers as P

# The real envelope, captured from a live gateway on 2026-08-02. Two things
# in it contradict a reasonable first guess, and both were bugs before this
# response was actually looked at.
LIVE_NON_QUOTA = {
    "error": "invalid request",
    "code": "upstream_rejected",
    "theclawbayError": {
        "requestId": "7b75cd73-6e1c-4265-98db-1f40bf75ca70",
        "category": "internal",
        "code": "upstream_rejected",
        "userMessage": "The upstream model provider rejected this request.",
        "retryable": False,
        "retryAfterSeconds": None,
        "nextAction": "Review the request and retry.",
    },
}

WEEKLY_CAP = {
    "error": "weekly cost limit reached for this account",
    "code": "weekly_cost_limit_reached",
    "theclawbayError": {
        "category": "quota",
        "code": "weekly_cost_limit_reached",
        "userMessage": "Your weekly usage limit has been reached.",
        "retryable": False,
    },
}

SHORT_CAP = {
    "code": "5h_cost_limit_reached",
    "theclawbayError": {
        "category": "quota",
        "code": "5h_cost_limit_reached",
        "retryable": False,
    },
}


def classify(
    body: object, status: int = 429, headers: dict[str, str] | None = None
) -> P.Classification:
    payload = json.dumps(body) if body is not None else None
    return P.CLAW_BAY.classify(status, headers or {}, payload)


# ------------------------------------------------------- the two live findings


def test_not_retryable_is_not_the_same_as_out_of_budget() -> None:
    """Providers use the flag for ordinary upstream failures. Calling this a
    cap would park a healthy endpoint for an hour over one hiccup."""
    verdict = classify(LIVE_NON_QUOTA)
    assert verdict.kind == P.NON_RETRYABLE
    assert verdict.kind not in P.CAPS
    assert "upstream_rejected" in (verdict.message or "")


def test_retry_after_is_read_from_the_body_not_only_the_header() -> None:
    """The gateway states the wait in the body. Parsing only headers
    silently discards the server's own instruction."""
    body = {
        "theclawbayError": {
            "category": "rate",
            "code": "rate_limited",
            "retryable": True,
            "retryAfterSeconds": 42,
        }
    }
    verdict = classify(body)
    assert verdict.kind == P.RPM
    assert verdict.retry_after == 42.0


def test_the_longer_of_header_and_body_retry_after_wins() -> None:
    # Undershooting earns another rejection; overshooting costs idle time.
    body = {"theclawbayError": {"retryable": True, "retryAfterSeconds": 10}}
    assert classify(body, headers={"Retry-After": "90"}).retry_after == 90.0
    body_big = {"theclawbayError": {"retryable": True, "retryAfterSeconds": 90}}
    assert classify(body_big, headers={"Retry-After": "10"}).retry_after == 90.0


def test_a_null_retry_after_is_absent_not_zero() -> None:
    """'Wait zero seconds' and 'no instruction given' are different claims;
    collapsing them would erase the backoff on every such response."""
    assert classify(LIVE_NON_QUOTA).retry_after is None


# ------------------------------------------------------------------- caps


def test_a_long_window_cap_is_terminal() -> None:
    verdict = classify(WEEKLY_CAP)
    assert verdict.kind == P.TERMINAL_CAP
    assert "weekly" in (verdict.message or "").lower()


def test_a_short_window_cap_is_distinguished_from_a_long_one() -> None:
    # They differ only in how long to wait, but that is the whole decision.
    assert classify(SHORT_CAP).kind == P.WINDOW_CAP


def test_a_rejected_credential_is_terminal() -> None:
    body = {
        "code": "invalid_api_key",
        "theclawbayError": {"code": "invalid_api_key", "retryable": False},
    }
    assert classify(body).kind == P.TERMINAL_CAP


def test_a_401_is_terminal_whatever_the_body_says() -> None:
    assert classify(None, status=401).kind == P.TERMINAL_CAP


# -------------------------------------------------------------------- rpm


def test_a_plain_burst_limit_is_rpm() -> None:
    body = {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}}
    assert classify(body).kind == P.RPM


def test_an_unparseable_body_is_rpm() -> None:
    """Asymmetric on purpose: wrongly retrying a permanent condition costs
    one backoff; wrongly giving up on a transient one costs the work."""
    assert P.CLAW_BAY.classify(429, {}, b"<html>nope</html>").kind == P.RPM


def test_a_non_dict_body_is_rpm() -> None:
    assert P.CLAW_BAY.classify(429, {}, b"[1,2,3]").kind == P.RPM


def test_a_missing_body_is_rpm() -> None:
    assert P.CLAW_BAY.classify(429, {}, None).kind == P.RPM


# ------------------------------------------------------------ other statuses


def test_5xx_is_transient() -> None:
    assert classify(None, status=503).kind == P.TRANSIENT


def test_a_4xx_that_is_not_429_is_fatal() -> None:
    assert classify(None, status=400).kind == P.FATAL


# --------------------------------------------------------------- retry-after


def test_retry_after_accepts_delta_seconds() -> None:
    assert P.parse_retry_after({"Retry-After": "120"}) == 120.0


def test_retry_after_accepts_an_http_date() -> None:
    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=60)
    header = {"Retry-After": email.utils.format_datetime(when)}
    parsed = P.parse_retry_after(header)
    assert parsed is not None
    assert 50 <= parsed <= 70


def test_a_past_http_date_clamps_to_zero_not_negative() -> None:
    past = email.utils.format_datetime(datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC))
    assert P.parse_retry_after({"Retry-After": past}) == 0.0


def test_a_malformed_retry_after_is_none_not_an_exception() -> None:
    # A broken header must never take down a caller.
    assert P.parse_retry_after({"Retry-After": "soon-ish"}) is None
    assert P.parse_retry_after({"Retry-After": ""}) is None
    assert P.parse_retry_after(None) is None


# ------------------------------------------------------------------ generic


def test_the_generic_provider_cannot_tell_a_cap_from_a_burst_limit() -> None:
    """It has no evidence to do so, and guessing would be worse than saying
    'rpm'. The docstring warns that accepting this default against a metered
    API inherits exactly the blindness this module exists to remove."""
    assert P.GENERIC.classify(429, {}, json.dumps(WEEKLY_CAP)).kind == P.RPM


def test_the_generic_provider_still_honours_retry_after() -> None:
    assert P.GENERIC.classify(429, {"Retry-After": "30"}, None).retry_after == 30.0


def test_the_generic_provider_treats_auth_failures_as_terminal() -> None:
    assert P.GENERIC.classify(403, {}, None).kind == P.TERMINAL_CAP


def test_every_kind_has_a_human_meaning() -> None:
    # The dashboard renders these; a class with no explanation is a class a
    # reader has to guess at.
    for kind in (P.RPM, P.WINDOW_CAP, P.TERMINAL_CAP, P.NON_RETRYABLE, P.TRANSIENT, P.FATAL):
        assert kind in P.MEANING
