"""Read-only plan admission preview.

The preview is a proposal, not a decision.  It performs deterministic plan
validation and read-only local Git inspection, but creates no project, queue,
revision, ref, worktree, model call, or external request.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .graph import parse_dependencies
from .plan_contract import PlanContract, PlanContractError, parse_plan_contract
from .plan_revisions import acceptance_for_item, current_revision, revision_items
from .plan_validation import ValidationReport, validate_plan
from .work import BLOCKED, CLAIMED, DONE, FAILED, HELD, STOPPED, WorkQueue


class AdmissionError(ValueError):
    """A preview cannot be constructed from the supplied local facts."""


class GitFacts(Protocol):
    def __call__(self, argv: tuple[str, ...], cwd: Path, /) -> str: ...


def _git(argv: tuple[str, ...], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AdmissionError(result.stderr.strip() or f"git {' '.join(argv)} failed")
    return result.stdout.strip()


@dataclass(frozen=True)
class AdmissionProposal:
    proposal_digest: str
    plan_digest: str
    manifest_digest: str
    project_id: str
    worktree: str
    repository_identity: str
    base_ref: str
    base_sha: str
    integration_ref: str
    finalise_ref: str | None
    current_revision: int | None
    validation: ValidationReport

    def as_dict(self) -> dict[str, object]:
        return {
            "proposal_digest": self.proposal_digest,
            "plan_digest": self.plan_digest,
            "manifest_digest": self.manifest_digest,
            "project_id": self.project_id,
            "worktree": self.worktree,
            "repository_identity": self.repository_identity,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "integration_ref": self.integration_ref,
            "finalise_ref": self.finalise_ref,
            "current_revision": self.current_revision,
            "validation": self.validation.as_dict(),
        }


@dataclass(frozen=True)
class RevisionChange:
    item_id: str
    kind: str
    old_state: str | None
    new_state: str
    requires_reopen: bool = False
    removal_reason: str | None = None


@dataclass(frozen=True)
class RevisionProposal:
    admission: AdmissionProposal
    changes: tuple[RevisionChange, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            **self.admission.as_dict(),
            "changes": [
                {
                    "item_id": change.item_id,
                    "kind": change.kind,
                    "old_state": change.old_state,
                    "new_state": change.new_state,
                    "requires_reopen": change.requires_reopen,
                    "removal_reason": change.removal_reason,
                }
                for change in self.changes
            ],
        }


def revision_preview(
    queue: WorkQueue,
    *,
    admission: AdmissionProposal,
    plan_path: str | Path,
    worktree: str | Path,
    removed_items: dict[str, str] | None = None,
    reopen_items: set[str] | None = None,
    high_risk_removals: set[str] | None = None,
    git: GitFacts = _git,
) -> RevisionProposal:
    """Classify a revision without changing queue state."""
    removed = removed_items or {}
    reopen = reopen_items or set()
    high_risk = high_risk_removals or set()
    current = preview(
        queue,
        project_id=admission.project_id,
        plan_path=plan_path,
        worktree=worktree,
        git=git,
    )
    if current.plan_digest != admission.plan_digest:
        raise AdmissionError("proposal facts changed; preview the plan again")
    project = queue.get_project(admission.project_id)
    if project is not None and project.current_plan_revision is not None:
        control_state, _ = queue.control(admission.project_id)
        if control_state != STOPPED:
            raise AdmissionError("plan revision requires a stopped project")
        if queue.claimed(admission.project_id):
            raise AdmissionError("plan revision requires no claimed or held items")
        existing_revision = current_revision(queue, admission.project_id)
        if (
            existing_revision is not None
            and existing_revision.integration_ref != current.integration_ref
        ):
            raise AdmissionError("changing integration_ref requires a new project identity")
        if existing_revision is not None and existing_revision.initial_base_sha != current.base_sha:
            # This is informational in the diff model; the immutable new SHA is
            # persisted by apply and never follows a moving branch implicitly.
            pass

    contract = parse_plan_contract(Path(plan_path).read_text(encoding="utf-8"))
    old_rows = {
        row["item_id"]: row
        for row in revision_items(queue, admission.project_id, admission.current_revision or 0)
        if row["active"]
    }
    work_rows = {row.item_id: row for row in queue.items(project_id=admission.project_id)}
    new_items = {item.id: item for item in contract.parsed.items}
    omitted = sorted(set(old_rows) - set(new_items))
    missing_removals = [item_id for item_id in omitted if item_id not in removed]
    if missing_removals:
        raise AdmissionError(
            "omitted items require explicit removed_items reasons: " + ", ".join(missing_removals)
        )
    changes: list[RevisionChange] = []
    for item_id in sorted(set(old_rows) | set(new_items)):
        old = old_rows.get(item_id)
        item = new_items.get(item_id)
        work = work_rows.get(item_id)
        state: str | None = work.state if work is not None else None
        if item is None:
            if state in {CLAIMED, HELD}:
                raise AdmissionError(f"cannot remove active item {item_id}")
            if state in {DONE, FAILED, BLOCKED} and item_id not in high_risk:
                raise AdmissionError(f"removing {state} item {item_id} requires high-risk approval")
            changes.append(
                RevisionChange(
                    item_id, "removed", state, state or "pending", removal_reason=removed[item_id]
                )
            )
            continue
        acceptance = acceptance_for_item(item.body)
        changed = old is None or (
            old["title"] != item.title
            or old["brief"] != item.brief()
            or old["deliverable"] != item.deliverable
            or json.loads(old["depends_on_json"]) != item.depends_on
            or json.loads(old["acceptance_json"]) != acceptance
        )
        if old is None:
            changes.append(RevisionChange(item_id, "added", None, "pending"))
        elif not changed:
            changes.append(RevisionChange(item_id, "unchanged", state, state or "pending"))
        else:
            if state == DONE and item_id not in reopen:
                raise AdmissionError(
                    f"done item {item_id} changed; explicit reopen approval is required"
                )
            new_state = "pending" if state == DONE and item_id in reopen else state or "pending"
            changes.append(RevisionChange(item_id, "changed", state, new_state, state == DONE))
    return RevisionProposal(admission=current, changes=tuple(changes))


def apply(
    queue: WorkQueue,
    *,
    proposal: AdmissionProposal,
    plan_path: str | Path,
    worktree: str | Path,
    operator: str,
    expected_base_sha: str,
    expected_current_revision: int | None,
    git: GitFacts = _git,
    fail_at: str | None = None,
    removed_items: dict[str, str] | None = None,
    reopen_items: set[str] | None = None,
    high_risk_removals: set[str] | None = None,
) -> int:
    """Apply one reviewed proposal atomically and idempotently.

    Git is only read before the transaction.  The transaction itself contains
    no ref creation, process start, model call, or remote operation.
    """
    current = preview(
        queue,
        project_id=proposal.project_id,
        plan_path=plan_path,
        worktree=worktree,
        git=git,
    )
    if current.plan_digest != proposal.plan_digest:
        raise AdmissionError("proposal facts changed; preview the plan again")
    revision = revision_preview(
        queue,
        admission=proposal,
        plan_path=plan_path,
        worktree=worktree,
        removed_items=removed_items,
        reopen_items=reopen_items,
        high_risk_removals=high_risk_removals,
        git=git,
    )
    if current.base_sha != expected_base_sha:
        raise AdmissionError("the reviewed base commit changed")
    if (
        current.current_revision != expected_current_revision
        and current.current_revision is not None
    ):
        # An already-present exact plan is the idempotent replay case.  The
        # transaction below proves that identity before returning it.
        existing_check = queue._connect()
        try:
            existing_row = existing_check.execute(
                "SELECT 1 FROM plan_revisions WHERE project_id = ? AND plan_digest = ?",
                (proposal.project_id, proposal.plan_digest),
            ).fetchone()
        finally:
            existing_check.close()
        if existing_row is None:
            raise AdmissionError("the reviewed current revision changed")
    markdown = Path(plan_path).read_text(encoding="utf-8")
    contract = parse_plan_contract(markdown)
    now = float(queue.now())
    conn = queue._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT revision FROM plan_revisions WHERE project_id = ? AND plan_digest = ?",
            (proposal.project_id, proposal.plan_digest),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return int(existing["revision"])
        live = conn.execute(
            "SELECT current_plan_revision FROM projects WHERE project_id = ?",
            (proposal.project_id,),
        ).fetchone()
        live_revision = live["current_plan_revision"] if live is not None else None
        if live_revision != expected_current_revision:
            raise AdmissionError("the current revision changed during admission")
        revision_row = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) AS revision FROM plan_revisions "
            "WHERE project_id = ?",
            (proposal.project_id,),
        ).fetchone()
        revision_number = int(revision_row["revision"]) + 1
        conn.execute(
            "INSERT INTO projects (project_id, name, repo, work_dir, base_branch, plan_branch, "
            "current_plan_revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET name=excluded.name, repo=excluded.repo, "
            "work_dir=excluded.work_dir, base_branch=excluded.base_branch, "
            "plan_branch=excluded.plan_branch, updated_at=excluded.updated_at",
            (
                proposal.project_id,
                contract.project.name,
                proposal.repository_identity,
                str(Path(worktree).resolve()),
                contract.repository.base_ref,
                contract.repository.integration_ref,
                now,
                now,
            ),
        )
        if fail_at == "project":
            raise RuntimeError("injected admission failure: project")
        conn.execute(
            "INSERT INTO control (project_id, state, changed_at) VALUES (?, ?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET state=excluded.state, "
            "changed_at=excluded.changed_at",
            (proposal.project_id, STOPPED, now),
        )
        conn.execute(
            "INSERT INTO plan_revisions (project_id, revision, plan_digest, manifest_digest, "
            "plan_markdown, manifest_json, repository_identity, initial_base_sha, integration_ref, "
            "finalise_ref, adapter_versions_json, admitted_by, admitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)",
            (
                proposal.project_id,
                revision_number,
                proposal.plan_digest,
                proposal.manifest_digest,
                markdown,
                contract.canonical_json(),
                proposal.repository_identity,
                proposal.base_sha,
                proposal.integration_ref,
                proposal.finalise_ref,
                operator,
                now,
            ),
        )
        if fail_at == "revision":
            raise RuntimeError("injected admission failure: revision")
        change_by_id = {change.item_id: change for change in revision.changes}
        old_rows = {
            row["item_id"]: row
            for row in conn.execute(
                "SELECT * FROM plan_revision_items WHERE project_id = ? AND revision = ?",
                (proposal.project_id, expected_current_revision or 0),
            )
        }
        for ordinal, item in enumerate(contract.parsed.items):
            change = change_by_id[item.id]
            state = change.new_state
            conn.execute(
                "INSERT INTO plan_revision_items (project_id, revision, item_id, ordinal, title, "
                "brief, deliverable, depends_on_json, acceptance_json, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    proposal.project_id,
                    revision_number,
                    item.id,
                    ordinal,
                    item.title,
                    item.brief(),
                    item.deliverable,
                    json.dumps(item.depends_on),
                    json.dumps(acceptance_for_item(item.body)),
                ),
            )
            existing_work = conn.execute(
                "SELECT 1 FROM work WHERE project_id = ? AND item_id = ?",
                (proposal.project_id, item.id),
            ).fetchone()
            if existing_work is None:
                conn.execute(
                    "INSERT INTO work (project_id, item_id, title, brief, depends_on, state, "
                    "branch, "
                    "updated_at, deliverable, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        proposal.project_id,
                        item.id,
                        item.title,
                        item.brief(),
                        json.dumps(item.depends_on),
                        state,
                        f"refs/heads/harness/{proposal.project_id}/r{revision_number}/{item.id}",
                        now,
                        item.deliverable,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE work SET title = ?, brief = ?, depends_on = ?, deliverable = ?, "
                    "state = ?, active = 1, updated_at = ? WHERE project_id = ? AND item_id = ?",
                    (
                        item.title,
                        item.brief(),
                        json.dumps(item.depends_on),
                        item.deliverable,
                        state,
                        now,
                        proposal.project_id,
                        item.id,
                    ),
                )
            queue.graph.set_edges(
                proposal.project_id,
                item.id,
                parse_dependencies(item.depends_on),
                conn=conn,
            )
        for item_id, change in change_by_id.items():
            if change.kind != "removed":
                continue
            old = old_rows.get(item_id)
            if old is None:
                continue
            conn.execute(
                "INSERT INTO plan_revision_items (project_id, revision, item_id, ordinal, title, "
                "brief, deliverable, depends_on_json, acceptance_json, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    proposal.project_id,
                    revision_number,
                    item_id,
                    len(contract.parsed.items),
                    old["title"],
                    old["brief"],
                    old["deliverable"],
                    old["depends_on_json"],
                    old["acceptance_json"],
                ),
            )
            conn.execute(
                "UPDATE work SET active = 0, updated_at = ? WHERE project_id = ? AND item_id = ?",
                (now, proposal.project_id, item_id),
            )
            queue.graph.set_edges(proposal.project_id, item_id, [], conn=conn)
        if fail_at == "items":
            raise RuntimeError("injected admission failure: items")
        conn.execute(
            "UPDATE projects SET current_plan_revision = ?, updated_at = ? WHERE project_id = ?",
            (revision_number, now, proposal.project_id),
        )
        if fail_at == "before_commit":
            raise RuntimeError("injected admission failure: before_commit")
        conn.commit()
        return revision_number
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _manifest_hash(contract: PlanContract) -> str:
    return hashlib.sha256(contract.canonical_json().encode("utf-8")).hexdigest()


def preview(
    queue: WorkQueue,
    *,
    project_id: str,
    plan_path: str | Path,
    worktree: str | Path,
    git: GitFacts = _git,
) -> AdmissionProposal:
    """Build an admission proposal without changing local or queue state."""
    plan = Path(plan_path)
    root = Path(worktree).resolve()
    if not plan.is_file():
        raise AdmissionError(f"plan file does not exist: {plan}")
    markdown = plan.read_text(encoding="utf-8")
    plan_digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    validation = validate_plan(markdown, source=str(plan))
    if not validation.valid:
        raise AdmissionError(json.dumps(validation.as_dict(), sort_keys=True))
    try:
        contract = parse_plan_contract(markdown)
    except PlanContractError as exc:
        raise AdmissionError(str(exc)) from exc
    try:
        repository_identity = git(("rev-parse", "--show-toplevel"), root)
        base_sha = git(
            ("rev-parse", "--verify", f"{contract.repository.base_ref}^{{commit}}"), root
        )
    except (AdmissionError, OSError) as exc:
        raise AdmissionError(f"could not resolve local repository facts: {exc}") from exc
    current = current_revision(queue, project_id)
    manifest_digest = _manifest_hash(contract)
    proposal_body = {
        "base_ref": contract.repository.base_ref,
        "base_sha": base_sha,
        "current_revision": current.revision if current else None,
        "finalise_ref": contract.repository.finalise_ref,
        "integration_ref": contract.repository.integration_ref,
        "manifest_digest": manifest_digest,
        "plan_digest": plan_digest,
        "project_id": project_id,
        "repository_identity": repository_identity,
        "worktree": str(root),
    }
    proposal_digest = hashlib.sha256(
        json.dumps(proposal_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return AdmissionProposal(
        proposal_digest=proposal_digest,
        plan_digest=plan_digest,
        manifest_digest=manifest_digest,
        project_id=project_id,
        worktree=str(root),
        repository_identity=repository_identity,
        base_ref=contract.repository.base_ref,
        base_sha=base_sha,
        integration_ref=contract.repository.integration_ref,
        finalise_ref=contract.repository.finalise_ref,
        current_revision=current.revision if current else None,
        validation=validation,
    )
