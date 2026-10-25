"""Safe defaults for exploring a project that is new to the harness.

These are policy defaults, not workload facts. They give the scoper and
surveyor a consistent starting point while keeping repository-specific values
such as the owner, remote, checks, base commit and toolchain evidence-based.
An operator can override a default during the proposal review.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExplorationDefault:
    """One reviewable default used during project exploration."""

    key: str
    decision: str
    rationale: str


EXPLORATION_DEFAULTS: tuple[ExplorationDefault, ...] = (
    ExplorationDefault(
        "deployment",
        "Use a declared self-hosted Docker Compose deployment, built by GitHub Actions "
        "or equivalent self-hosted runners and released by the deployment orchestrator; "
        "keep deployment approval separate.",
        "The harness must not silently depend on a session host or deploy after merge.",
    ),
    ExplorationDefault(
        "third_party_gates",
        "Allow third-party gates through installed metadata, but register none by "
        "default; plan-declared checks remain the initial gate set.",
        "Generic extension must not become an undeclared vendor dependency.",
    ),
    ExplorationDefault(
        "review_rationale",
        "Keep reviewer-rationale exposure as an authorized A/B experiment, not a "
        "settled product claim.",
        "The effect must be measured rather than argued.",
    ),
    ExplorationDefault(
        "holds",
        "Allow one open hold per item and attempt; sequential holds are allowed "
        "after the prior hold is answered, cancelled or expired.",
        "A hold suspends a live attempt and must not create ambiguous concurrent questions.",
    ),
    ExplorationDefault(
        "lessons",
        "Defer shared lessons or memory until a real multi-item run measures "
        "repeated failures with comparable prompts.",
        "Changing prompts between items would make the measurement incomparable.",
    ),
    ExplorationDefault(
        "plan_shape",
        "Treat phase headings as tracking-only containers by default; only explicit "
        "work-item headings are claimable.",
        "A phase rationale is not an implementable work specification.",
    ),
    ExplorationDefault(
        "terminal_outcomes",
        "Give work that exhausts its retry/attempt policy a distinct terminal "
        "exhausted/gave-up disposition.",
        "An empty disposition must not look like unfinished work.",
    ),
    ExplorationDefault(
        "operator_actions",
        "Require exact expected state for bulk transitions and make continue or "
        "force-start explicit reviewed actions; neither bypasses gates or policy refusals.",
        "A stale operator view must not authorize a partial or unsafe transition.",
    ),
    ExplorationDefault(
        "diagnostics",
        "Redact credentials before durable writes; use bounded structured diagnostics "
        "and hashes for reproducibility instead of retaining raw sensitive responses.",
        "Debuggability cannot reintroduce secrets into append-only records.",
    ),
    ExplorationDefault(
        "edit_safety",
        "Canonicalize relative edit paths before grouping so equivalent paths cannot "
        "apply twice; reject ambiguous or out-of-tree edits.",
        "A correct edit must not fail or land twice because of path spelling.",
    ),
    ExplorationDefault(
        "execution_boundary",
        "Require live validation of the execution security profile and a suitable "
        "project toolchain image before any real workload run.",
        "Mocked subprocess tests are not evidence of isolation or tool compatibility.",
    ),
    ExplorationDefault(
        "fleet_acceptance",
        "Require local acceptance with concurrent independent items, dependent work "
        "and failure isolation before remote publication.",
        "One successful item does not prove fleet scheduling or isolation.",
    ),
    ExplorationDefault(
        "promotion",
        "Serialize integration-branch promotion, wait for all prerequisites, and "
        "return conflicts for agent repair instead of auto-merging them.",
        "Promotion is a gate-controlled coordination operation.",
    ),
    ExplorationDefault(
        "review_feedback",
        "Treat human review as authoritative: actionable feedback creates or resumes "
        "one correction item, ambiguous feedback creates a hold, and duplicates are no-ops.",
        "The harness must not guess at ambiguous human intent.",
    ),
    ExplorationDefault(
        "baseline",
        "Record the project’s own plan, declared checks, exact clean base commit and "
        "baseline metrics before acceptance. If checks fail, repair project setup or "
        "check configuration before recording the baseline.",
        "The harness supplies measurement machinery, never workload-specific numbers.",
    ),
    ExplorationDefault(
        "repository_setup",
        "If the source is not a Git checkout, create a fresh private repository only "
        "after the operator supplies the owner and name. Preserve provenance, add a "
        "minimal README and CI workflow for pushes and pull requests, use `main`, "
        "protect it with required CI/PR review, no force-push and linear-history "
        "rules where supported, scan credentials and artifacts, and verify licensing "
        "before publication.",
        "Repository creation and publication are external mutations requiring explicit review.",
    ),
    ExplorationDefault(
        "remote_publication",
        "A private production remote may be used for remote acceptance after local "
        "acceptance and evidence review; use one integration branch and one human-reviewed "
        "pull request, and keep merge and deployment as separate human approvals.",
        "A private remote reduces exposure but does not make publication or deployment automatic.",
    ),
    ExplorationDefault(
        "scope_control",
        "Pause unrelated framework or UI expansion while the first real local "
        "acceptance is outstanding.",
        "Execution evidence is the prerequisite for broadening the product.",
    ),
    ExplorationDefault(
        "legacy_session_host",
        "Treat silent activity in a legacy session-host path as a known limitation; "
        "it does not block the harness-owned local fleet.",
        "The primary execution path must have its own progress and isolation semantics.",
    ),
)


def render_exploration_defaults() -> str:
    """Render the defaults for a model prompt without naming a workload."""

    lines = [
        "## Generic exploration defaults",
        "",
        "Use these as defaults when repository evidence and the operator have not "
        "specified otherwise. Record material overrides as open questions or "
        "assumptions; do not silently turn a default into a project fact.",
        "",
    ]
    for item in EXPLORATION_DEFAULTS:
        lines.append(f"- **{item.key}**: {item.decision} ({item.rationale})")
    return "\n".join(lines)
