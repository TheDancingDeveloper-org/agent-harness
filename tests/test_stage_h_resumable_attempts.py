"""Stage H: a killed worker resumes where it stopped instead of re-paying.

**How a killed worker is simulated, and why it matters.** The transport raises
`KeyboardInterrupt` — a `BaseException`, so it escapes `run_once`'s
`except Exception` without releasing the item. The row stays `claimed` with a
live lease, which is exactly the state a `kill -9` leaves behind. The clock is
then advanced past the lease and a *second* executor over the *same database*
claims it, which is what a fleet does.

That is deliberate rather than convenient. Raising an ordinary exception would
release the item cleanly and prove something much weaker — that the harness can
resume work it tidied up after itself.

**Cost is counted as model calls per role**, not as time. §7.4 says the
no-second-planner claim must be proven by the event stream rather than by
timing, and a call counter on the transport is the same fact one layer lower.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_harness import attempts as A
from agent_harness import providers as P
from agent_harness.executor import Checks, Executor
from agent_harness.model_client import ModelClient, Response, RetryPolicy, Route
from agent_harness.work import CLAIMED, DONE, PENDING, RUNNING, WorkQueue, WorkRecord

DIFF = """\
diff --git a/hello.txt b/hello.txt
index 3b18e51..8c7e5a6 100644
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello world
+hello harness
"""

REPLIES = {
    "planner": json.dumps(
        {
            "plan": "edit hello.txt",
            "targets": [{"path": "hello.txt", "reason": "the greeting is here"}],
            "cannot_identify_target": None,
        }
    ),
    "implementer": DIFF,
    "reviewer": "APPROVED\nlooks right",
}


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


class Clock:
    """A clock a test can move. No sleeping anywhere in this file."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class CountingModel:
    """Replies per role, counted, and able to die on command.

    `die_on` is a role name. The first call to that role raises
    `KeyboardInterrupt`, which is how this file spells "the worker was killed";
    afterwards the role answers normally, so the second worker can finish.
    """

    def __init__(self, die_on: str | None = None) -> None:
        self.calls: dict[str, int] = {}
        self.die_on = die_on
        self.died = False

    def __call__(
        self, route: Route, messages: Sequence[Mapping[str, Any]], options: Mapping[str, Any]
    ) -> Response:
        role = str(route.options.get("role", route.model))
        self.calls[role] = self.calls.get(role, 0) + 1
        if role == self.die_on and not self.died:
            self.died = True
            raise KeyboardInterrupt(f"the worker was killed during {role}")
        body = json.dumps({"choices": [{"message": {"content": REPLIES.get(role, "ok")}}]})
        return Response(200, {}, body)


class DyingChecks(Checks):
    """Checks that kill the worker after passing, once.

    The §7.4 case verbatim: *killed after checks pass and before review*. Any
    later boundary is reached through `CountingModel(die_on="reviewer")`.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.died = False

    def run(self, repo: Path) -> Any:
        result = super().run(repo)
        if not self.died:
            self.died = True
            raise KeyboardInterrupt("the worker was killed just after the checks passed")
        return result


def make(
    repo: Path,
    db: Path,
    clock: Clock,
    *,
    die_on: str | None = None,
    checks: Checks | None = None,
    durability: str | None = None,
    events: list[dict[str, Any]] | None = None,
) -> tuple[Executor, WorkQueue, CountingModel]:
    queue = WorkQueue(str(db), lease_seconds=100.0, now=clock)
    queue.set_control(RUNNING)
    transport = CountingModel(die_on=die_on)
    client = ModelClient(
        roles={
            role: Route(f"model-{role}", "https://api.example", P.GENERIC, options={"role": role})
            for role in ("planner", "implementer", "reviewer")
        },
        transport=transport,
        policy=RetryPolicy(max_attempts=1, backoff_seconds=0.001),
        sleep=lambda _s: None,
        now=clock,
    )
    executor = Executor(
        queue,
        client,
        repo,
        checks=checks if checks is not None else Checks(),
        push=False,
        now=clock,
        durability=durability,
        on_event=(events.append if events is not None else None),
    )
    return executor, queue, transport


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


# ------------------------------------------------------- the headline claim


def test_a_worker_killed_before_review_resumes_without_a_second_planner_or_implementer(
    repo: Path, tmp_path: Path
) -> None:
    """§7.4's first criterion, exactly as it is written."""
    clock = Clock()
    db = tmp_path / "q.sqlite"

    first, queue, first_model = make(repo, db, clock, checks=DyingChecks(commands=[]))
    add_item(queue)
    with pytest.raises(KeyboardInterrupt):
        first.run_once()

    # The killed worker released nothing: the row is still claimed, exactly as
    # `kill -9` leaves it.
    record = queue.get("T1")
    assert record is not None and record.state == CLAIMED
    assert first_model.calls == {"planner": 1, "implementer": 1}

    # The lease expires and someone else picks it up.
    clock.advance(200.0)
    events: list[dict[str, Any]] = []
    second, _queue, second_model = make(repo, db, clock, events=events)
    outcome = second.run_once()

    assert outcome is not None and outcome.state == DONE
    assert second_model.calls == {"reviewer": 1}, (
        "the resumed attempt must not re-pay for the planner or the implementer"
    )

    # Proven by the event stream, not by timing, as §7.4 requires.
    resumed = [e for e in events if e.get("outcome") == "resumed"]
    assert resumed, "the resumption is not in the event stream"
    assert any("implemented" in (e.get("detail") or "") for e in resumed)


def test_the_resumed_attempt_is_the_same_attempt(repo: Path, tmp_path: Path) -> None:
    """D11, resolved: a resumed attempt continues the existing one.

    So `max_attempts` goes on bounding genuine failures rather than crashes.
    """
    clock = Clock()
    db = tmp_path / "q.sqlite"
    first, queue, _ = make(repo, db, clock, checks=DyingChecks(commands=[]))
    add_item(queue)
    with pytest.raises(KeyboardInterrupt):
        first.run_once()
    after_crash = queue.get("T1")
    assert after_crash is not None and after_crash.attempts == 1

    clock.advance(200.0)
    second, _queue, _model = make(repo, db, clock)
    second.run_once()

    finished = queue.get("T1")
    assert finished is not None
    assert finished.attempts == 1, "the crash and the resumption are one attempt, not two"


def test_a_first_attempt_still_counts(repo: Path, tmp_path: Path) -> None:
    """The other half of D11: nothing else about the accounting moved."""
    clock = Clock()
    executor, queue, _ = make(repo, tmp_path / "q.sqlite", clock)
    add_item(queue)
    executor.run_once()
    record = queue.get("T1")
    assert record is not None and record.attempts == 1


# ------------------------------------------- killed at each boundary in turn


@pytest.mark.parametrize(
    ("die_on", "checks_die", "expected_second_calls"),
    [
        # Killed in the planner: nothing durable yet, so everything is re-paid.
        # This is the honest floor, not a failure of the stage.
        ("planner", False, {"planner": 1, "implementer": 1, "reviewer": 1}),
        # Killed in the implementer: the plan survives.
        ("implementer", False, {"implementer": 1, "reviewer": 1}),
        # Killed just after the checks: the plan and the diff survive.
        (None, True, {"reviewer": 1}),
        # Killed in the reviewer: the checkpoint survives, so the resumed
        # attempt pays for the review and nothing else.
        ("reviewer", False, {"reviewer": 1}),
    ],
)
def test_a_worker_killed_at_each_boundary_resumes_from_it(
    repo: Path,
    tmp_path: Path,
    die_on: str | None,
    checks_die: bool,
    expected_second_calls: dict[str, int],
) -> None:
    """§7.4's second criterion. Each boundary, in turn, on its own database."""
    clock = Clock()
    db = tmp_path / "q.sqlite"
    first, queue, _first_model = make(
        repo,
        db,
        clock,
        die_on=die_on,
        checks=DyingChecks(commands=[]) if checks_die else None,
    )
    add_item(queue)
    with pytest.raises(KeyboardInterrupt):
        first.run_once()

    clock.advance(200.0)
    second, _queue, second_model = make(repo, db, clock)
    outcome = second.run_once()

    assert outcome is not None and outcome.state == DONE
    assert second_model.calls == expected_second_calls


def test_the_stage_reached_is_recorded_for_every_boundary(repo: Path, tmp_path: Path) -> None:
    """One row per stage, keyed by attempt, in the fixed order."""
    clock = Clock()
    db = tmp_path / "q.sqlite"
    executor, queue, _ = make(repo, db, clock)
    add_item(queue)
    executor.run_once()

    history = queue.attempts_log.history("default", "T1")
    stages = [record.stage for _attempt, record in history]
    assert stages == list(A.STAGES), stages
    assert {attempt for attempt, _ in history} == {1}


def test_each_recorded_stage_carries_the_artefact_that_makes_it_resumable(
    repo: Path, tmp_path: Path
) -> None:
    clock = Clock()
    executor, queue, _ = make(repo, tmp_path / "q.sqlite", clock)
    add_item(queue)
    executor.run_once()

    by_stage = {record.stage: record for _a, record in queue.attempts_log.history("default", "T1")}
    assert by_stage[A.PLANNED].artefact["targets"][0]["path"] == "hello.txt"
    assert "hello harness" in by_stage[A.IMPLEMENTED].artefact["diff"]
    assert by_stage[A.APPLIED].artefact["branch"] == "harness/t1"
    assert by_stage[A.CHECKPOINTED].artefact["sha"]
    assert by_stage[A.REVIEWED].artefact["verdict"] == "approved"
    # And the graph revision it was admitted at, on every row.
    assert all(
        record.admitted_revision >= 0 for _a, record in queue.attempts_log.history("default", "T1")
    )


# ------------------------------------------------------- durability modes


@pytest.mark.parametrize("mode", list(A.MODES))
def test_every_durability_mode_completes_an_item_and_records_which_it_used(
    repo: Path, tmp_path: Path, mode: str
) -> None:
    """§7.4's fifth criterion. Every mode is exercised, and the mode is
    recorded — on the rows and in the resumption events."""
    clock = Clock()
    executor, queue, _ = make(repo, tmp_path / "q.sqlite", clock, durability=mode)
    add_item(queue)
    outcome = executor.run_once()

    assert outcome is not None and outcome.state == DONE
    history = queue.attempts_log.history("default", "T1")
    assert history, f"{mode} recorded nothing at all"
    assert {record.mode for _a, record in history} == {mode}


def test_exit_durability_loses_the_position_and_says_nothing_it_should_not(
    repo: Path, tmp_path: Path
) -> None:
    """The mode's whole meaning: nothing is durable until the attempt ends, so
    a killed worker resumes from the planner exactly as it did before this
    module existed. That is the cost of the cheapest setting, and it is stated
    rather than discovered."""
    clock = Clock()
    db = tmp_path / "q.sqlite"
    first, queue, _ = make(repo, db, clock, checks=DyingChecks(commands=[]), durability=A.EXIT)
    add_item(queue)
    with pytest.raises(KeyboardInterrupt):
        first.run_once()

    assert queue.attempts_log.history("default", "T1") == []

    clock.advance(200.0)
    second, _q, second_model = make(repo, db, clock, durability=A.EXIT)
    outcome = second.run_once()
    assert outcome is not None and outcome.state == DONE
    assert second_model.calls == {"planner": 1, "implementer": 1, "reviewer": 1}


def test_boundary_durability_writes_as_it_goes(repo: Path, tmp_path: Path) -> None:
    clock = Clock()
    db = tmp_path / "q.sqlite"
    first, queue, _ = make(repo, db, clock, checks=DyingChecks(commands=[]), durability=A.BOUNDARY)
    add_item(queue)
    with pytest.raises(KeyboardInterrupt):
        first.run_once()

    stages = [record.stage for _a, record in queue.attempts_log.history("default", "T1")]
    assert A.PLANNED in stages and A.IMPLEMENTED in stages and A.APPLIED in stages


def test_sync_durability_notices_an_effect_that_may_have_half_happened(
    repo: Path, tmp_path: Path
) -> None:
    """The one thing `sync` buys over `boundary`.

    A push that began and did not confirm is a fact, not a gap. In the other
    modes the same crash is simply invisible, which is the honest description
    of what they cost.
    """
    clock = Clock()
    db = tmp_path / "q.sqlite"
    queue = WorkQueue(str(db), lease_seconds=100.0, now=clock)
    log = queue.attempts_log

    log.opening("default", "T1", 1, "push", "harness/t1", mode=A.SYNC)
    assert log.resume("default", "T1", 1).open_intents == ("push",)
    log.closed("default", "T1", 1, "push", mode=A.SYNC)
    assert log.resume("default", "T1", 1).open_intents == ()

    # And in the cheaper modes it records nothing, deliberately.
    assert not log.opening("default", "T2", 1, "push", "", mode=A.BOUNDARY)
    assert log.resume("default", "T2", 1).open_intents == ()


def test_an_unknown_durability_mode_is_refused_rather_than_defaulted(tmp_path: Path) -> None:
    """A typo that silently downgraded durability to `exit` would look exactly
    like a harness that had stopped resuming, and nobody would know which."""
    queue = WorkQueue(str(tmp_path / "q.sqlite"))
    with pytest.raises(ValueError, match="unknown durability mode"):
        queue.attempts_log.mode_for("probably-fine")
    with pytest.raises(ValueError, match="unknown durability mode"):
        WorkQueue(str(tmp_path / "q2.sqlite"), durability="probably-fine")


# ------------------------------------------- the brief cannot move under it


def test_an_attempt_briefed_at_one_revision_is_not_resumed_against_a_newer_one(
    repo: Path, tmp_path: Path
) -> None:
    """§7.4's fourth criterion, and the failure it exists to prevent.

    `WorkQueue.add` rewrites `title`, `brief` and `depends_on` on live claimed
    rows. Without this, a worker briefed to do one thing would be resumed
    holding a diff that answers it, and judged against a different question.
    """
    clock = Clock()
    db = tmp_path / "q.sqlite"
    first, queue, _ = make(repo, db, clock, checks=DyingChecks(commands=[]))
    add_item(queue)
    with pytest.raises(KeyboardInterrupt):
        first.run_once()
    assert queue.attempts_log.history("default", "T1"), "precondition: a durable position exists"

    # The plan moves under the live claim.
    queue.add(
        [
            WorkRecord(
                item_id="T1",
                title="Change the greeting",
                brief="Actually, change hello.txt to say something else entirely.",
            )
        ]
    )

    clock.advance(200.0)
    events: list[dict[str, Any]] = []
    second, _q, second_model = make(repo, db, clock, events=events)
    outcome = second.run_once()

    assert outcome is not None
    moved = [e for e in events if e.get("outcome") == "brief_moved"]
    assert moved, "the brief moved and nothing said so"
    assert "discarded" in (moved[0].get("detail") or "")
    # Re-planned against the current brief rather than resumed into the old one.
    assert second_model.calls.get("planner") == 1
    assert second_model.calls.get("implementer") == 1


def test_the_brief_is_pinned_at_the_revision_it_was_given(repo: Path, tmp_path: Path) -> None:
    clock = Clock()
    executor, queue, _ = make(repo, tmp_path / "q.sqlite", clock)
    add_item(queue)
    executor.run_once()

    pinned = queue.attempts_log.resume("default", "T1", 1).brief
    assert pinned is not None
    assert pinned.brief == "Change hello.txt to say 'hello harness'."
    assert pinned.admitted_revision >= 0


def test_a_dependency_that_moves_mid_attempt_discards_the_position(
    repo: Path, tmp_path: Path
) -> None:
    """The graph re-check before the durable gate already discards the
    candidate. It must discard the resumable position with it, or the next
    claim resumes into a diff that answers a superseded plan."""
    clock = Clock()
    db = tmp_path / "q.sqlite"
    executor, queue, _ = make(repo, db, clock)
    add_item(queue)

    class NotReady:
        ready = False

        def explain(self) -> str:
            return "a dependency it declares is no longer satisfied"

    def moved(item_id: str, **kwargs: Any) -> Any:
        return NotReady()

    queue.readiness = moved  # type: ignore[method-assign]
    outcome = executor.run_once()

    assert outcome is not None and outcome.state == PENDING
    assert queue.attempts_log.history("default", "T1") == [], (
        "a discarded candidate must not leave a resumable position behind"
    )


# ---------------------------------------------- what it must not have become


def test_the_stage_list_is_fixed_and_not_registrable() -> None:
    """§7.3, and risk R2. No DSL, no user-defined graph, no dynamic step
    registration. If this module ever grows one, the stage became a workflow
    engine and the non-goal in §12 was breached."""
    forbidden = [name for name in dir(A) if "register" in name or "plugin" in name]
    assert not forbidden, f"attempts.py grew a registration mechanism: {forbidden}"
    assert A.STAGES == (
        A.PLANNED,
        A.IMPLEMENTED,
        A.APPLIED,
        A.CHECKED,
        A.CHECKPOINTED,
        A.REVIEWED,
    )


def test_recording_a_stage_is_not_the_same_as_resuming_at_it() -> None:
    """Stated in code because an implied resumable position is a promise
    nothing keeps. `applied` and `checked` resume as `implemented` does,
    because an uncommitted working tree does not survive a crash."""
    assert set(A.RESUMES_AT) == set(A.STAGES)
    assert A.RESUMES_AT[A.APPLIED] == A.IMPLEMENTED
    assert A.RESUMES_AT[A.CHECKED] == A.IMPLEMENTED
    assert A.RESUMES_AT[A.REVIEWED] == A.REVIEWED
    for stage, at in A.RESUMES_AT.items():
        assert A.STAGES.index(at) <= A.STAGES.index(stage), (
            "a stage cannot resume at a later position than it reached"
        )


def test_the_pre_review_checkpoint_is_not_optional_in_any_mode(repo: Path, tmp_path: Path) -> None:
    """§7.3: a durability mode may make other boundaries more frequent; none
    may remove that one. The git commit is the checkpoint, and the durability
    mode governs the attempt *record*, not the commit."""
    for mode in A.MODES:
        clock = Clock()
        events: list[dict[str, Any]] = []
        executor, queue, _ = make(
            repo, tmp_path / f"q-{mode}.sqlite", clock, durability=mode, events=events
        )
        add_item(queue)
        executor.run_once()
        outcomes = [e.get("outcome") for e in events]
        assert "checkpointed" in outcomes, f"{mode} skipped the pre-review checkpoint"
        assert outcomes.index("checkpointed") < outcomes.index("review_approved")


def test_a_resumed_attempt_runs_the_same_gates(repo: Path, tmp_path: Path) -> None:
    """A resumed attempt that reviewed more cheaply would be this stage
    weakening the thing the whole pipeline exists to protect."""
    clock = Clock()
    db = tmp_path / "q.sqlite"
    first, queue, _ = make(repo, db, clock, die_on="reviewer")
    add_item(queue)
    with pytest.raises(KeyboardInterrupt):
        first.run_once()

    clock.advance(200.0)
    events: list[dict[str, Any]] = []
    second, _q, second_model = make(repo, db, clock, events=events)
    outcome = second.run_once()

    assert outcome is not None and outcome.state == DONE
    assert second_model.calls == {"reviewer": 1}, "the reviewer is still asked"
    assert any(e.get("outcome") == "review_approved" for e in events)


def test_a_recorded_verdict_is_not_re_asked(repo: Path, tmp_path: Path) -> None:
    """A crash *after* the reviewer answered must not shop for a second
    verdict: a model is not deterministic, so re-asking would make a crash a
    way to get a different answer."""
    clock = Clock()
    db = tmp_path / "q.sqlite"
    queue = WorkQueue(str(db), lease_seconds=100.0, now=clock)
    queue.set_control(RUNNING)
    add_item(queue)
    claimed = queue.claim("worker-1")
    assert claimed is not None

    log = queue.attempts_log
    for stage in (A.PLANNED, A.IMPLEMENTED, A.APPLIED, A.CHECKED, A.CHECKPOINTED):
        log.record("default", "T1", claimed.attempts, stage, {})
    log.record(
        "default",
        "T1",
        claimed.attempts,
        A.REVIEWED,
        {"verdict": "approved", "text": "APPROVED\nthe first worker already judged this"},
    )

    resume = log.resume("default", "T1", claimed.attempts)
    assert resume.at == A.REVIEWED
    assert resume.skips(A.REVIEWED)
    assert resume.artefact(A.REVIEWED)["verdict"] == "approved"


# --------------------------------------------- cost, measured both ways


def test_cost_under_induced_crashes_is_lower_with_resumption_than_without(
    repo: Path, tmp_path: Path
) -> None:
    """§7.4's third criterion: the same fixture, resumption on and off, both
    numbers reported.

    Counted in model calls per completed item, which is the unit the cost
    argument is actually about. Time is not measured and is not the claim.
    """

    def run_with(durability: str) -> int:
        clock = Clock()
        db = tmp_path / f"cost-{durability}.sqlite"
        first, queue, first_model = make(
            repo, db, clock, checks=DyingChecks(commands=[]), durability=durability
        )
        add_item(queue)
        with pytest.raises(KeyboardInterrupt):
            first.run_once()
        clock.advance(200.0)
        second, _q, second_model = make(repo, db, clock, durability=durability)
        outcome = second.run_once()
        assert outcome is not None and outcome.state == DONE
        return sum(first_model.calls.values()) + sum(second_model.calls.values())

    with_resumption = run_with(A.BOUNDARY)
    without = run_with(A.EXIT)

    # Reported, not merely compared: the numbers go in the evidence report.
    assert with_resumption == 3, with_resumption
    assert without == 5, without
    assert with_resumption < without


def test_an_item_that_never_crashes_costs_exactly_what_it_did(repo: Path, tmp_path: Path) -> None:
    """Resumption must not make the happy path more expensive."""
    for mode in A.MODES:
        clock = Clock()
        executor, queue, model = make(repo, tmp_path / f"h-{mode}.sqlite", clock, durability=mode)
        add_item(queue)
        assert executor.run_once() is not None
        assert model.calls == {"planner": 1, "implementer": 1, "reviewer": 1}


# ---------------------------------- a decision is history, not a position


def test_a_retry_re_plans_rather_than_replaying_the_verdict_it_is_retrying(
    repo: Path, tmp_path: Path
) -> None:
    """Found by a Stage K test failing when resumption landed, and it is the
    sharpest edge in this stage.

    A reviewer rejected an item. An operator retries it. If the rejection were
    a resumable position, the retry would resume into it, re-report the same
    rejection, and cost no model call — an operator's retry silently replaying
    the verdict it was retrying.
    """
    clock = Clock()
    db = tmp_path / "q.sqlite"
    rejecting = dict(REPLIES)
    executor, queue, model = make(repo, db, clock)
    add_item(queue)

    original = REPLIES["reviewer"]
    try:
        REPLIES["reviewer"] = "REJECTED\nnot this"
        executor.run_once()
    finally:
        REPLIES["reviewer"] = original
    del rejecting

    record = queue.get("T1")
    assert record is not None and record.state == "failed"

    queue.requeue("T1")
    second, _q, second_model = make(repo, db, clock)
    outcome = second.run_once()

    assert outcome is not None and outcome.state == DONE
    assert second_model.calls == {"planner": 1, "implementer": 1, "reviewer": 1}, (
        "a retry must re-plan against the current brief, not resume a decision"
    )
    assert model.calls  # the first attempt really did run


def test_a_decided_attempt_is_sealed_and_a_killed_one_is_not(repo: Path, tmp_path: Path) -> None:
    """The distinction the whole module turns on, asserted directly."""
    clock = Clock()

    decided_db = tmp_path / "decided.sqlite"
    executor, decided_queue, _ = make(repo, decided_db, clock)
    add_item(decided_queue)
    executor.run_once()
    assert not decided_queue.attempts_log.has_resumable_work("default", "T1", 1)
    assert decided_queue.attempts_log.history("default", "T1"), "the history is kept, not deleted"

    killed_db = tmp_path / "killed.sqlite"
    first, killed_queue, _ = make(repo, killed_db, clock, checks=DyingChecks(commands=[]))
    add_item(killed_queue)
    with pytest.raises(KeyboardInterrupt):
        first.run_once()
    assert killed_queue.attempts_log.has_resumable_work("default", "T1", 1)


def test_a_requeue_forgets_every_attempt_at_the_item(repo: Path, tmp_path: Path) -> None:
    clock = Clock()
    db = tmp_path / "q.sqlite"
    first, queue, _ = make(repo, db, clock, checks=DyingChecks(commands=[]))
    add_item(queue)
    with pytest.raises(KeyboardInterrupt):
        first.run_once()
    assert queue.attempts_log.history("default", "T1")

    queue.requeue("T1")
    assert queue.attempts_log.history("default", "T1") == []
    assert queue.attempts_log.resume("default", "T1", 1).brief is None


def test_a_provider_that_would_not_answer_leaves_the_position_intact(
    repo: Path, tmp_path: Path
) -> None:
    """`withheld` is the exception to sealing, and the point of it.

    A spend cap or an unanswering provider decided nothing about the item, so
    the next claim continues rather than restarts. This is where the cost
    saving under real-world failure actually lives — those are the common
    interruptions, not `kill -9`.
    """
    from agent_harness.model_client import RetryExhausted

    clock = Clock()
    db = tmp_path / "q.sqlite"
    executor, queue, _ = make(repo, db, clock)
    add_item(queue)

    real_call = executor.client.call
    calls = {"n": 0}

    def flaky(role: str, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if role == "reviewer" and calls["n"] < 99:
            raise RetryExhausted(
                "the reviewer's endpoint would not answer",
                role="reviewer",
                kind=P.TRANSIENT,
                endpoint="https://api.example",
                model="model-reviewer",
            )
        return real_call(role, *args, **kwargs)

    executor.client.call = flaky  # type: ignore[method-assign]
    outcome = executor.run_once()
    assert outcome is not None and outcome.state == PENDING

    assert queue.attempts_log.has_resumable_work("default", "T1", 1), (
        "a provider failure decided nothing; the position must survive it"
    )
    resume = queue.attempts_log.resume("default", "T1", 1)
    assert resume.skips(A.CHECKPOINTED), "the checkpoint is still there to resume from"
