from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_harness.plan_contract import parse_plan_contract
from agent_harness.plan_revisions import (
    RevisionConflict,
    admit_snapshot,
    current_revision,
    revision_items,
)
from agent_harness.redaction import Redactor
from agent_harness.work import Project, WorkQueue


def test_admitted_revision_is_immutable_and_idempotent(tmp_path: Path) -> None:
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project("widgets", "Widgets", work_dir=str(tmp_path)))
    markdown = Path("examples/PLAN.md").read_text(encoding="utf-8")
    contract = parse_plan_contract(markdown)
    digest = hashlib.sha256(markdown.encode()).hexdigest()

    first = admit_snapshot(
        queue,
        project_id="widgets",
        markdown=markdown,
        plan_digest=digest,
        contract=contract,
        repository_identity="repo-1",
        initial_base_sha="a" * 40,
        admitted_by="operator",
    )
    again = admit_snapshot(
        queue,
        project_id="widgets",
        markdown=markdown,
        plan_digest=digest,
        contract=contract,
        repository_identity="repo-1",
        initial_base_sha="a" * 40,
        admitted_by="another-operator",
    )

    assert first == again
    assert current_revision(queue, "widgets") == first
    assert len(revision_items(queue, "widgets", 1)) == 4
    assert json.loads(revision_items(queue, "widgets", 1)[0]["acceptance_json"])


def test_new_bytes_create_new_revision_and_old_content_survives(tmp_path: Path) -> None:
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project("widgets", "Widgets"))
    markdown = Path("examples/PLAN.md").read_text(encoding="utf-8")
    contract = parse_plan_contract(markdown)
    digest = hashlib.sha256(markdown.encode()).hexdigest()
    first = admit_snapshot(
        queue,
        project_id="widgets",
        markdown=markdown,
        plan_digest=digest,
        contract=contract,
        repository_identity="repo",
        initial_base_sha="a" * 40,
        admitted_by="op",
    )

    changed = markdown.replace("Widget service", "Widget service v2", 1)
    changed_contract = parse_plan_contract(changed)
    second = admit_snapshot(
        queue,
        project_id="widgets",
        markdown=changed,
        plan_digest=hashlib.sha256(changed.encode()).hexdigest(),
        contract=changed_contract,
        repository_identity="repo",
        initial_base_sha="b" * 40,
        admitted_by="op",
    )

    assert first.revision == 1
    assert second.revision == 2
    assert first.plan_markdown != second.plan_markdown
    assert current_revision(queue, "widgets") == second


def test_secret_shaped_plan_never_reaches_revision_table(tmp_path: Path) -> None:
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project("widgets", "Widgets"))
    markdown = Path("examples/PLAN.md").read_text(encoding="utf-8")
    contract = parse_plan_contract(markdown)
    secret_plan = markdown.replace("Widget service", "token = super-secret-value", 1)
    with pytest.raises(RevisionConflict):
        admit_snapshot(
            queue,
            project_id="widgets",
            markdown=secret_plan,
            plan_digest=hashlib.sha256(secret_plan.encode()).hexdigest(),
            contract=contract,
            repository_identity="repo",
            initial_base_sha="a" * 40,
            admitted_by="op",
            redactor=Redactor(),
        )
    assert current_revision(queue, "widgets") is None
