"""Request and response models.

These exist so the OpenAPI document is worth reading. A FastAPI route that
returns a bare dict produces a schema of `{}` — technically valid, useless to
anyone generating a client or trying to see what a field means. Every model
here carries field descriptions, because the schema IS the documentation.

They are deliberately separate from the domain types (`work.WorkRecord`,
`plan.WorkItem`): the wire format is a contract with clients and should be
free to stay stable while the internals move.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------- work

WorkState = Literal["pending", "claimed", "done", "failed", "blocked"]


class LatestEvent(BaseModel):
    """The newest thing that happened to an item."""

    outcome: str = Field(description="Stage name, e.g. `agent_started`, `checks_passed`.")
    detail: str | None = Field(None, description="Human-readable detail, may be long.")
    ts: float = Field(description="Unix timestamp.")
    session_id: str | None = Field(
        None,
        description="Terminal session the agent is running in, if any. This is the "
        "deep link: open it in the host UI to watch or answer the agent.",
    )
    session_url: str | None = Field(None, description="Fully-qualified URL for that session.")


class WorkItem(BaseModel):
    item_id: str = Field(description="Stable id from the plan, e.g. `T4`.")
    title: str
    brief: str = Field(description="The full specification given to the agent.")
    issue: int | None = Field(None, description="GitHub issue number, when synced.")
    depends_on: list[str] = Field(default_factory=list)
    state: WorkState
    owner: str | None = Field(None, description="host:pid of the worker holding the claim.")
    lease_until: float = Field(
        0.0,
        description="Unix time the claim expires. A claim is a LEASE — past this, "
        "the item is re-claimable without anyone intervening.",
    )
    attempts: int = 0
    last_error: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    updated_at: float = 0.0
    latest: LatestEvent | None = None


class WorkList(BaseModel):
    configured: bool = Field(
        description="False when no queue is attached. Everything else is then empty — "
        "distinguish this from 'no work left', which looks identical otherwise."
    )
    reason: str | None = Field(None, description="Why it is not configured, when it is not.")
    counts: dict[str, int] = Field(default_factory=dict, description="Item count per state.")
    stale: list[str] = Field(
        default_factory=list,
        description="Items whose lease expired without finishing — the worker is gone. "
        "They are re-claimed automatically; a rising count means something is killing workers.",
    )
    items: list[WorkItem] = Field(default_factory=list)


class RetryResult(BaseModel):
    ok: bool
    item_id: str
    state: WorkState


class NewWorkItem(BaseModel):
    item_id: str
    title: str
    brief: str = ""
    issue: int | None = None
    depends_on: list[str] = Field(default_factory=list)


class AddItemsRequest(BaseModel):
    project_id: str = Field(
        "default",
        description="Which project these items belong to. Items are keyed by "
        "(project_id, item_id), so two projects may each have a `T1` -- and without "
        "this they would be the same row.",
    )
    items: list[NewWorkItem] = Field(
        description="Items to add. Existing ids are refreshed, "
        "never reset — re-adding cannot un-finish work."
    )


class AddItemsResult(BaseModel):
    added: int = Field(description="Items that did not already exist.")
    total: int = Field(description="Items in the queue afterwards.")


class FleetControl(BaseModel):
    state: Literal["running", "paused", "draining", "stopped"] = Field(
        description="`running` claims freely. `paused` and `draining` both stop new "
        "claims; neither interrupts work in flight, because killing an agent mid-item "
        "destroys its context and leaves a half-finished worktree. The difference is "
        "what the operator meant. `stopped` means no workers exist for this project "
        "at all -- it is what every project is set to on boot, and only an explicit "
        "start leaves it."
    )
    reason: str | None = Field(
        None,
        description="Why it was set. Shown to whoever finds "
        "the fleet stopped and has to decide "
        "whether to resume it.",
    )


class SetFleetControl(BaseModel):
    state: Literal["running", "paused", "draining", "stopped"]
    reason: str | None = None


class RoleRoute(BaseModel):
    model: str = Field(description="Model identifier as the provider names it.")
    endpoint: str = Field(description="Base URL of the provider API.")
    provider: str = Field(
        "claw-bay",
        description="Failure classifier to use: `generic` "
        "cannot tell a spend cap from a burst "
        "limit, because nothing in HTTP can.",
    )


class RoleMap(BaseModel):
    roles: dict[str, RoleRoute] = Field(
        description="role -> where its calls go. Changing this takes effect on the next "
        "call: the call site names a ROLE, never a model, which is what makes the map "
        "changeable without a redeploy."
    )


class ProjectSpec(BaseModel):
    """A project as it is registered. Persisted, so nothing here has to be
    supplied again after a restart -- every field was previously a CLI flag
    with nowhere to be written down."""

    project_id: str = Field(description="Stable id, used to scope every other call.")
    name: str
    repo: str | None = Field(None, description="GitHub repo as `owner/name`.")
    work_dir: str | None = Field(None, description="Checkout the worktrees branch from.")
    base_branch: str = "main"
    checks: list[str] = Field(
        default_factory=list, description="Commands run before the reviewer, cheapest first."
    )
    plan_path: str | None = None
    roles: dict[str, RoleRoute] | None = Field(
        None, description="Role overrides for this project. Null uses the global map."
    )
    max_workers: int = Field(
        1,
        description="Concurrency budget. Its purpose is that one project cannot "
        "starve another, so it is per project rather than per fleet.",
    )


class ProjectSummary(BaseModel):
    """A project plus enough state for the overview screen."""

    project: ProjectSpec
    counts: dict[str, int] = Field(default_factory=dict)
    control: FleetControl
    previous_state: str | None = Field(
        None,
        description="What it was doing before the process last stopped it. This is what "
        "keeps 'was running' distinguishable from 'was drained because we were deploying' "
        "across a restart -- the operator's intent is otherwise what a restart destroys.",
    )
    stale: int = 0


class ProjectList(BaseModel):
    projects: list[ProjectSummary]


# --------------------------------------------------------------------- plan


class PlanItem(BaseModel):
    id: str
    title: str
    body: str
    labels: list[str] = Field(default_factory=list)
    milestone: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    done: bool = False
    line: int = Field(description="Line in the source plan, so a reader can find it again.")


class PlanParseResult(BaseModel):
    items: list[PlanItem]
    skipped: list[str] = Field(
        description="Headings not recognised as work. Never empty on a real plan — most "
        "headings are narrative — but a large number relative to items means the plan "
        "does not use a recognised shape."
    )
    duplicate_ids: dict[str, list[int]] = Field(
        default_factory=dict,
        description="Ids stated more than once, with their lines. Each id becomes ONE "
        "issue, so these must be resolved or explicitly collapsed.",
    )
    unresolved_dependencies: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Dependencies naming items that do not exist. A typo here would "
        "block an item forever, silently.",
    )


class PlanSyncRequest(BaseModel):
    path: str = Field(description="Path to the plan markdown, on the harness's filesystem.")
    repo: str = Field(description="GitHub repo as `owner/name`.")
    dry_run: bool = Field(
        True,
        description="Report what would change without writing. "
        "Defaults to true: syncing creates real issues.",
    )
    allow_duplicates: bool = Field(
        False, description="Collapse duplicate ids (richest description wins) instead of refusing."
    )


class PlanSyncResult(BaseModel):
    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    orphaned: list[str] = Field(
        default_factory=list,
        description="Issues for items no longer in the plan. Never closed automatically — "
        "an item vanishing from a document is not grounds to close work.",
    )
    labels_created: list[str] = Field(default_factory=list)
    milestones_created: list[str] = Field(default_factory=list)
    dry_run: bool


# ------------------------------------------------------------------- errors


class RateLimits(BaseModel):
    window: str
    classified: dict[str, int] = Field(
        description="Counts per class: `rpm` (going too fast), `window_cap` (short spend "
        "window exhausted), `terminal_cap` (spend cap or credential rejected)."
    )
    meaning: dict[str, str] = Field(description="What each class means and how it is handled.")
    unclassified: int = Field(
        description="Rate limits recorded before anything classified them. Reported "
        "SEPARATELY and never folded into a class — the breakdown does not exist for "
        "these and cannot be recovered."
    )
    total: int = Field(description="Classified rate limits only. Excludes `unclassified`.")
    by_worker: list[dict[str, Any]] = Field(default_factory=list)
    by_endpoint: list[dict[str, Any]] = Field(default_factory=list)
    by_role: list[dict[str, Any]] = Field(default_factory=list)


# ------------------------------------------------------------------- events


class Event(BaseModel):
    id: int = Field(
        description="Monotonic row id. Page with this, not with `ts`: two "
        "events in one millisecond must still have a total order."
    )
    ts: float
    kind: str
    source: str
    worker: str | None = None
    role: str | None = None
    model: str | None = None
    endpoint: str | None = None
    outcome: str | None = None
    error_class: str | None = None
    latency_s: float | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class EventPage(BaseModel):
    events: list[Event]
    cursor: int = Field(description="Pass as `since_id` next time. Unchanged when empty.")


# ------------------------------------------------------------------ summary


class WaitingItem(BaseModel):
    item_id: str | None = None
    session_url: str | None = None


class Summary(BaseModel):
    running: int
    pending: int
    done: int
    failed: int
    stale: int
    abandoned_sessions: int = Field(
        0,
        description="Terminal sessions kept alive after an agent timed out. They hold "
        "the agent's context so a human can pick the item up, and each may still hold "
        "an agent spending tokens. A rising count nobody returns to is waste, not "
        "resilience -- the reaper collects them past a max age.",
    )
    waiting_for_input: list[WaitingItem] = Field(
        description="Agents that have stopped to ask a human something. Its own field "
        "rather than a count, because it is the one state that needs a person."
    )


class Health(BaseModel):
    ok: bool
    events: int
    queue: bool = Field(description="Whether a work queue is attached.")
    authenticated: bool = Field(
        description="Whether a token is configured. False means every authenticated route refuses."
    )
    version: str
