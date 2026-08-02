"""GitHub as the backlog. Issues are the source of truth for status.

The plan `.md` says what the work *is*; the issue says where it *got to*.
That split is deliberate — you keep editing the plan, agents and humans keep
moving issues, and neither overwrites the other.

Syncing is idempotent, and the mechanism is a marker in the issue body:

    <!-- harness:id=T1 -->

Re-running a sync after editing the plan **updates** the matching issue
rather than creating a second one. Without a marker, matching would have to
be by title, and a plan whose title you improved would silently fork into
two issues.

What sync will never do: reopen an issue you closed, or close one an agent
did not finish. Status belongs to the issue.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .plan import WorkItem

MARKER = "<!-- harness:id={id} -->"
_MARKER_RE = re.compile(r"<!--\s*harness:id=([^\s>]+)\s*-->")


class GitHubError(RuntimeError):
    pass


class Runner(Protocol):
    """Runs one `gh` invocation. Injected so every test here is offline and
    so a caller can substitute an authenticated transport of its own."""

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str: ...


@dataclass
class Issue:
    number: int
    title: str
    body: str
    state: str
    labels: list[str] = field(default_factory=list)
    milestone: str | None = None
    assignees: list[str] = field(default_factory=list)
    url: str = ""

    @property
    def harness_id(self) -> str | None:
        match = _MARKER_RE.search(self.body or "")
        return match.group(1) if match else None


@dataclass
class SyncReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    #: Issues carrying a marker for an item the plan no longer contains.
    #: Never deleted or closed automatically -- an item vanishing from a plan
    #: is usually an edit, sometimes a mistake, and never grounds for the
    #: harness to close work on its own.
    orphaned: list[str] = field(default_factory=list)
    #: Repository metadata the plan asked for and the repo did not have.
    #: Reported because creating labels and milestones changes the
    #: repository, and that should never happen silently.
    labels_created: list[str] = field(default_factory=list)
    milestones_created: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        parts = [
            f"created {len(self.created)}",
            f"updated {len(self.updated)}",
            f"unchanged {len(self.unchanged)}",
        ]
        if self.orphaned:
            parts.append(f"orphaned {len(self.orphaned)} (left alone)")
        return ", ".join(parts)


class GitHub:
    """Thin wrapper over the `gh` CLI.

    `gh` rather than raw HTTP on purpose: it already holds the user's auth,
    handles enterprise hosts and token refresh, and means this module never
    touches a credential. The cost is a subprocess per call, which is
    irrelevant at backlog scale.
    """

    def __init__(self, repo: str, runner: Runner | None = None) -> None:
        self.repo = repo
        self._run: Runner = runner or self._subprocess_run

    @staticmethod
    def _subprocess_run(args: Sequence[str], stdin: str | None = None) -> str:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            args,
            capture_output=True,
            text=True,
            input=stdin,
        )
        if result.returncode != 0:
            raise GitHubError(f"{' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    def list_issues(self, limit: int = 500) -> list[Issue]:
        out = self._run(
            [
                "gh",
                "issue",
                "list",
                "-R",
                self.repo,
                "--state",
                "all",
                "--limit",
                str(limit),
                "--json",
                "number,title,body,state,labels,milestone,assignees,url",
            ]
        )
        issues = []
        for raw in json.loads(out or "[]"):
            issues.append(
                Issue(
                    number=raw["number"],
                    title=raw["title"],
                    body=raw.get("body") or "",
                    state=(raw.get("state") or "").lower(),
                    labels=[label["name"] for label in raw.get("labels") or []],
                    milestone=(raw.get("milestone") or {}).get("title"),
                    assignees=[a["login"] for a in raw.get("assignees") or []],
                    url=raw.get("url", ""),
                )
            )
        return issues

    def list_labels(self) -> set[str]:
        out = self._run(
            ["gh", "label", "list", "-R", self.repo, "--limit", "200", "--json", "name"]
        )
        return {row["name"] for row in json.loads(out or "[]")}

    def create_label(self, name: str, description: str = "") -> None:
        self._run(
            [
                "gh",
                "label",
                "create",
                name,
                "-R",
                self.repo,
                "--description",
                description or f"created by agent-harness for {name}",
                "--force",
            ]
        )

    def list_milestones(self) -> set[str]:
        out = self._run(["gh", "api", f"repos/{self.repo}/milestones", "--jq", "[.[].title]"])
        return set(json.loads(out or "[]"))

    def create_milestone(self, title: str) -> None:
        self._run(
            ["gh", "api", "-X", "POST", f"repos/{self.repo}/milestones", "-f", f"title={title}"]
        )

    def create_issue(self, item: WorkItem) -> str:
        args = [
            "gh",
            "issue",
            "create",
            "-R",
            self.repo,
            "--title",
            item.title,
            "--body",
            body_for(item),
        ]
        if item.labels:
            args += ["--label", ",".join(item.labels)]
        if item.milestone:
            args += ["--milestone", item.milestone]
        return self._run(args).strip()

    def update_issue(self, number: int, item: WorkItem) -> None:
        args = [
            "gh",
            "issue",
            "edit",
            str(number),
            "-R",
            self.repo,
            "--title",
            item.title,
            "--body",
            body_for(item),
        ]
        if item.labels:
            args += ["--add-label", ",".join(item.labels)]
        if item.milestone:
            args += ["--milestone", item.milestone]
        self._run(args)

    def comment(self, number: int, text: str) -> None:
        self._run(
            ["gh", "issue", "comment", str(number), "-R", self.repo, "--body-file", "-"], stdin=text
        )

    def close(self, number: int, comment: str | None = None) -> None:
        args = ["gh", "issue", "close", str(number), "-R", self.repo]
        if comment:
            args += ["--comment", comment]
        self._run(args)


def body_for(item: WorkItem) -> str:
    """The issue body: the brief, plus the marker that makes sync idempotent."""
    parts = [item.body.strip()] if item.body.strip() else []
    if item.depends_on:
        parts.append("**Depends on:** " + ", ".join(item.depends_on))
    parts.append(
        "<sub>Synced from the plan by agent-harness. Edits here will be "
        "overwritten on the next sync — change the plan instead.</sub>"
    )
    parts.append(MARKER.format(id=item.id))
    return "\n\n".join(parts)


def ensure_metadata(
    github: GitHub, items: Iterable[WorkItem], *, dry_run: bool = False
) -> tuple[list[str], list[str]]:
    """Create any labels and milestones the plan uses but the repo lacks.

    `gh issue create --label` fails outright on an unknown label, so without
    this the first sync of any plan dies on its first item. Creating them is
    also simply what a person would do: the plan naming a label *is* the
    request for it to exist.

    Returns what was created, so the caller can say so rather than making
    repository changes silently.
    """
    wanted_labels = {label for item in items for label in item.labels}
    wanted_milestones = {item.milestone for item in items if item.milestone}

    missing_labels = sorted(wanted_labels - github.list_labels()) if wanted_labels else []
    missing_milestones = (
        sorted(wanted_milestones - github.list_milestones()) if wanted_milestones else []
    )
    if not dry_run:
        for name in missing_labels:
            github.create_label(name)
        for title in missing_milestones:
            github.create_milestone(title)
    return missing_labels, missing_milestones


def sync(github: GitHub, items: Iterable[WorkItem], *, dry_run: bool = False) -> SyncReport:
    """Create or update one issue per work item.

    Never closes, reopens or deletes anything. An item that disappears from
    the plan is reported as orphaned and left exactly as it is: the harness
    does not get to decide that work stopped mattering because a document
    changed.
    """
    items = list(items)
    report = SyncReport()
    report.labels_created, report.milestones_created = ensure_metadata(
        github, items, dry_run=dry_run
    )
    existing = {i.harness_id: i for i in github.list_issues() if i.harness_id}

    for item in items:
        issue = existing.get(item.id)
        if issue is None:
            if not dry_run:
                github.create_issue(item)
            report.created.append(item.id)
            continue
        if _matches(issue, item):
            report.unchanged.append(item.id)
            continue
        if not dry_run:
            github.update_issue(issue.number, item)
        report.updated.append(item.id)

    planned = {item.id for item in items}
    report.orphaned = sorted(set(existing) - planned)
    return report


def _matches(issue: Issue, item: WorkItem) -> bool:
    """Whether an issue already says what the plan says.

    Compared on the fields sync actually writes. Labels are a subset check,
    not equality: labels added on GitHub by a human are theirs to keep, and
    a sync that stripped them would make the backlog hostile to use.
    """
    if issue.title.strip() != item.title.strip():
        return False
    if issue.body.strip() != body_for(item).strip():
        return False
    if not set(item.labels) <= set(issue.labels):
        return False
    return not (item.milestone and issue.milestone != item.milestone)
