"""Immutable admitted-plan snapshots and current membership.

The revision store is intentionally small.  It owns historical plan content;
the queue's ``work`` and ``plans`` tables remain mutable runtime projections.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .plan_contract import PlanContract
from .redaction import Redactor
from .work import WorkQueue


class RevisionConflict(ValueError):
    """The requested snapshot conflicts with an existing immutable revision."""


@dataclass(frozen=True)
class PlanRevision:
    project_id: str
    revision: int
    plan_digest: str
    manifest_digest: str
    plan_markdown: str
    manifest_json: str
    repository_identity: str
    initial_base_sha: str
    integration_ref: str
    finalise_ref: str | None
    adapter_versions_json: str
    admitted_by: str
    admitted_at: float


def manifest_digest(contract: PlanContract) -> str:
    return hashlib.sha256(contract.canonical_json().encode("utf-8")).hexdigest()


def acceptance_for_item(item_body: str) -> list[str]:
    marker = "**Acceptance:**"
    if marker not in item_body:
        return []
    return [
        line.strip()[2:].strip()
        for line in item_body.splitlines()[item_body.splitlines().index(marker) + 1 :]
        if line.strip().startswith("-")
    ]


def _row(row: sqlite3.Row) -> PlanRevision:
    return PlanRevision(**dict(row))


def admit_snapshot(
    queue: WorkQueue,
    *,
    project_id: str,
    markdown: str,
    plan_digest: str,
    contract: PlanContract,
    repository_identity: str,
    initial_base_sha: str,
    admitted_by: str,
    adapter_versions: dict[str, Any] | None = None,
    now: float | None = None,
    redactor: Redactor | None = None,
) -> PlanRevision:
    """Persist one immutable revision, idempotently for identical bytes."""
    if not plan_digest or len(plan_digest) != 64:
        raise ValueError("plan_digest must be a SHA-256 hex digest")
    if redactor is not None and redactor.redacted(markdown):
        raise RevisionConflict("plan contains credential-shaped content and was not stored")
    if now is None:
        now = float(queue.now())
    manifest_json = contract.canonical_json()
    manifest_hash = manifest_digest(contract)
    adapters_json = json.dumps(adapter_versions or {}, sort_keys=True, separators=(",", ":"))
    conn = queue._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM plan_revisions WHERE project_id = ? AND plan_digest = ?",
            (project_id, plan_digest),
        ).fetchone()
        if existing is not None:
            return _row(existing)
        latest = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) AS revision FROM plan_revisions "
            "WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        revision = int(latest["revision"]) + 1
        conn.execute(
            "INSERT INTO plan_revisions (project_id, revision, plan_digest, manifest_digest, "
            "plan_markdown, manifest_json, repository_identity, initial_base_sha, integration_ref, "
            "finalise_ref, adapter_versions_json, admitted_by, admitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                revision,
                plan_digest,
                manifest_hash,
                markdown,
                manifest_json,
                repository_identity,
                initial_base_sha,
                contract.repository.integration_ref,
                contract.repository.finalise_ref,
                adapters_json,
                admitted_by,
                now,
            ),
        )
        for ordinal, item in enumerate(contract.parsed.items):
            conn.execute(
                "INSERT INTO plan_revision_items (project_id, revision, item_id, ordinal, title, "
                "brief, deliverable, depends_on_json, acceptance_json, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    project_id,
                    revision,
                    item.id,
                    ordinal,
                    item.title,
                    item.brief(),
                    item.deliverable,
                    json.dumps(item.depends_on),
                    json.dumps(acceptance_for_item(item.body)),
                ),
            )
        conn.execute(
            "UPDATE projects SET current_plan_revision = ?, updated_at = ? WHERE project_id = ?",
            (revision, now, project_id),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise RevisionConflict(f"project does not exist: {project_id}")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM plan_revisions WHERE project_id = ? AND revision = ?",
            (project_id, revision),
        ).fetchone()
        assert row is not None
        return _row(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def current_revision(queue: WorkQueue, project_id: str) -> PlanRevision | None:
    conn = queue._connect()
    try:
        row = conn.execute(
            "SELECT r.* FROM plan_revisions r JOIN projects p ON p.project_id = r.project_id "
            "AND p.current_plan_revision = r.revision WHERE r.project_id = ?",
            (project_id,),
        ).fetchone()
        return _row(row) if row is not None else None
    finally:
        conn.close()


def revision_items(queue: WorkQueue, project_id: str, revision: int) -> list[dict[str, Any]]:
    conn = queue._connect()
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM plan_revision_items WHERE project_id = ? AND revision = ? "
                "ORDER BY ordinal",
                (project_id, revision),
            )
        ]
    finally:
        conn.close()
