"""Run a work item as a CLI agent in a MyDevEnv2 terminal session.

The difference from calling a model API directly is not implementation
detail — it is the product. An agent running in a PTY session is one you can
**attach to**: streaming output, full scrollback, from a phone, and an
approval prompt you can answer. An agent behind an API call produces none of
that; you get a result or you get nothing.

    claim item
      -> git worktree for the item, branch off its base
      -> write the brief to a prompt file
      -> ask MyDevEnv2 to run `claude -p @prompt.md` (or codex, or anything)
      -> WAIT, surfacing `waiting-for-input` rather than treating it as done
      -> checks -> review -> commit -> push -> PR

Two things this design gets from MyDevEnv2 for free, which are the reason for
it: the session id deep-links to a terminal tab in the UI the user already
has open, and MyDevEnv2's own push notifications fire when an agent stops to
ask something.

Each item gets its own **git worktree**. Two agents editing one working tree
is a data race that corrupts both, and the failure looks like a bad model
rather than a bad harness — which is the worst kind of bug to chase.
"""

from __future__ import annotations

import contextlib
import shlex
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .executor import APPROVED, REJECTED, Checks, Outcome, run_git
from .model_client import CapExhausted, ModelClient, RequestRefused
from .mydevenv2 import Session, SessionHost
from .work import DONE, FAILED, PENDING, WorkQueue, WorkRecord, worker_identity

#: The default agent. `-p` takes the prompt; the harness supplies it as a
#: file so a long brief is not mangled by shell quoting, and so the exact
#: prompt an agent was given stays on disk next to its result.
DEFAULT_AGENT_COMMAND = ("claude", "-p", "{prompt_file}", "--permission-mode", "acceptEdits")

PROMPT_TEMPLATE = """\
You are working one item from a plan. Work only on this item.

# {title}

{brief}

## How this is judged

Your changes are checked and then reviewed by a different model before
anything is proposed. Specifically:

{checks_description}

A reviewer then reads your diff against the item above and can reject it.

## Rules

- Change only what this item asks for. Unrelated edits will be rejected.
- Do not commit; the harness commits what you leave in the working tree.
- Do not push, and do not open a pull request.
- If the item cannot be done as written — it is ambiguous, contradicts the
  code, or depends on something absent — stop and say so plainly. Saying
  "this cannot be done as specified" is a correct outcome; inventing a way
  around it is not.
"""

REVIEW_PROMPT = """\
Review this change. You did not write it.

The task:
{brief}

The diff:
```diff
{diff}
```

Checks: {checks}

Answer with a first line of exactly APPROVED or REJECTED, then your reasoning.
Reject if the change does not do what the task asked, breaks something, or
claims to do more than it does. Approving work that does not do what was
asked is the expensive failure here; an unnecessary rejection costs one
retry.
"""


@dataclass
class AgentSpec:
    """How to launch the CLI agent for one item."""

    command: Sequence[str] = DEFAULT_AGENT_COMMAND
    env: Mapping[str, str] = field(default_factory=dict)
    #: How long to let one agent run before giving up on it. Generous:
    #: a real task can legitimately take an hour, and killing honest work
    #: is more expensive than waiting.
    timeout_seconds: float = 3600.0
    poll_seconds: float = 5.0

    def render(self, prompt_file: Path, item_id: str) -> list[str]:
        return [part.format(prompt_file=str(prompt_file), item_id=item_id) for part in self.command]


class SessionExecutor:
    """Executes work items as attachable MyDevEnv2 sessions."""

    def __init__(
        self,
        queue: WorkQueue,
        devenv: SessionHost,
        repo: Path,
        *,
        agent: AgentSpec | None = None,
        checks: Checks | None = None,
        reviewer: ModelClient | None = None,
        github: Any | None = None,
        base_branch: str = "main",
        branch_prefix: str = "harness/",
        worktrees: Path | None = None,
        ui_base_url: str = "",
        on_event: Callable[[dict[str, Any]], None] | None = None,
        push: bool = True,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.queue = queue
        self.devenv = devenv
        self.repo = Path(repo)
        self.agent = agent or AgentSpec()
        self.checks = checks or Checks()
        self.reviewer = reviewer
        self.github = github
        self.base_branch = base_branch
        self.branch_prefix = branch_prefix
        self.worktrees = Path(worktrees) if worktrees else self.repo.parent / ".harness-work"
        self.ui_base_url = ui_base_url
        self.on_event = on_event
        self.push = push
        self.now = now
        self.owner = worker_identity()

    # ------------------------------------------------------------- driving

    def run_once(self) -> Outcome | None:
        record = self.queue.claim(self.owner)
        if record is None:
            return None
        try:
            outcome = self._execute(record)
        except CapExhausted as exc:
            self._emit(record, "budget_exhausted", detail=str(exc))
            self.queue.release(record.item_id, PENDING, error=f"budget: {exc}")
            raise
        except Exception as exc:  # noqa: BLE001 - one item must not kill the loop
            self._emit(record, "error", detail=str(exc))
            self.queue.release(record.item_id, FAILED, error=str(exc))
            return Outcome(record.item_id, FAILED, reason=str(exc))
        self.queue.release(
            record.item_id,
            outcome.state,
            error=outcome.reason or None,
            branch=outcome.branch,
            pr_url=outcome.pr_url,
        )
        return outcome

    def run(self, limit: int | None = None) -> list[Outcome]:
        outcomes: list[Outcome] = []
        while limit is None or len(outcomes) < limit:
            try:
                outcome = self.run_once()
            except CapExhausted:
                break
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes

    # ------------------------------------------------------------ the loop

    def _execute(self, record: WorkRecord) -> Outcome:
        outcome = Outcome(record.item_id, FAILED)
        self._emit(record, "started")

        branch = f"{self.branch_prefix}{record.item_id.lower()}"
        base, stacked_on = self._base_for(record)
        outcome.branch, outcome.base = branch, base
        tree = self._add_worktree(record.item_id, branch, base)
        if stacked_on:
            self._emit(record, "stacked", detail=f"based on {base} ({stacked_on})")

        try:
            prompt_file = tree / ".harness-prompt.md"
            prompt_file.write_text(
                PROMPT_TEMPLATE.format(
                    title=record.title,
                    brief=record.brief,
                    checks_description=self._describe_checks(),
                )
            )

            session = self.devenv.create_session(
                name=f"{record.item_id}: {record.title[:40]}",
                command=self.agent.render(prompt_file, record.item_id),
                cwd=str(tree),
                env=dict(self.agent.env),
            )
            outcome.session_id = session.id
            outcome.stages.append("agent")
            self._emit(
                record,
                "agent_started",
                detail=session.id,
                session_id=session.id,
                url=session.tab_url(self.ui_base_url) if self.ui_base_url else None,
            )

            finished = self.devenv.wait_for_exit(
                session.id,
                timeout=self.agent.timeout_seconds,
                poll_seconds=self.agent.poll_seconds,
                on_waiting=lambda s: self._on_waiting(record, s),
            )
            if not finished.finished:
                # A timeout, or a prompt nobody answered. The session is left
                # alive on purpose: it holds the agent's context, and killing
                # it would destroy the one thing that makes the item
                # resumable by a human.
                outcome.reason = (
                    f"agent did not finish within {self.agent.timeout_seconds:.0f}s "
                    f"(activity={finished.activity}); session {session.id} left running"
                )
                self._emit(record, "agent_timeout", detail=outcome.reason, session_id=session.id)
                return outcome
            if finished.exit_code != 0:
                outcome.reason = f"agent exited {finished.exit_code}"
                self._emit(record, "agent_failed", detail=outcome.reason, session_id=session.id)
                return outcome
            self._emit(record, "agent_finished", session_id=session.id)

            # What did it actually change? A CLI agent that decided the task
            # was impossible leaves a clean tree, and that is a real answer,
            # not a failure to paper over.
            prompt_file.unlink(missing_ok=True)
            diff = run_git(tree, "diff", "HEAD")
            if not diff.strip() and not run_git(tree, "status", "--porcelain").strip():
                outcome.reason = "the agent made no changes"
                self._emit(record, "no_changes", session_id=session.id)
                return outcome
            outcome.stages.append("changes")

            passed, failure = self.checks.run(tree)
            outcome.stages.append("checks")
            if not passed:
                outcome.reason = failure
                self._emit(record, "checks_failed", detail=failure[:2000], session_id=session.id)
                return outcome
            self._emit(record, "checks_passed", session_id=session.id)
            self.queue.heartbeat(record.item_id, self.owner)

            verdict_text = self._review(record, tree, passed, failure)
            outcome.stages.append("review")
            verdict = APPROVED if verdict_text.strip().upper().startswith("APPROVED") else REJECTED
            outcome.verdict = verdict
            self._emit(
                record, f"review_{verdict}", detail=verdict_text[:2000], session_id=session.id
            )
            if verdict != APPROVED:
                outcome.reason = f"review rejected: {verdict_text.strip()[:500]}"
                return outcome

            self._commit(tree, record, verdict_text)
            outcome.stages.append("commit")
            if self.push:
                run_git(tree, "push", "-u", "origin", branch)
                outcome.stages.append("push")
            if self.github is not None and record.issue:
                outcome.pr_url = self._open_pr(record, branch, base, verdict_text)
                outcome.stages.append("pr")

            outcome.state = DONE
            self._emit(record, "done", detail=outcome.pr_url or branch, session_id=session.id)
            return outcome
        finally:
            # The worktree goes whatever happened. Its branch survives, so a
            # rejected attempt is still inspectable; leaving the tree behind
            # would just accumulate copies of the repo.
            self._remove_worktree(tree, keep_branch=outcome.state == DONE)

    # ------------------------------------------------------------- helpers

    def _describe_checks(self) -> str:
        if not self.checks.commands:
            return "- No automated checks are configured for this repository."
        return "\n".join(f"- `{shlex.join(list(c))}` must pass" for c in self.checks.commands)

    def _on_waiting(self, record: WorkRecord, session: Session) -> None:
        """The agent is asking a human something.

        Not an error and not completion. The lease is extended because the
        work is genuinely alive, and the event carries the session id so the
        UI can put a human straight into the terminal that is asking.
        """
        self.queue.heartbeat(record.item_id, self.owner)
        self._emit(
            record,
            "waiting_for_input",
            session_id=session.id,
            detail="the agent is asking for input",
            url=session.tab_url(self.ui_base_url) if self.ui_base_url else None,
        )

    def _review(self, record: WorkRecord, tree: Path, passed: bool, failure: str) -> str:
        diff = run_git(tree, "diff", "HEAD")
        if self.reviewer is None:
            # No reviewer configured. Say so rather than silently treating
            # unreviewed work as approved.
            return "REJECTED\nNo reviewer is configured, so nothing has reviewed this."
        prompt = REVIEW_PROMPT.format(
            brief=record.brief,
            diff=diff[:20000],
            checks="passed" if passed else failure,
        )
        try:
            response = self.reviewer.call("reviewer", [{"role": "user", "content": prompt}])
        except RequestRefused as exc:
            return f"REJECTED\nThe reviewer refused to answer: {exc}"
        from .executor import _text_of

        return _text_of(response.body)

    def _base_for(self, record: WorkRecord) -> tuple[str, str | None]:
        candidates = [
            dependency
            for dependency in record.depends_on
            if (found := self.queue.get(dependency)) and found.branch and found.state == DONE
        ]
        if not candidates:
            return self.base_branch, None
        first = self.queue.get(candidates[0])
        assert first is not None and first.branch is not None
        note = candidates[0]
        if len(candidates) > 1:
            note = f"{candidates[0]}; NOT stacked on {', '.join(candidates[1:])}"
        return first.branch, note

    def _add_worktree(self, item_id: str, branch: str, base: str) -> Path:
        """A private tree per item, so concurrent agents cannot collide."""
        self.worktrees.mkdir(parents=True, exist_ok=True)
        tree = self.worktrees / item_id
        if tree.exists():
            self._remove_worktree(tree, keep_branch=True)
        run_git(self.repo, "worktree", "add", "-B", branch, str(tree), base)
        return tree

    def _remove_worktree(self, tree: Path, *, keep_branch: bool) -> None:
        run_git(self.repo, "worktree", "remove", "--force", str(tree), check=False)
        if tree.exists():  # pragma: no cover - only when git refuses
            with contextlib.suppress(OSError):
                shutil.rmtree(tree)
        run_git(self.repo, "worktree", "prune", check=False)

    def _commit(self, tree: Path, record: WorkRecord, verdict: str) -> None:
        run_git(tree, "add", "-A")
        message = (
            f"{record.title}\n\n"
            f"{record.brief.strip()[:1500]}\n\n"
            f"Reviewer verdict:\n{verdict.strip()[:1500]}\n\n"
            f"harness-item: {record.item_id}\n"
        )
        run_git(tree, "commit", "-m", message)

    def _open_pr(self, record: WorkRecord, branch: str, base: str, verdict: str) -> str | None:
        github = self.github
        if github is None:  # pragma: no cover - guarded by the caller
            return None
        body = (
            f"{record.brief.strip()[:3000]}\n\n"
            f"---\n\n**Reviewer verdict**\n\n{verdict.strip()[:3000]}\n\n"
            f"Closes #{record.issue}\n"
        )
        try:
            url = github.create_pr(title=record.title, body=body, head=branch, base=base)
            return str(url) if url else None
        except Exception as exc:  # noqa: BLE001 - a PR failure must not lose the work
            self._emit(record, "pr_failed", detail=str(exc))
            return None

    def _emit(
        self,
        record: WorkRecord,
        stage: str,
        detail: str | None = None,
        session_id: str | None = None,
        url: str | None = None,
    ) -> None:
        if self.on_event is None:
            return
        # Telemetry is never load-bearing.
        with contextlib.suppress(Exception):
            self.on_event(
                {
                    "ts": self.now(),
                    "kind": "work",
                    "worker": self.owner,
                    "item_id": record.item_id,
                    "issue": record.issue,
                    "outcome": stage,
                    "detail": detail,
                    # The deep link. This is what lets the UI put a human into
                    # the terminal that is asking them a question.
                    "session_id": session_id,
                    "session_url": url,
                }
            )
