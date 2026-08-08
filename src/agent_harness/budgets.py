"""How long one item may take, and how much it may spend.

The lease bounds a worker's **absence**, not an item's **duration**. A
heartbeat proves a process is alive; it proves nothing about progress. There
was no per-item wall-clock budget in the queue at all — `session_executor` has
an agent timeout and `Checks` has a subprocess timeout, but the item itself was
unbounded. An item that heartbeats forever is indistinguishable from one making
progress, and it is the failure mode a seven-day unattended run is most likely
to produce.

Stage H made that worse before it made it better. D11 ruled that **a resumed
attempt continues the existing one**, so `max_attempts` no longer counts
crashes — which is right, and which left an item that crashes in a loop bounded
by nothing. This is what bounds it.

The same hole exists for money. An item can consume an unbounded number of
model calls across unbounded attempts; the only ceiling was `max_attempts`,
which counts attempts, not spend.

## Three rules

**A spend ceiling is not a cost cap.** `providers.WINDOW_CAP` and
`TERMINAL_CAP` are a *provider's* statement about our budget, and they are in
the never-retry set for a reason. This is *our* statement about *one item*.
Conflating them would put a local policy decision into the never-retry set and
park an endpoint the whole fleet needs because one item was expensive.

**Unknown cost stays unknown.** An item whose spend cannot be determined —
session-mode traffic bypassing `ModelClient`, an unpriced model — must not be
treated as having spent zero. That is `pricing.py`'s existing rule and it
decides this module's edge case: the ceiling is reported as *unenforceable*
rather than quietly satisfied.

**A budget stop never kills work in flight.** Stopping mid-item destroys the
agent's context and leaves a half-finished worktree, which is the reasoning
`work.py` already gives about pause semantics. The check runs at the boundaries
that already exist, and the item stops at the next one.

Defaults are **unlimited**, so an existing database upgrades with no behaviour
change at all. Whether that is the right default for an unattended run is
**D14**, and it is recorded as open rather than decided by accident here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Which ceiling stopped an item. Distinct from `providers`' vocabulary on
#: purpose — see the module docstring.
WALL_CLOCK = "wall_clock"
SPEND = "spend"

CEILINGS = (WALL_CLOCK, SPEND)

#: What "no ceiling" is spelled as. Zero rather than None because it is a
#: column default, and a nullable numeric ceiling would mean two ways to say
#: unlimited.
UNLIMITED = 0.0


class BudgetExceeded(RuntimeError):
    """An item reached a ceiling it declared. Raised at a boundary, never
    mid-stage."""

    def __init__(self, ceiling: str, limit: float, observed: float, detail: str) -> None:
        super().__init__(detail)
        self.ceiling = ceiling
        self.limit = limit
        self.observed = observed
        self.detail = detail


@dataclass(frozen=True)
class Budget:
    """The ceilings in force for one item, after per-item overrides.

    Both default to unlimited. A per-item value of zero means "take the
    project's", not "no budget" — otherwise every existing row would read as
    deliberately unlimited and an operator could never express it either way.
    """

    seconds: float = UNLIMITED
    spend_usd: float = UNLIMITED

    @property
    def bounded(self) -> bool:
        return bool(self.seconds or self.spend_usd)

    def as_dict(self) -> dict[str, Any]:
        return {"seconds": self.seconds, "spend_usd": self.spend_usd}

    def describe(self) -> str:
        parts = []
        if self.seconds:
            parts.append(f"{self.seconds:g}s wall clock")
        if self.spend_usd:
            parts.append(f"{self.spend_usd:g} spend")
        return ", ".join(parts) if parts else "unlimited"


def budget_for(project: Any, record: Any) -> Budget:
    """The ceilings this item runs under. Per-item wins; zero means inherit."""
    project_seconds = float(getattr(project, "max_item_seconds", 0.0) or 0.0)
    project_spend = float(getattr(project, "max_item_spend_usd", 0.0) or 0.0)
    return Budget(
        seconds=float(getattr(record, "budget_seconds", 0.0) or 0.0) or project_seconds,
        spend_usd=float(getattr(record, "budget_spend_usd", 0.0) or 0.0) or project_spend,
    )


@dataclass
class Spend:
    """What an item has cost so far, and what nobody could measure.

    `unpriced` is not a rounding error. It is the count of calls whose cost is
    unknown, and while it is non-zero the spend ceiling **cannot be enforced**
    — `usd` is a lower bound, and stopping an item on a lower bound would
    stop it for a number nobody can defend.
    """

    usd: float = 0.0
    unpriced: int = 0
    priced: int = 0

    @property
    def measurable(self) -> bool:
        """Whether `usd` is the whole story. False the moment one call is unpriced."""
        return self.unpriced == 0

    def add_call(self, usage: Mapping[str, Any]) -> None:
        """Fold in one model call that actually happened.

        **A call that reported no usage is `unpriced`, not free.** That is the
        whole edge case: a provider that reports nothing, a model with no price
        in the table, and session-mode traffic that never passed through
        `ModelClient` at all (#128) are all calls whose cost is unknown, and
        the ceiling stops being enforceable for every one of them. Treating an
        empty usage dict as zero would let an item run past a spend ceiling
        while the harness reported it as comfortably inside one.

        The caller decides what counts as a call. This is deliberately not
        given raw events to sift, because a function that guesses which events
        are calls is a function that will one day guess wrong in the direction
        of "free".
        """
        from .pricing import cost_of

        cost = cost_of(usage)
        if cost is None:
            self.unpriced += 1
            return
        self.usd += cost
        self.priced += 1

    def add(self, other: Spend) -> None:
        """Fold an already classified set of calls into this item."""
        self.usd += other.usd
        self.unpriced += other.unpriced
        self.priced += other.priced

    def as_dict(self) -> dict[str, Any]:
        return {
            "usd": round(self.usd, 6),
            "priced_calls": self.priced,
            "unpriced_calls": self.unpriced,
            "measurable": self.measurable,
        }


@dataclass(frozen=True)
class Verdict:
    """Whether an item may carry on, and what could not be checked."""

    exceeded: BudgetExceeded | None = None
    #: Ceilings that are declared and cannot be enforced, with why. Reported
    #: rather than treated as satisfied: a ceiling nobody can check is not a
    #: ceiling that was met.
    unenforceable: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return self.exceeded is None


def check(budget: Budget, *, elapsed: float, spend: Spend) -> Verdict:
    """Has this item passed a ceiling it declared?

    Wall clock first, because it is the one that can always be measured. An
    item that has run for a week and whose spend is unknown should stop for the
    reason that is knowable rather than survive on the reason that is not.
    """
    unenforceable: list[tuple[str, str]] = []

    if budget.seconds and elapsed > budget.seconds:
        return Verdict(
            BudgetExceeded(
                WALL_CLOCK,
                budget.seconds,
                elapsed,
                f"the item has been in progress for {elapsed:.0f}s, over its "
                f"{budget.seconds:g}s wall-clock ceiling; it stops at this boundary "
                "rather than being killed mid-stage",
            )
        )

    if budget.spend_usd:
        if not spend.measurable:
            unenforceable.append(
                (
                    SPEND,
                    f"{spend.unpriced} model call(s) have no known price, so the "
                    f"{spend.usd:.4f} recorded so far is a LOWER BOUND and the "
                    f"{budget.spend_usd:g} ceiling cannot be enforced. Unknown cost "
                    "is not zero cost.",
                )
            )
        elif spend.usd > budget.spend_usd:
            return Verdict(
                BudgetExceeded(
                    SPEND,
                    budget.spend_usd,
                    spend.usd,
                    f"the item has spent {spend.usd:.4f}, over its "
                    f"{budget.spend_usd:g} ceiling; it stops at this boundary rather "
                    "than being killed mid-stage",
                ),
                tuple(unenforceable),
            )

    return Verdict(None, tuple(unenforceable))
