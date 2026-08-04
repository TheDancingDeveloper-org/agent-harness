"""Classification tests.

This is the highest-risk logic in the project: it decides whether a fleet
waits half a second or a week. It is also trivially testable — a status, a
header map and a body.

Nothing here loads an adapter. Core ships two classifiers and neither names a
vendor: `GENERIC`, which has only HTTP to go on, and `VendorEnvelopeProvider`,
which reads an envelope whose field names it is *told*. A particular gateway's
names live in `adapters/`, and their tests with them —
`tests/test_adapter_claw_bay.py`.
"""

from __future__ import annotations

import datetime
import email.utils
import json

from agent_harness import providers as P

WEEKLY_CAP = {
    "error": "weekly cost limit reached for this account",
    "code": "weekly_cost_limit_reached",
    "category": "quota",
    "retryable": False,
}


def classify(
    body: object,
    status: int = 429,
    headers: dict[str, str] | None = None,
    provider: P.Provider | None = None,
) -> P.Classification:
    payload = json.dumps(body) if body is not None else None
    return (provider or P.VendorEnvelopeProvider()).classify(status, headers or {}, payload)


# --------------------------------------------------- the configurable envelope


def test_an_unconfigured_envelope_reads_the_top_level() -> None:
    """The default names no nesting key, because naming one would be naming a
    vendor. A body that states its reason plainly is still read."""
    assert classify(WEEKLY_CAP).kind == P.TERMINAL_CAP


def test_the_nesting_key_is_configuration_not_a_branch() -> None:
    """Same classifier, a different gateway's envelope name. Adding a vendor
    is a construction, never an edit to this module."""
    nested = P.VendorEnvelopeProvider(name="somewhere", vendor_field="somewhereError")
    body = {"somewhereError": {"category": "quota", "code": "5h_cost_limit_reached"}}
    assert classify(body, provider=nested).kind == P.WINDOW_CAP
    # ...and the same body means nothing to a classifier told a different name.
    assert classify(body).kind == P.RPM


def test_not_retryable_is_not_the_same_as_out_of_budget() -> None:
    """Providers use the flag for ordinary upstream failures. Calling this a
    cap would park a healthy endpoint for an hour over one hiccup."""
    verdict = classify(
        {"error": "upstream said no", "code": "upstream_rejected", "retryable": False}
    )
    assert verdict.kind == P.NON_RETRYABLE
    assert verdict.kind not in P.CAPS


def test_retry_after_is_read_from_the_body_not_only_the_header() -> None:
    """A gateway may state the wait in the body. Parsing only headers silently
    discards the server's own instruction."""
    verdict = classify({"retryable": True, "retryAfterSeconds": 42})
    assert verdict.kind == P.RPM
    assert verdict.retry_after == 42.0


def test_the_longer_of_header_and_body_retry_after_wins() -> None:
    # Undershooting earns another rejection; overshooting costs idle time.
    assert classify({"retryAfterSeconds": 10}, headers={"Retry-After": "90"}).retry_after == 90.0
    assert classify({"retryAfterSeconds": 90}, headers={"Retry-After": "10"}).retry_after == 90.0


def test_a_null_retry_after_is_absent_not_zero() -> None:
    """'Wait zero seconds' and 'no instruction given' are different claims;
    collapsing them would erase the backoff on every such response."""
    assert classify({"code": "upstream_rejected", "retryAfterSeconds": None}).retry_after is None


def test_a_short_window_cap_is_distinguished_from_a_long_one() -> None:
    # They differ only in how long to wait, but that is the whole decision.
    assert classify({"category": "quota", "code": "5h_cost_limit_reached"}).kind == P.WINDOW_CAP
    assert classify(WEEKLY_CAP).kind == P.TERMINAL_CAP


def test_a_rejected_credential_is_terminal() -> None:
    assert classify({"code": "invalid_api_key", "retryable": False}).kind == P.TERMINAL_CAP


def test_a_401_is_terminal_whatever_the_body_says() -> None:
    assert classify(None, status=401).kind == P.TERMINAL_CAP


def test_an_unparseable_body_is_rpm() -> None:
    """Asymmetric on purpose: wrongly retrying a permanent condition costs
    one backoff; wrongly giving up on a transient one costs the work."""
    envelope = P.VendorEnvelopeProvider()
    assert envelope.classify(429, {}, b"<html>nope</html>").kind == P.RPM
    assert envelope.classify(429, {}, b"[1,2,3]").kind == P.RPM
    assert envelope.classify(429, {}, None).kind == P.RPM


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


def test_core_knows_exactly_one_classifier_by_name() -> None:
    """A name that resolves without loading anything. Everything else is a
    preset, and a preset is discovered rather than built in."""
    assert set(P.PROVIDERS) == {"generic"}


def test_every_kind_has_a_human_meaning() -> None:
    # The dashboard renders these; a class with no explanation is a class a
    # reader has to guess at.
    for kind in (P.RPM, P.WINDOW_CAP, P.TERMINAL_CAP, P.NON_RETRYABLE, P.TRANSIENT, P.FATAL):
        assert kind in P.MEANING
