"""Publish one plan branch as exactly one pull request, and keep it that way.

The coordinator in :mod:`plan_integration` owns the local integration branch
and knows nothing about a remote. This module owns the single remote step the
product allows: push that branch and put **one** pull request in front of a
person. It is deliberately separate so local promotion never depends on a
remote being reachable, and so a deployment that never publishes never runs
this code.

Three rules make it safe to call repeatedly, which is what "corrections resume
automatically" requires:

1. **One pull request per plan.** A recorded URL is adopted before anything is
   created, and `find_open_pr` is asked before that. There is never a second
   pull request for a plan, and never a per-item one.
2. **Republishing an unchanged head does nothing.** A correction that produced
   no new plan head must not push, must not comment and must not reopen a
   decision a person has already been given.
3. **The harness never merges.** Publication is the hand-off, not the
   decision. Nothing here approves, marks ready or merges.

The push uses `--force-with-lease` against the sha this harness last
published, because a plan branch legitimately rewinds: a moved target branch
rebuilds it from the new target and replays every promotion. The lease is what
turns "the branch was rebuilt" into a safe update and "somebody else pushed to
it" into a refusal.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .plan_integration import PlanState
from .work import BLOCKED, CLAIMED, EXHAUSTED, FAILED, HELD, PENDING, WorkQueue

SETTING_PREFIX = "plan-publication:"

#: Work that could still change the tree. Publishing over it would put a plan
#: in front of a person while the harness was still writing it.
IN_FLIGHT_STATES = (PENDING, CLAIMED, HELD)

#: Work that stopped without delivering. This is an exception for a person
#: under P10, and it withholds publication rather than quietly shipping a
#: partial plan whose gaps only the queue knows about.
UNRESOLVED_STATES = (FAILED, EXHAUSTED, BLOCKED)


class PublicationError(RuntimeError):
    """The plan could not be published, and no remote state was assumed."""


class PullRequests(Protocol):
    """The subset of the GitHub client publication uses."""

    def create_pr(
        self, *, title: str, body: str, head: str, base: str, draft: bool = False
    ) -> str: ...

    def find_open_pr(self, head: str) -> str | None: ...

    def comment_pr(self, pr: str, body: str) -> None: ...


@dataclass(frozen=True)
class Publication:
    """What one publish call did, and the durable record it left."""

    status: str  # "created" | "updated" | "unchanged"
    pr_url: str
    head_sha: str
    published_sha: str | None
    detail: str = ""


@dataclass(frozen=True)
class PlanReadiness:
    """Whether the whole plan is locally acceptable, and why not if it is not."""

    ready: bool
    detail: str
    in_flight: int = 0
    unresolved: int = 0


@dataclass(frozen=True)
class PublicationRecord:
    """The durable answer to "have we already published this plan, and where"."""

    pr_url: str | None = None
    head_sha: str | None = None
    branch: str | None = None
    target_branch: str | None = None


class PlanPublisher:
    """Push one plan branch and maintain its single pull request."""

    def __init__(
        self,
        queue: WorkQueue,
        project_id: str,
        repo: Path,
        github: PullRequests,
        *,
        remote: str = "origin",
        on_event: Callable[[dict[str, Any]], None] | None = None,
        runner: Callable[[list[str]], str] | None = None,
    ) -> None:
        if not str(project_id).strip():
            raise ValueError("publication needs a project id")
        if not str(remote).strip():
            raise ValueError("publication needs an explicit remote name")
        self.queue = queue
        self.project_id = project_id
        self.repo = Path(repo).resolve()
        self.github = github
        self.remote = remote
        self.on_event = on_event
        self._run = runner or self._subprocess_run
        self.setting_key = f"{SETTING_PREFIX}{project_id}"

    # -- durable record --------------------------------------------------

    @property
    def record(self) -> PublicationRecord:
        raw = self.queue.get_setting(self.setting_key)
        if not raw:
            return PublicationRecord()
        try:
            data = json.loads(str(raw))
        except ValueError:
            # An unreadable record is not permission to open a second pull
            # request, so it is reported rather than silently discarded.
            raise PublicationError(
                f"publication record for {self.project_id!r} is unreadable; "
                "repair or clear it before publishing again"
            ) from None
        if not isinstance(data, dict):
            raise PublicationError(f"publication record for {self.project_id!r} is not an object")
        return PublicationRecord(
            pr_url=data.get("pr_url"),
            head_sha=data.get("head_sha"),
            branch=data.get("branch"),
            target_branch=data.get("target_branch"),
        )

    def _remember(self, state: PlanState, pr_url: str) -> None:
        self.queue.set_setting(
            self.setting_key,
            json.dumps(
                {
                    "pr_url": pr_url,
                    "head_sha": state.head_sha,
                    "branch": state.branch,
                    "target_branch": state.target_branch,
                }
            ),
        )

    # -- when a plan is ready to be seen ---------------------------------

    def readiness(self, *, excluding: str | None = None) -> PlanReadiness:
        """Is the whole plan locally acceptable?

        Publication is a hand-off to a person, so it waits for the plan to
        stop moving. Anything still claimable, claimed or held could change
        the tree; anything failed, exhausted or blocked did not deliver, and
        shipping around it would present a partial plan as a finished one.
        Both are reported by name so an operator is never left guessing which
        item is holding publication up.

        `excluding` names the item whose promotion is asking. It is still
        claimed at that moment — the queue marks it done afterwards — but its
        work is already in the plan branch, so counting it as in flight would
        mean the last item of a plan could never trigger publication.
        """
        counts = self._counts(excluding)
        in_flight = sum(counts.get(state, 0) for state in IN_FLIGHT_STATES)
        unresolved = sum(counts.get(state, 0) for state in UNRESOLVED_STATES)
        if in_flight:
            detail = f"{in_flight} item(s) still in flight"
        elif unresolved:
            detail = f"{unresolved} item(s) did not deliver and need a person"
        elif not counts:
            detail = "the plan has no items"
        else:
            detail = "every item is done"
        return PlanReadiness(
            ready=not in_flight and not unresolved and bool(counts),
            detail=detail,
            in_flight=in_flight,
            unresolved=unresolved,
        )

    def _counts(self, excluding: str | None) -> dict[str, int]:
        counts = dict(self.queue.counts(self.project_id))
        if excluding is None:
            return counts
        record = self.queue.get(excluding, project_id=self.project_id)
        if record is not None and counts.get(record.state):
            counts[record.state] -= 1
        return counts

    def item_evidence(self) -> str:
        """The promoted items, in promotion order, for the pull-request body.

        P8 requires item commits, dependencies and gate results to stay
        visible in the one pull request. This is the item half of that: the
        queue is the source, so the body cannot claim a promotion the durable
        record does not have.
        """
        rows = self.queue.successful_promotions(self.project_id)
        if not rows:
            return "_No promoted items._"
        lines = ["| item | item commit | plan head |", "|---|---|---|"]
        for row in rows:
            item_sha = str(row["item_sha"] or "")[:12] or "—"
            head_sha = str(row["new_head_sha"] or "")[:12] or "—"
            lines.append(f"| `{row['item_id']}` | `{item_sha}` | `{head_sha}` |")
        return "\n".join(lines)

    def publish_if_ready(
        self,
        state: PlanState,
        *,
        title: str,
        body: str = "",
        draft: bool = False,
        summary: str | None = None,
        excluding: str | None = None,
    ) -> Publication | None:
        """Publish only a plan that has stopped moving; otherwise do nothing.

        Returning `None` rather than raising is deliberate: "not finished
        yet" is the normal answer on every promotion but the last, and it is
        not a failure of the item that triggered the check.
        """
        ready = self.readiness(excluding=excluding)
        if not ready.ready:
            return None
        return self.publish(
            state,
            title=title,
            body=body or self.item_evidence(),
            draft=draft,
            summary=summary,
        )

    # -- publication -----------------------------------------------------

    def publish(
        self,
        state: PlanState,
        *,
        title: str,
        body: str,
        draft: bool = False,
        summary: str | None = None,
    ) -> Publication:
        """Publish, or update, the one pull request for this plan.

        `summary` is the note left on an existing pull request when a
        correction moves the plan head. It is evidence for the reviewer who
        already looked once, not a new request for review.
        """
        record = self.record
        if record.branch and record.branch != state.branch:
            raise PublicationError(
                f"plan {self.project_id!r} was published from branch {record.branch!r}, "
                f"not {state.branch!r}; publishing a second branch would open a second "
                "pull request"
            )
        existing = record.pr_url or self._find_existing(state.branch)
        if existing and record.head_sha == state.head_sha:
            # Nothing was promoted since the last publication. A duplicate
            # review event, a retried poll or a no-op correction all land
            # here, and none of them may touch the remote.
            result = Publication(
                status="unchanged",
                pr_url=existing,
                head_sha=state.head_sha,
                published_sha=record.head_sha,
                detail="plan head is unchanged since it was published",
            )
            self._emit(state, result)
            return result

        self._push(state, record)

        if existing:
            if summary:
                with contextlib.suppress(Exception):
                    # A comment is evidence, not the publication. Failing to
                    # leave one must not lose the fact that the branch moved.
                    self.github.comment_pr(existing, summary)
            status, detail = "updated", "existing plan pull request now carries the new head"
            pr_url = existing
        else:
            pr_url = str(
                self.github.create_pr(
                    title=title,
                    body=body,
                    head=state.branch,
                    base=state.target_branch,
                    draft=draft,
                )
            ).strip()
            if not pr_url:
                raise PublicationError("the remote returned no pull request URL")
            status, detail = "created", "one plan pull request opened for human review"

        self._remember(state, pr_url)
        result = Publication(
            status=status,
            pr_url=pr_url,
            head_sha=state.head_sha,
            published_sha=record.head_sha,
            detail=detail,
        )
        self._emit(state, result)
        return result

    def _find_existing(self, branch: str) -> str | None:
        try:
            found = self.github.find_open_pr(branch)
        except Exception as exc:  # noqa: BLE001 - a blind create would duplicate
            raise PublicationError(
                f"could not ask the remote whether {branch!r} already has a pull "
                f"request, so none was created: {exc}"
            ) from exc
        return str(found) if found else None

    def _lease(self, state: PlanState, record: PublicationRecord) -> str:
        """What this harness expects the remote branch to be right now.

        Normally that is the sha it last published. When there is no record —
        a first publication, or an adopted pull request whose record was lost
        — the only safe expectation is one this checkout can prove it already
        contains. An empty lease means "expect the ref not to exist", which is
        correct for a branch nobody has published, and a refusal for one
        somebody has.
        """
        if record.head_sha:
            return record.head_sha
        remote_sha = self._remote_sha(state.branch)
        if not remote_sha:
            return ""
        if self._contains(state.head_sha, remote_sha):
            return remote_sha
        raise PublicationError(
            f"{self.remote}/{state.branch} is at {remote_sha}, which this plan does not "
            "contain, and no publication record explains it; publishing would discard "
            "work this harness cannot see"
        )

    def _remote_sha(self, branch: str) -> str:
        """The remote branch's sha, fetched so it is a local object."""
        try:
            self._run(["git", "-C", str(self.repo), "fetch", self.remote, branch])
        except Exception:  # noqa: BLE001 - an absent branch is the common case
            return ""
        return self._run(["git", "-C", str(self.repo), "rev-parse", "FETCH_HEAD"]).strip()

    def _contains(self, head_sha: str, candidate: str) -> bool:
        try:
            self._run(
                ["git", "-C", str(self.repo), "merge-base", "--is-ancestor", candidate, head_sha]
            )
        except Exception:  # noqa: BLE001 - "not an ancestor" is an exit code
            return False
        return True

    def _push(self, state: PlanState, record: PublicationRecord) -> None:
        lease = self._lease(state, record)
        args = [
            "git",
            "-C",
            str(self.repo),
            "push",
            f"--force-with-lease={state.branch}:{lease}",
            self.remote,
            f"{state.branch}:{state.branch}",
        ]
        try:
            self._run(args)
        except Exception as exc:  # noqa: BLE001 - remote divergence is a named refusal
            raise PublicationError(
                f"could not publish {state.branch!r} to {self.remote!r}: {exc}"
            ) from exc

    @staticmethod
    def _subprocess_run(args: list[str]) -> str:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            args, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "git push failed")
        return result.stdout

    def _emit(self, state: PlanState, result: Publication) -> None:
        if self.on_event is None:
            return
        with contextlib.suppress(Exception):
            self.on_event(
                {
                    "kind": "work",
                    "outcome": "plan_published",
                    "project_id": self.project_id,
                    "status": result.status,
                    "branch": state.branch,
                    "target_branch": state.target_branch,
                    "head_sha": result.head_sha,
                    "previous_head_sha": result.published_sha,
                    "pr_url": result.pr_url,
                    "detail": result.detail,
                }
            )
