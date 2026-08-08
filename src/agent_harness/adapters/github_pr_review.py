"""Review source for one GitHub pull request.

**Opt-in.** Nothing in the core imports this. `review_sources.resolve` knows
the *name* `github-pr-review` and the module path installed metadata gives it,
and imports this file only when a deployment selects it. Core never learns
what a GitHub review comment looks like.

The harness owns correction semantics; this adapter owns two things GitHub
knows and the harness does not:

    identity      a review comment's immutable numeric id, per endpoint
    disposition   whether the comment asks for work, asks a person a
                  question, or reports something already settled

Both are decided here by **explicit, deterministic rules**. No model reads a
human's prose to guess what they meant — that is the failure this contract was
written to avoid. A reviewer who wants a specific outcome says so with a
marker; anything unmarked defaults to `ambiguous`, which opens a hold for a
person rather than sending an agent after a guess.

Markers, case-insensitive, anywhere in the comment body, first match wins::

    harness: fix        actionable — create correction work
    harness: hold       ambiguous  — hold for a person
    harness: resolved   already settled — record it, create nothing

With no marker, the review state decides:

    CHANGES_REQUESTED   actionable
    APPROVED            already_resolved
    anything else       `default_disposition` (itself defaulting to ambiguous)

The item a comment is about is likewise explicit. `harness-item: T3` in the
body names one; otherwise the configured `default_item_id` is used, because a
plan pull request carries every item in the plan and this adapter will not
infer which one a line comment belongs to.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..review_events import RemoteReviewEvent, ReviewDisposition
from ..review_sources import API_VERSION, ReviewBatch

__all__ = ["GitHubPullRequestReviewSource", "source"]

Runner = Callable[[Sequence[str]], str]

#: `harness-item: T3`. Deliberately strict — a body that does not name an item
#: in this exact form falls back to the configured default rather than being
#: pattern-matched against whatever else looks like an identifier.
_ITEM = re.compile(r"harness-item:\s*(?P<item>[A-Za-z0-9._\-]+)", re.IGNORECASE)

_MARKERS: tuple[tuple[re.Pattern[str], ReviewDisposition], ...] = (
    (re.compile(r"harness:\s*fix\b", re.IGNORECASE), "actionable"),
    (re.compile(r"harness:\s*hold\b", re.IGNORECASE), "ambiguous"),
    (re.compile(r"harness:\s*resolved\b", re.IGNORECASE), "already_resolved"),
)

_BY_REVIEW_STATE: dict[str, ReviewDisposition] = {
    "CHANGES_REQUESTED": "actionable",
    "APPROVED": "already_resolved",
}

#: A summary is evidence for a person, not a transcript. Long review prose is
#: truncated here so a single comment cannot dominate a queue row or a hold.
SUMMARY_LIMIT = 2000


class GitHubPullRequestReviewSource:
    """Poll one pull request's reviews and review comments."""

    api_version = API_VERSION

    def __init__(
        self,
        *,
        repo: str,
        pr: int | str,
        project_id: str,
        default_item_id: str,
        name: str = "github-pr-review",
        default_disposition: ReviewDisposition = "ambiguous",
        limit: int = 100,
        runner: Runner | None = None,
    ) -> None:
        if not str(repo).strip() or "/" not in str(repo):
            raise ValueError("github-pr-review needs an OWNER/REPO repo")
        if not str(project_id).strip() or not str(default_item_id).strip():
            raise ValueError("github-pr-review needs a project_id and default_item_id")
        if default_disposition not in ("actionable", "ambiguous", "already_resolved"):
            raise ValueError(f"unknown default_disposition {default_disposition!r}")
        self.name = str(name)
        self.repo = str(repo)
        self.pr = int(pr)
        self.project_id = str(project_id)
        self.default_item_id = str(default_item_id)
        self.default_disposition: ReviewDisposition = default_disposition
        self.limit = max(1, int(limit))
        self._run: Runner = runner or _gh

    # -- polling ---------------------------------------------------------

    def poll(self, cursor: str | None, /) -> ReviewBatch:
        """Return everything updated since `cursor`, and the next cursor.

        The cursor is GitHub's own `updated_at` timestamp, which is a
        recovery aid rather than the deduplication mechanism: overlap is
        expected and harmless because the harness deduplicates on the
        immutable per-endpoint comment identity carried in each event.
        """
        rows: list[tuple[str, dict[str, Any]]] = []
        for endpoint in ("reviews", "comments"):
            for raw in self._fetch(endpoint, cursor):
                rows.append((endpoint, raw))
        events: list[RemoteReviewEvent] = []
        stamps: list[str] = []
        for endpoint, raw in rows:
            stamp = str(raw.get("updated_at") or raw.get("submitted_at") or "")
            if stamp:
                stamps.append(stamp)
            event = self._event(endpoint, raw)
            if event is not None:
                events.append(event)
        next_cursor = max(stamps) if stamps else cursor
        return ReviewBatch(tuple(events), next_cursor=next_cursor or None)

    def _fetch(self, endpoint: str, cursor: str | None) -> list[dict[str, Any]]:
        path = f"repos/{self.repo}/pulls/{self.pr}/{endpoint}?per_page={self.limit}"
        if cursor and endpoint == "comments":
            # Only the review-comments endpoint supports `since`. Reviews are
            # filtered client-side below, so a missing filter costs bandwidth
            # rather than correctness.
            path = f"{path}&since={cursor}"
        out = self._run(["gh", "api", "--paginate", path])
        try:
            raw = json.loads(out or "[]")
        except ValueError as exc:
            raise RuntimeError(
                f"gh returned unreadable {endpoint} for {self.repo}#{self.pr}: {exc}"
            ) from exc
        if not isinstance(raw, list):
            raise RuntimeError(f"gh returned no {endpoint} list for {self.repo}#{self.pr}")
        return [row for row in raw if isinstance(row, dict)]

    def _event(self, endpoint: str, raw: Mapping[str, Any]) -> RemoteReviewEvent | None:
        identity = raw.get("id")
        if identity is None:
            return None
        body = str(raw.get("body") or "").strip()
        state = str(raw.get("state") or "").upper()
        if state == "PENDING":
            # An unsubmitted draft review is not feedback yet. Acting on one
            # would answer a reviewer before they had finished writing.
            return None
        disposition = self._disposition(body, state)
        summary = self._summary(body, state, raw)
        if not summary:
            return None
        return RemoteReviewEvent(
            source=self.name,
            # Reviews and review comments number independently, so the
            # endpoint is part of the identity rather than the id alone.
            remote_id=f"{self.repo}#{self.pr}/{endpoint}/{identity}",
            project_id=self.project_id,
            item_id=self._item_id(body),
            disposition=disposition,
            summary=summary,
            pr_url=str(raw.get("html_url") or "") or None,
        )

    # -- explicit rules --------------------------------------------------

    def _disposition(self, body: str, state: str) -> ReviewDisposition:
        for pattern, disposition in _MARKERS:
            if pattern.search(body):
                return disposition
        return _BY_REVIEW_STATE.get(state, self.default_disposition)

    def _item_id(self, body: str) -> str:
        match = _ITEM.search(body)
        return match.group("item") if match else self.default_item_id

    @staticmethod
    def _summary(body: str, state: str, raw: Mapping[str, Any]) -> str:
        where = str(raw.get("path") or "").strip()
        prefix = f"{state or 'COMMENT'}"
        if where:
            line = raw.get("line") or raw.get("original_line")
            prefix = f"{prefix} on {where}{f':{line}' if line else ''}"
        text = body or ("approved with no comment" if state == "APPROVED" else "")
        if not text:
            return ""
        return f"[{prefix}] {text}"[:SUMMARY_LIMIT]


def source(config: Mapping[str, Any]) -> GitHubPullRequestReviewSource:
    """The factory `review_sources.resolve` looks for by name."""
    known = {
        "repo",
        "pr",
        "project_id",
        "default_item_id",
        "name",
        "default_disposition",
        "limit",
    }
    unknown = sorted(set(config) - known)
    if unknown:
        raise ValueError(f"github-pr-review does not accept {', '.join(unknown)}")
    missing = sorted({"repo", "pr", "project_id", "default_item_id"} - set(config))
    if missing:
        raise ValueError(f"github-pr-review needs {', '.join(missing)}")
    return GitHubPullRequestReviewSource(**config)


def _gh(args: Sequence[str]) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        args, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh failed")
    return result.stdout
