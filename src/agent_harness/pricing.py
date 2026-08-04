"""Token usage and what it cost.

Two jobs, deliberately separate.

**Reading usage out of a provider response.** Vendors report it differently and
none of them promise to keep reporting it, so this returns None rather than
zero when it cannot find any. Zero tokens is a measurement — it says the call
was free — and a parser that emits it on an unrecognised shape produces a cost
series that is quietly wrong and never complains.

**Applying a price.** The price is recorded alongside the tokens on every
event, and the reason is worth stating plainly: prices change. A cost series
computed by applying today's rates to last year's tokens is not history, it is
a projection, and it silently rewrites the past every time a vendor reprices.
Recording the rate that was applied turns a repricing into a visible step in
the series rather than an invisible retroactive edit.

The table here is a **default, not an authority.** It ships empty of real
prices on purpose: this harness is not tied to any vendor, and a table of
guessed numbers would produce confident, wrong money. Supply your own through
`HARNESS_PRICE_TABLE` and the events record which table was used.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: Identifies which set of prices produced a cost. Recorded on every priced
#: event so a series can be split at the point rates changed, rather than
#: appearing to have always been the new ones.
UNKNOWN_TABLE = "none"


@dataclass(frozen=True)
class Price:
    """What one million tokens costs, in whatever currency the table uses."""

    in_per_mtok: float
    out_per_mtok: float


@dataclass
class PriceTable:
    """A named, dated set of prices.

    `version` is free text and ends up on every event: a date, a vendor
    announcement id, whatever lets someone later say "this cost was computed
    under those rates".
    """

    version: str = UNKNOWN_TABLE
    prices: dict[str, Price] | None = None

    def price_for(self, model: str | None) -> Price | None:
        """The price for a model, or None.

        None is the honest answer for an unknown model, and it propagates: the
        event records tokens with a null cost, and the API counts it as
        `unpriced` rather than adding zero to a total.
        """
        if not model or not self.prices:
            return None
        if model in self.prices:
            return self.prices[model]
        # Longest-prefix match, so `a-model-2026-08-01` inherits `a-model`
        # without every dated snapshot needing its own entry.
        matches = [key for key in self.prices if model.startswith(key)]
        return self.prices[max(matches, key=len)] if matches else None


def load_price_table(source: str | None = None) -> PriceTable:
    """Load prices from JSON, by path or inline.

    Shape:

        {"version": "2026-08-01",
         "prices": {"a-model": {"in_per_mtok": 3.0, "out_per_mtok": 15.0}}}

    A malformed table is a warning and an empty table, not a crash: pricing is
    an observation, and a typo in a config file must not stop the fleet.
    """
    raw = source or os.environ.get("HARNESS_PRICE_TABLE", "")
    if not raw:
        return PriceTable()
    try:
        text = raw
        if not raw.lstrip().startswith("{"):
            with open(raw, encoding="utf-8") as handle:
                text = handle.read()
        parsed = json.loads(text)
        prices = {
            model: Price(
                in_per_mtok=float(value["in_per_mtok"]),
                out_per_mtok=float(value["out_per_mtok"]),
            )
            for model, value in (parsed.get("prices") or {}).items()
        }
        return PriceTable(version=str(parsed.get("version") or UNKNOWN_TABLE), prices=prices)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("pricing: could not load price table (%s); costs will be unpriced", exc)
        return PriceTable()


#: The key names in common use, as defaults rather than as the only ones a
#: build can read. A preset's response reader supplies its own when a provider
#: reports usage under different names — that is configuration, not a branch
#: here on which vendor sent the body.
INPUT_TOKEN_KEYS = ("input_tokens", "prompt_tokens", "tokens_in")
OUTPUT_TOKEN_KEYS = ("output_tokens", "completion_tokens", "tokens_out")
CACHED_TOKEN_KEYS = ("cache_read_input_tokens", "cached_tokens", "tokens_cached")


def extract_usage(
    body: bytes | str | None,
    *,
    usage_key: str = "usage",
    tokens_in_keys: tuple[str, ...] = INPUT_TOKEN_KEYS,
    tokens_out_keys: tuple[str, ...] = OUTPUT_TOKEN_KEYS,
    cached_token_keys: tuple[str, ...] = CACHED_TOKEN_KEYS,
) -> dict[str, int] | None:
    """Token usage from a response body, or None if it does not report any.

    Handles the shapes in common use — `prompt_tokens`/`completion_tokens`,
    `input_tokens`/`output_tokens`, and either nested under `usage` or at the
    top level. Deliberately conservative: an unrecognised body returns None,
    because inventing zeros here would understate every total downstream and
    nothing would ever flag it.

    The key names are arguments so that a provider reporting under other names
    is a preset's configuration rather than another `if` in this function.
    """
    if body is None:
        return None
    try:
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        parsed = json.loads(text)
    except (ValueError, AttributeError):
        return None
    if not isinstance(parsed, dict):
        return None

    usage = parsed.get(usage_key)
    if not isinstance(usage, dict):
        usage = parsed

    def first_int(keys: tuple[str, ...]) -> int | None:
        for key in keys:
            value = usage.get(key)
            # `bool` is an `int`; a flag is not a token count.
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    tokens_in = first_int(tokens_in_keys)
    tokens_out = first_int(tokens_out_keys)
    cached = first_int(cached_token_keys)

    if tokens_in is None and tokens_out is None:
        return None

    out: dict[str, int] = {}
    if tokens_in is not None:
        out["tokens_in"] = tokens_in
    if tokens_out is not None:
        out["tokens_out"] = tokens_out
    if cached is not None:
        out["tokens_cached"] = cached
    return out


def price_fields(
    usage: Mapping[str, int] | None, model: str | None, table: PriceTable
) -> dict[str, Any]:
    """Counted tokens, and the rate applied to them if one is known.

    Separate from reading the body because who reads the usage is a route's
    business — a preset's reader knows its own field names — while what a token
    costs is the price table's. An unpriced model keeps its token counts and
    gets no price keys at all, so the API can count it as `unpriced` instead of
    adding zero to a total.
    """
    if usage is None:
        return {}
    fields: dict[str, Any] = dict(usage)
    price = table.price_for(model)
    if price is not None:
        fields["price_in_per_mtok"] = price.in_per_mtok
        fields["price_out_per_mtok"] = price.out_per_mtok
        fields["price_table"] = table.version
    return fields


def usage_fields(body: bytes | str | None, model: str | None, table: PriceTable) -> dict[str, Any]:
    """Everything a `model_call` event should carry about what it consumed.

    Returns an empty dict when the response reported no usage — an event with
    no usage keys is honestly silent, where one carrying zeros is a claim.

    The generic reading, for a caller that has a body and no route. A client
    holding a route asks that route's reader and then `price_fields`.
    """
    return price_fields(extract_usage(body), model, table)
