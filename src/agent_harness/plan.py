"""Turn a plan written as markdown into work items.

The workflow this serves: you write a plan `.md` the way you already do, and
the harness turns it into a backlog it can execute. The plan stays the thing
you edit; the items are derived from it.

Parsing prose into work is inherently lossy, so this module is built around
one rule: **never silently drop a heading.** Anything it does not recognise
as work is reported as skipped, with its heading, so you can see what it
ignored and either rewrite that section or accept it. A parser that quietly
finds three items in a fifty-item plan is worse than one that finds none,
because the first looks like it worked.

Three shapes are recognised, all of which occur naturally in plans people
actually write:

    ### T1: Create the repository          <- id + title heading
    - [ ] T2 Create labels                 <- checkbox, optional leading id
    | T3 | Create milestones | area:ci |   <- table row with an id column

An item's body is the prose beneath it, which is what an agent will be given
as its brief. Everything else — labels, milestone, dependencies — is optional
metadata parsed from `key: value` lines within that prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: An id looks like T1, D9, E0, ITEM-3 -- letters then digits, optionally
#: hyphenated. Deliberately narrow: a heading like "## 5. Architecture" is
#: NOT an item, and treating it as one would fill the backlog with sections.
ID = r"[A-Z][A-Z0-9]{0,7}-?\d{1,4}"

_SEP = r"[:.)\s\u2010-\u2015-]+"
_HEADING = re.compile(rf"^(#{{2,6}})\s+({ID}){_SEP}\s*(.+?)\s*$")
_PLAIN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_CHECKBOX = re.compile(rf"^\s*[-*]\s+\[( |x|X)\]\s+(?:({ID}){_SEP}\s*)?(.+?)\s*$")
_TABLE_ROW = re.compile(rf"^\|\s*({ID})\s*\|(.+)$")
_META = re.compile(
    r"^\s*(labels?|milestone|phase|depends[ _-]?on|size|risk)\s*:\s*(.+?)\s*$", re.IGNORECASE
)
_TABLE_SEP = re.compile(r"^\|[\s:|-]+\|$")


@dataclass
class WorkItem:
    """One unit of work, derived from a plan."""

    id: str
    title: str
    body: str = ""
    labels: list[str] = field(default_factory=list)
    milestone: str | None = None
    depends_on: list[str] = field(default_factory=list)
    done: bool = False
    #: Line number in the source plan, so a reader can find it again.
    line: int = 0

    def brief(self) -> str:
        """What an agent is told to do. The title alone is rarely enough;
        the prose under it is the actual specification."""
        return f"{self.title}\n\n{self.body}".strip()


@dataclass
class ParsedPlan:
    items: list[WorkItem] = field(default_factory=list)
    #: Headings that were not recognised as work, with line numbers. Never
    #: empty on a real plan -- most headings are narrative -- but a *large*
    #: number relative to items means the plan does not use a recognised
    #: shape, which is worth knowing before executing it.
    skipped: list[tuple[int, str]] = field(default_factory=list)

    @property
    def ids(self) -> set[str]:
        return {item.id for item in self.items}

    def duplicate_ids(self) -> dict[str, list[int]]:
        """Ids that appear more than once, with the lines they appear on.

        Real plans do this constantly — a phase gets a heading in one
        section and a row in a summary table in another. Both are the same
        work. Syncing them blind would create two issues for one item, so
        the caller must decide: merge, rename, or exclude.
        """
        seen: dict[str, list[int]] = {}
        for item in self.items:
            seen.setdefault(item.id, []).append(item.line)
        return {k: v for k, v in seen.items() if len(v) > 1}

    def deduplicated(self) -> list[WorkItem]:
        """One item per id, keeping the richest description of each.

        'Richest' is the longest body: when a plan states an item twice, the
        version with more prose is the specification and the other is a
        summary row. Picking the longer one is a heuristic, which is why
        `duplicate_ids` exists to show what was collapsed.
        """
        best: dict[str, WorkItem] = {}
        for item in self.items:
            current = best.get(item.id)
            if current is None or len(item.body) > len(current.body):
                if current is not None:
                    # Preserve metadata the shorter version carried.
                    item.labels = sorted(set(item.labels) | set(current.labels))
                    item.milestone = item.milestone or current.milestone
                    item.depends_on = sorted(set(item.depends_on) | set(current.depends_on))
                best[item.id] = item
            else:
                current.labels = sorted(set(current.labels) | set(item.labels))
                current.milestone = current.milestone or item.milestone
                current.depends_on = sorted(set(current.depends_on) | set(item.depends_on))
        return [best[i] for i in dict.fromkeys(item.id for item in self.items)]

    def unresolved_dependencies(self) -> dict[str, list[str]]:
        """Dependencies naming items that do not exist. A typo'd dependency
        would otherwise block an item forever, silently."""
        known = self.ids
        out = {}
        for item in self.items:
            missing = [d for d in item.depends_on if d not in known]
            if missing:
                out[item.id] = missing
        return out


def parse_plan(text: str) -> ParsedPlan:
    """Extract work items from a markdown plan."""
    lines = text.splitlines()
    plan = ParsedPlan()
    current: WorkItem | None = None
    body: list[str] = []
    in_code = False

    def flush() -> None:
        nonlocal current, body
        if current is not None:
            current.body = "\n".join(body).strip()
            _apply_metadata(current)
            plan.items.append(current)
        current, body = None, []

    for number, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            if current is not None:
                body.append(line)
            continue
        if in_code:
            if current is not None:
                body.append(line)
            continue

        heading = _HEADING.match(line)
        if heading:
            flush()
            current = WorkItem(id=heading.group(2), title=heading.group(3), line=number)
            continue

        plain = _PLAIN_HEADING.match(line)
        if plain:
            # A heading that is not an item ends the previous item's body --
            # otherwise an item would absorb the entire rest of the document.
            flush()
            plan.skipped.append((number, plain.group(2)))
            continue

        checkbox = _CHECKBOX.match(line)
        if checkbox:
            checked, item_id, title = checkbox.groups()
            if item_id:
                flush()
                current = WorkItem(
                    id=item_id, title=title, line=number, done=checked.lower() == "x"
                )
                continue
            # An unidentified checkbox is an acceptance criterion or a
            # sub-task of the item it sits under, not an item of its own.
            if current is not None:
                body.append(line)
            continue

        row = _TABLE_ROW.match(line)
        if row and not _TABLE_SEP.match(line):
            flush()
            cells = [c.strip() for c in row.group(2).split("|")]
            cells = [c for c in cells if c]
            title = cells[0] if cells else row.group(1)
            item = WorkItem(id=row.group(1), title=title, line=number)
            item.labels = [c for c in cells[1:] if ":" in c and " " not in c]
            plan.items.append(item)
            continue

        if current is not None:
            body.append(line)

    flush()
    return plan


def _apply_metadata(item: WorkItem) -> None:
    """Pull `labels:` / `milestone:` / `depends on:` lines out of the body.

    They are removed from the body once parsed: an agent reading its brief
    should see the specification, not the bookkeeping.
    """
    kept: list[str] = []
    for line in item.body.splitlines():
        match = _META.match(line)
        if not match:
            kept.append(line)
            continue
        key = match.group(1).lower().replace("-", "_").replace(" ", "_")
        value = match.group(2).strip()
        if key.startswith("label"):
            item.labels.extend(_split(value))
        elif key in ("milestone", "phase"):
            item.milestone = value
        elif key.startswith("depends"):
            item.depends_on.extend(_split(value))
        elif key in ("size", "risk"):
            item.labels.append(f"{key}:{value.lower()}")
    item.body = "\n".join(kept).strip()


def _split(value: str) -> list[str]:
    return [part.strip().strip("`") for part in re.split(r"[,;]", value) if part.strip()]


def parse_plan_file(path: Path | str) -> ParsedPlan:
    return parse_plan(Path(path).read_text())
