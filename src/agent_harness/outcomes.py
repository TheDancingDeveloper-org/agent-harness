"""What a gate answered, and what stopped an item — as types, not as one bit.

Two vocabularies live here, and they are about different things.

**A check outcome** is what one gate said. `Checks.run()` used to return
`(bool, str)`, and that one bit collapsed four different situations: a check
that could not run, a check that ran and the diff is wrong, a check whose
failure a mechanical fix would clear, and a condition that needs a person. Only
the second of those is the item's fault, and only the second should be held
against it.

**An attempt disposition** is what stopped the item. `Outcome.state` reused the
queue's states — `done`, `failed`, `blocked`, `pending` — so a checks failure, a
reviewer rejection, a spend cap and a crashed worker all arrived at the queue
looking similar. The repository already knew this was wrong: `EXHAUSTED` exists
and `release(consume_attempt=False)` exists, and both are patches over a missing
distinction. This names the distinction.

**This is not `providers.py` and does not overlap it.** Those classes say what a
*provider* answered — burst limit, spent window, spent cap, refused. These say
what a *gate* answered. Two vocabularies, deliberately, because a gateway's
opinion about our budget and a test suite's opinion about a diff are not the
same kind of fact and merging them would put a local policy decision into the
never-retry set.

**What this module is not.** It is not a gate plugin interface. Nothing here
registers a check, discovers one, or lets a third party add a gate type — that
is the open **D8** question and it is not answered here. A richer *result* from
the gates that already exist is a different thing from a mechanism for adding
new ones, and the difference is the whole reason this stage could be built while
D8 is open.

Nor does anything here let a gate decline to fail. `ESCALATE` is an *additional*
outcome for a condition a person has to resolve; it is not a softer `FAIL`, and
a check cannot reach for it to avoid failing an item.

**And a fix does not let a gate decline to fail either.** `FIX_AVAILABLE` may
now be acted on rather than merely recorded (#155), but only under the boundary
`Checks.apply_fixes` documents: the fix runs once, the check is re-run, and the
*re-run* is the verdict. A gate that still says no after its declared fix has
run is a `FAIL` and refuses the item exactly as it always did. Nothing here
suppresses, downgrades or retries a failure — it re-asks the same question of a
tree the operator's own command rewrote, and takes the answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ------------------------------------------------------------ queue states

# These live here rather than in `work.py` because both vocabularies below
# refer to them, and `work.py` in turn refers to these vocabularies: one of
# the three modules had to own the words, and the one whose entire subject is
# "what happened" is the honest choice. `work.py` re-exports every name, so
# `from .work import DONE` reads exactly as it always did.

PENDING = "pending"
CLAIMED = "claimed"
DONE = "done"
FAILED = "failed"
BLOCKED = "blocked"
#: Tried too many times and given up on. Distinct from `failed`, which is one
#: attempt that did not work: `exhausted` says the harness will not try again
#: without a human. Without it, an item that reliably kills its worker is
#: re-claimed forever -- it sinks to the back of the queue on `attempts` and
#: returns every cycle, spending real money each time, looking identical to
#: an item that is merely busy.
EXHAUSTED = "exhausted"
#: Waiting on a person. Distinct from `blocked`, which is an operator parking
#: an item, and distinct from `claimed`, which is a worker working on it: a
#: held item has a worker attached and is making no progress on purpose, and
#: the whole point of the state is that a silent session and a hung one stop
#: looking the same (#103).
#:
#: **D12, resolved: a hold suspends the lease and keeps the claim.** The owner
#: stays on the row so answering hands the item back to the worker that asked,
#: with its worktree and context intact. `claim` never selects a held row, so
#: no lease expiry can hand it to somebody else while a person is thinking.
HELD = "held"

# ---------------------------------------------------------- check outcomes

#: The gate is satisfied.
PASS = "pass"
#: The gate did not get an answer, for a reason that is nothing to do with the
#: item — a timeout, a machine under load, a flake. The work is not wrong; the
#: question was not answered. Retrying may answer it.
RETRY = "retry"
#: The gate ran and the item's work is wrong. The only outcome that is the
#: item's fault, and the only one that should be read as a defect.
FAIL = "fail"
#: The gate ran, the item's work is wrong, **and** a mechanical fix for it is
#: derivable. Recorded. Run only where the operator has explicitly turned
#: `Checks.apply_fixes` on, and even then the check is re-run afterwards and
#: its second answer is the one that counts — see the rule below.
FIX_AVAILABLE = "fix_available"
#: The gate could not run, or ran into a condition no retry and no diff will
#: clear: a full disk, a missing interpreter, a check command that does not
#: exist. A person has to do something.
ESCALATE = "escalate"

CHECK_OUTCOMES = (PASS, RETRY, FAIL, FIX_AVAILABLE, ESCALATE)

#: Outcomes that mean the gate is satisfied. Exactly one, and it is spelled
#: out as a set so that adding a sixth outcome forces a decision about which
#: side of this line it falls on rather than defaulting to "not a pass".
SATISFIED = frozenset({PASS})


@dataclass(frozen=True)
class AppliedFix:
    """One declared fix the harness ran, and exactly what it changed.

    Exists so that "the harness edited the tree" is a **fact carried in the
    result**, not an inference somebody has to make by diffing two commits.
    Every consumer that shows a human what happened — the event stream, the
    reviewer's prompt — reads this.
    """

    #: The check that failed and provoked the fix.
    check: tuple[str, ...] = ()
    #: The argv that was run, verbatim, in the item's own worktree.
    fix: tuple[str, ...] = ()
    #: Repository-relative paths whose content the fix rewrote. Derived from
    #: git, not from the tool's output: a formatter that lies about what it
    #: touched still cannot hide from a tree comparison.
    paths: tuple[str, ...] = ()
    #: Whether re-running the check afterwards passed. False means the fix was
    #: run and the gate still said no, which is a real failure and is refused.
    cleared: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": list(self.check),
            "fix": list(self.fix),
            "paths": list(self.paths),
            "cleared": self.cleared,
        }


@dataclass(frozen=True)
class CheckResult:
    """One gate's answer, and the evidence for it.

    Iterable as `(ok, detail)` on purpose. Three call sites already unpack
    `Checks.run()` that way and a flag day across them would have been change
    for its own sake — the point of this stage is that callers who *want* the
    distinction can have it, not that everybody must restructure to keep the
    bit they already had.
    """

    outcome: str
    detail: str = ""
    #: The argv that produced this, for a report that has to name the gate.
    command: tuple[str, ...] = ()
    #: For `FIX_AVAILABLE`: the argv that is believed to clear it. Recorded
    #: whether or not it is run, so a deployment that has not turned
    #: `Checks.apply_fixes` on still learns what would have cleared the gate.
    fix: tuple[str, ...] = ()
    #: Fixes the harness actually ran while producing this result, in order.
    #: Never empty on a result whose `PASS` was bought by a fix — that is the
    #: whole point: a pass that the harness helped produce is distinguishable
    #: from one the agent earned unaided, at every layer above this one.
    applied: tuple[AppliedFix, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome not in CHECK_OUTCOMES:
            raise ValueError(f"unknown check outcome {self.outcome!r}; expected {CHECK_OUTCOMES}")
        if self.fix and self.outcome != FIX_AVAILABLE:
            raise ValueError(
                f"a fix is only meaningful with {FIX_AVAILABLE!r}, not {self.outcome!r}"
            )

    @property
    def ok(self) -> bool:
        return self.outcome in SATISFIED

    def __iter__(self) -> Any:
        """`passed, failure = checks.run(repo)` keeps working."""
        return iter((self.ok, self.detail))

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "detail": self.detail,
            "command": list(self.command),
            "fix": list(self.fix),
            "applied": [one.as_dict() for one in self.applied],
        }


PASSED = CheckResult(PASS)


# ------------------------------------------------------ attempt disposition

#: The item is finished.
COMPLETED = "completed"
#: A gate said no about *this item's work*: its checks failed, or the reviewer
#: rejected it. The work exists and is wrong. This is a verdict, not a crash.
REFUSED = "refused"
#: The worker or the harness broke, or never finished. Nothing was decided
#: about the item: an exception happened while deciding, or the agent ran out
#: of time still holding it. A human reading this should be looking at the
#: harness, not at the diff.
CRASHED = "crashed"
#: Never attempted, or attempted and discarded through no fault of the item: a
#: spend cap with nothing to spend, a provider that would not answer, a
#: dependency that moved under a live claim, a claim lost to another worker.
#: The item goes back to the queue.
WITHHELD = "withheld"
#: A person has to resolve something before this can be attempted again. Not a
#: failure of the item and not a transient condition.
ESCALATED = "escalated"
#: The harness refused to run a command on the item's behalf: it matched the
#: deployment's refusal list, or it reached outside the item's tree. See
#: `guard.py`.
#:
#: Kept apart from `REFUSED`, which is a gate's verdict *about the work*, and
#: from `CRASHED`, which is the harness breaking. Neither is true here: the work
#: was never judged, and nothing broke — the harness declined, on purpose, and
#: an operator reading the queue must be able to see that without opening a log.
#: It is also apart from `ESCALATED` because the two answer different questions:
#: escalated is "nobody could decide this", and this is "the harness decided,
#: and the answer is no".
#:
#: **A refusal is terminal (owner decision, 2026-08-05.)** The command is
#: blocked, the item stops in `blocked`, and it is not handed back to the agent
#: as a correction to retry: terminal is the safest of the two answers, the
#: cheapest, and it cannot loop. The cost accepted with it is that an agent
#: reaching for a forbidden command that had a permitted equivalent loses the
#: whole item and needs a person.
BLOCKED_BY_POLICY = "blocked_by_policy"

DISPOSITIONS = (COMPLETED, REFUSED, CRASHED, WITHHELD, ESCALATED, BLOCKED_BY_POLICY)


# ------------------------------------------------------------- reason kinds

#: Why, as a token a client can branch on rather than a sentence it has to
#: parse. The same idea as `graph.REASON_*`, for the same reason: an API that
#: reports only English forces every consumer to match on prose.
CHECKS_FAILED = "checks_failed"
CHECK_ESCALATED = "check_escalated"
CHECK_TRANSIENT = "check_transient"
REVIEW_REJECTED = "review_rejected"
PATCH_REJECTED = "patch_rejected"
NO_TARGET = "no_target"
WORKER_ERROR = "worker_error"
PROVIDER_EXHAUSTED = "provider_exhausted"
BUDGET_EXHAUSTED = "budget_exhausted"
DEPENDENCY_INVALIDATED = "dependency_invalidated"
AGENT_TIMEOUT = "agent_timeout"
CLAIM_LOST = "claim_lost"
#: The item passed a ceiling **this deployment** declared for it. Kept apart
#: from `BUDGET_EXHAUSTED`, which is a *provider* saying our account is out of
#: budget: one is a local policy decision and the other is in the never-retry
#: set, and conflating them would park a shared endpoint because one item was
#: expensive.
ITEM_WALL_CLOCK = "item_wall_clock"
ITEM_SPEND = "item_spend"
#: A question went unanswered for longer than the hold allowed. The item is
#: `blocked`, never `ready`: a hold that times out has not been approved.
HOLD_EXPIRED = "hold_expired"
#: The harness could not show the implementer a file the planner named, because
#: that file alone is larger than the whole context budget. Kept apart from
#: `NO_TARGET`, which is the planner failing to find one: here the target is
#: known and correct, and the *harness* cannot supply it. Retrying changes
#: nothing — the file is the size it is — so it needs a person to raise the
#: budget or split the file, which is why it escalates.
CONTEXT_UNAVAILABLE = "context_unavailable"
#: The agent read the repository, concluded the item cannot be done as written,
#: and said why. Kept apart from `NO_TARGET`, which is the agent finding nothing
#: to change: here it found the target and the *brief* is what does not work.
#: Retrying is pointless — the brief is the brief — so this needs a person to
#: rewrite or withdraw the item, which is why it escalates rather than failing.
ITEM_IMPOSSIBLE = "item_impossible"
#: A command matched this deployment's refusal list. The command is named in
#: the detail along with the pattern that refused it, so the answer to "why did
#: this stop?" is one line of the queue rather than a session scrollback.
COMMAND_BLOCKED = "command_blocked"
#: A command the harness was asked to run named a path outside the item's tree.
#: Kept apart from `COMMAND_BLOCKED` because the two need different responses: a
#: pattern hit is answered by looking at the policy, and this is answered by
#: looking at what the command was reaching for.
PATH_ESCAPE = "path_escape"
# The item gates passed, but its committed delta could not be replayed onto
# the current local plan head. This is repair work, not a provider failure.
PLAN_PROMOTION_CONFLICT = "plan_promotion_conflict"

REASON_KINDS = (
    CHECKS_FAILED,
    CHECK_ESCALATED,
    CHECK_TRANSIENT,
    REVIEW_REJECTED,
    PATCH_REJECTED,
    NO_TARGET,
    WORKER_ERROR,
    PROVIDER_EXHAUSTED,
    BUDGET_EXHAUSTED,
    DEPENDENCY_INVALIDATED,
    AGENT_TIMEOUT,
    CLAIM_LOST,
    ITEM_WALL_CLOCK,
    ITEM_SPEND,
    HOLD_EXPIRED,
    CONTEXT_UNAVAILABLE,
    ITEM_IMPOSSIBLE,
    COMMAND_BLOCKED,
    PATH_ESCAPE,
    PLAN_PROMOTION_CONFLICT,
)


#: What each non-passing check outcome means, all the way to the queue:
#: `(disposition, reason kind, queue state, does it cost an attempt)`.
#: `PASS` is absent because a satisfied gate stops nothing.
#:
#: **Only the two outcomes that did not exist before decline to consume an
#: attempt.** `FAIL` and `FIX_AVAILABLE` are the old boolean `False`, and they
#: cost an attempt exactly as it always did. `RETRY` and `ESCALATE` are new
#: paths — a gate that could not answer, and a gate that needs a person — and
#: an item nobody managed to judge has not used up a try.
#:
#: What `max_attempts` bounds is otherwise unchanged, deliberately. Whether a
#: crash should cost an attempt at all is **D11**, it is open, and a stage that
#: names distinctions is not the place to answer it by moving a number.
FROM_CHECK: dict[str, tuple[str, str, str, bool]] = {
    RETRY: (WITHHELD, CHECK_TRANSIENT, PENDING, False),
    FAIL: (REFUSED, CHECKS_FAILED, FAILED, True),
    FIX_AVAILABLE: (REFUSED, CHECKS_FAILED, FAILED, True),
    ESCALATE: (ESCALATED, CHECK_ESCALATED, BLOCKED, False),
}


@dataclass(frozen=True)
class Stop:
    """What stopped an item, in the terms above.

    Carried on `Outcome` so the queue and the API can tell a reviewer's
    rejection from a crashed worker without reading a log line and guessing.
    """

    disposition: str
    reason_kind: str = ""
    detail: str = ""
    #: The gate's own answer, when a gate is what stopped it.
    check: CheckResult | None = field(default=None)
    #: The queue state this stop lands in, when the stop is what decides it.
    #: Empty means the caller already chose — which is the case for every path
    #: that existed before this taxonomy did. Those keep the state they had;
    #: this records *why*, and changes nothing about *where*.
    state: str = ""
    #: Whether this costs the item one of its attempts. True is what every
    #: pre-existing path did and still does. See `FROM_CHECK`.
    consumes_attempt: bool = True

    def __post_init__(self) -> None:
        if self.disposition not in DISPOSITIONS:
            raise ValueError(f"unknown disposition {self.disposition!r}; expected {DISPOSITIONS}")
        if self.reason_kind and self.reason_kind not in REASON_KINDS:
            raise ValueError(f"unknown reason kind {self.reason_kind!r}; expected {REASON_KINDS}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition,
            "reason_kind": self.reason_kind,
            "detail": self.detail,
            "check": self.check.as_dict() if self.check is not None else None,
        }


#: Dispositions in which something was actually *decided* about the item.
#: An attempt that ends in one of these is history; one that ends in
#: `withheld`, or that never ends at all because its worker was killed, is a
#: position to continue from.
DECIDED = frozenset({COMPLETED, REFUSED, ESCALATED, CRASHED, BLOCKED_BY_POLICY})


#: Dispositions a human should be looking at rather than waiting through.
#: `REFUSED` is absent on purpose: a rejected diff is the system working.
#: `BLOCKED_BY_POLICY` is present because the refusal is terminal — nothing will
#: pick the item up again, so if no person looks at it, nobody ever does.
NEEDS_A_PERSON = frozenset({ESCALATED, BLOCKED_BY_POLICY})


def stop_for(result: CheckResult) -> Stop:
    """The `Stop` a non-passing check produces."""
    if result.ok:  # pragma: no cover - callers check first; a guard, not a path
        raise ValueError("a passing check does not stop anything")
    disposition, reason_kind, state, consumes = FROM_CHECK[result.outcome]
    return Stop(
        disposition,
        reason_kind,
        detail=result.detail,
        check=result,
        state=state,
        consumes_attempt=consumes,
    )
