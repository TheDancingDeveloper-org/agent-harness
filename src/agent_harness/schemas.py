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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

# --------------------------------------------------------------------- work

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
    blocked_reason: str | None = Field(
        None,
        description="Why an operator blocked this item, when its state is `blocked`. "
        "A block is a decision someone made, not a failure the harness had -- reading "
        "it out of `last_error` would make the two indistinguishable.",
    )
    disposition: str = Field(
        "",
        description="WHY the item is in `state`, from the Stage K taxonomy: "
        "`completed`, `refused` (a gate said no about this item's work), `crashed` "
        "(the worker or harness broke, and nothing judged the work), `withheld` "
        "(never attempted, or discarded through no fault of the item) or `escalated` "
        "(a person has to resolve something). `state` alone cannot tell a reviewer's "
        "rejection from a crashed worker — both are `failed` — and those want "
        "different responses. Empty means nobody has finished with it yet, which is "
        "not a sixth disposition.",
    )
    reason_kind: str = Field(
        "",
        description="The specific reason, as a token rather than English, so a client "
        "can branch on it: `checks_failed`, `check_escalated`, `check_transient`, "
        "`review_rejected`, `patch_rejected`, `no_target`, `worker_error`, "
        "`provider_exhausted`, `budget_exhausted`, `dependency_invalidated`, "
        "`agent_timeout`, `claim_lost`.",
    )
    branch: str | None = None
    pr_url: str | None = None
    updated_at: float = 0.0
    latest: LatestEvent | None = None

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
    items: list[WorkItem] = Field(default_factory=list)


class RetryResult(BaseModel):
    ok: bool
    item_id: str
    state: WorkState


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
    ok: bool
    item_id: str
    state: WorkState
    reason: str = Field(description="The reason as it is now recorded on the item.")


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
    model: str = Field(
        "",
        description="Model identifier as the provider names it. The PREFERRED "
        "one when `models` names several; filled in from the first of them "
        "when omitted.",
    )
    models: list[str] = Field(
        default_factory=list,
        description="Models to try for this role, in preference order. The "
        "first that answers does the work; the rest are tried only when it "
        "will not, and the whole list is tried before any backoff. Omit for a "
        "single model — `model` alone still works and always has.",
    )
    endpoint: str = Field(description="Base URL of the provider API.")
    provider: str = Field(
        "claw-bay",
        description="Failure classifier to use, by preset name: `generic` "
        "cannot tell a spend cap from a burst "
        "limit, because nothing in HTTP can. This field selects ONLY the "
        "classifier; the wire protocol comes from `preset`, or from the "
        "deployment's default when this route names none.",
    )
    preset: str = Field(
        "",
        description="Route preset: the wire protocol, the authentication "
        "strategy, the response/usage reader and a failure classifier, as one "
        "registered name. Overrides `provider` when both are given. Empty means "
        "the deployment's default preset. The names a deployment can use are "
        "whatever is registered in its process, named in $HARNESS_ROUTE_PRESETS "
        "or published by an installed distribution — adding a vendor needs no "
        "change to this service.",
    )
    price_ref: str = Field(
        "",
        description="What to look this model up as in the price table, when "
        "that is not its model id. Empty uses the model id. A model the table "
        "does not price keeps its token counts and gets no cost at all: an "
        "unknown price is reported as unknown, never as zero.",
    )

    @model_validator(mode="after")
    def one_source_of_truth(self) -> RoleRoute:
        """Keep `model` and `models` from disagreeing.

        They are two views of one thing, and a route where they contradict
        each other is a route whose behaviour depends on which field a reader
        happens to consult -- exactly the ambiguity that made the old
        single-field map unable to express a fallback at all.
        """
        if self.models and not self.model:
            self.model = self.models[0]
        elif self.model and not self.models:
            self.models = [self.model]
        elif not self.model and not self.models:
            raise ValueError("a role needs a model: set `model`, or `models` in preference order")
        elif self.models[0] != self.model:
            raise ValueError(
                f"`model` is {self.model!r} but `models` prefers {self.models[0]!r}; "
                "`model` is the preferred route, so either match it or omit it"
            )
        return self


class RoutedRole(RoleRoute):
    """A route, and whether this deployment's executor ever calls it."""

    used: bool = Field(
        True,
        description="False when the active executor never calls this role, so the "
        "route is configuration nothing acts on. In session mode the agent process "
        "plans and implements with its own credentials and endpoint, and only the "
        "reviewer is a routed model call -- an operator reading `implementer` here "
        "would otherwise look for that spend in an audit log where it can never "
        "appear.",
    )
    unused_reason: str = Field(
        "", description="What does this role's work instead, in words. Empty when `used`."
    )


class RoleMap(BaseModel):
    """The role map as it is set. Sent to `PUT /api/roles`."""

    roles: dict[str, RoleRoute] = Field(
        description="role -> where its calls go. Changing this takes effect on the next "
        "call: the call site names a ROLE, never a model, which is what makes the map "
        "changeable without a redeploy."
    )


class RoleMapView(BaseModel):
    """The role map as it will actually be used.

    Returned by both the read and the write, because a `PUT` that stores a
    route nothing calls should say so in the same breath rather than echo it
    back as if it had changed what runs.
    """

    reviewer_independent: bool = Field(
        True,
        description="False when the reviewer is the same model, or the same vendor, as "
        "the implementer -- some share of reviews is then a model grading its own work. "
        "Computed against the implementer the active executor *actually* uses, and by "
        "the same code as preflight's `reviewer independence` check, so the two cannot "
        "disagree. Reported rather than refused: running one model is a legitimate "
        "deliberate choice, but it must not be a surprise.",
    )
    reviewer_note: str = Field("", description="Why, in words.")
    roles: dict[str, RoutedRole] = Field(
        description="role -> where its calls go, and whether anything calls it. This is "
        "the global map; a project may override any role, in which case its own "
        "preflight reports the route that project will use."
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
        default_factory=list,
        description=(
            "Commands run before the reviewer, cheapest first. Each entry is an argv command "
            "(shlex-split, no shell); shell operators such as &&, ||, |, ; and > are rejected."
        ),
    )
    plan_path: str | None = None
    roles: dict[str, RoleRoute] | None = Field(
        None, description="Role overrides for this project. Null uses the global map."
    )
    max_workers: int = Field(
        1,
        description="Concurrency budget. Its purpose is that one project cannot "
        "starve another, so it is per project rather than per fleet. Each worker owns "
        "a worktree and its build output, so raising this also multiplies peak disk use.",
    )
    min_free_disk_gb: float = Field(
        0.0,
        ge=0,
        description="Minimum free GiB required on the volume holding work_dir. Zero "
        "reports disk space without imposing a floor; a positive value blocks preflight.",
    )
    max_attempts: int = Field(
        5,
        ge=0,
        description="Item-level attempts before repeatedly retryable work becomes exhausted. "
        "Zero disables automatic retirement.",
    )

    @field_validator("checks")
    @classmethod
    def check_commands_are_argv(cls, commands: list[str]) -> list[str]:
        from .runtime import validate_check_command

        for command in commands:
            validate_check_command(command)
        return commands


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
    draining_items: list[str] = Field(
        default_factory=list,
        description="Claimed items a draining project is waiting for. Empty outside a drain, "
        "or once every in-flight item has reached its boundary.",
    )


class PreflightCheck(BaseModel):
    name: str
    ok: bool
    detail: str
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

    project_id: str
    ready: bool
    summary: str
    checks: list[PreflightCheck] = Field(default_factory=list)


class ProjectList(BaseModel):
    projects: list[ProjectSummary]


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
    project_id: str
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


class InceptionDraft(BaseModel):
    """A scoping session that has been opened and nothing more.

    Named rather than returned as a bare dictionary: a generated client cannot
    discover `state` or `project_id` from `additionalProperties: true`, and
    these two fields are the whole answer to "did it start, and under what id".
    """

    project_id: str = Field(description="The project being scoped.")
    state: str = Field(description="Where in the inception flow this sits, e.g. `draft`.")
    overview: str = Field(description="The paragraph the human supplied.")
    revisions: list[str] = Field(
        default_factory=list, description="Feedback given on previous proposals, oldest first."
    )
    created_at: float | None = Field(None, description="When scoping began, unix seconds.")


class InceptionPlan(BaseModel):
    """A proposal rendered as a PLAN.md."""

    markdown: str = Field(
        description="The plan document, ready to be written to a file and synced."
    )


class BaseCheckStatus(BaseModel):
    """A base-branch check run, which happens off the request thread.

    The suite takes as long as a build, so the request that starts it cannot
    also wait for it. This is what you poll instead.
    """

    project_id: str
    state: Literal["running", "passed", "failed", "not_run"] = Field(
        description="`not_run` means no run has been started since this process came up."
    )
    ok: bool | None = Field(
        None, description="Whether the checks passed. Null while running, or before any run."
    )
    detail: str = Field("", description="The command that failed, or what passed on which branch.")
    started_at: float | None = None
    finished_at: float | None = None


class StopProjectRequest(BaseModel):
    """Optional context for stopping one project.

    The target state is deliberately absent: the route already says stop.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(
        None,
        description="Why the project is being stopped. Omit when there is no operator note.",
    )


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
        description="LOCAL dependencies naming items this plan does not define. A typo "
        "here blocks the item — explicitly, with a reason, rather than silently running "
        "it. External, decision and cross-project targets are reported separately "
        "because they are legitimate and need a different fix.",
    )
    external_dependencies: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Item id -> declared `external:RESOLVER:IDENTITY` tokens. Legitimate, "
        "and satisfied only once that resolver reports an outcome.",
    )
    decision_dependencies: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Item id -> declared `decision:ID` tokens. A decision must exist as "
        "work in the project before it can be made.",
    )
    cross_project_dependencies: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Item id -> declared `project:PROJECT/ITEM` tokens.",
    )
    malformed_dependencies: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Item id -> tokens whose grammar could not be read, with what was "
        "wrong. Carried rather than raised, so a bad line blocks its own item instead of "
        "failing the whole parse.",
    )
    dependency_cycles: list[list[str]] = Field(
        default_factory=list,
        description="Loops through required local dependencies. A plan containing one "
        "can never finish, and the cheapest place to hear that is before it becomes a "
        "backlog.",
    )
    unattached_arrows: list[str] = Field(
        default_factory=list,
        description="Lines in a ```dependencies block that named no item in this plan, "
        "or were not arrows at all. An arrow that lands nowhere would otherwise be "
        "discarded silently.",
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


# -------------------------------------------------------------------- graph

TargetKind = Literal["local_work", "external_reference", "human_decision", "cross_project_work"]
EdgeState = Literal["unresolved", "blocked", "satisfied"]


class DependencyEdgeModel(BaseModel):
    """One dependency edge, as the graph holds it."""

    source_item: str = Field(description="The item that is waiting.")
    target_kind: TargetKind = Field(
        description="What sort of thing is being waited for. `local_work` is an item in "
        "the same project; `external_reference` is outside the harness and needs a "
        "resolver; `human_decision` is a decision parked as work; `cross_project_work` "
        "is an item in another project, named `PROJECT/ITEM`."
    )
    target_id: str = Field(description="Identity of the target, within its kind.")
    required: bool = Field(
        description="Required edges gate admission. Advisory edges (`?T9` in a plan) are "
        "reported and never block -- an advisory edge that vanished silently would be "
        "indistinguishable from one that was never declared."
    )
    resolver: str | None = Field(
        None,
        description="Which resolver answers for an external target. An external target "
        "with no resolver stays `unresolved`, because nothing here can say whether it "
        "is done.",
    )
    state: EdgeState = Field(
        description="`unresolved` means the graph does not know and is NEVER a synonym "
        "for satisfied; `blocked` means the target is known and unfinished; `satisfied` "
        "means it is finished."
    )
    evidence: str = Field(
        description="Why the edge is in that state, in words. A satisfied external "
        "dependency with no stated reason is exactly the assumption this graph exists "
        "to remove."
    )
    provenance: str = Field(
        description="Where the edge came from, e.g. `work.depends_on` or `rebuild`."
    )
    revision: int = Field(description="Graph revision this edge was last written at.")


class ReadinessReasonModel(BaseModel):
    """One reason an item is not ready."""

    kind: str = Field(
        description="`dependency` for an unsatisfied edge, `cycle` for a loop that can "
        "never resolve, `stale_graph` for an item that declares dependencies the edge "
        "table does not hold yet (a database upgraded in place before "
        "`agent-harness graph rebuild` ran). Present so a client can branch on something "
        "other than English."
    )
    explanation: str = Field(description="The reason in words, safe to show a human.")
    target_kind: TargetKind | None = Field(None, description="Kind of the target involved.")
    target_id: str | None = Field(None, description="Identity of the target involved.")
    required: bool = Field(True, description="Whether this edge gates admission.")
    resolver: str | None = Field(None, description="Resolver for an external target.")
    state: EdgeState | None = Field(None, description="Resolution state of the edge.")
    evidence: str | None = Field(
        None, description="Evidence behind the state; for a cycle, the loop itself."
    )


class ItemReadiness(BaseModel):
    """Whether one item may be admitted, and why not."""

    project_id: str
    item_id: str
    ready: bool = Field(
        description="Whether the graph would admit this item right now. The SAME "
        "evaluation `claim` makes and the same one the executor repeats before the "
        "expensive gate -- two implementations would be two answers."
    )
    graph_revision: int = Field(
        description="The authoritative graph revision this answer was computed at. "
        "Admission records the revision it admitted at, so a later check can say the "
        "graph moved rather than only that the item is no longer eligible."
    )
    admitted_revision: int | None = Field(
        None,
        description="Graph revision the item's current claim was admitted at, when it "
        "is claimed. 0 for an item never claimed under a graph-aware build.",
    )
    reasons: list[ReadinessReasonModel] = Field(
        default_factory=list, description="Every required edge or cycle blocking the item."
    )
    advisory: list[ReadinessReasonModel] = Field(
        default_factory=list,
        description="Unsatisfied advisory edges. Reported, never blocking.",
    )
    overridden: bool = Field(
        False,
        description="Whether an operator override at this exact revision is what makes "
        "the item ready. An override is scoped to the revision it was granted at, so a "
        "later graph correction re-blocks the item rather than inheriting a decision "
        "nobody made about it.",
    )
    override_reason: str | None = Field(
        None, description="The recorded reason for that override, with who recorded it."
    )
    explanation: str = Field(
        description="The whole answer as one sentence, for a log line or an event detail."
    )


class DependencyGraphReport(BaseModel):
    """The whole dependency graph for one project."""

    project_id: str
    revision: int = Field(description="Current authoritative graph revision.")
    edges: list[DependencyEdgeModel] = Field(
        default_factory=list, description="Every declared edge, with its current state."
    )
    cycles: list[list[str]] = Field(
        default_factory=list,
        description="Loops through required local edges. Every member is unclaimable, "
        "and saying so is the difference between a queue that is waiting and a queue "
        "that can never finish.",
    )
    ready: list[str] = Field(
        default_factory=list, description="Items the graph would admit right now."
    )
    not_ready: list[ItemReadiness] = Field(
        default_factory=list, description="Items it would not, each with its reasons."
    )


class DependencyOverrideRequest(BaseModel):
    """Admitting blocked work deliberately, and recording who did."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        min_length=1,
        description="Why the block is being overridden, and REQUIRED. An override with "
        "no reason is indistinguishable from a gate that was never there.",
    )
    who: str | None = Field(
        None, description="Who took responsibility. Recorded with the reason, not verified."
    )


class DependencyOverrideResult(BaseModel):
    ok: bool
    project_id: str
    item_id: str
    revision: int = Field(
        description="The graph revision the override was granted at. It applies to THAT "
        "revision only: a later correction to the graph re-blocks the item."
    )
    readiness: ItemReadiness = Field(description="The item's readiness after the override.")


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
    events: int = 0
    oldest: float | None = Field(None, description="Unix time of the earliest event.")
    newest: float | None = None
    schema_version: int | None = None


class AuditCostRow(BaseModel):
    project_id: str | None = None
    role: str | None = None
    model: str | None = None
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
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
    window: str
    rows: list[AuditCostRow] = Field(default_factory=list)
    total_cost_usd: float | None = None
    total_unpriced: int = 0
    partial: bool = Field(
        False,
        description="True when the requested window starts before the earliest "
        "recorded event, so the answer covers less than it was asked for.",
    )


class AuditDeliveryRow(BaseModel):
    project_id: str | None = None
    outcome: str | None = None
    n: int = 0
    items: int = Field(0, description="Distinct items, not events.")


class AuditDelivery(BaseModel):
    window: str
    rows: list[AuditDeliveryRow] = Field(default_factory=list)
    partial: bool = False


class AuditRollupRow(BaseModel):
    day: str
    project_id: str | None = None
    role: str | None = None
    model: str | None = None
    outcome: str | None = None
    events: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    latency_p50: float | None = None


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
    errors: list[str] = Field(default_factory=list)


class ReconcileResult(BaseModel):
    """What GitHub said happened to the work."""

    merged: int = 0
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
    errors: list[str] = Field(default_factory=list)


class InceptionStart(BaseModel):
    project_id: str
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
    id: str
    question: str
    severity: Literal["blocking", "deferrable"] = Field(
        description="`blocking` means the answer changes what gets built -- choosing "
        "wrong means work is done and thrown away. `deferrable` means a reasonable "
        "default holds. Blocking on EVERY question is worse than no gate: one cosmetic "
        "question stalls the project and people answer carelessly to get past it."
    )
    why_it_matters: str = ""
    answer: str | None = None
    deferred_reason: str | None = Field(
        None,
        description="Deferring is answering 'not now', which is different from unasked. "
        "It survives approval and stays visible on the plan.",
    )
    resolved_by: str | None = None


class ProposalModel(BaseModel):
    revision: int
    created_at: float
    goal: str = ""
    assumptions: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    phases: list[dict[str, Any]] = Field(default_factory=list)
    questions: list[OpenQuestion] = Field(default_factory=list)
    feedback: str | None = None
    item_count: int = 0
    blocking_open: int = Field(
        0, description="Unanswered blocking questions. Approval is refused while > 0."
    )


class ResolveQuestion(BaseModel):
    answer: str | None = None
    defer_reason: str | None = Field(
        None, description="Required to defer. Silence never resolves a question."
    )
    severity: Literal["blocking", "deferrable"] | None = Field(
        None,
        description="Overrule the model, in either direction. It proposes severity so "
        "you are not triaging a flat list, but it does not decide what matters.",
    )
    who: str = "operator"


class Baseline(BaseModel):
    baseline_id: str
    project_id: str
    recorded_at: float
    label: str
    window_days: int
    items_done: int | None = None
    cost_usd: float | None = None
    notes: str | None = None


class BaselineList(BaseModel):
    baselines: list[Baseline] = Field(default_factory=list)


class NewBaseline(BaseModel):
    baseline_id: str = Field(description="Stable id. Recording twice under one id is refused.")
    project_id: str
    label: str = Field(description="What was measured, in words.")
    window_days: int
    items_done: int | None = None
    cost_usd: float | None = None
    notes: str | None = None


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
