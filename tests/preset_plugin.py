"""A route preset defined outside the harness entirely.

This module stands in for a third party's package. It is not a test module, it
is not imported by any test module, and nothing in `src/` mentions it. The only
way it reaches the harness is by name:

    HARNESS_ROUTE_PRESETS="fixture-plugin=preset_plugin:PRESET"

If Stage B's claim is true — that a vendor is addable without editing
`model_client.py` — then everything here is achievable with the pieces core
already exports, and the conformance suite in `test_route_conformance.py` runs
against it unchanged. Every field it configures is one a real gateway differs
on: a different completion path, a credential in `x-api-key` with no scheme
word, an envelope nested under a different key, a reply in a different place
and token counts under different names.
"""

from __future__ import annotations

from agent_harness.protocols import BearerAuth, JsonChatRequest, JsonResponseReader, RoutePreset
from agent_harness.providers import VendorEnvelopeProvider

PRESET = RoutePreset(
    name="fixture-plugin",
    request=JsonChatRequest(
        name="fixture-generate",
        path="/v2/generate",
        model_key="model_id",
        messages_key="turns",
        extra_payload={"stream": False},
    ),
    # No scheme word: the credential is the whole header value.
    auth=BearerAuth(name="header-key", header="x-api-key", scheme=""),
    reader=JsonResponseReader(
        name="fixture-generate",
        text_paths=("result.reply",),
        usage_key="counters",
        tokens_in_keys=("in_units",),
        tokens_out_keys=("out_units",),
        cached_token_keys=("reused_units",),
    ),
    classifier=VendorEnvelopeProvider(
        name="fixture-plugin",
        vendor_field="problem",
        quota_categories=("budget",),
        auth_categories=("identity",),
        quota_code_marks=("budget_spent",),
        auth_code_marks=("bad_token",),
        short_window_marks=("daily",),
    ),
    hosts=("fixture.invalid",),
    summary="A third party's gateway, for proving that adding one changes no core code.",
)
