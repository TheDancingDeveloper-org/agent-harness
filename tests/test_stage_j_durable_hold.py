"""Stage J: an item waiting on a person, durably, answerable from anywhere.

`waiting_for_input` was a projection over recent events. The row stayed
`claimed`, the heartbeat kept stamping, and the lease kept renewing — so a
lease whose whole purpose is to distinguish *slow* from *dead* was holding open
a human's inbox. Issue #103 is that hole seen from outside.

**The load-bearing test in this file is
`test_a_held_item_survives_its_worker_being_killed`.** Everything else is
detail; that one is the claim.

The answering process is deliberately built with no reference at all to the
worker that asked — a fresh `WorkQueue` over the same file, which is what a
phone hitting the API is.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_harness import holds as H
from agent_harness import outcomes as O
from agent_harness.work import (
    BLOCKED,
    CLAIMED,
    HELD,
    PENDING,
    RUNNING,
    Project,
    WorkQueue,
    WorkRecord,
)


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make(
    tmp_path: Path, clock: Clock, *, lease: float = 100.0, max_hold: float = 3600.0
) -> WorkQueue:
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=lease, now=clock)
    queue.add_project(Project(project_id="default", name="Default", max_hold_seconds=max_hold))
    queue.set_control(RUNNING)
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


def test_a_held_item_survives_its_worker_being_killed(tmp_path: Path) -> None:
    """§9.4's first criterion, and the whole point of the stage.

    Held for longer than a lease, worker gone: still held. Not re-claimed, not
    failed, not silently resumed.
    """
    clock = Clock()
    queue = make(tmp_path, clock, lease=100.0, max_hold=100_000.0)
    claimed(queue)
    queue.hold("T1", question="Which database should this use?", owner="worker-1")

    # Far past the lease, and nowhere near the hold window. The worker is
    # gone; nothing renewed anything.
    clock.advance(10_000.0)

    assert queue.claim("worker-2") is None, "a held item was handed to another worker"
    record = queue.get("T1")
    assert record is not None
    assert record.state == HELD
    assert record.owner == "worker-1", "D12 keeps the claim, so the worktree survives"
    hold = queue.holds.current("default", "T1")
    assert hold is not None and hold.state == H.OPEN


def test_the_lease_is_suspended_rather_than_renewed(tmp_path: Path) -> None:
    """D12: suspended, not heartbeated through.

    A lease that kept renewing would be using the mechanism for telling slow
    from dead to hold open an inbox — which is what this replaced.
    """
    clock = Clock()
    queue = make(tmp_path, clock)
    claimed(queue)
    before = queue.get("T1")
    assert before is not None and before.lease_until > clock()

    queue.hold("T1", question="Which one?", owner="worker-1")
    after = queue.get("T1")
    assert after is not None
    assert after.lease_until == 0, "the lease is suspended, not extended"
    assert after.held_until > clock()


def test_a_held_item_is_not_stale(tmp_path: Path) -> None:
    """`stale` means a worker died holding an item. A held item's worker may
    well have died, and the item is still not up for grabs."""
    clock = Clock()
    queue = make(tmp_path, clock, lease=10.0)
    claimed(queue)
    queue.hold("T1", question="Which one?", owner="worker-1")
    clock.advance(1000.0)
    assert [r.item_id for r in queue.stale()] == []


# ----------------------------------------------- answering from elsewhere


def test_the_answer_arrives_from_a_process_with_no_attachment_to_the_original(
    tmp_path: Path,
) -> None:
    """§9.4's second criterion. The answer comes from a phone, not from the
    terminal that asked."""
    clock = Clock()
    asking = make(tmp_path, clock)
    claimed(asking, "worker-1")
    hold = asking.hold("T1", question="Which database?", owner="worker-1")

    # A different process entirely: its own queue object over the same file,
    # holding nothing of the worker that asked.
    answering = WorkQueue(str(tmp_path / "w.sqlite"), now=clock)
    answered = answering.answer_hold(
        "T1",
        hold.resume_token,
        H.Answer(text="postgres", data={"choice": "postgres"}, who="a human on a train"),
    )

    assert answered.state == H.ANSWERED
    record = asking.get("T1")
    assert record is not None
    assert record.state == CLAIMED, "it goes back to the worker that asked"
    assert record.owner == "worker-1"
    assert record.lease_until > clock(), "with a fresh lease, so it can carry on"
    assert record.held_until == 0


def test_the_worker_that_asked_gets_the_item_back_and_a_dead_one_loses_it_normally(
    tmp_path: Path,
) -> None:
    """The claim is handed back to the asker. If that worker really is gone,
    the lease expires exactly as it always did and someone else continues."""
    clock = Clock()
    queue = make(tmp_path, clock, lease=100.0)
    claimed(queue, "worker-1")
    hold = queue.hold("T1", question="Which one?", owner="worker-1")
    queue.answer_hold("T1", hold.resume_token, H.Answer(text="that one"))

    assert queue.claim("worker-2") is None, "the lease is live; worker-1 owns it"
    clock.advance(1000.0)
    taken = queue.claim("worker-2")
    assert taken is not None and taken.item_id == "T1"


def test_a_wrong_token_is_refused(tmp_path: Path) -> None:
    """The token authorises an answer to *this* question. A reply arriving
    after a timeout must not land on whatever the item is doing later."""
    clock = Clock()
    queue = make(tmp_path, clock)
    claimed(queue)
    queue.hold("T1", question="Which one?", owner="worker-1")
    with pytest.raises(H.HoldError, match="does not answer this question"):
        queue.answer_hold("T1", "not-the-token", H.Answer(text="x"))
    record = queue.get("T1")
    assert record is not None and record.state == HELD


def test_answering_an_item_with_no_question_says_so(tmp_path: Path) -> None:
    clock = Clock()
    queue = make(tmp_path, clock)
    with pytest.raises(H.HoldError, match="no open question"):
        queue.answer_hold("T1", "anything", H.Answer(text="x"))


def test_a_hold_needs_a_question(tmp_path: Path) -> None:
    """A hold with no question is indistinguishable from the hang it exists to
    be distinguished from."""
    clock = Clock()
    queue = make(tmp_path, clock)
    claimed(queue)
    with pytest.raises(H.HoldError, match="needs a question"):
        queue.hold("T1", question="   ", owner="worker-1")


def test_an_unclaimed_item_cannot_be_held(tmp_path: Path) -> None:
    """A hold suspends an *attempt*. Parking an item nobody is working on is
    what `block` already is."""
    clock = Clock()
    queue = make(tmp_path, clock)
    with pytest.raises(H.HoldError, match="not claimed"):
        queue.hold("T1", question="Which one?")


def test_a_worker_cannot_hold_an_item_it_does_not_own(tmp_path: Path) -> None:
    clock = Clock()
    queue = make(tmp_path, clock)
    claimed(queue, "worker-1")
    with pytest.raises(H.HoldError, match="not owned by"):
        queue.hold("T1", question="Which one?", owner="worker-2")


def test_one_question_at_a_time(tmp_path: Path) -> None:
    """The session host reports "waiting" every few seconds. A question per
    poll would be an inbox nobody could read."""
    clock = Clock()
    queue = make(tmp_path, clock)
    claimed(queue)
    queue.hold("T1", question="Which one?", owner="worker-1")
    with pytest.raises(H.HoldError, match="already has an unanswered question"):
        queue.hold("T1", question="And which other one?", owner="worker-1")


# ------------------------------------------------------------- expiry


def test_hold_expiry_returns_the_item_to_blocked_with_the_question_preserved(
    tmp_path: Path,
) -> None:
    """§9.4's fourth criterion, and the rule that makes it safe.

    **To `blocked`, never to `ready`.** A hold that times out has not been
    approved, and an item that walked back onto the ready queue would be one
    that passed a gate by waiting.
    """
    clock = Clock()
    queue = make(tmp_path, clock, max_hold=60.0)
    claimed(queue)
    queue.hold("T1", question="Which database should this use?", owner="worker-1")

    clock.advance(100.0)
    expired = queue.expire_holds()
    assert len(expired) == 1

    record = queue.get("T1")
    assert record is not None
    assert record.state == BLOCKED, "never ready, and never done"
    assert record.state != PENDING
    assert "Which database should this use?" in (record.last_error or ""), (
        "the question was lost with the hold"
    )
    assert record.disposition == O.ESCALATED
    assert record.reason_kind == O.HOLD_EXPIRED
    assert record.owner is None and record.held_until == 0

    closed = queue.holds.history("default", "T1")
    assert [h.state for h in closed] == [H.EXPIRED]


def test_an_expired_hold_cannot_then_be_answered(tmp_path: Path) -> None:
    """A person who typed an answer too late is told, rather than having it
    land silently on an item that has moved on."""
    clock = Clock()
    queue = make(tmp_path, clock, max_hold=60.0)
    claimed(queue)
    hold = queue.hold("T1", question="Which one?", owner="worker-1")
    clock.advance(100.0)
    queue.expire_holds()

    with pytest.raises(H.HoldError, match="no open question"):
        queue.answer_hold("T1", hold.resume_token, H.Answer(text="too late"))


def test_expiry_is_swept_by_a_claim_scan_without_anything_scheduling_it(
    tmp_path: Path,
) -> None:
    """A sweep that only ran under a cron would leave a held item stuck for as
    long as the cron was broken."""
    clock = Clock()
    queue = make(tmp_path, clock, max_hold=60.0)
    claimed(queue)
    queue.hold("T1", question="Which one?", owner="worker-1")
    queue.add([WorkRecord(item_id="T2", title="Something else", brief="Also do it.")])

    clock.advance(100.0)
    taken = queue.claim("worker-2")

    assert taken is not None and taken.item_id == "T2", "it took the other item"
    held = queue.get("T1")
    assert held is not None and held.state == BLOCKED, "and swept the expired hold on the way"


def test_a_hold_can_be_unbounded_and_the_cost_is_visible(tmp_path: Path) -> None:
    """`max_seconds=0` never expires. Allowed, and it ties up a worker for
    ever — which is why the default is not that."""
    clock = Clock()
    queue = make(tmp_path, clock)
    claimed(queue)
    hold = queue.hold("T1", question="Which one?", owner="worker-1", max_seconds=0)
    assert hold.expires_at == 0.0
    clock.advance(10_000_000.0)
    assert queue.expire_holds() == []
    record = queue.get("T1")
    assert record is not None and record.state == HELD


def test_the_default_maximum_is_not_unlimited() -> None:
    """Unlike the budgets, where unlimited is the safe upgrade default. A hold
    keeps the claim, so an unbounded default would let one unanswered question
    tie up a worker for ever, and "unlimited" is not a safe reading of "nobody
    said"."""
    assert Project(project_id="p", name="P").max_hold_seconds == H.DEFAULT_MAX_HOLD_SECONDS
    assert H.DEFAULT_MAX_HOLD_SECONDS > 0


# --------------------------------------------------------------- the API


def test_a_held_item_shows_its_question_and_its_age(tmp_path: Path) -> None:
    """§9.4's third criterion, and the answer to #103: a silent-but-active
    session is distinguishable from a hang."""
    clock = Clock()
    queue = make(tmp_path, clock)
    claimed(queue)
    queue.hold(
        "T1",
        question="Which database should this use?",
        owner="worker-1",
        session_url="https://sessions.invalid/t/abc",
    )
    clock.advance(120.0)

    with api(queue) as client:
        item = client.get("/api/work/T1", headers=AUTH).json()
    assert item["state"] == HELD
    assert item["hold"]["question"] == "Which database should this use?"
    assert item["hold"]["age_seconds"] == pytest.approx(120.0, abs=1.0)
    assert item["hold"]["session_url"] == "https://sessions.invalid/t/abc"
    assert item["hold"]["expires_at"]


def test_a_running_item_has_no_hold_and_that_is_how_a_hang_reads(tmp_path: Path) -> None:
    """The other half of #103. An item that is claimed, making no progress and
    holding no question is a hang, and now says so by omission."""
    clock = Clock()
    queue = make(tmp_path, clock)
    claimed(queue)
    with api(queue) as client:
        item = client.get("/api/work/T1", headers=AUTH).json()
    assert item["state"] == CLAIMED
    assert item["hold"] is None


def test_the_inbox_lists_every_open_question_oldest_first(tmp_path: Path) -> None:
    clock = Clock()
    queue = make(tmp_path, clock)
    queue.add([WorkRecord(item_id="T2", title="Another", brief="Also.")])
    claimed(queue, "worker-1")
    queue.hold("T1", question="First question?", owner="worker-1")
    clock.advance(10.0)
    second = queue.claim("worker-2")
    assert second is not None
    queue.hold(second.item_id, question="Second question?", owner="worker-2")

    with api(queue) as client:
        body = client.get("/api/holds", headers=AUTH).json()
    assert [h["question"] for h in body["open"]] == ["First question?", "Second question?"]


def test_answering_over_the_api_releases_the_item(tmp_path: Path) -> None:
    clock = Clock()
    queue = make(tmp_path, clock)
    claimed(queue)
    hold = queue.hold("T1", question="Which one?", owner="worker-1")

    with api(queue) as client:
        response = client.post(
            "/api/work/T1/answer",
            json={"resume_token": hold.resume_token, "text": "that one", "who": "someone"},
            headers=AUTH,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == CLAIMED
        assert body["hold"]["state"] == H.ANSWERED
        assert body["hold"]["answered_by"] == "someone"

        item = client.get("/api/work/T1", headers=AUTH).json()
    assert item["state"] == CLAIMED
    assert item["hold"] is None


def test_the_api_says_which_kind_of_no_it_is(tmp_path: Path) -> None:
    """404 for "there is nothing to answer", 409 for "you may not answer this
    one". A person who typed an answer deserves to know which."""
    clock = Clock()
    queue = make(tmp_path, clock)
    claimed(queue)
    hold = queue.hold("T1", question="Which one?", owner="worker-1")

    with api(queue) as client:
        wrong = client.post("/api/work/T1/answer", json={"resume_token": "nope"}, headers=AUTH)
        assert wrong.status_code == 409

        queue.answer_hold("T1", hold.resume_token, H.Answer(text="done"))
        gone = client.post(
            "/api/work/T1/answer", json={"resume_token": hold.resume_token}, headers=AUTH
        )
        assert gone.status_code == 404


def test_an_answer_with_no_token_is_refused_by_the_schema() -> None:
    import pydantic

    from agent_harness.schemas import AnswerRequest

    with pytest.raises(pydantic.ValidationError):
        AnswerRequest(resume_token="")


# ------------------------------------------------ what it must not become


def _holds_code() -> str:
    """`holds.py` with its docstrings and comments removed.

    The prose in that module *forbids* the things these tests grep for, at
    length, so grepping the raw text would fail on the very sentences that say
    it must not happen. What is asserted is the code.
    """
    import ast

    path = Path(__file__).resolve().parents[1] / "src" / "agent_harness" / "holds.py"
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0) if len(body) > 1 else body.__setitem__(0, ast.Pass())
    return ast.unparse(tree)


def test_nothing_interprets_an_answer() -> None:
    """§9.3: no model may read the human's answer to decide what it meant.

    CrewAI routes on an LLM's reading of human feedback; under `AGENTS.md`
    that is a gate decided by a model. Asserted against the code, because this
    is the kind of thing that gets added later by somebody being helpful.
    """
    code = _holds_code()
    for forbidden in ("ModelClient", "model_client", "PLAN_PROMPT", "prompt"):
        assert forbidden not in code, f"holds.py's code mentions {forbidden!r}"


def test_nothing_writes_into_a_session() -> None:
    """§9.3 and `COORDINATION-PLANE.md` §5.1: the process may be at a shell,
    and an answer becomes a command."""
    code = _holds_code()
    for forbidden in ("send_keys", "write_session", "send_input", "stdin"):
        assert forbidden not in code


def test_the_answer_is_stored_verbatim(tmp_path: Path) -> None:
    """Structured data or a message, recorded exactly. Not summarised, not
    normalised, not interpreted."""
    clock = Clock()
    queue = make(tmp_path, clock)
    claimed(queue)
    hold = queue.hold("T1", question="Which one?", owner="worker-1")
    answered = queue.answer_hold(
        "T1",
        hold.resume_token,
        H.Answer(text="the second one, but only on tuesdays", data={"choice": 2}),
    )
    import json

    stored = json.loads(answered.answer)
    assert stored["text"] == "the second one, but only on tuesdays"
    assert stored["data"] == {"choice": 2}


def test_being_held_is_not_approval(tmp_path: Path) -> None:
    """A hold weakens no gate. An answered item goes back to `claimed` — back
    into the pipeline at the point it stopped, not past anything."""
    clock = Clock()
    queue = make(tmp_path, clock)
    claimed(queue)
    hold = queue.hold("T1", question="Which one?", owner="worker-1")
    queue.answer_hold("T1", hold.resume_token, H.Answer(text="approved!"))
    record = queue.get("T1")
    assert record is not None
    assert record.state == CLAIMED, "not done, and not past any gate"
    assert record.disposition == ""


def test_this_is_not_the_coordination_plane() -> None:
    """§9.3: the item-level hold only. No ledger, no rooms, no oversight
    actor."""
    code = _holds_code()
    for forbidden in ("class Room", "class Ledger", "oversight", "broadcast"):
        assert forbidden not in code
