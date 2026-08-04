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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .work import BLOCKED, FAILED, PENDING

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
#: derivable. Recorded, never applied — see the rule below.
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
    #: For `FIX_AVAILABLE`: the argv that is believed to clear it. **Recorded,
    #: not run.** Applying a fix is a decision with its own consequences and
    #: its own evidence, and a gate that silently repaired what it was meant to
    #: catch would be a gate that cannot be trusted to have caught anything.
    fix: tuple[str, ...] = ()

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

DISPOSITIONS = (COMPLETED, REFUSED, CRASHED, WITHHELD, ESCALATED)


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
DECIDED = frozenset({COMPLETED, REFUSED, ESCALATED, CRASHED})


#: Dispositions a human should be looking at rather than waiting through.
#: `REFUSED` is absent on purpose: a rejected diff is the system working.
NEEDS_A_PERSON = frozenset({ESCALATED})


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
