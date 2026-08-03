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

from pydantic import BaseModel, Field, computed_field

# --------------------------------------------------------------------- work

#: Every state the queue can actually store. `exhausted` is the one that is
#: easy to forget and the worst to omit: it is what the harness sets when an
#: item has burned its attempt limit, so it marks precisely the rows that
#: need a human. Leaving it out of the union did not hide those rows -- it
#: made the list and detail endpoints fail response validation and return
#: 500, so the safety mechanism broke the API exactly when it engaged.
WorkState = Literal["pending", "claimed", "done", "failed", "blocked", "exhausted"]


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
    title: str = Field(description="One line, from the plan heading.")
    brief: str = Field(description="The full specification given to the agent.")
    issue: int | None = Field(None, description="GitHub issue number, when synced.")
    depends_on: list[str] = Field(
        default_factory=list, description="Ids this item waits on before it can be claimed."
    )
    state: WorkState = Field(description="Where the item is. See `WorkState`.")
    owner: str | None = Field(None, description="host:pid of the worker holding the claim.")
    lease_until: float = Field(
        0.0,
        description="Unix time the claim expires. A claim is a LEASE — past this, "
        "the item is re-claimable without anyone intervening.",
    )
    attempts: int = Field(
        0,
        description="Claims so far. An item is given up on (`exhausted`) at the "
        "project's limit, because one that reliably kills its worker would "
        "otherwise be re-claimed forever, spending money each cycle.",
    )
    last_error: str | None = Field(
        None, description="Why the most recent attempt did not finish, in full."
    )
    failure_summary: str | None = Field(
        None,
        description="The first line of `last_error`, bounded in length, for a list "
        "view. A backlog with several failures is untriageable when every row says "
        "only `failed` and the reason is one pane deeper. The full text stays in "
        "`last_error`; this never replaces it.",
    )
    failed_stage: str | None = Field(
        None,
        description="Where it failed -- `checks_failed`, `review_rejected`, "
        "`agent_timeout` -- from the last recorded event. Which stage failed usually "
        "decides whether retrying is worth anything.",
    )
    retryable: bool = Field(
        True,
        description="Whether `POST /api/work/{item_id}/retry` would be accepted right "
        "now. Computed by the same rule the route enforces, so a client offering the "
        "action cannot drift from the server refusing it -- the alternative is a "
        "button that exists to produce a 409.",
    )
    retry_blocked_reason: str | None = Field(
        None,
        description="Why retry would be refused, when it would be. A disabled action "
        "with no explanation is indistinguishable from a broken one.",
    )
    blocked_reason: str | None = Field(
        None,
        description="Why an operator blocked this item, when its state is `blocked`. "
        "A block is a decision someone made, not a failure the harness had -- reading "
        "it out of `last_error` would make the two indistinguishable.",
    )
    branch: str | None = Field(None, description="Candidate branch, once one exists.")
    pr_url: str | None = Field(None, description="Pull request, once review has approved one.")
    updated_at: float = Field(0.0, description="Unix time this row last changed.")
    latest: LatestEvent | None = Field(
        None, description="Newest event for this item, or null if nothing has happened yet."
    )

    @computed_field(  # type: ignore[prop-decorator]
        description="The same identifier as `item_id`, under the name most clients "
        "look for. Emitted because a list whose rows cannot be addressed is a list "
        "you can render and not act on: every action route takes this value."
    )
    @property
    def id(self) -> str:
        # Derived, never stored. Two independent fields holding one identifier
        # is two chances to serialize one of them as null.
        return self.item_id


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
    items: list[WorkItem] = Field(
        default_factory=list, description="Every item, filtered to one project when asked."
    )


class RetryResult(BaseModel):
    ok: bool = Field(description="Whether the item was re-queued.")
    item_id: str = Field(description="The item, as it was addressed.")
    state: WorkState = Field(description="Its state afterwards -- `pending` on success.")


class BlockRequest(BaseModel):
    """Blocking an item is a decision, so it is recorded as one."""

    reason: str = Field(
        min_length=1,
        description="Why it is blocked, in words, and REQUIRED. An item parked with no "
        "reason is indistinguishable from one nobody got to, and the person who has to "
        "decide whether to unblock it is rarely the person who blocked it.",
    )
    who: str | None = Field(
        None, description="Who decided. Recorded with the reason, not verified."
    )
    override: bool = Field(
        False,
        description="Block even when a worker holds a live claim, or when the item is "
        "already done. Deliberately explicit: the first yanks an item out from under a "
        "running agent, and the second un-finishes work.",
    )


class BlockResult(BaseModel):
    ok: bool = Field(description="Whether the item was blocked.")
    item_id: str = Field(description="The item, as it was addressed.")
    state: WorkState = Field(description="Its state afterwards.")
    reason: str = Field(description="The reason as it is now recorded on the item.")


class NewWorkItem(BaseModel):
    item_id: str = Field(description="Stable id, unique within the project.")
    title: str = Field(description="One line.")
    brief: str = Field("", description="The full specification the agent will be given.")
    issue: int | None = Field(None, description="GitHub issue number, when there is one.")
    depends_on: list[str] = Field(
        default_factory=list, description="Ids this item waits on before it can be claimed."
    )


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
    state: Literal["running", "paused", "draining", "stopped"] = Field(
        description="The state to move to. See `FleetControl.state` for what each means."
    )
    reason: str | None = Field(
        None, description="Why. Shown to whoever finds the fleet in this state later."
    )


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
    reviewer_independent: bool = Field(
        True,
        description="False when the reviewer is the same model, or the same vendor, as "
        "the implementer -- some share of reviews is then a model grading its own work. "
        "Reported rather than refused: running one model is a legitimate deliberate "
        "choice, but it must not be a surprise.",
    )
    reviewer_note: str = Field("", description="Why, in words.")
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
    name: str = Field(description="Human-readable name. Never used as an identifier.")
    repo: str | None = Field(None, description="GitHub repo as `owner/name`.")
    work_dir: str | None = Field(None, description="Checkout the worktrees branch from.")
    base_branch: str = Field(
        "main", description="Branch candidates are cut from and proposed against."
    )
    checks: list[str] = Field(
        default_factory=list, description="Commands run before the reviewer, cheapest first."
    )
    plan_path: str | None = Field(
        None, description="Plan document for this project, as the service sees the filesystem."
    )
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

    project: ProjectSpec = Field(description="The project as registered.")
    counts: dict[str, int] = Field(
        default_factory=dict, description="Item count per state, for this project only."
    )
    control: FleetControl = Field(description="What the operator has instructed.")
    previous_state: str | None = Field(
        None,
        description="What it was doing before the process last stopped it. This is what "
        "keeps 'was running' distinguishable from 'was drained because we were deploying' "
        "across a restart -- the operator's intent is otherwise what a restart destroys.",
    )
    stale: int = Field(
        0, description="Claims whose lease expired without finishing -- the worker is gone."
    )
    orphaned: int = Field(
        0,
        description="Claims held by a process that no longer exists. Distinct from "
        "`stale`, and the distinction is the point: stale means a LEASE ran out, which "
        "is a timeout and therefore a guess; orphaned means the owning pid is gone, "
        "which is a fact. Orphans are reclaimed when the project next starts rather "
        "than waiting out a lease as work nobody is doing.",
    )
    workers: int = Field(
        0,
        description="Workers actually alive for this project. Distinct from the control "
        "state on purpose: `running` is an instruction, this is whether anything is "
        "carrying it out. A project marked running with zero workers is the failure "
        "that otherwise looks like success.",
    )
    worker_failures: int = Field(
        0,
        description="Workers that stopped without being asked to, since this process "
        "started. A fleet whose workers are dying and a fleet with nothing to do both "
        "report no work in progress, which is why this is counted separately.",
    )
    last_worker_error: str | None = Field(
        None, description="Why the most recent one died, and what it was holding."
    )


class PreflightCheck(BaseModel):
    name: str = Field(description="What was checked, e.g. `reviewer`, `checkout`.")
    ok: bool = Field(description="Whether it passed.")
    detail: str = Field(description="What was found, in words. Never a credential.")
    blocking: bool = Field(
        description="Blocking means the definition of done is unreachable, not merely "
        "that quality suffers. Only blocking checks refuse a start."
    )


class PreflightResult(BaseModel):
    """Whether a project can actually finish an item.

    A queue resumed without a reviewer, a checkout or write access claims work,
    spends money and fails everything -- while reporting `running`. The
    expensive part is that a nonproductive fleet looks exactly like a
    productive one until the bill arrives.
    """

    project_id: str = Field(description="The project this report is about.")
    ready: bool = Field(description="Whether a start would be accepted right now.")
    summary: str = Field(description="`ready`, or the blocking reasons joined.")
    checks: list[PreflightCheck] = Field(
        default_factory=list, description="Every check run, blocking and advisory alike."
    )


class ProjectList(BaseModel):
    projects: list[ProjectSummary] = Field(description="Every registered project.")


class ReadinessProbe(BaseModel):
    """One capability, and whether it is actually available."""

    configured: bool = Field(
        description="Whether this deployment was given the thing at all. Absent by "
        "configuration and present-but-broken are different problems with different "
        "fixes, and a single boolean cannot tell them apart."
    )
    ok: bool = Field(description="Whether it answered. False whenever it is unconfigured.")
    detail: str = Field(description="What was found, in words. Never a credential.")


class ProjectReadiness(BaseModel):
    project_id: str = Field(description="The project this entry is about.")
    ready_to_start: bool = Field(
        description="Whether `POST /api/projects/{id}/start` would be accepted. Derived "
        "from the same preflight the start action runs, so the two cannot disagree."
    )
    summary: str = Field(description="`ready`, or the blocking reasons joined.")
    blockers: list[PreflightCheck] = Field(
        default_factory=list, description="Checks that make the definition of done unreachable."
    )
    warnings: list[PreflightCheck] = Field(
        default_factory=list,
        description="Checks that reduce quality without making finishing impossible — "
        "no verification commands, a reviewer sharing a vendor with the implementer.",
    )


class ExecutionReadiness(BaseModel):
    """Whether this harness can execute anything, and why not.

    Separate from `/healthz` on purpose. Health answers whether the service is
    up, and a monitoring-only deployment is perfectly healthy while being
    unable to run a single item — so a healthy service reads as an executable
    fleet, and the only way to find out otherwise was to attempt a
    state-changing start.

    Nothing here writes: no worker is created, no session is started, no item
    is claimed, and no state is mutated.
    """

    mode: Literal["supervised", "monitoring-only"] = Field(
        description="`supervised` means a worker pool is attached and starting a project "
        "can create workers. `monitoring-only` is a legitimate deployment — a dashboard "
        "over someone else's harness — and starting is expected to refuse."
    )
    ready_to_start: bool = Field(
        description="Whether at least one project could be started right now. False on a "
        "monitoring-only deployment, and false when every project is blocked."
    )
    workers: ReadinessProbe = Field(description="Is there a worker pool at all?")
    session_host: ReadinessProbe = Field(
        description="The terminal-session host the agents run in. Probed with a read, so "
        "it proves reachability AND that the token is accepted, without creating a session."
    )
    reviewer: ReadinessProbe = Field(
        description="Is a reviewer role routed? Without one every review fails closed, so "
        "every item fails after the implementation has been paid for."
    )
    projects: list[ProjectReadiness] = Field(
        default_factory=list, description="Per project, because readiness is per project."
    )


# --------------------------------------------------------------------- plan


class PlanItem(BaseModel):
    id: str = Field(description="Stable id from the plan, e.g. `T4`.")
    title: str = Field(description="The heading text, without the id.")
    body: str = Field(
        description="The prose under the heading. NOT the whole brief -- an agent is "
        "given the title and this together, which is what `POST /api/plan/load` builds."
    )
    labels: list[str] = Field(default_factory=list)
    milestone: str | None = Field(None, description="Milestone the plan assigns it to.")
    depends_on: list[str] = Field(
        default_factory=list, description="Ids this item says it waits on."
    )
    done: bool = Field(
        False, description="The plan already marks it finished. Not loaded by default."
    )
    line: int = Field(description="Line in the source plan, so a reader can find it again.")


class PlanParseResult(BaseModel):
    items: list[PlanItem] = Field(description="Everything recognised as work.")
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
    created: list[str] = Field(default_factory=list, description="Issues created.")
    updated: list[str] = Field(
        default_factory=list, description="Issues whose description was refreshed."
    )
    unchanged: list[str] = Field(default_factory=list, description="Issues already matching.")
    orphaned: list[str] = Field(
        default_factory=list,
        description="Issues for items no longer in the plan. Never closed automatically — "
        "an item vanishing from a document is not grounds to close work.",
    )
    labels_created: list[str] = Field(
        default_factory=list,
        description="Labels the plan asked for and the repository lacked. Reported "
        "because creating them changes the repository, and that must not be silent.",
    )
    milestones_created: list[str] = Field(
        default_factory=list, description="Milestones created, for the same reason."
    )
    dry_run: bool = Field(description="Whether this was a report or a real sync.")


# ------------------------------------------------------------------- errors


class RateLimits(BaseModel):
    window: str = Field(description="The window these counts cover, e.g. `24h`.")
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
    by_worker: list[dict[str, Any]] = Field(
        default_factory=list, description="Counts split by worker, busiest first."
    )
    by_endpoint: list[dict[str, Any]] = Field(
        default_factory=list, description="Counts split by provider endpoint."
    )
    by_role: list[dict[str, Any]] = Field(
        default_factory=list, description="Counts split by role, so spend caps can be attributed."
    )


# -------------------------------------------------------------------- audit


class AuditHealth(BaseModel):
    """Whether history is actually being recorded.

    Its own field because a degraded audit store is invisible otherwise: a
    fleet running unaudited looks exactly like a fleet running audited, and
    the difference is only discovered when someone asks a question months
    later and the answer is empty.
    """

    configured: bool = Field(description="False when no audit store is attached.")
    degraded: bool = Field(
        description="True when the store could not be opened and writes are being "
        "dropped. The harness keeps working on purpose -- observation failing must "
        "not stop delivery -- so this is the only signal that it is happening."
    )
    path: str | None = Field(None, description="Where the audit database lives.")
    events: int = Field(0, description="Events recorded.")
    oldest: float | None = Field(None, description="Unix time of the earliest event.")
    newest: float | None = Field(None, description="Unix time of the most recent event.")
    schema_version: int | None = Field(
        None, description="Schema the store was written under. Migrations are deliberate."
    )


class AuditCostRow(BaseModel):
    project_id: str | None = Field(None, description="Project these calls belong to.")
    role: str | None = Field(None, description="Role that made them, e.g. `reviewer`.")
    model: str | None = Field(None, description="Model as the provider names it.")
    calls: int = Field(0, description="Calls in this group.")
    tokens_in: int = Field(0, description="Prompt tokens.")
    tokens_out: int = Field(0, description="Completion tokens.")
    cost_usd: float | None = Field(
        None, description="Null when no call in this group carried a known price."
    )
    unpriced: int = Field(
        0,
        description="Calls whose price was unknown, counted SEPARATELY and never "
        "folded into the total. A sum that silently omits them reads as complete "
        "and is not.",
    )


class AuditCost(BaseModel):
    window: str = Field(description="The window these rows cover.")
    rows: list[AuditCostRow] = Field(
        default_factory=list, description="One row per project/role/model group."
    )
    total_cost_usd: float | None = Field(
        None, description="Summed across priced calls only. Null when nothing was priced."
    )
    total_unpriced: int = Field(
        0,
        description="Calls excluded from that total because their price was unknown. "
        "A total that silently absorbed them would read as complete and not be.",
    )
    partial: bool = Field(
        False,
        description="True when the requested window starts before the earliest "
        "recorded event, so the answer covers less than it was asked for.",
    )


class AuditDeliveryRow(BaseModel):
    project_id: str | None = Field(None, description="Project this row belongs to.")
    outcome: str | None = Field(None, description="Terminal outcome, e.g. `done`, `failed`.")
    n: int = Field(0, description="Events with this outcome.")
    items: int = Field(0, description="Distinct items, not events.")


class AuditDelivery(BaseModel):
    window: str = Field(description="The window these rows cover.")
    rows: list[AuditDeliveryRow] = Field(
        default_factory=list, description="One row per project and outcome."
    )
    partial: bool = Field(
        False,
        description="True when the window starts before the earliest recorded event, "
        "so the answer covers less than it was asked for.",
    )


class AuditRollupRow(BaseModel):
    day: str = Field(description="The day summarised, as `YYYY-MM-DD`.")
    project_id: str | None = Field(None, description="Project this row belongs to.")
    role: str | None = Field(None, description="Role these calls were made for.")
    model: str | None = Field(None, description="Model as the provider names it.")
    outcome: str | None = Field(None, description="Outcome these events shared.")
    events: int = Field(0, description="Events folded into this row.")
    tokens_in: int = Field(0, description="Prompt tokens for the day.")
    tokens_out: int = Field(0, description="Completion tokens for the day.")
    cost_usd: float | None = Field(None, description="Priced calls only, as in `AuditCostRow`.")
    latency_p50: float | None = Field(None, description="Median call latency, seconds.")


class AuditRollups(BaseModel):
    rows: list[AuditRollupRow] = Field(default_factory=list)
    rolled_up_through: str | None = Field(
        None,
        description="Last day covered. Raw events are only ever thinned once their day "
        "appears here -- thinning first would leave a hole in the series that nothing "
        "reports.",
    )


class MaintenanceResult(BaseModel):
    rolled_up: int = Field(
        description="Daily rows written. Zero is normal once today is the only uncovered day."
    )
    thinned: int = Field(
        description="Raw events removed. Only ever events whose day a rollup already covers."
    )
    errors: list[str] = Field(
        default_factory=list,
        description="What went wrong, if anything. Maintenance is best-effort.",
    )


class ReconcileResult(BaseModel):
    """What GitHub said happened to the work."""

    merged: int = Field(0, description="Pull requests that landed.")
    closed_unmerged: int = Field(
        0,
        description="Rejected outright -- from inside the harness this looks identical "
        "to a pull request still waiting.",
    )
    reverted: int = Field(
        0,
        description="Merged and then undone. The only honest quality metric here: "
        "approval rate says a reviewer agreed, revert rate says whether they should have.",
    )
    skipped: int = Field(
        0,
        description="Pull requests the harness did not create -- dependabot, humans. "
        "Counted, never attributed: an outcome belonging to no item inflates every "
        "rate it appears in.",
    )
    errors: list[str] = Field(
        default_factory=list, description="Pull requests that could not be read, and why."
    )


class InceptionStart(BaseModel):
    project_id: str = Field(description="Id the project will be registered under.")
    overview: str = Field(
        description="A paragraph describing what you want. Not a plan -- the point is "
        "that you do not have to write one."
    )


class ScopeRequest(BaseModel):
    feedback: str | None = Field(
        None,
        description="What was wrong with the previous proposal. Revises it rather than "
        "starting over, so points you already settled are not re-argued.",
    )


class OpenQuestion(BaseModel):
    id: str = Field(description="Stable id, used to answer or defer it.")
    question: str = Field(description="What is being asked.")
    severity: Literal["blocking", "deferrable"] = Field(
        description="`blocking` means the answer changes what gets built -- choosing "
        "wrong means work is done and thrown away. `deferrable` means a reasonable "
        "default holds. Blocking on EVERY question is worse than no gate: one cosmetic "
        "question stalls the project and people answer carelessly to get past it."
    )
    why_it_matters: str = Field(
        "", description="What changes depending on the answer. The case for asking at all."
    )
    answer: str | None = Field(None, description="The answer given, if it has been answered.")
    deferred_reason: str | None = Field(
        None,
        description="Deferring is answering 'not now', which is different from unasked. "
        "It survives approval and stays visible on the plan.",
    )
    resolved_by: str | None = Field(None, description="Who answered or deferred it.")


class ProposalModel(BaseModel):
    revision: int = Field(
        description="Which revision this is. Feedback revises rather than restarts, so "
        "these accumulate instead of replacing one another."
    )
    created_at: float = Field(description="Unix time this revision was produced.")
    goal: str = Field("", description="What the project is for, in a sentence.")
    assumptions: list[str] = Field(
        default_factory=list,
        description="What it took as given. The most useful thing to argue with.",
    )
    non_goals: list[str] = Field(
        default_factory=list,
        description="Explicitly out of scope, so it is not quietly added later.",
    )
    risks: list[str] = Field(default_factory=list, description="What could make this go wrong.")
    phases: list[dict[str, Any]] = Field(
        default_factory=list, description="Proposed phases, each with its items."
    )
    questions: list[OpenQuestion] = Field(
        default_factory=list, description="What it could not decide on its own."
    )
    feedback: str | None = Field(None, description="The feedback that produced this revision.")
    item_count: int = Field(0, description="Work items across every phase.")
    blocking_open: int = Field(
        0, description="Unanswered blocking questions. Approval is refused while > 0."
    )


class ResolveQuestion(BaseModel):
    answer: str | None = Field(None, description="The answer. Required unless deferring.")
    defer_reason: str | None = Field(
        None, description="Required to defer. Silence never resolves a question."
    )
    severity: Literal["blocking", "deferrable"] | None = Field(
        None,
        description="Overrule the model, in either direction. It proposes severity so "
        "you are not triaging a flat list, but it does not decide what matters.",
    )
    who: str = Field("operator", description="Who is answering. Recorded, not verified.")


class Baseline(BaseModel):
    baseline_id: str = Field(description="Stable id for this measurement.")
    project_id: str = Field(description="Project it was measured on.")
    recorded_at: float = Field(description="Unix time it was recorded.")
    label: str = Field(description="What was measured, in words.")
    window_days: int = Field(description="Days the measurement covers.")
    items_done: int | None = Field(None, description="Items finished in that window.")
    cost_usd: float | None = Field(None, description="Spend across that window, where priced.")
    notes: str | None = Field(None, description="Anything a later comparison would need to know.")


class BaselineList(BaseModel):
    baselines: list[Baseline] = Field(
        default_factory=list, description="Recorded baselines, newest first."
    )


class NewBaseline(BaseModel):
    baseline_id: str = Field(description="Stable id. Recording twice under one id is refused.")
    project_id: str = Field(description="Project being measured.")
    label: str = Field(description="What was measured, in words.")
    window_days: int = Field(description="Days the measurement covers.")
    items_done: int | None = Field(None, description="Items finished in that window.")
    cost_usd: float | None = Field(None, description="Spend across that window, where priced.")
    notes: str | None = Field(None, description="Anything a later comparison would need to know.")


# ------------------------------------------------------------------- events


class Event(BaseModel):
    id: int = Field(
        description="Monotonic row id. Page with this, not with `ts`: two "
        "events in one millisecond must still have a total order."
    )
    ts: float = Field(description="Unix time the event happened.")
    kind: str = Field(description="What kind of event, e.g. `work`, `model_call`.")
    source: str = Field(description="Which stream it was read from.")
    worker: str | None = None
    role: str | None = None
    model: str | None = None
    endpoint: str | None = None
    outcome: str | None = None
    error_class: str | None = None
    latency_s: float | None = None
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="The event's own payload. For work events this carries "
        "`project_id`, `item_id` and any session ids -- an item is identified by "
        "project AND id, so a reader keying on `item_id` alone conflates projects.",
    )


class EventPage(BaseModel):
    events: list[Event]
    cursor: int = Field(description="Pass as `since_id` next time. Unchanged when empty.")


# ------------------------------------------------------------------ summary


class WaitingItem(BaseModel):
    item_id: str | None = None
    session_url: str | None = None


class Summary(BaseModel):
    running: int = Field(description="Items claimed right now.")
    pending: int = Field(description="Items waiting to be claimed.")
    done: int = Field(description="Items finished.")
    failed: int = Field(description="Items whose last attempt did not work.")
    stale: int = Field(
        description="Claims whose lease expired without finishing. Re-claimed "
        "automatically; a rising count means something is killing workers."
    )
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
    ok: bool = Field(description="Always true when the service answers at all.")
    events: int = Field(description="Events in the store. Zero is normal on a fresh deployment.")
    queue: bool = Field(description="Whether a work queue is attached.")
    authenticated: bool = Field(
        description="Whether a token is configured. False means every authenticated route refuses."
    )
    version: str = Field(description="The build serving this response.")


class InceptionRecord(BaseModel):
    """A scoping conversation before anything external exists.

    Nothing here has created a repository, an issue, a branch or a queue row.
    That only happens on approval, which is what makes `state` the field to
    read: it says how far a project is from being real.
    """

    project_id: str = Field(description="Id the project will be registered under.")
    state: str = Field(
        description="`draft` (a paragraph, nothing proposed yet), `scoping`, "
        "`proposed`, or `approved`. Only approval creates anything external."
    )
    overview: str = Field(description="The paragraph the project was described in.")
    revisions: list[str] = Field(
        default_factory=list,
        description="Feedback given on successive proposals, oldest first. Kept "
        "because a revised scope should not make somebody re-argue a settled point.",
    )
    created_at: float = Field(description="Unix time scoping began.")


class InceptionPlan(BaseModel):
    """The proposal rendered as a plan document."""

    markdown: str = Field(
        description="A real PLAN.md, not queue rows. It goes through the same "
        "parser as a hand-written plan -- including the part that reports what it "
        "could not read -- so a proposal the harness cannot consume is caught "
        "before it creates a single issue."
    )
