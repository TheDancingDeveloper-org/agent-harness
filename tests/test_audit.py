"""The audit store: separate fates, and observation that cannot stop work.

The event store shared a SQLite file with the work queue, so six months of
history rode on the same file as claims and leases. Phase 1 rewrote the `work`
table in place; that migration worked, but a routine schema change being ABLE
to touch the audit log is the defect. So is "reset the queue and re-sync the
plan", which is a reasonable thing to do and would have taken the history.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from agent_harness.audit import AuditStore, open_audit_store
from agent_harness.events import MODEL_CALL, WORK, Event
from agent_harness.store import EventStore


def ev(**kw: object) -> Event:
    kw.setdefault("ts", time.time())
    kw.setdefault("kind", WORK)
    kw.setdefault("source", "test")
    return Event(**kw)  # type: ignore[arg-type]


# ------------------------------------------------------- separate fates


def test_the_audit_store_is_a_different_file(tmp_path: Path) -> None:
    """The whole point. If it shares a file it shares a fate."""
    audit = AuditStore(tmp_path / "audit.sqlite")
    audit.append([ev(outcome="done")])

    assert (tmp_path / "audit.sqlite").exists()
    assert not (tmp_path / "harness.sqlite").exists()


def test_history_survives_the_operational_database_being_deleted(tmp_path: Path) -> None:
    """The definition of durable, stated as a test.

    Deleting the queue is a legitimate operational act -- re-sync the plan and
    carry on. It must not cost a single row of history.
    """
    operational = tmp_path / "harness.sqlite"
    audit = AuditStore(tmp_path / "audit.sqlite")
    EventStore(operational).append([ev(outcome="ignored")])
    audit.append([ev(outcome="done", data={"item_id": "T1"})])
    audit.close()

    operational.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(operational) + suffix).unlink(missing_ok=True)

    reopened = AuditStore(tmp_path / "audit.sqlite")
    assert reopened.count() == 1
    assert reopened.recent(limit=1)[0]["outcome"] == "done"


def test_a_broken_audit_store_does_not_stop_work(tmp_path: Path) -> None:
    """Observation failing must never wedge the fleet.

    The reverse is not true: the harness may lose its audit trail and keep
    going, but it must not stop delivering because a disk filled up.
    """
    # A FILE where a directory must be: mkdir(parents=True) would happily
    # create a merely-missing path, so "missing" is not the failure to test.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    store = open_audit_store(blocker / "audit.sqlite", required=False)

    assert store is not None
    assert store.degraded is True
    # Every write is a no-op rather than an exception.
    assert store.append([ev(outcome="done")]) == 0
    assert store.count() == 0


def test_a_required_audit_store_refuses_to_start_silently(tmp_path: Path) -> None:
    """When history is the point of the deployment, failing open is worse
    than failing loudly -- a fleet that runs unaudited looks identical to one
    that is audited."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    with pytest.raises(OSError):
        open_audit_store(blocker / "audit.sqlite", required=True)


# ------------------------------------------------------- append-only


def test_the_audit_store_exposes_no_general_purpose_mutation(tmp_path: Path) -> None:
    """Append-only is a property, not an intention. A method that exists gets
    called during an incident by someone with a good reason.

    `thin` is the one sanctioned deletion and is named here deliberately
    rather than quietly excluded: it removes only raw events whose day a
    rollup already covers, so the series survives what it removes. Naming it
    keeps this test honest -- a test called "no way to change a row" that
    silently tolerates a delete is worse than no test, because the name is
    what stops anyone looking.
    """
    forbidden = {"update", "delete", "remove", "purge", "clear", "set", "edit", "truncate"}
    offered = {name for name in dir(AuditStore) if not name.startswith("_")}
    assert not (offered & forbidden), f"AuditStore exposes mutation: {offered & forbidden}"
    assert "thin" in offered, "the sanctioned deletion vanished; this test now proves less"


def test_thin_is_the_only_deletion_and_it_is_fenced(tmp_path: Path) -> None:
    """Guarding the guard: thin must refuse when nothing has been rolled up,
    whatever the retention window says."""
    audit = AuditStore(tmp_path / "audit.sqlite")
    audit.append([ev(ts=1_700_000_000.0, outcome="done", data={"run_id": "r", "seq": 1})])

    assert audit.thin(older_than_days=0, now=1_700_000_000.0 + 86400 * 999) == 0
    assert audit.count() == 1


def test_replaying_the_same_event_does_not_duplicate_or_amend_it(tmp_path: Path) -> None:
    """Replay is normal -- an ingester re-reading a log, a retried batch.

    The second write must be dropped rather than applied, so history does not
    depend on how many times it was delivered.
    """
    audit = AuditStore(tmp_path / "audit.sqlite")
    event = ev(outcome="approved", data={"run_id": "r1", "seq": 1})

    assert audit.append([event]) == 1
    assert audit.append([event]) == 0, "a replayed event was written twice"
    assert audit.count() == 1
    assert audit.recent(limit=1)[0]["outcome"] == "approved"


# ------------------------------------------------------- adoption


def test_existing_history_is_copied_not_moved(tmp_path: Path) -> None:
    """Adoption must not be destructive. If the new store turns out to be on
    the wrong disk, the old one is still the record."""
    legacy = tmp_path / "harness.sqlite"
    old = EventStore(legacy)
    old.append([ev(outcome=f"e{i}", data={"run_id": "r", "seq": i}) for i in range(25)])
    old.close()

    audit = AuditStore(tmp_path / "audit.sqlite")
    copied = audit.adopt(legacy)

    assert copied == 25
    assert audit.count() == 25
    assert EventStore(legacy).count() == 25, "adoption deleted the source"


def test_adoption_is_idempotent(tmp_path: Path) -> None:
    """It runs on startup. Twice must not double the history."""
    legacy = tmp_path / "harness.sqlite"
    EventStore(legacy).append([ev(outcome="x", data={"run_id": "r", "seq": 1})])

    audit = AuditStore(tmp_path / "audit.sqlite")
    assert audit.adopt(legacy) == 1
    assert audit.adopt(legacy) == 0
    assert audit.count() == 1


def test_adopting_a_missing_database_is_not_an_error(tmp_path: Path) -> None:
    """A fresh deployment has no history to adopt, which is normal."""
    audit = AuditStore(tmp_path / "audit.sqlite")
    assert audit.adopt(tmp_path / "nothing-here.sqlite") == 0


# ------------------------------------------------------- schema durability


def test_every_row_records_the_schema_it_was_written_under(tmp_path: Path) -> None:
    """History is never rewritten to fit a new shape, so a reader has to know
    which shape it is looking at. A backfill is indistinguishable from
    falsification, and once done nothing in the series can be trusted."""
    audit = AuditStore(tmp_path / "audit.sqlite")
    audit.append([ev(outcome="done")])

    conn = sqlite3.connect(tmp_path / "audit.sqlite")
    row = conn.execute("SELECT schema_version FROM events LIMIT 1").fetchone()
    assert row[0] == AuditStore.SCHEMA_VERSION


def test_a_newer_column_does_not_break_reading_older_rows(tmp_path: Path) -> None:
    """Additive-only migration: rows written before a column existed keep
    their absence rather than acquiring an invented value."""
    path = tmp_path / "audit.sqlite"
    audit = AuditStore(path)
    audit.append([ev(outcome="old")])
    audit.close()

    conn = sqlite3.connect(path)
    # A column that does not exist yet, standing in for a future addition.
    conn.execute("ALTER TABLE events ADD COLUMN carbon_grams REAL")
    conn.commit()
    conn.close()

    reopened = AuditStore(path)
    reopened.append([ev(outcome="new", data={"run_id": "r", "seq": 9})])
    rows = {r["outcome"]: r for r in reopened.recent(limit=10)}
    assert set(rows) == {"old", "new"}
    # The pre-existing row keeps its absence rather than acquiring a value.
    assert rows["old"]["carbon_grams"] is None


# ------------------------------------------------------- cost capture


def test_cost_is_recorded_with_the_price_that_was_applied(tmp_path: Path) -> None:
    """Tokens alone are not a cost series.

    Prices change. Applying today's price to last year's tokens is a
    projection, not history, and it silently rewrites the past every time a
    vendor changes a rate. Recording the price used makes a repricing a
    visible step rather than an invisible retroactive edit.
    """
    audit = AuditStore(tmp_path / "audit.sqlite")
    audit.append(
        [
            ev(
                kind=MODEL_CALL,
                role="implementer",
                model="a-model",
                data={
                    "run_id": "r",
                    "seq": 1,
                    "tokens_in": 1000,
                    "tokens_out": 500,
                    "price_in_per_mtok": 3.0,
                    "price_out_per_mtok": 15.0,
                    "price_table": "2026-08-01",
                },
            )
        ]
    )
    row = audit.recent(limit=1)[0]
    assert row["tokens_in"] == 1000
    assert row["tokens_out"] == 500
    assert row["price_table"] == "2026-08-01"
    # 1000/1e6*3 + 500/1e6*15 = 0.003 + 0.0075
    assert row["cost_usd"] == pytest.approx(0.0105)


def test_cost_is_null_rather_than_zero_when_no_price_is_known(tmp_path: Path) -> None:
    """Zero is a measurement. Unknown is not, and folding one into the other
    understates spend in a way no later query can detect."""
    audit = AuditStore(tmp_path / "audit.sqlite")
    audit.append([ev(kind=MODEL_CALL, data={"tokens_in": 100, "tokens_out": 50})])
    row = audit.recent(limit=1)[0]
    assert row["tokens_in"] == 100
    assert row["cost_usd"] is None


def test_events_carry_the_project_they_belong_to(tmp_path: Path) -> None:
    """Without this no series can be sliced per project -- and unlike code,
    history cannot be backfilled later."""
    audit = AuditStore(tmp_path / "audit.sqlite")
    audit.append([ev(outcome="done", data={"project_id": "ngms", "item_id": "T1"})])
    assert audit.recent(limit=1)[0]["project_id"] == "ngms"


# ------------------------------------------------------------------- API


@pytest.fixture
def client(tmp_path: Path):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from agent_harness.api import create_api

    audit = AuditStore(tmp_path / "audit.sqlite")
    store = EventStore(tmp_path / "harness.sqlite")
    with TestClient(create_api(store, token="tok", audit=audit)) as c:  # noqa: S106
        holder: Any = c
        holder.audit = audit
        yield c


def hdr() -> dict[str, str]:
    return {"Authorization": "Bearer tok"}


def test_health_reports_a_degraded_store(tmp_path: Path) -> None:
    """The only signal that history is silently not being kept.

    Writes are dropped rather than raised so observation cannot stop work,
    which means nothing else will ever tell you.
    """
    from fastapi.testclient import TestClient

    from agent_harness.api import create_api

    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    degraded = open_audit_store(blocker / "audit.sqlite", required=False)
    store = EventStore(tmp_path / "harness.sqlite")

    with TestClient(create_api(store, token="tok", audit=degraded)) as c:  # noqa: S106
        body = c.get("/api/audit/health", headers=hdr()).json()

    assert body["configured"] is True
    assert body["degraded"] is True


def test_cost_reports_unpriced_calls_separately(client) -> None:  # type: ignore[no-untyped-def]
    """A total that silently omits unpriced calls reads as complete."""
    client.audit.append(
        [
            ev(
                kind=MODEL_CALL,
                role="implementer",
                model="m",
                data={
                    "run_id": "r",
                    "seq": 1,
                    "project_id": "p",
                    "tokens_in": 1_000_000,
                    "tokens_out": 0,
                    "price_in_per_mtok": 2.0,
                    "price_table": "t",
                },
            ),
            ev(
                kind=MODEL_CALL,
                role="implementer",
                model="m",
                data={"run_id": "r", "seq": 2, "project_id": "p", "tokens_in": 500},
            ),
        ]
    )
    body = client.get("/api/audit/cost?window=all", headers=hdr()).json()

    assert body["total_cost_usd"] == pytest.approx(2.0)
    assert body["total_unpriced"] == 1, "an unpriced call was folded into the total"


def test_analytics_projection_keeps_classes_denominators_and_baselines(client) -> None:  # type: ignore[no-untyped-def]
    client.audit.append(
        [
            ev(
                kind=MODEL_CALL,
                error_class="rpm",
                data={"run_id": "r", "seq": 1, "project_id": "p", "tokens_in": 10},
            ),
            ev(
                kind=MODEL_CALL,
                error_class="unclassified",
                data={"run_id": "r", "seq": 2, "project_id": "p"},
            ),
            ev(
                kind=WORK,
                outcome="done",
                data={"run_id": "r", "seq": 3, "project_id": "p", "item_id": "T1"},
            ),
        ]
    )
    body = client.get("/api/analytics?window=all&project_id=p", headers=hdr())
    assert body.status_code == 200
    payload = body.json()
    assert payload["rate_limits"]["classified"]["rpm"] == 1
    assert payload["rate_limits"]["unclassified"] == 1
    assert payload["rate_limits"]["total"] == 1
    assert payload["rate_limits"]["denominator"] == 2
    assert payload["cost"]["denominator"] == 2
    assert payload["delivery"]["denominator"] == 1
    assert payload["audit_health"]["events"] == 3

    baseline = {
        "baseline_id": "p-before",
        "project_id": "p",
        "label": "before",
        "window_days": 7,
        "items_done": 12,
    }
    assert client.post("/api/audit/baselines", headers=hdr(), json=baseline).status_code == 200
    refreshed = client.get("/api/analytics?window=all&project_id=p", headers=hdr()).json()
    assert refreshed["baselines"]["baselines"][0]["items_done"] == 12


def test_analytics_projection_marks_partial_history_and_degraded_store(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from agent_harness.api import create_api

    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    audit = open_audit_store(blocker / "audit.sqlite", required=False)
    with TestClient(
        create_api(EventStore(tmp_path / "harness.sqlite"), token="tok", audit=audit)
    ) as c:  # noqa: S106
        payload = c.get("/api/analytics?window=7d", headers=hdr()).json()
    assert payload["audit_health"]["configured"] is True
    assert payload["audit_health"]["degraded"] is True
    assert payload["cost"]["denominator"] == 0


def test_a_window_longer_than_the_history_is_flagged_partial(client) -> None:  # type: ignore[no-untyped-def]
    """A chart labelled '7 days' drawn from one hour of history is not wrong
    about the data, it is wrong about the question -- and the numbers alone
    never reveal it."""
    client.audit.append([ev(kind=MODEL_CALL, data={"run_id": "r", "seq": 1})])

    assert client.get("/api/audit/cost?window=7d", headers=hdr()).json()["partial"] is True
    assert client.get("/api/audit/cost?window=all", headers=hdr()).json()["partial"] is False


def test_events_page_by_id(client) -> None:  # type: ignore[no-untyped-def]
    client.audit.append([ev(outcome=f"e{i}", data={"run_id": "r", "seq": i}) for i in range(5)])
    first = client.get("/api/audit/events?limit=2", headers=hdr()).json()
    assert len(first["events"]) == 2

    second = client.get(
        f"/api/audit/events?since_id={first['cursor']}&limit=2", headers=hdr()
    ).json()
    assert len(second["events"]) == 2
    assert {e["id"] for e in first["events"]} & {e["id"] for e in second["events"]} == set()


def test_a_baseline_cannot_be_quietly_replaced(client) -> None:  # type: ignore[no-untyped-def]
    """A baseline that can be edited is not a baseline; it is a target that
    moves to wherever the current numbers are."""
    body = {
        "baseline_id": "ngms-pre-harness",
        "project_id": "ngms",
        "label": "manual delivery, six weeks before the harness",
        "window_days": 42,
        "items_done": 18,
        "cost_usd": 0.0,
    }
    assert client.post("/api/audit/baselines", headers=hdr(), json=body).status_code == 200

    body["items_done"] = 999
    conflict = client.post("/api/audit/baselines", headers=hdr(), json=body)
    assert conflict.status_code == 409
    assert "immutable" in conflict.json()["detail"]

    listed = client.get("/api/audit/baselines", headers=hdr()).json()["baselines"]
    assert len(listed) == 1
    assert listed[0]["items_done"] == 18, "an immutable baseline was overwritten"


def test_the_audit_api_says_so_when_no_store_is_attached(tmp_path: Path) -> None:
    """Distinguishable from 'no history yet', which looks identical."""
    from fastapi.testclient import TestClient

    from agent_harness.api import create_api

    store = EventStore(tmp_path / "harness.sqlite")
    with TestClient(create_api(store, token="tok")) as c:  # noqa: S106
        assert c.get("/api/audit/health", headers=hdr()).json()["configured"] is False
        assert c.get("/api/audit/cost", headers=hdr()).status_code == 409
