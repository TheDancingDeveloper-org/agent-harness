"""Ground truth, fetched from GitHub.

Everything the harness records about quality is a proxy. A reviewer approved
it; the checks passed; a pull request was opened. None of that says the change
was any good — only what happened to it afterwards does, and that happens
outside the harness entirely.

Two facts matter and neither is observable from in here:

**Was it merged, or closed unmerged?** A rejected pull request is the clearest
possible statement that the work was not wanted, and from inside the harness
it looks identical to one still waiting.

**Was it reverted?** The only honest quality metric in the whole system.
Approval rate measures whether a reviewer agreed; revert rate measures whether
it should have. They come apart exactly when it matters, and a harness that
tracks only the first will report improving quality right up until someone
looks at the repository.

Reconciliation is **append-only and idempotent**, like everything else in the
audit layer. It emits events; it never edits the ones already written. A pull
request that is merged today and reverted next week produces two facts, in
order, both true when recorded — not one fact that changes its mind.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .audit import AuditStore
from .events import WORK, Event

log = logging.getLogger(__name__)

#: Outcomes this writes. Distinct from the harness's own `work` outcomes so a
#: query can separate "what we did" from "what the world did with it".
PR_MERGED = "pr_merged"
PR_CLOSED_UNMERGED = "pr_closed_unmerged"
PR_REVERTED = "pr_reverted"

#: How git and GitHub spell a revert. Both forms appear: the CLI writes the
#: first, the web UI's "Revert" button writes the second.
_REVERT_SUBJECT = re.compile(r'^Revert\s+"(?P<subject>.+)"|^Revert\s+(?P<pr>#\d+)', re.IGNORECASE)
_REVERTS_COMMIT = re.compile(r"This reverts commit (?P<sha>[0-9a-f]{7,40})", re.IGNORECASE)


class Runner(Protocol):
    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str: ...


@dataclass
class ReconcileReport:
    merged: int = 0
    closed_unmerged: int = 0
    reverted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        parts = [
            f"merged {self.merged}",
            f"closed {self.closed_unmerged}",
            f"reverted {self.reverted}",
        ]
        if self.errors:
            parts.append(f"errors {len(self.errors)}")
        return ", ".join(parts)


@dataclass
class PullRequest:
    number: int
    state: str
    merged: bool
    merge_commit: str | None
    title: str
    url: str
    merged_at: float | None = None
    closed_at: float | None = None


def _as_float(value: str | None) -> float | None:
    """A git `%ct` seconds-since-epoch string, or None."""
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        # GitHub returns RFC3339 with a trailing Z.
        return time.mktime(time.strptime(value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z"))
    except (ValueError, OverflowError):
        return None


class GitHubReconciler:
    """Pulls PR outcomes and revert facts, and records them as audit events.

    The runner is injected for the same reason the model transport is: this
    should be testable without a network and without a real repository.
    """

    def __init__(self, repo: str, audit: AuditStore, runner: Runner | None = None) -> None:
        self.repo = repo
        self.audit = audit
        self._run = runner or _subprocess_runner

    # ------------------------------------------------------------- fetching

    def pull_requests(self, limit: int = 200) -> list[PullRequest]:
        out = self._run(
            [
                "gh",
                "pr",
                "list",
                "-R",
                self.repo,
                "--state",
                "all",
                "--limit",
                str(limit),
                "--json",
                "number,state,mergedAt,closedAt,mergeCommit,title,url",
            ]
        )
        prs = []
        for raw in json.loads(out or "[]"):
            merge_commit = (raw.get("mergeCommit") or {}).get("oid")
            prs.append(
                PullRequest(
                    number=raw["number"],
                    state=(raw.get("state") or "").lower(),
                    merged=bool(raw.get("mergedAt")),
                    merge_commit=merge_commit,
                    title=raw.get("title") or "",
                    url=raw.get("url") or "",
                    merged_at=_parse_ts(raw.get("mergedAt")),
                    closed_at=_parse_ts(raw.get("closedAt")),
                )
            )
        return prs

    def revert_commits(self, limit: int = 500) -> list[dict[str, str]]:
        """Recent commits that look like reverts, with what they reverted."""
        out = self._run(
            ["git", "-C", ".", "log", f"-{limit}", "--format=%H%x00%ct%x00%s%x00%b%x1e"]
        )
        reverts = []
        for record in (out or "").split("\x1e"):
            if not record.strip():
                continue
            parts = record.strip().split("\x00")
            if len(parts) < 3:
                continue
            sha, committed_at, subject = parts[0], parts[1], parts[2]
            body = parts[3] if len(parts) > 3 else ""
            if not _REVERT_SUBJECT.match(subject):
                continue
            reverted = _REVERTS_COMMIT.search(body)
            reverts.append(
                {
                    "sha": sha,
                    "committed_at": committed_at,
                    "subject": subject,
                    "reverts_commit": reverted.group("sha") if reverted else "",
                }
            )
        return reverts

    # --------------------------------------------------------- reconciling

    def reconcile(self, items_by_pr: dict[int, dict[str, str]] | None = None) -> ReconcileReport:
        """Record what happened to each pull request.

        `items_by_pr` maps a PR number to `{"project_id": ..., "item_id": ...}`
        so an outcome can be attributed to the work that caused it. A PR the
        harness does not recognise is skipped rather than recorded against
        nothing -- an unattributed outcome inflates every rate it appears in
        while belonging to no item.
        """
        report = ReconcileReport()
        known = items_by_pr or {}
        try:
            prs = self.pull_requests()
        except Exception as exc:  # noqa: BLE001 - reconciliation must not stop the fleet
            report.errors.append(f"pull_requests: {exc}")
            log.warning("reconcile: could not list pull requests: %s", exc)
            return report

        try:
            reverts = self.revert_commits()
        except Exception as exc:  # noqa: BLE001
            # A missing git checkout is normal for a remote-only deployment.
            # Merge state is still worth recording without revert detection.
            reverts = []
            log.info("reconcile: revert detection unavailable (%s)", exc)

        by_commit = {r["reverts_commit"]: r for r in reverts if r["reverts_commit"]}
        by_title: dict[str, dict[str, str]] = {}
        for r in reverts:
            match = _REVERT_SUBJECT.match(r["subject"])
            if match and match.group("subject"):
                by_title[match.group("subject")] = r

        events: list[Event] = []
        now = time.time()
        for pr in prs:
            attribution = known.get(pr.number)
            if attribution is None:
                report.skipped += 1
                continue
            base = {
                "run_id": f"reconcile:{self.repo}",
                "project_id": attribution.get("project_id"),
                "item_id": attribution.get("item_id"),
                "pr": pr.number,
                "pr_url": pr.url,
            }
            if pr.merged:
                events.append(
                    Event(
                        ts=pr.merged_at or now,
                        kind=WORK,
                        source="reconcile",
                        outcome=PR_MERGED,
                        data={**base, "seq": pr.number, "merge_commit": pr.merge_commit},
                    )
                )
                report.merged += 1
                revert = (by_commit.get(pr.merge_commit or "")) or by_title.get(pr.title)
                if revert is not None:
                    # The revert commit's own time, never `now`. A wall-clock
                    # stamp changes the event's identity on every pass, so the
                    # same revert is recorded again each time reconciliation
                    # runs -- which is both wrong and unbounded.
                    events.append(
                        Event(
                            ts=_as_float(revert.get("committed_at")) or now,
                            kind=WORK,
                            source="reconcile",
                            outcome=PR_REVERTED,
                            # A distinct seq: merged and reverted are two
                            # facts about the same PR, both true when
                            # recorded, and neither replaces the other.
                            data={
                                **base,
                                "seq": -pr.number,
                                "revert_commit": revert.get("sha"),
                            },
                        )
                    )
                    report.reverted += 1
            elif pr.state == "closed":
                events.append(
                    Event(
                        ts=pr.closed_at or now,
                        kind=WORK,
                        source="reconcile",
                        outcome=PR_CLOSED_UNMERGED,
                        data={**base, "seq": pr.number},
                    )
                )
                report.closed_unmerged += 1

        if events:
            self.audit.append(events)
        if report.merged or report.reverted or report.closed_unmerged:
            log.info("reconcile %s: %s", self.repo, report)
        return report


def _subprocess_runner(args: Sequence[str], stdin: str | None = None) -> str:
    import subprocess

    result = subprocess.run(  # noqa: S603
        list(args), input=stdin, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def items_by_pr(queue: Any) -> dict[int, dict[str, str]]:
    """Map PR numbers to the item that produced them, from the queue.

    The queue records `pr_url` per item, which is the only link between a
    harness item and a GitHub outcome.
    """
    mapping: dict[int, dict[str, str]] = {}
    for record in queue.items():
        if not record.pr_url:
            continue
        match = re.search(r"/pull/(\d+)", record.pr_url)
        if match:
            mapping[int(match.group(1))] = {
                "project_id": record.project_id,
                "item_id": record.item_id,
            }
    return mapping
