"""One gateway's error envelope, and the protocol it speaks.

The Claw Bay serves the chat-completions shape and states *why* it rejected a
request in a JSON envelope of its own. That second half is the valuable part:
`429` alone cannot distinguish going too fast from a weekly spend cap, and a
fleet that cannot tell them apart accumulates tens of thousands of
undifferentiated rate-limit errors with no way to say what any of them meant.

Captured live on 2026-08-02:

    {"error": "invalid request", "code": "upstream_rejected",
     "theclawbayError": {"category": "internal", "code": "upstream_rejected",
                         "userMessage": "…", "retryable": false,
                         "retryAfterSeconds": null}}

Two things in it contradict a reasonable first guess, and both were defects
before the response was actually looked at:

1. **The retry delay can be in the body**, not only in a `Retry-After` header.
   Parse only headers and the server's own instruction is discarded.
2. **"not retryable" is not "out of budget".** The flag is set for ordinary
   upstream failures too, so treating every non-retryable rejection as a spend
   cap parks a healthy endpoint for an hour over one hiccup. A cap has to be
   quota-shaped on its own evidence.

None of that is in core. This module constructs the configurable pieces core
provides, and is reached by name — `preset: claw-bay` — through the entry point
this distribution declares.
"""

from __future__ import annotations

from ..protocols import BearerAuth, RoutePreset
from ..providers import VendorEnvelopeProvider
from .chat_completions import READER, REQUEST

#: The classifier, configured with this gateway's field names. `5h` and the
#: hourly marks separate the *short* window — hours — from the weekly cap,
#: which parks for much longer because retrying it is pointless for days.
CLASSIFIER = VendorEnvelopeProvider(
    name="claw-bay",
    vendor_field="theclawbayError",
    quota_categories=("quota",),
    auth_categories=("auth",),
    quota_code_marks=("_limit_reached", "cost_limit", "quota"),
    auth_code_marks=("api_key", "invalid_key"),
    short_window_marks=("5h", "hourly", "minute"),
)

PRESET = RoutePreset(
    name="claw-bay",
    request=REQUEST,
    auth=BearerAuth(),
    reader=READER,
    classifier=CLASSIFIER,
    # A hint for `protocols.suggest()` and for nothing else. Hosts are proxied,
    # renamed and self-hosted; this may not choose a protocol on its own.
    hosts=("theclawbay.com",),
    summary=(
        "Chat completions, plus a classifier that reads this gateway's error "
        "envelope and can therefore tell a burst limit from a spend cap."
    ),
)
