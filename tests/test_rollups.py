"""Rollups and retention: bound the growth without losing the series."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_harness.audit import AuditStore
from agent_harness.events import MODEL_CALL, WORK, Event

DAY = 86400.0


def ev(ts: float, **kw: object) -> Event:
    kw.setdefault("kind", WORK)
    kw.setdefault("source", "test")
    return Event(ts=ts, **kw)  # type: ignore[arg-type]


@pytest.fixture
def audit(tmp_path: Path) -> AuditStore:
    return AuditStore(tmp_path / "audit.sqlite")


def test_a_day_rolls_up_into_one_immutable_row(audit: AuditStore) -> None:
    base = 1_700_000_000.0
    audit.append(
        [
            ev(
                base + n,
                kind=MODEL_CALL,
                role="implementer",
                model="m",
                outcome="ok",
                data={
                    "run_id": "r",
                    "seq": n,
                    "project_id": "p",
                    "tokens_in": 1000,
                    "tokens_out": 100,
                    "price_in_per_mtok": 1.0,
                    "price_out_per_mtok": 2.0,
                    "price_table": "t",
                },
            )
            for n in range(10)
        ]
    )
    written = audit.rollup(until=base + DAY * 2)

    assert written == 1
    rows = audit.rollups()
    assert len(rows) == 1
    assert rows[0]["events"] == 10
    assert rows[0]["tokens_in"] == 10_000
    assert rows[0]["cost_usd"] == pytest.approx(10 * (0.001 * 1.0 + 0.0001 * 2.0))


def test_rolling_up_twice_does_not_double_count(audit: AuditStore) -> None:
    """It runs on a timer. Re-running must be a no-op, not an accumulation."""
    base = 1_700_000_000.0
    audit.append([ev(base, outcome="done", data={"run_id": "r", "seq": 1, "project_id": "p"})])

    assert audit.rollup(until=base + DAY * 2) == 1
    assert audit.rollup(until=base + DAY * 2) == 0
    assert len(audit.rollups()) == 1


def test_today_is_never_rolled_up(audit: AuditStore) -> None:
    """A rollup is immutable, so writing one for a day still in progress
    would freeze a partial day as if it were the whole of it."""
    now = time.time()
    audit.append([ev(now, outcome="done", data={"run_id": "r", "seq": 1, "project_id": "p"})])

    assert audit.rollup() == 0, "an in-progress day was frozen as complete"


def test_raw_events_are_only_thinned_once_a_rollup_covers_them(audit: AuditStore) -> None:
    """The whole discipline. Thinning before aggregating is silent data loss
    that leaves a tidy-looking database."""
    base = 1_700_000_000.0
    audit.append(
        [
            ev(base + n, outcome="done", data={"run_id": "r", "seq": n, "project_id": "p"})
            for n in range(5)
        ]
    )

    # No rollup yet: nothing may be removed, however old it is.
    assert audit.thin(older_than_days=1, now=base + DAY * 400) == 0
    assert audit.count() == 5

    audit.rollup(until=base + DAY * 2)
    removed = audit.thin(older_than_days=1, now=base + DAY * 400)

    assert removed == 5
    assert audit.count() == 0
    assert audit.rollups()[0]["events"] == 5, "the series survived the thinning"


def test_thinning_never_touches_events_outside_the_rolled_up_range(audit: AuditStore) -> None:
    base = 1_700_000_000.0
    audit.append([ev(base, outcome="old", data={"run_id": "r", "seq": 1, "project_id": "p"})])
    audit.append(
        [ev(base + DAY * 5, outcome="newer", data={"run_id": "r", "seq": 2, "project_id": "p"})]
    )

    audit.rollup(until=base + DAY)  # covers the first day only
    removed = audit.thin(older_than_days=0, now=base + DAY * 400)

    assert removed == 1
    remaining = [r["outcome"] for r in audit.recent(limit=10)]
    assert remaining == ["newer"], "an unrolled-up event was thinned"


def test_a_rollup_is_never_rewritten(audit: AuditStore) -> None:
    """Late-arriving events must not silently change a published number.

    If they could, every historical figure would be provisional forever and
    no report could be reproduced.
    """
    base = 1_700_000_000.0
    audit.append([ev(base, outcome="done", data={"run_id": "r", "seq": 1, "project_id": "p"})])
    audit.rollup(until=base + DAY * 2)
    first = audit.rollups()[0]["events"]

    audit.append(
        [ev(base + 10, outcome="done", data={"run_id": "r", "seq": 99, "project_id": "p"})]
    )
    audit.rollup(until=base + DAY * 2)

    assert audit.rollups()[0]["events"] == first, "a published rollup was rewritten"


def test_rollups_are_readable_through_the_api(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from agent_harness.api import create_api
    from agent_harness.store import EventStore

    audit = AuditStore(tmp_path / "audit.sqlite")
    base = 1_700_000_000.0
    audit.append(
        [
            ev(
                base,
                kind=MODEL_CALL,
                role="r",
                model="m",
                outcome="ok",
                data={"run_id": "x", "seq": 1, "project_id": "p", "tokens_in": 5},
            )
        ]
    )
    audit.rollup(until=base + DAY * 2)

    with TestClient(create_api(EventStore(tmp_path / "h.sqlite"), token="tok", audit=audit)) as c:  # noqa: S106
        body = c.get("/api/audit/rollups", headers={"Authorization": "Bearer tok"}).json()

    assert len(body["rows"]) == 1
    assert body["rows"][0]["tokens_in"] == 5
