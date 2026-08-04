"""The backlog manifest seeds the GitHub issues, so a malformed entry is a broken P0.

These checks mirror what ``gh issue create`` will reject: unknown labels, unknown
milestones, missing fields.

The file is now **historical** — `docs/backlog-seed-2026-08-02.json`, the manifest
that seeded the issues on that date. GitHub is the tracker (D1) and the seed carries
no state field, so it cannot report one. These tests still run because the file is
still the record of what was created, and a record that no longer parses is not a
record. They are not, and must not become, a check on what is currently open.

`MILESTONES` below is the superseded `HARNESS-PLAN.md` phase order. It is kept
because the items in this file were filed under it, not because anything new
should be.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKLOG = REPO_ROOT / "docs" / "backlog-seed-2026-08-02.json"

LABELS = {
    "area:model-client",
    "area:dispatch",
    "area:gui",
    "area:store",
    "area:github",
    "area:ci",
    "area:docs",
    "type:epic",
    "type:task",
    "type:decision",
    "type:spike",
    "risk:high",
    "blocked",
}
MILESTONES = {"P0", "P1", "P2", "P3", "P4"}
TYPE_LABELS = {"type:epic", "type:task", "type:decision", "type:spike"}


def load() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = json.loads(BACKLOG.read_text())
    return items


def test_manifest_is_a_non_empty_list() -> None:
    items = load()
    assert isinstance(items, list)
    assert items


def test_every_item_has_the_required_fields() -> None:
    for item in load():
        for field in ("id", "title", "body", "labels", "milestone"):
            assert item.get(field), f"{item.get('id', '?')} is missing {field}"


def test_ids_are_unique() -> None:
    ids = [item["id"] for item in load()]
    assert len(ids) == len(set(ids))


def test_labels_and_milestones_exist() -> None:
    for item in load():
        assert set(item["labels"]) <= LABELS, f"{item['id']} uses an unknown label"
        assert item["milestone"] in MILESTONES, f"{item['id']} uses an unknown milestone"


def test_every_item_carries_exactly_one_type_label() -> None:
    for item in load():
        types = TYPE_LABELS & set(item["labels"])
        assert len(types) == 1, f"{item['id']} has {types or 'no'} type label"


def test_no_phase_labels() -> None:
    """§7 P0: milestones carry phase. Two encodings of one fact drift."""
    for item in load():
        assert not any(label.startswith("phase:") for label in item["labels"])
