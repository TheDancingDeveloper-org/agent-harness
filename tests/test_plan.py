"""Plan-parsing tests.

The property that matters most is that nothing is silently dropped: a
parser that finds three items in a fifty-item plan looks like it worked.
"""

from __future__ import annotations

from agent_harness.plan import parse_plan

PLAN = """\
# Project plan

Some narrative that is not work.

## 1. Background

More narrative.

### T1: Create the repository

Create the repo, private, actions enabled.

labels: area:ci, type:task
milestone: P0

**Acceptance:** `gh repo view` succeeds.

### T2: Create labels

Create the labels the backlog uses.

depends on: T1
risk: high

## Tasks

- [x] T3 Create milestones
- [ ] T4 Write the README
- [ ] an unidentified sub-task

## Table form

| ID | Title | Area |
|----|-------|------|
| T5 | Branch protection | area:ci |
| T6 | Projects board | area:ci |
"""


def test_finds_every_shape_of_item() -> None:
    plan = parse_plan(PLAN)
    assert [i.id for i in plan.items] == ["T1", "T2", "T3", "T4", "T5", "T6"]


def test_narrative_headings_are_reported_as_skipped_not_dropped() -> None:
    plan = parse_plan(PLAN)
    skipped = [title for _, title in plan.skipped]
    assert "1. Background" in skipped
    assert "Tasks" in skipped
    # ...and they did NOT become items.
    assert "Background" not in [i.title for i in plan.items]


def test_a_numbered_section_heading_is_not_mistaken_for_an_item() -> None:
    """'## 5. Architecture' is a section. Treating it as work would fill the
    backlog with the document's own structure."""
    plan = parse_plan("## 5. Architecture\n\nprose\n")
    assert plan.items == []
    assert plan.skipped


def test_the_body_becomes_the_brief() -> None:
    plan = parse_plan(PLAN)
    t1 = plan.items[0]
    assert "Create the repo, private" in t1.body
    assert "Acceptance" in t1.body
    assert t1.brief().startswith("T1" if False else "Create the repository")


def test_metadata_is_parsed_and_removed_from_the_brief() -> None:
    """An agent reading its brief should see the specification, not the
    bookkeeping."""
    t1 = parse_plan(PLAN).items[0]
    assert t1.labels == ["area:ci", "type:task"]
    assert t1.milestone == "P0"
    assert "labels:" not in t1.body
    assert "milestone:" not in t1.body


def test_dependencies_and_risk_are_parsed() -> None:
    t2 = parse_plan(PLAN).items[1]
    assert t2.depends_on == ["T1"]
    assert "risk:high" in t2.labels


def test_a_checked_box_is_recorded_as_done() -> None:
    items = {i.id: i for i in parse_plan(PLAN).items}
    assert items["T3"].done is True
    assert items["T4"].done is False


def test_an_unidentified_checkbox_stays_with_its_item() -> None:
    """It is an acceptance criterion or sub-task, not work of its own."""
    ids = [i.id for i in parse_plan(PLAN).items]
    assert len(ids) == len(set(ids))
    assert "an unidentified sub-task" not in [i.title for i in parse_plan(PLAN).items]


def test_table_rows_pick_up_label_like_cells() -> None:
    items = {i.id: i for i in parse_plan(PLAN).items}
    assert items["T5"].title == "Branch protection"
    assert "area:ci" in items["T5"].labels


def test_a_table_separator_row_is_not_an_item() -> None:
    assert parse_plan("| ID | T |\n|----|---|\n| T1 | x |\n").ids == {"T1"}


def test_an_item_does_not_absorb_the_rest_of_the_document() -> None:
    plan = parse_plan("### T1: One\n\nbody one\n\n## Narrative\n\nlots of prose\n")
    assert plan.items[0].body == "body one"


def test_code_blocks_are_kept_verbatim_in_the_brief() -> None:
    """A plan's code block is usually the most precise part of the spec;
    losing it would strip exactly the detail an agent needs."""
    plan = parse_plan("### T1: One\n\n```bash\n# not a heading\nrun --this\n```\n")
    assert "run --this" in plan.items[0].body
    assert "# not a heading" in plan.items[0].body
    assert not plan.skipped


def test_unresolved_dependencies_are_reported() -> None:
    """A typo'd dependency would otherwise block an item forever, silently."""
    plan = parse_plan("### T1: One\n\ndepends on: T99\n")
    assert plan.unresolved_dependencies() == {"T1": ["T99"]}


def test_an_empty_plan_yields_nothing_rather_than_erroring() -> None:
    plan = parse_plan("")
    assert plan.items == []
    assert plan.skipped == []


def test_ids_are_exposed_for_dependency_checking() -> None:
    assert parse_plan(PLAN).ids == {"T1", "T2", "T3", "T4", "T5", "T6"}


DUPLICATED = """\
### T1: Build the thing

The full specification, with plenty of detail about what to build.

## Summary

| ID | Title |
|----|-------|
| T1 | Build the thing |
"""


def test_duplicate_ids_are_reported_with_their_lines() -> None:
    """Real plans state an item twice — a heading in one section, a summary
    row in another. Syncing blind would create two issues for one item."""
    plan = parse_plan(DUPLICATED)
    dupes = plan.duplicate_ids()
    assert list(dupes) == ["T1"]
    assert len(dupes["T1"]) == 2


def test_deduplication_keeps_the_richest_description() -> None:
    items = parse_plan(DUPLICATED).deduplicated()
    assert len(items) == 1
    assert "full specification" in items[0].body


def test_deduplication_unions_metadata_from_both_statements() -> None:
    text = (
        "### T1: Thing\n\nlong body here with detail\n\nlabels: area:a\n\n"
        "## Summary\n\n| T1 | Thing | area:b |\n"
    )
    item = parse_plan(text).deduplicated()[0]
    assert set(item.labels) == {"area:a", "area:b"}


def test_a_plan_with_no_duplicates_reports_none() -> None:
    assert parse_plan(PLAN).duplicate_ids() == {}


def test_dotted_sub_items_are_distinct_from_their_parent() -> None:
    """A phase and the items nested under it are separate work.

    Plans in the wild nest work under a phase heading. Without dotted-id
    support every sub-item collapses onto its parent's id, so five real
    items become one -- and the sync then refuses the plan for stating
    the same id five times. Both symptoms, one cause.
    """
    plan = parse_plan(
        """
### P0 — Repository and guardrails

Set the repository up.

#### P0.1 — Repository

Configure metadata.

#### P0.2 — Documentation set

Write the docs.

#### P0.10 — Tenth item

Two digits after the dot must not truncate to one.
"""
    )
    assert [i.id for i in plan.items] == ["P0", "P0.1", "P0.2", "P0.10"]
    assert plan.duplicate_ids() == {}
    assert plan.items[1].title == "Repository"


def test_a_dotted_dependency_resolves_to_the_sub_item() -> None:
    """`depends on: P0.1` must not be read as a dependency on `P0`."""
    plan = parse_plan(
        """
#### P0.1 — Repository

Configure metadata.

#### P0.2 — Documentation set

depends on: P0.1
"""
    )
    assert plan.items[1].depends_on == ["P0.1"]
    assert plan.unresolved_dependencies() == {}
