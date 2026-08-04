"""Stage K: a gate's answer, and what stopped an item, as types rather than one bit.

Two acceptance criteria drive this file, and they are asserted the way the
proposal states them — **through the API, without reading a log**. A taxonomy
that exists only inside the executor is a taxonomy nobody operating the system
can act on, so every claim about distinguishability ends at
`GET /api/work/{id}`.

The five check outcomes are exercised against a real subprocess wherever a real
subprocess can produce the condition: a command that exits non-zero, one that
never returns, one that is not installed. Only disk exhaustion is faked, and it
is faked in the *output* rather than by stubbing the classifier, because the
classifier reading that output is the thing under test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_harness import outcomes as O
from agent_harness import providers as P
from agent_harness.executor import Checks, Executor
from agent_harness.model_client import ModelClient, Response, RetryPolicy, Route
from agent_harness.outcomes import CheckResult, Stop
from agent_harness.work import BLOCKED, DONE, FAILED, PENDING, WorkQueue, WorkRecord
from conftest import make_queue

DIFF = """\
diff --git a/hello.txt b/hello.txt
index 3b18e51..8c7e5a6 100644
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello world
+hello harness
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    (path / "hello.txt").write_text("hello world\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


class ScriptedModel:
    def __init__(self, replies: Mapping[str, str]) -> None:
        self.replies = dict(replies)
        self.roles: list[str] = []

    def __call__(
        self, route: Route, messages: Sequence[Mapping[str, Any]], options: Mapping[str, Any]
    ) -> Response:
        role = str(route.options.get("role", route.model))
        self.roles.append(role)
        body = json.dumps({"choices": [{"message": {"content": self.replies.get(role, "ok")}}]})
        return Response(200, {}, body)


APPROVING = {"planner": "plan", "implementer": DIFF, "reviewer": "APPROVED\nfine"}
REJECTING = {"planner": "plan", "implementer": DIFF, "reviewer": "REJECTED\nno"}


def build(
    repo: Path,
    tmp_path: Path,
    replies: Mapping[str, str] = APPROVING,
    *,
    checks: Checks | None = None,
    events: list[dict[str, Any]] | None = None,
) -> tuple[Executor, WorkQueue]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    queue = make_queue(str(tmp_path / "w.sqlite"), lease_seconds=100.0)
    client = ModelClient(
        roles={
            role: Route(f"model-{role}", "https://api.example", P.GENERIC, options={"role": role})
            for role in ("planner", "implementer", "reviewer")
        },
        transport=ScriptedModel(replies),
        policy=RetryPolicy(max_attempts=2, backoff_seconds=0.001),
        sleep=lambda _s: None,
    )
    executor = Executor(
        queue,
        client,
        repo,
        checks=checks or Checks(),
        on_event=(events.append if events is not None else None),
        push=False,
    )
    return executor, queue


def add_item(queue: WorkQueue, item_id: str = "T1") -> None:
    queue.add(
        [
            WorkRecord(
                item_id=item_id,
                title="Change the greeting",
                brief="Change hello.txt to say 'hello harness'.",
            )
        ]
    )


def python(*code: str) -> list[str]:
    return [sys.executable, "-c", "; ".join(code)]


# ------------------------------------------------- the five check outcomes


def test_a_command_that_succeeds_passes(repo: Path) -> None:
    result = Checks(commands=[python("pass")]).run(repo)
    assert result.outcome == O.PASS
    assert result.ok


def test_a_command_that_exits_nonzero_is_the_item_s_fault(repo: Path) -> None:
    result = Checks(commands=[python("import sys", "sys.exit(1)")]).run(repo)
    assert result.outcome == O.FAIL
    assert not result.ok
    assert result.command


def test_a_command_that_never_returns_is_a_retry_not_a_failure(repo: Path) -> None:
    """The question was not answered. That is not the answer being no.

    Before this, a timeout escaped `Checks.run` as an exception and killed the
    attempt through the generic handler, so a busy machine and a broken diff
    were the same event.
    """
    result = Checks(commands=[python("import time", "time.sleep(30)")], timeout=0.3).run(repo)
    assert result.outcome == O.RETRY
    assert "0.3s" in result.detail


def test_a_command_that_is_not_installed_needs_a_person(repo: Path) -> None:
    """No diff fixes a missing interpreter and no retry clears it."""
    result = Checks(commands=[["definitely-not-a-real-program-xyz"]]).run(repo)
    assert result.outcome == O.ESCALATE
    assert "could not be started" in result.detail


def test_a_full_disk_needs_a_person_rather_than_failing_the_item(repo: Path) -> None:
    """Every subsequent item fails the same way, each paying a planner and an
    implementer first. That is a machine to fix, not a diff to reject."""
    result = Checks(
        commands=[
            python(
                "import sys",
                "sys.stderr.write('OSError: [Errno 28] No space left on device')",
                "sys.exit(1)",
            )
        ]
    ).run(repo)
    assert result.outcome == O.ESCALATE


def test_a_declared_fix_is_recorded_and_never_run(repo: Path, tmp_path: Path) -> None:
    """`fix_available` says a fix is derivable. Applying it is a later
    decision with its own evidence — a gate that silently repaired what it was
    meant to catch could not be trusted to have caught anything."""
    witness = tmp_path / "the-fix-ran"
    fix = python("import pathlib", f"pathlib.Path({str(witness)!r}).write_text('x')")
    failing = python("import sys", "sys.exit(1)")
    result = Checks(commands=[failing], fixes={" ".join(failing): fix}).run(repo)

    assert result.outcome == O.FIX_AVAILABLE
    assert result.fix == tuple(fix)
    assert not result.ok, "a derivable fix does not make a failing gate pass"
    assert not witness.exists(), "the fix was executed; it must only be recorded"


def test_a_fix_cannot_be_attached_to_an_outcome_that_is_not_fix_available() -> None:
    with pytest.raises(ValueError, match="only meaningful"):
        CheckResult(O.FAIL, "no", fix=("do", "something"))


def test_an_unknown_outcome_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown check outcome"):
        CheckResult("probably-fine")


def test_the_old_two_tuple_still_unpacks(repo: Path) -> None:
    """Three call sites already did this. The point of the stage is that a
    caller who wants the distinction can have it, not that everybody must
    restructure to keep the bit they already had."""
    passed, detail = Checks(commands=[python("pass")]).run(repo)
    assert passed is True and detail == ""
    passed, detail = Checks(commands=[python("import sys", "sys.exit(1)")]).run(repo)
    assert passed is False and detail


# --------------------------------- each outcome reaches the queue distinctly


def _api_item(queue: WorkQueue, item_id: str = "T1") -> dict[str, Any]:
    """What a client sees. Read through the API, never off the row."""
    import tempfile

    from fastapi.testclient import TestClient

    from agent_harness.api import create_api
    from agent_harness.store import EventStore

    store = EventStore(Path(tempfile.mkdtemp()) / "e.sqlite")
    with TestClient(create_api(store, queue=queue, token="t")) as client:  # noqa: S106
        response = client.get(f"/api/work/{item_id}", headers={"Authorization": "Bearer t"})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.parametrize(
    ("command", "expected_state", "expected_disposition", "expected_reason"),
    [
        (
            python("import sys", "sys.exit(1)"),
            FAILED,
            O.REFUSED,
            O.CHECKS_FAILED,
        ),
        (
            ["definitely-not-a-real-program-xyz"],
            BLOCKED,
            O.ESCALATED,
            O.CHECK_ESCALATED,
        ),
    ],
)
def test_a_check_outcome_reaches_the_api_as_its_own_state(
    repo: Path,
    tmp_path: Path,
    command: list[str],
    expected_state: str,
    expected_disposition: str,
    expected_reason: str,
) -> None:
    executor, queue = build(repo, tmp_path, checks=Checks(commands=[command]))
    add_item(queue)
    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == expected_state
    item = _api_item(queue)
    assert item["state"] == expected_state
    assert item["disposition"] == expected_disposition
    assert item["reason_kind"] == expected_reason


def test_a_check_that_could_not_answer_returns_the_item_without_costing_an_attempt(
    repo: Path, tmp_path: Path
) -> None:
    """A machine that was busy has not used up one of the item's tries."""
    executor, queue = build(
        repo,
        tmp_path,
        checks=Checks(commands=[python("import time", "time.sleep(30)")], timeout=0.3),
    )
    add_item(queue)
    outcome = executor.run_once()

    assert outcome is not None and outcome.state == PENDING
    item = _api_item(queue)
    assert item["state"] == PENDING
    assert item["disposition"] == O.WITHHELD
    assert item["reason_kind"] == O.CHECK_TRANSIENT
    assert item["attempts"] == 0, "a gate that could not answer must not cost an attempt"


def test_an_escalating_check_does_not_cost_an_attempt_either(repo: Path, tmp_path: Path) -> None:
    executor, queue = build(
        repo, tmp_path, checks=Checks(commands=[["definitely-not-a-real-program-xyz"]])
    )
    add_item(queue)
    executor.run_once()
    assert _api_item(queue)["attempts"] == 0


def test_a_failing_check_still_costs_an_attempt_exactly_as_it_always_did(
    repo: Path, tmp_path: Path
) -> None:
    """The accounting `max_attempts` does is unchanged by this stage.

    Whether a crash should cost an attempt is D11, it is open, and naming a
    distinction is not the place to answer it by moving a number.
    """
    executor, queue = build(
        repo, tmp_path, checks=Checks(commands=[python("import sys", "sys.exit(1)")])
    )
    add_item(queue)
    executor.run_once()
    assert _api_item(queue)["attempts"] == 1


def test_a_declared_fix_is_announced_in_the_event_stream(repo: Path, tmp_path: Path) -> None:
    events: list[dict[str, Any]] = []
    failing = python("import sys", "sys.exit(1)")
    executor, queue = build(
        repo,
        tmp_path,
        checks=Checks(commands=[failing], fixes={" ".join(failing): ["ruff", "format"]}),
        events=events,
    )
    add_item(queue)
    executor.run_once()

    announced = [e for e in events if e.get("outcome") == "fix_available"]
    assert len(announced) == 1
    assert "ruff format" in announced[0]["detail"]


# ----------------------- a rejection and a crash are not the same thing


def test_a_reviewer_rejection_and_a_worker_crash_are_distinguishable(
    repo: Path, tmp_path: Path
) -> None:
    """§6.4's second criterion, and the reason the stage exists.

    Both land in `failed`. One is the system working — a gate caught something
    — and the other is the system broken. Reading them as the same thing is
    how a fleet with a crashing worker looks like a fleet producing bad diffs.
    """
    rejected_executor, rejected_queue = build(repo, tmp_path / "a", REJECTING)
    add_item(rejected_queue)
    rejected = rejected_executor.run_once()

    crashed_executor, crashed_queue = build(repo, tmp_path / "b", APPROVING)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the harness fell over")

    crashed_executor.client.call = explode  # type: ignore[assignment]
    add_item(crashed_queue)
    crashed = crashed_executor.run_once()

    assert rejected is not None and crashed is not None
    assert rejected.state == crashed.state == FAILED, "the precondition: both look the same"

    rejected_item = _api_item(rejected_queue)
    crashed_item = _api_item(crashed_queue)
    assert rejected_item["disposition"] == O.REFUSED
    assert rejected_item["reason_kind"] == O.REVIEW_REJECTED
    assert crashed_item["disposition"] == O.CRASHED
    assert crashed_item["reason_kind"] == O.WORKER_ERROR
    assert rejected_item["disposition"] != crashed_item["disposition"]


def test_a_completed_item_says_so(repo: Path, tmp_path: Path) -> None:
    executor, queue = build(repo, tmp_path)
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None and outcome.state == DONE

    item = _api_item(queue)
    assert item["disposition"] == O.COMPLETED
    assert item["reason_kind"] == ""


def test_an_item_nobody_has_finished_with_has_no_disposition(tmp_path: Path) -> None:
    """Empty is "nobody has finished with this", which is not a sixth
    disposition and must not read as one."""
    queue = make_queue(str(tmp_path / "w.sqlite"))
    add_item(queue)
    item = _api_item(queue)
    assert item["disposition"] == ""
    assert item["reason_kind"] == ""


def test_a_diff_that_does_not_apply_is_a_refusal_not_a_crash(repo: Path, tmp_path: Path) -> None:
    executor, queue = build(
        repo,
        tmp_path,
        {"planner": "plan", "implementer": "```diff\ndiff --git a/x b/x\n@@ junk @@\n```"},
    )
    add_item(queue)
    executor.run_once()

    item = _api_item(queue)
    assert item["disposition"] == O.REFUSED
    assert item["reason_kind"] == O.PATCH_REJECTED


def test_a_new_result_clears_the_previous_one_s_reason(repo: Path, tmp_path: Path) -> None:
    """A stale explanation attached to a fresh result is worse than none."""
    executor, queue = build(repo, tmp_path, REJECTING)
    add_item(queue)
    executor.run_once()
    assert _api_item(queue)["reason_kind"] == O.REVIEW_REJECTED

    queue.requeue("T1")
    approving, _ = build(repo, tmp_path, APPROVING)
    approving.queue = queue
    approving.run_once()
    item = _api_item(queue)
    assert item["state"] == DONE
    assert item["disposition"] == O.COMPLETED
    assert item["reason_kind"] == ""


# ------------------------------------------------------ the rules it keeps


def test_no_gate_became_skippable_optional_or_cheaper(repo: Path, tmp_path: Path) -> None:
    """§6.4's third criterion, asserted as behaviour rather than promised.

    A check that escalates still stops the item, and the reviewer is still
    never reached. `escalate` is an additional outcome, never a way for a
    check to decline to fail.
    """
    events: list[dict[str, Any]] = []
    executor, queue = build(
        repo,
        tmp_path,
        checks=Checks(commands=[["definitely-not-a-real-program-xyz"]]),
        events=events,
    )
    add_item(queue)
    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state != DONE
    assert "review" not in outcome.stages
    assert not any(e.get("outcome", "").startswith("review_") for e in events)


def test_escalate_is_not_a_softer_pass() -> None:
    """Only `pass` satisfies a gate. Spelled as a set so a sixth outcome
    forces a decision rather than defaulting to one side."""
    assert {O.PASS} == O.SATISFIED
    for outcome in O.CHECK_OUTCOMES:
        if outcome != O.PASS:
            assert not CheckResult(outcome, "x").ok


def test_the_taxonomy_does_not_overlap_the_provider_one() -> None:
    """Two vocabularies, deliberately. A gateway's opinion about our budget
    and a test suite's opinion about a diff are not the same kind of fact, and
    merging them would put a local policy decision into the never-retry set."""
    provider_kinds = {P.RPM, P.WINDOW_CAP, P.TERMINAL_CAP, P.NON_RETRYABLE, P.TRANSIENT, P.FATAL}
    assert not provider_kinds & set(O.DISPOSITIONS)
    assert not provider_kinds & set(O.CHECK_OUTCOMES)


def test_a_stop_refuses_a_disposition_or_reason_it_does_not_know() -> None:
    with pytest.raises(ValueError, match="unknown disposition"):
        Stop("probably-fine")
    with pytest.raises(ValueError, match="unknown reason kind"):
        Stop(O.REFUSED, "probably-fine")


def test_every_check_outcome_except_pass_has_a_route_to_the_queue() -> None:
    """A sixth outcome added without a mapping would silently take whatever
    the previous line left in `outcome.state`."""
    assert set(O.FROM_CHECK) == set(O.CHECK_OUTCOMES) - {O.PASS}
    for disposition, reason, state, _consumes in O.FROM_CHECK.values():
        assert disposition in O.DISPOSITIONS
        assert reason in O.REASON_KINDS
        assert state in (PENDING, FAILED, BLOCKED, DONE)


def test_stage_k_did_not_answer_d8() -> None:
    """The one thing this stage was told to stop at.

    D8 is whether third-party gates get a registration mechanism. A richer
    result from the gates that already exist is a different thing, and if this
    module ever grows a registry the stage has reached D8 and must stop.
    """
    import agent_harness.outcomes as module

    forbidden = [name for name in dir(module) if "register" in name or "plugin" in name]
    assert not forbidden, f"outcomes.py grew a registration mechanism: {forbidden}"


# ----------------------------------------------------- the stored column


def test_an_upgraded_database_reports_no_disposition_rather_than_a_wrong_one(
    tmp_path: Path,
) -> None:
    """Additive, so an older row reads as "nobody has finished with this"."""
    import sqlite3

    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE work (
            project_id TEXT NOT NULL DEFAULT 'default',
            item_id TEXT NOT NULL,
            issue INTEGER,
            title TEXT NOT NULL,
            brief TEXT NOT NULL DEFAULT '',
            depends_on TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL DEFAULT 'pending',
            owner TEXT,
            lease_until REAL NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            branch TEXT,
            pr_url TEXT,
            updated_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (project_id, item_id)
        );
        INSERT INTO work (item_id, title, state, attempts, last_error)
        VALUES ('OLD1', 'From before', 'failed', 3, 'something went wrong');
        """
    )
    connection.commit()
    connection.close()

    queue = WorkQueue(str(path))
    record = queue.get("OLD1")
    assert record is not None
    assert record.state == FAILED
    assert record.attempts == 3, "the upgrade did not lose what was there"
    assert record.disposition == ""
    assert record.reason_kind == ""
