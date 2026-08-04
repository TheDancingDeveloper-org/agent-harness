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

Dependencies come in two notations, because plans in the wild use both. A
`depends on:` line states them from inside the item; a fenced ```dependencies
block states them as a graph, which is how people write them when there are
enough edges that repeating them per item stops reading well:

    ```dependencies
    W1 -> W2        # the arrow follows the work: W2 waits for W1
    W1 -> W3
    ```

Both notations produce the same tokens, and the token grammar
(`agent_harness.graph.parse_dependency`) is what says whether a target is
local work, an external reference, a human decision or another project's
work. A target the plan does not define is reported, never assumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .graph import (
    CROSS_PROJECT_WORK,
    EXTERNAL_REFERENCE,
    HUMAN_DECISION,
    LOCAL_WORK,
    find_cycles,
    parse_dependency,
)

#: An id looks like T1, D9, E0, ITEM-3, or P0.1 -- letters then digits,
#: optionally hyphenated, optionally with dotted sub-numbering.
#:
#: The dotted form is not cosmetic. Real plans nest work under a phase
#: (`### P0` with `#### P0.1`, `#### P0.2` beneath it), and without it every
#: sub-item collapses onto its parent's id: eleven distinct items became
#: three, and the sync would have refused the plan as full of duplicates.
#: Found by parsing a second, independently-written plan — which is exactly
#: what a second workload is for.
#:
#: Still deliberately narrow: "## 5. Architecture" is NOT an item, and
#: treating it as one would fill the backlog with the document's structure.
ID = r"[A-Z][A-Z0-9]{0,7}-?\d{1,4}(?:\.\d{1,3})*"

_SEP = r"[:.)\s\u2010-\u2015-]+"
_HEADING = re.compile(rf"^(#{{2,6}})\s+({ID}){_SEP}\s*(.+?)\s*$")
_PLAIN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_CHECKBOX = re.compile(rf"^\s*[-*]\s+\[( |x|X)\]\s+(?:({ID}){_SEP}\s*)?(.+?)\s*$")
_TABLE_ROW = re.compile(rf"^\|\s*({ID})\s*\|(.+)$")
_META = re.compile(
    r"^\s*(labels?|milestone|phase|depends[ _-]?on|size|risk)\s*:\s*(.+?)\s*$", re.IGNORECASE
)
_TABLE_SEP = re.compile(r"^\|[\s:|-]+\|$")

#: The info string that turns a fenced block into dependency declarations
#: rather than sample code. Deliberately explicit: a ```mermaid graph is a
#: picture of the plan, and reading one as authoritative would let a diagram
#: nobody maintains gate work.
_DEPENDENCY_FENCE = "dependencies"
#: `TARGET -> SOURCE`, plus `-->` and `→` because all three get typed.
#: A trailing `#` comment is allowed; plans are written by humans.
_ARROW = re.compile(r"^\s*(.+?)\s*(?:-->|->|→)\s*([^#]+?)\s*(?:#.*)?$")


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
class ArrowEdge:
    """One `TARGET -> SOURCE` line from a ```dependencies block."""

    line: int
    target: str
    source: str
    text: str


@dataclass
class DependencyReport:
    """What a plan's dependencies say, case by case.

    Each field is a different problem with a different fix, which is why they
    are not one list. A missing local target is a typo or an omission; an
    external target is legitimate and needs a resolver; a cycle is a plan
    that can never finish. Collapsing them would produce a warning nobody
    can act on.
    """

    #: item id -> local tokens naming nothing in this plan.
    unresolved: dict[str, list[str]] = field(default_factory=dict)
    #: item id -> declared external tokens, with their resolver.
    external: dict[str, list[str]] = field(default_factory=dict)
    #: item id -> declared human-decision tokens.
    decisions: dict[str, list[str]] = field(default_factory=dict)
    #: item id -> declared cross-project tokens.
    cross_project: dict[str, list[str]] = field(default_factory=dict)
    #: item id -> tokens whose grammar could not be read at all.
    malformed: dict[str, list[str]] = field(default_factory=dict)
    #: Loops through required local dependencies.
    cycles: list[tuple[str, ...]] = field(default_factory=list)
    #: (line, text) of arrow lines whose dependent side names no item.
    unattached_arrows: list[tuple[int, str]] = field(default_factory=list)

    def is_clean(self) -> bool:
        return not (self.unresolved or self.malformed or self.cycles or self.unattached_arrows)

    def lines(self) -> list[str]:
        """The report as text, one finding per line, for a CLI."""
        out: list[str] = []
        for item_id, tokens in sorted(self.unresolved.items()):
            out.append(f"{item_id}: unresolved local target(s) {', '.join(tokens)}")
        for item_id, notes in sorted(self.malformed.items()):
            out.extend(f"{item_id}: {note}" for note in notes)
        for item_id, tokens in sorted(self.external.items()):
            out.append(f"{item_id}: external target(s) {', '.join(tokens)} — needs a resolver")
        for item_id, tokens in sorted(self.decisions.items()):
            out.append(f"{item_id}: human decision(s) {', '.join(tokens)}")
        for item_id, tokens in sorted(self.cross_project.items()):
            out.append(f"{item_id}: cross-project target(s) {', '.join(tokens)}")
        for cycle in self.cycles:
            out.append("cycle: " + " -> ".join([*cycle, cycle[0]]))
        for line, text in self.unattached_arrows:
            out.append(f"line {line}: arrow {text!r} names no item in this plan")
        return out


@dataclass
class ParsedPlan:
    items: list[WorkItem] = field(default_factory=list)
    #: Headings that were not recognised as work, with line numbers. Never
    #: empty on a real plan -- most headings are narrative -- but a *large*
    #: number relative to items means the plan does not use a recognised
    #: shape, which is worth knowing before executing it.
    skipped: list[tuple[int, str]] = field(default_factory=list)
    #: Every arrow line read from a ```dependencies block, whether or not it
    #: named an item the plan defines. Kept so the report can quote the line
    #: a reader has to go and fix.
    arrows: list[ArrowEdge] = field(default_factory=list)
    #: Arrow lines whose *dependent* side names nothing in this plan. An
    #: arrow that lands nowhere would otherwise be silently discarded, which
    #: is the one outcome worse than refusing it.
    unattached_arrows: list[ArrowEdge] = field(default_factory=list)

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
        """Local dependencies naming items this plan does not define.

        Only **local** targets are counted. `external:tracker:TICKET-9` names
        something this document was never going to contain, and reporting it
        as a typo would teach a reader to ignore the report. A local id that
        is absent is exactly a typo or an omitted item, and it now blocks the
        work rather than passing silently — so saying which one it is at
        parse time is the difference between a plan you can fix and a queue
        that stops for no stated reason.
        """
        known = self.ids
        out = {}
        for item in self.items:
            missing = [
                token
                for token in item.depends_on
                if (spec := parse_dependency(token)).target_kind == LOCAL_WORK
                and spec.target_id not in known
            ]
            if missing:
                out[item.id] = missing
        return out

    def cycles(self) -> list[tuple[str, ...]]:
        """Groups of items in this plan that require each other.

        Detected at parse time as well as in the queue, because the cheapest
        place to be told a plan can never finish is before it becomes a
        backlog.
        """
        adjacency: dict[str, set[str]] = {}
        known = self.ids
        for item in self.items:
            for token in item.depends_on:
                spec = parse_dependency(token)
                if spec.required and spec.target_kind == LOCAL_WORK and spec.target_id in known:
                    adjacency.setdefault(item.id, set()).add(spec.target_id)
        return find_cycles(adjacency)

    def dependency_report(self) -> DependencyReport:
        """Everything the parser can say about this plan's dependencies.

        One answer rather than five calls, because the five questions are
        always asked together and a reader who forgets the last one gets a
        plan that looks clean and can never finish.
        """
        external: dict[str, list[str]] = {}
        decisions: dict[str, list[str]] = {}
        cross_project: dict[str, list[str]] = {}
        malformed: dict[str, list[str]] = {}
        for item in self.items:
            for token in item.depends_on:
                spec = parse_dependency(token)
                if spec.malformed:
                    malformed.setdefault(item.id, []).append(spec.malformed)
                elif spec.target_kind == EXTERNAL_REFERENCE:
                    external.setdefault(item.id, []).append(token)
                elif spec.target_kind == HUMAN_DECISION:
                    decisions.setdefault(item.id, []).append(token)
                elif spec.target_kind == CROSS_PROJECT_WORK:
                    cross_project.setdefault(item.id, []).append(token)
        return DependencyReport(
            unresolved=self.unresolved_dependencies(),
            external=external,
            decisions=decisions,
            cross_project=cross_project,
            malformed=malformed,
            cycles=self.cycles(),
            unattached_arrows=[(a.line, a.text) for a in self.unattached_arrows],
        )


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

    in_dependency_fence = False

    for number, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            info = line.lstrip()[3:].strip().lower()
            if in_dependency_fence:
                # Closing a dependency block. It is not part of any brief:
                # an agent should read its specification, not the graph.
                in_dependency_fence = False
                in_code = False
                continue
            if not in_code and info == _DEPENDENCY_FENCE:
                in_dependency_fence = True
                in_code = True
                continue
            in_code = not in_code
            if current is not None:
                body.append(line)
            continue
        if in_dependency_fence:
            _read_arrow(plan, number, line)
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
    _apply_arrows(plan)
    return plan


def _read_arrow(plan: ParsedPlan, number: int, line: str) -> None:
    """One line inside a ```dependencies block."""
    text = line.strip()
    if not text or text.startswith("#"):
        return
    match = _ARROW.match(text)
    if not match:
        # Not silently dropped: a line in a dependency block that is not an
        # arrow is a line whose author believed it declared something.
        plan.unattached_arrows.append(ArrowEdge(number, "", "", text))
        return
    target, source = match.group(1).strip(), match.group(2).strip()
    plan.arrows.append(ArrowEdge(number, target, source, text))


def _apply_arrows(plan: ParsedPlan) -> None:
    """Attach arrow edges to the items they name.

    Applied after parsing rather than during it, because a graph block
    routinely sits at the top of a plan and names items defined below it.
    """
    by_id = {item.id: item for item in plan.items}
    for arrow in plan.arrows:
        item = by_id.get(arrow.source)
        if item is None:
            plan.unattached_arrows.append(arrow)
            continue
        if arrow.target not in item.depends_on:
            item.depends_on.append(arrow.target)


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
