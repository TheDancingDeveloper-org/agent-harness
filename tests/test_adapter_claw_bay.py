"""One gateway's envelope, read by the adapter that knows it.

These assertions were core tests until Stage B. Nothing about them changed —
the same live-captured payloads produce the same classifications — but they now
exercise `adapters/claw_bay.py`, because a vendor's field names are an
adapter's business and core must be able to pass its own tests without loading
one.

The two findings at the top are why the adapter exists at all: `429` alone
cannot say whether to wait half a second or a week.
"""

from __future__ import annotations

import json

from agent_harness import protocols
from agent_harness import providers as P
from agent_harness.adapters.claw_bay import CLASSIFIER, PRESET

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
    return CLASSIFIER.classify(status, headers or {}, payload)


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
    assert CLASSIFIER.classify(429, {}, b"<html>nope</html>").kind == P.RPM


def test_a_non_dict_body_is_rpm() -> None:
    assert CLASSIFIER.classify(429, {}, b"[1,2,3]").kind == P.RPM


def test_a_missing_body_is_rpm() -> None:
    assert CLASSIFIER.classify(429, {}, None).kind == P.RPM


# ------------------------------------------------------------ other statuses


def test_5xx_is_transient() -> None:
    assert classify(None, status=503).kind == P.TRANSIENT


def test_a_4xx_that_is_not_429_is_fatal() -> None:
    assert classify(None, status=400).kind == P.FATAL


# ------------------------------------------------------------------- preset


def test_the_preset_is_resolvable_by_name_without_importing_it() -> None:
    """The whole registry claim, in one assertion: core reaches this adapter
    through declared metadata, not through an import."""
    assert protocols.resolve("claw-bay") == PRESET


def test_the_preset_pairs_the_wire_shape_with_the_classifier_that_reads_it() -> None:
    assert PRESET.classifier is CLASSIFIER
    assert PRESET.request.name == "chat-completions"
    assert PRESET.auth.name == "bearer"
