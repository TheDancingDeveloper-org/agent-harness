"""How to read one provider's failures. Nothing here knows about any repo.

A rate limit is not one thing, and the difference is not visible in the HTTP
status. `429` covers at least:

  - going too fast (retry in a moment),
  - a spend window exhausted (retry cannot help for hours),
  - a spend cap exhausted (retry cannot help for days),
  - "we refuse this request" (retry cannot help at all).

Every provider encodes that distinction differently, and most bury it in a
vendor-specific field. A harness that ignores the field cannot tell a
half-second problem from a week-long one — which is exactly how a fleet ends
up with tens of thousands of undifferentiated rate-limit errors and no way
to say what any of them meant.

So classification is a provider's own concern, expressed as a `Provider`.
Two are shipped: `GENERIC`, which assumes nothing beyond HTTP, and
`CLAW_BAY`, a worked example of reading a vendor envelope. Add your own; the
rest of the harness only sees a `Classification`.
"""

from __future__ import annotations

import datetime
import email.utils
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

# What a failure means for control flow. These, not status codes, are what
# the retry loop switches on.
RPM = "rpm"  # too fast. Retry, jittered.
WINDOW_CAP = "window_cap"  # short spend window exhausted. Hours.
TERMINAL_CAP = "terminal_cap"  # long window / bad credential. Days, or never.
NON_RETRYABLE = "non_retryable"  # refused, but nothing is exhausted.
TRANSIENT = "transient"  # 5xx, connection, timeout. Retry.
FATAL = "fatal"  # our fault: bad request, unknown model.

#: Classes where retrying the same call cannot possibly succeed.
NO_RETRY = (WINDOW_CAP, TERMINAL_CAP, NON_RETRYABLE, FATAL)
#: Classes that mean *this endpoint* is out of budget, not that we are fast.
CAPS = (WINDOW_CAP, TERMINAL_CAP)

MEANING = {
    RPM: "going too fast — retried locally with jitter",
    WINDOW_CAP: "short spend window exhausted — not retried, endpoint parked",
    TERMINAL_CAP: "spend cap or credential rejected — not retried, endpoint parked",
    NON_RETRYABLE: "provider refused the request — not retried, endpoint kept",
    TRANSIENT: "transient upstream or network failure — retried",
    FATAL: "request is wrong — not retried",
}


@dataclass(frozen=True)
class Classification:
    """What one failure means.

    `retry_after` is the provider's own instruction, in seconds, when it gave
    one. It always beats a computed backoff: a server that says how long to
    wait knows something the client does not.
    """

    kind: str
    message: str | None = None
    retry_after: float | None = None


class Provider(Protocol):
    """Everything the harness needs to know about one API's failures."""

    # A read-only property, not a settable attribute: implementations are
    # frozen dataclasses, and a provider whose name can be reassigned at
    # runtime would make every emitted event unattributable after the fact.
    @property
    def name(self) -> str: ...

    def classify(
        self, status: int, headers: Mapping[str, str] | None, body: bytes | str | None
    ) -> Classification: ...


def parse_retry_after(headers: Mapping[str, str] | None, now: float | None = None) -> float | None:
    """RFC 9110 `Retry-After`: delta-seconds or an HTTP-date. Providers send
    both. A malformed value returns None rather than raising — a broken
    header must never take down a caller."""
    if not headers:
        return None
    raw = None
    for key in ("retry-after", "Retry-After"):
        try:
            raw = headers.get(key)
        except AttributeError:
            return None
        if raw is not None:
            break
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(int(text)))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.UTC)
    reference = now if now is not None else datetime.datetime.now(datetime.UTC).timestamp()
    return max(0.0, when.timestamp() - reference)


def _load(body: bytes | str | None) -> dict[str, Any] | None:
    if body is None:
        return None
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass(frozen=True)
class GenericProvider:
    """Classification from HTTP alone, for a provider whose error shape we
    do not know.

    It deliberately cannot distinguish a spend cap from a burst limit,
    because nothing in HTTP does. Every 429 is `rpm`. That is the safe
    default — retrying a cap wastes one backoff, whereas giving up on a
    burst limit loses the work — but it is *only* a default: a fleet running
    against a metered API should implement `classify` properly rather than
    accept it, or it inherits the exact blindness this module exists to
    remove.
    """

    name: str = "generic"

    def classify(
        self, status: int, headers: Mapping[str, str] | None, body: bytes | str | None
    ) -> Classification:
        retry_after = parse_retry_after(headers)
        if status == 429:
            return Classification(RPM, None, retry_after)
        if status in (401, 403):
            return Classification(TERMINAL_CAP, f"HTTP {status}: credential rejected", retry_after)
        if status >= 500:
            return Classification(TRANSIENT, f"HTTP {status}", retry_after)
        return Classification(FATAL, f"HTTP {status}", retry_after)


@dataclass(frozen=True)
class VendorEnvelopeProvider:
    """A provider that states the reason in a JSON envelope.

    Configured rather than hardcoded, because the *shape* is common even
    though the field names are not: a nested object carrying a machine code,
    a category, a retryable flag and sometimes a retry delay.

    The defaults describe The Claw Bay's envelope, verified live 2026-08-02:

        {"error": "invalid request", "code": "upstream_rejected",
         "theclawbayError": {"category": "internal", "code": "upstream_rejected",
                             "userMessage": "…", "retryable": false,
                             "retryAfterSeconds": null}}

    Two properties of that envelope generalise, and both are easy to get
    wrong:

    1. **The retry delay can live in the body**, not only in a header. Parse
       only headers and you silently discard the server's own instruction.
    2. **"not retryable" is not "out of budget".** Providers use the flag for
       ordinary upstream failures too. Treating every non-retryable rejection
       as a spend cap parks a healthy endpoint for an hour over one hiccup,
       so a cap must be *quota-shaped* on its own evidence.
    """

    name: str = "vendor-envelope"
    vendor_field: str = "theclawbayError"
    quota_categories: tuple[str, ...] = ("quota",)
    auth_categories: tuple[str, ...] = ("auth",)
    #: Substrings that mark a code as a spend cap.
    quota_code_marks: tuple[str, ...] = ("_limit_reached", "cost_limit", "quota")
    #: Substrings that mark a code as a rejected credential.
    auth_code_marks: tuple[str, ...] = ("api_key", "invalid_key")
    #: Substrings marking the *short* window, as opposed to the long one.
    short_window_marks: tuple[str, ...] = ("5h", "hourly", "minute")

    def classify(
        self, status: int, headers: Mapping[str, str] | None, body: bytes | str | None
    ) -> Classification:
        header_retry = parse_retry_after(headers)
        payload = _load(body) or {}
        vendor = payload.get(self.vendor_field)
        vendor = vendor if isinstance(vendor, dict) else {}
        body_retry = _positive_number(
            vendor.get("retryAfterSeconds", payload.get("retryAfterSeconds"))
        )
        # If both are present the longer wins: undershooting earns another
        # rejection, overshooting costs only idle time.
        candidates = [v for v in (header_retry, body_retry) if v is not None]
        retry_after = max(candidates) if candidates else None

        code = str(vendor.get("code") or payload.get("code") or "")
        category = str(vendor.get("category") or "")
        retryable = vendor.get("retryable", payload.get("retryable"))
        detail = vendor.get("userMessage") or payload.get("error") or code or None
        message = f"{detail} (code={code or 'unknown'})" if detail else None

        if status >= 500:
            return Classification(TRANSIENT, message or f"HTTP {status}", retry_after)

        lowered_code = code.lower()
        is_auth = category in self.auth_categories or any(
            m in lowered_code for m in self.auth_code_marks
        )
        is_quota = category in self.quota_categories or any(
            m in lowered_code for m in self.quota_code_marks
        )

        if status in (401, 403) or (is_auth and status == 429):
            # A rejected credential is terminal in the strongest sense:
            # every later call fails identically until a human replaces it.
            return Classification(TERMINAL_CAP, message or "credential rejected", retry_after)
        if status != 429:
            return Classification(FATAL, message or f"HTTP {status}", retry_after)
        if is_quota:
            haystack = f"{lowered_code} {(detail or '')}".lower()
            kind = (
                WINDOW_CAP if any(m in haystack for m in self.short_window_marks) else TERMINAL_CAP
            )
            return Classification(kind, message, retry_after)
        if retryable is False:
            return Classification(NON_RETRYABLE, message, retry_after)
        # Unparseable or unremarkable: treat as a burst limit and retry.
        # Asymmetric on purpose -- wrongly retrying a permanent condition
        # costs one backoff, wrongly giving up on a transient one costs the
        # work.
        return Classification(RPM, None, retry_after)


def _positive_number(value: Any) -> float | None:
    """A usable non-negative number, or None.

    `retryAfterSeconds` is explicitly null in the common case, and "wait zero
    seconds" is not the same statement as "no instruction given" — collapsing
    them would erase the backoff on every such response.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


GENERIC = GenericProvider()
CLAW_BAY = VendorEnvelopeProvider(name="claw-bay")

PROVIDERS = {p.name: p for p in (GENERIC, CLAW_BAY)}
