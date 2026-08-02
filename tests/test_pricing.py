"""Usage and cost. The recurring rule: unknown is not zero."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_harness.pricing import (
    Price,
    PriceTable,
    extract_usage,
    load_price_table,
    usage_fields,
)

# ------------------------------------------------------------ extraction


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('{"usage": {"input_tokens": 10, "output_tokens": 5}}', {"tokens_in": 10, "tokens_out": 5}),
        (
            '{"usage": {"prompt_tokens": 10, "completion_tokens": 5}}',
            {"tokens_in": 10, "tokens_out": 5},
        ),
        ('{"input_tokens": 10, "output_tokens": 5}', {"tokens_in": 10, "tokens_out": 5}),
        (b'{"usage": {"input_tokens": 3, "output_tokens": 1}}', {"tokens_in": 3, "tokens_out": 1}),
    ],
)
def test_the_shapes_vendors_actually_use(body: bytes | str, expected: dict[str, int]) -> None:
    assert extract_usage(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        "not json at all",
        "[1, 2, 3]",
        '{"choices": []}',
        '{"usage": {"something_else": 4}}',
    ],
)
def test_an_unreported_usage_is_none_not_zero(body: bytes | str | None) -> None:
    """The load-bearing case.

    Zero tokens is a measurement -- it says the call was free. A parser that
    invents it on an unrecognised body understates every total downstream and
    nothing ever flags it, because the number looks like a number.
    """
    assert extract_usage(body) is None


def test_cached_tokens_are_kept_separate() -> None:
    usage = extract_usage('{"usage": {"input_tokens": 100, "cache_read_input_tokens": 90}}')
    assert usage == {"tokens_in": 100, "tokens_cached": 90}


# ------------------------------------------------------------ pricing


def test_an_unknown_model_is_unpriced_rather_than_free() -> None:
    table = PriceTable(version="t", prices={"known": Price(1.0, 2.0)})
    assert table.price_for("something-else") is None
    assert table.price_for(None) is None


def test_a_dated_model_inherits_its_family_price() -> None:
    """Vendors ship `a-model-2026-08-01` snapshots; requiring an entry per
    snapshot means the newest model is silently unpriced the day it ships."""
    table = PriceTable(version="t", prices={"a-model": Price(1.0, 2.0)})
    assert table.price_for("a-model-2026-08-01") == Price(1.0, 2.0)


def test_the_longest_matching_prefix_wins() -> None:
    """A specific override must beat the family it belongs to."""
    table = PriceTable(
        version="t",
        prices={"a-model": Price(1.0, 2.0), "a-model-large": Price(10.0, 20.0)},
    )
    assert table.price_for("a-model-large-2026") == Price(10.0, 20.0)


def test_the_default_table_prices_nothing() -> None:
    """Shipping guessed prices would produce confident, wrong money. The
    harness is not tied to a vendor, so it does not pretend to know rates."""
    assert PriceTable().price_for("any-model") is None


def test_a_price_table_loads_from_json_or_a_path(tmp_path: Path) -> None:
    payload = {"version": "2026-08-01", "prices": {"m": {"in_per_mtok": 3, "out_per_mtok": 15}}}
    inline = load_price_table(json.dumps(payload))
    assert inline.version == "2026-08-01"
    assert inline.price_for("m") == Price(3.0, 15.0)

    path = tmp_path / "prices.json"
    path.write_text(json.dumps(payload))
    assert load_price_table(str(path)).price_for("m") == Price(3.0, 15.0)


def test_a_broken_price_table_does_not_stop_the_fleet(tmp_path: Path) -> None:
    """Pricing is an observation. A typo in a config file must cost accuracy,
    not delivery."""
    assert load_price_table("{not json").price_for("m") is None
    assert load_price_table(str(tmp_path / "missing.json")).price_for("m") is None


# ------------------------------------------------------------ the event


def test_usage_fields_carry_the_price_that_was_applied() -> None:
    """Recording the rate is what makes a later repricing a visible step
    rather than an invisible retroactive edit to every past number."""
    table = PriceTable(version="2026-08-01", prices={"m": Price(3.0, 15.0)})
    fields = usage_fields('{"usage": {"input_tokens": 1000, "output_tokens": 500}}', "m", table)

    assert fields["tokens_in"] == 1000
    assert fields["price_in_per_mtok"] == 3.0
    assert fields["price_table"] == "2026-08-01"


def test_tokens_are_recorded_even_when_the_price_is_unknown() -> None:
    """Usage is worth having on its own -- a price can be applied later by a
    reader, but tokens nobody wrote down are gone."""
    fields = usage_fields(
        '{"usage": {"input_tokens": 10, "output_tokens": 5}}', "mystery", PriceTable()
    )

    assert fields == {"tokens_in": 10, "tokens_out": 5}
    assert "price_table" not in fields


def test_no_usage_reported_means_no_fields_at_all() -> None:
    """An event with no usage keys is honestly silent. One carrying zeros is
    a claim, and the difference is invisible once it is in the database."""
    assert (
        usage_fields("no usage here", "m", PriceTable(version="t", prices={"m": Price(1, 2)})) == {}
    )


# ------------------------------------------------------- through the client


def test_a_successful_call_emits_usage(tmp_path: Path) -> None:
    """Wiring check. The store has accepted tokens all along; nothing was
    sending any, which is the same shape of bug as a reaper nobody calls."""
    from agent_harness import providers
    from agent_harness.model_client import ModelClient, Response, Route

    events: list[dict] = []

    def transport(route, messages, options):  # type: ignore[no-untyped-def]
        return Response(
            status=200,
            headers={},
            body='{"usage": {"input_tokens": 2000, "output_tokens": 1000}}',
        )

    client = ModelClient(
        roles={"implementer": Route("m", "https://api.example", providers.GENERIC)},
        transport=transport,
        on_event=events.append,
        prices=PriceTable(version="2026-08-01", prices={"m": Price(3.0, 15.0)}),
    )
    client.call("implementer", [{"role": "user", "content": "hi"}])

    ok = [e for e in events if e["outcome"] == "ok"]
    assert len(ok) == 1
    assert ok[0]["tokens_in"] == 2000
    assert ok[0]["tokens_out"] == 1000
    assert ok[0]["price_table"] == "2026-08-01"


def test_a_failed_call_carries_no_invented_usage(tmp_path: Path) -> None:
    """A 429 has no usage to report, and reporting zero would count every
    rate limit as a free call in the cost series."""
    from agent_harness import providers
    from agent_harness.model_client import ModelClient, Response, Route

    events: list[dict] = []

    def transport(route, messages, options):  # type: ignore[no-untyped-def]
        return Response(status=500, headers={}, body="upstream exploded")

    client = ModelClient(
        roles={"implementer": Route("m", "https://api.example", providers.GENERIC)},
        transport=transport,
        on_event=events.append,
        sleep=lambda _: None,
    )
    with pytest.raises(Exception):  # noqa: B017 - the ladder is exhausted; shape is not the point
        client.call("implementer", [{"role": "user", "content": "hi"}])

    assert all("tokens_in" not in e for e in events)
