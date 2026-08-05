"""Issue #188: a hold was durable and silent.

Stage J made a question survive the worker that asked it. Nothing said the
question existed. Every route to a hold was a pull — list the inbox, read the
item — so an item could sit unanswered overnight while every dashboard read
healthy, which is precisely the failure `/api/audit/health` exists to defend
against elsewhere: a system that looks identical whether or not it is doing the
thing it is for.

**The load-bearing test in this file is
`test_a_hook_that_raises_can_neither_fail_nor_stall_the_item`.** Everything
else is the notice's shape; that one is the promise that telling somebody can
never cost the fleet anything.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_harness import holds as H
from agent_harness.work import CLAIMED, HELD, Project, WorkQueue, WorkRecord
from agent_harness.work import RUNNING as RUNNING_CONTROL


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make(
    tmp_path: Path,
    clock: Clock,
    *,
    on_hold: Any = None,
    max_hold: float = 3600.0,
) -> WorkQueue:
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=100.0, now=clock, on_hold=on_hold)
    queue.add_project(Project(project_id="default", name="Default", max_hold_seconds=max_hold))
    queue.set_control(RUNNING_CONTROL)
    queue.add([WorkRecord(item_id="T1", title="Do a thing", brief="Do it.")])
    return queue


def claimed(queue: WorkQueue, owner: str = "worker-1") -> Any:
    record = queue.claim(owner)
    assert record is not None
    return record


def api(queue: WorkQueue) -> Any:
    from fastapi.testclient import TestClient

    from agent_harness.api import create_api
    from agent_harness.store import EventStore

    store = EventStore(Path(tempfile.mkdtemp()) / "e.sqlite")
    return TestClient(create_api(store, queue=queue, token="t"))  # noqa: S106


AUTH = {"Authorization": "Bearer t"}


# ------------------------------------------------------ the load-bearing one


def test_a_hook_that_raises_can_neither_fail_nor_stall_the_item(tmp_path: Path) -> None:
    """The rule `audit` already follows, applied to a question.

    A broken consumer is a broken consumer. It must not turn an agent asking
    for help into a failed item, must not leave the item un-held, and must not
    stop the answer arriving later.
    """
    calls: list[dict[str, Any]] = []

    def hostile(notice: dict[str, Any]) -> None:
        calls.append(notice)
        raise RuntimeError("the receiver is down, as receivers are")

    clock = Clock()
    queue = make(tmp_path, clock, on_hold=hostile)
    claimed(queue)

    hold = queue.hold("T1", question="Which database?", owner="worker-1")

    assert calls, "the hook was not even called"
    record = queue.get("T1")
    assert record is not None
    assert record.state == HELD, "a failed notification changed the state of the item"
    assert record.owner == "worker-1", "D12 keeps the claim; delivery must not touch it"
    assert queue.holds.current("default", "T1") is not None
    # And the item is still answerable, from anywhere, exactly as before.
    assert queue.claim("worker-2") is None
    answered = queue.answer_hold("T1", hold.resume_token, H.Answer(text="postgres", who="me"))
    assert answered.state == H.ANSWERED
    after = queue.get("T1")
    assert after is not None and after.state == CLAIMED


def test_a_hook_that_raises_does_not_stop_the_next_consumer(tmp_path: Path) -> None:
    """A deployment has two: the stream it already writes, and whatever the
    operator configured. One being down must not silence the other."""
    seen: list[str] = []

    def broken(_: dict[str, Any]) -> None:
        raise OSError("connection refused")

    def working(notice: dict[str, Any]) -> None:
        seen.append(notice["item_id"])

    clock = Clock()
    queue = make(tmp_path, clock, on_hold=H.fanout(broken, working))
    claimed(queue)
    queue.hold("T1", question="Which database?", owner="worker-1")

    assert seen == ["T1"]


# ------------------------------------------------------------- the notice


def test_opening_a_hold_says_so_rather_than_waiting_to_be_polled(tmp_path: Path) -> None:
    notices: list[dict[str, Any]] = []
    clock = Clock()
    queue = make(tmp_path, clock, on_hold=notices.append)
    claimed(queue)

    queue.hold(
        "T1",
        question="Which database should this use?",
        owner="worker-1",
        reason="the schema is not decided",
        session_url="https://sessions.invalid/t/abc",
    )

    assert len(notices) == 1, "one question, one notice"
    notice = notices[0]
    assert notice["kind"] == "work"
    assert notice["outcome"] == H.HOLD_OPENED
    assert notice["item_id"] == "T1"
    assert notice["project_id"] == "default"
    assert notice["question"] == "Which database should this use?"
    assert notice["reason"] == "the schema is not decided"
    assert notice["session_url"] == "https://sessions.invalid/t/abc"
    assert notice["expires_at"] == pytest.approx(clock.t + 3600.0)
    assert "T1" in notice["detail"] and "Which database" in notice["detail"]


def test_the_notice_carries_enough_to_build_the_url_that_answers_it(tmp_path: Path) -> None:
    """Relative, because the harness does not know what it is reached as from
    outside — and pointed at a route that really exists, which is the half a
    hand-written path gets wrong."""
    notices: list[dict[str, Any]] = []
    clock = Clock()
    queue = make(tmp_path, clock, on_hold=notices.append)
    claimed(queue)
    hold = queue.hold("T1", question="Which database?", owner="worker-1")

    path = notices[0]["answer_path"]
    assert path == "/api/work/T1/answer?project_id=default"

    with api(queue) as client:
        response = client.post(
            path,
            json={"resume_token": hold.resume_token, "text": "postgres", "who": "me"},
            headers=AUTH,
        )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "claimed"


def test_the_notice_carries_no_resume_token(tmp_path: Path) -> None:
    """`executor.py` already keeps the token out of the message ledger — "a
    token in a room is a token anything that can read the room may spend" —
    and a notice is read by strictly more things than a room is."""
    notices: list[dict[str, Any]] = []
    clock = Clock()
    queue = make(tmp_path, clock, on_hold=notices.append)
    claimed(queue)
    hold = queue.hold("T1", question="Which database?", owner="worker-1")

    flattened = repr(notices[0])
    assert "resume_token" not in notices[0]
    assert hold.resume_token not in flattened


def test_a_refused_hold_announces_nothing(tmp_path: Path) -> None:
    """A notice is a statement that a question exists. One that was refused
    does not."""
    notices: list[dict[str, Any]] = []
    clock = Clock()
    queue = make(tmp_path, clock, on_hold=notices.append)

    with pytest.raises(H.HoldError):
        # Nobody is working on it: `blocked` is how an operator parks a
        # decision, and a hold is a suspended attempt.
        queue.hold("T1", question="Which database?")
    claimed(queue)
    with pytest.raises(H.HoldError):
        queue.hold("T1", question="   ", owner="worker-1")

    assert notices == []


def test_no_hook_is_a_supported_configuration(tmp_path: Path) -> None:
    """The pull path is still the whole feature for anyone who wants no
    outbound anything."""
    clock = Clock()
    queue = make(tmp_path, clock)
    claimed(queue)
    queue.hold("T1", question="Which database?", owner="worker-1")
    assert queue.holds.current("default", "T1") is not None
    assert H.fanout(None, None) is None
    assert H.webhook_hook("") is None


# ------------------------------------------------- the one outbound hook


def test_the_operator_configures_one_url_and_nothing_else() -> None:
    """Core learns a URL. It does not learn what is on the other end of it:
    a chat product is that product's own receiver, not a branch in here."""
    sent: list[tuple[str, bytes, float]] = []
    hook = H.webhook_hook(
        "https://hooks.invalid/holds",
        timeout=2.5,
        send=lambda url, body, timeout: sent.append((url, body, timeout)),
    )
    assert hook is not None
    hook({"item_id": "T1", "question": "Which database?"})

    url, body, timeout = sent[0]
    assert url == "https://hooks.invalid/holds"
    assert b'"item_id": "T1"' in body
    # Bounded, because "never stalls the item" includes a receiver that
    # accepts the connection and then thinks about it for ever.
    assert timeout == 2.5


def test_a_webhook_that_fails_is_dropped_not_raised(tmp_path: Path) -> None:
    def explode(url: str, body: bytes, timeout: float) -> None:
        raise TimeoutError("the receiver never answered")

    clock = Clock()
    queue = make(
        tmp_path,
        clock,
        on_hold=H.webhook_hook("https://hooks.invalid/holds", send=explode),
    )
    claimed(queue)
    queue.hold("T1", question="Which database?", owner="worker-1")

    record = queue.get("T1")
    assert record is not None and record.state == HELD


def test_the_hook_is_injected_the_way_this_repo_injects_callables() -> None:
    """`on_hold` on the queue, assignable afterwards, exactly like
    `ModelClient.on_event`. Not a registry, not a plugin, not a provider."""
    import inspect

    from agent_harness.model_client import ModelClient

    assert "on_hold" in inspect.signature(WorkQueue.__init__).parameters
    on_event = inspect.signature(ModelClient.__init__).parameters["on_event"]
    on_hold = inspect.signature(WorkQueue.__init__).parameters["on_hold"]
    assert str(on_hold.annotation) == str(on_event.annotation)
    assert on_hold.default is None


def test_core_has_not_learned_what_any_product_is() -> None:
    """AGENTS.md's first rule, on the delivery path specifically."""
    source = (Path(H.__file__)).read_text().lower()
    for vendor in ("slack", "telegram", "discord", "pagerduty", "webhook.site"):
        assert vendor not in source, f"holds.py names {vendor}"


# ------------------------------------------------------ the pull path still


def test_a_hold_open_past_its_deadline_is_visible_in_the_summary(tmp_path: Path) -> None:
    """For anyone who configured no hook at all. One glance, and an item
    nobody answered is not indistinguishable from a healthy fleet."""
    clock = Clock()
    queue = make(tmp_path, clock, max_hold=600.0)
    claimed(queue)
    queue.hold("T1", question="Which database?", owner="worker-1")

    with api(queue) as client:
        fresh = client.get("/api/summary", headers=AUTH).json()
        assert fresh["holds_open"] == 1
        assert fresh["holds_overdue"] == []

        clock.advance(900.0)
        late = client.get("/api/summary", headers=AUTH).json()

    assert [h["item_id"] for h in late["holds_overdue"]] == ["T1"]
    overdue = late["holds_overdue"][0]
    assert overdue["project_id"] == "default"
    assert overdue["question"] == "Which database?"
    assert overdue["overdue_seconds"] == pytest.approx(300.0, abs=1.0)
    assert overdue["age_seconds"] == pytest.approx(900.0, abs=1.0)


def test_the_summary_does_not_sweep_away_what_it_is_reporting(tmp_path: Path) -> None:
    """`/api/holds` expires first on purpose — an operator must not answer
    into nothing. A status line doing the same would report an empty list for
    ever, which is the silence this issue is about."""
    clock = Clock()
    queue = make(tmp_path, clock, max_hold=600.0)
    claimed(queue)
    queue.hold("T1", question="Which database?", owner="worker-1")
    clock.advance(900.0)

    with api(queue) as client:
        first = client.get("/api/summary", headers=AUTH).json()
        second = client.get("/api/summary", headers=AUTH).json()

    assert len(first["holds_overdue"]) == 1
    assert len(second["holds_overdue"]) == 1, "reading the summary expired the hold"
