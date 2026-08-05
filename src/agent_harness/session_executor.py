"""Run a work item as a CLI agent in a hosted terminal session.

The difference from calling a model API directly is not implementation
detail — it is the product. An agent running in a PTY session is one you can
**attach to**: streaming output, full scrollback, from a phone, and an
approval prompt you can answer. An agent behind an API call produces none of
that; you get a result or you get nothing.

    claim item
      -> git worktree for the item, branch off its base
      -> write the brief to a prompt file
      -> ask the session host to run `claude -p @prompt.md` (or codex, or …)
      -> WAIT, surfacing `waiting-for-input` rather than treating it as done
      -> checks -> review -> commit -> push -> PR

Two things this design gets from the host for free, which are the reason for
it: the session id deep-links to a terminal tab in the UI the user already
has open, and the host's own push notifications fire when an agent stops to
ask something.

Each item gets its own **git worktree**. Two agents editing one working tree
is a data race that corrupts both, and the failure looks like a bad model
rather than a bad harness — which is the worst kind of bug to chase.
"""

from __future__ import annotations

import contextlib
import logging
import shlex
import shutil
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .executor import APPROVED, REJECTED, Checks, Outcome, is_disk_exhaustion, run_git
from .graph import LOCAL_WORK
from .model_client import CapExhausted, ModelClient, RequestRefused, RetryExhausted
from .outcomes import (
    AGENT_TIMEOUT,
    BUDGET_EXHAUSTED,
    CLAIM_LOST,
    COMPLETED,
    CRASHED,
    DEPENDENCY_INVALIDATED,
    NO_TARGET,
    PROVIDER_EXHAUSTED,
    REFUSED,
    REVIEW_REJECTED,
    WITHHELD,
    WORKER_ERROR,
    Stop,
    stop_for,
)
from .reaper import DEFAULT_MAX_AGE_SECONDS, ReapReport, reap_abandoned_sessions
from .session_host import Session, SessionHost
from .work import (
    CLAIMED,
    DEFAULT_PROJECT,
    DONE,
    FAILED,
    PENDING,
    ClaimLost,
    LeaseHeartbeat,
    WorkQueue,
    WorkRecord,
    worker_identity,
)

log = logging.getLogger(__name__)

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
Review this change. You did not write it, and your job is not to be agreeable.

Assume it is wrong until the diff shows otherwise. Most changes that fail
review fail because they do something *adjacent* to what was asked, or claim
more than they did — not because they are obviously broken.

The task:
{brief}

The diff:
```diff
{diff}
```

Checks: {checks}

## Answer

First line exactly APPROVED or REJECTED. Then, in order:

1. **What I verified** — the specific things in the diff you actually checked
   against the task. If you cannot name any, that is a REJECTED.
2. **What I could not verify** — anything the diff claims that the diff alone
   does not show. Say it, do not assume it.
3. **Why** — one paragraph.

## Reject if

- It does not do what the task asked, or does more than the task asked.
- It claims an effect the diff does not demonstrate.
- It changes something unrelated, however small.
- The task cannot be judged from what you were given.

Approving work that does not do what was asked is the expensive failure here:
it reaches a pull request, a human reads it as reviewed, and the cost lands
much later. An unnecessary rejection costs one retry.
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
    """Executes work items as attachable hosted terminal sessions."""

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
        session_max_age: float = DEFAULT_MAX_AGE_SECONDS,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        push: bool = True,
        now: Callable[[], float] = time.time,
        project_id: str = DEFAULT_PROJECT,
    ) -> None:
        self.queue = queue
        # Which project's queue this worker serves. Without it a worker in a
        # multi-project fleet claims from `default` regardless of which
        # project it was started for.
        self.project_id = project_id
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
        self.session_max_age = session_max_age
        self.on_event = on_event
        self.push = push
        self.now = now
        self.owner = worker_identity()
        self._partial: Outcome | None = None
        self._heartbeat: LeaseHeartbeat | None = None
        #: Whether this item already has an open question. One hold per
        #: waiting agent, not one per poll: the session host reports "waiting"
        #: every few seconds, and a question per poll would be an inbox nobody
        #: could read.
        self._held = False

    # ------------------------------------------------------------- driving

    def _execute_with_heartbeat(self, record: WorkRecord) -> Outcome:
        """One attempt, with its claim kept alive for the whole of it.

        The heartbeat covers every stage rather than the gaps between them,
        because the long stages are the ones that used to lose the claim.
        """
        heartbeat = LeaseHeartbeat(
            self.queue, record.item_id, self.owner, project_id=self.project_id
        )
        self._heartbeat = heartbeat
        try:
            with heartbeat:
                return self._execute(record)
        finally:
            self._heartbeat = None

    def run_once(self) -> Outcome | None:
        record = self.queue.claim(self.owner, project_id=self.project_id)
        if record is None:
            return None
        self._held = False
        try:
            outcome = self._execute_with_heartbeat(record)
        except ClaimLost as exc:
            # Deliberately no release: the item is not ours to finish. The
            # new owner is working on it right now, and reporting anything
            # here would overwrite a live claim.
            self._emit(record, "claim_lost", detail=str(exc), error_class=CLAIM_LOST)
            self._orphan_session(record, f"claim lost: {exc}")
            return None
        except CapExhausted as exc:
            self._emit(record, "budget_exhausted", detail=str(exc))
            self._orphan_session(record, f"budget exhausted: {exc}")
            partial = self._partial_for(record)
            self.queue.release(
                record.item_id,
                PENDING,
                error=f"budget: {exc}",
                branch=partial.branch if partial else None,
                pr_url=partial.pr_url if partial else None,
                owner=self.owner,
                consume_attempt=False,
                disposition=WITHHELD,
                reason_kind=BUDGET_EXHAUSTED,
                project_id=self.project_id,
            )
            raise
        except RetryExhausted as exc:
            # The call-level ladder is spent, not the item's life. Returning
            # it to pending lets WorkQueue's per-project max_attempts retire a
            # persistently bad item instead of making one provider wobble a
            # permanent failure.
            self._emit(
                record,
                "retry_exhausted",
                detail=str(exc),
                error_class=exc.kind,
            )
            self._orphan_session(record, str(exc))
            partial = self._partial_for(record)
            stop = Stop(WITHHELD, PROVIDER_EXHAUSTED, detail=str(exc))
            self.queue.release(
                record.item_id,
                PENDING,
                error=str(exc),
                branch=partial.branch if partial else None,
                pr_url=partial.pr_url if partial else None,
                owner=self.owner,
                disposition=stop.disposition,
                reason_kind=stop.reason_kind,
                project_id=self.project_id,
            )
            if partial is not None:
                partial.state = PENDING
                partial.reason = str(exc)
                partial.stop = stop
                return partial
            return Outcome(record.item_id, PENDING, reason=str(exc), stop=stop)
        except Exception as exc:  # noqa: BLE001 - one item must not kill the loop
            self._emit(record, "error", detail=str(exc))
            self._orphan_session(record, str(exc))
            partial = self._partial_for(record)
            stop = Stop(CRASHED, WORKER_ERROR, detail=str(exc))
            self.queue.release(
                record.item_id,
                FAILED,
                error=str(exc),
                branch=partial.branch if partial else None,
                pr_url=partial.pr_url if partial else None,
                owner=self.owner,
                disposition=stop.disposition,
                reason_kind=stop.reason_kind,
                project_id=self.project_id,
            )
            if partial is not None:
                partial.state = FAILED
                partial.reason = str(exc)
                partial.stop = stop
                return partial
            return Outcome(record.item_id, FAILED, reason=str(exc), stop=stop)
        self.queue.release(
            record.item_id,
            outcome.state,
            error=outcome.reason or None,
            branch=outcome.branch,
            pr_url=outcome.pr_url,
            owner=self.owner,
            consume_attempt=outcome.stop.consumes_attempt if outcome.stop else True,
            disposition=outcome.disposition,
            reason_kind=outcome.reason_kind,
            project_id=self.project_id,
        )
        return outcome

    def _partial_for(self, record: WorkRecord) -> Outcome | None:
        partial = self._partial
        if partial is not None and partial.item_id == record.item_id:
            return partial
        return None

    def _orphan_session(self, record: WorkRecord, reason: str) -> None:
        """Own the session of an item that failed unexpectedly.

        The item is released either way, but the PTY it was running in stays
        alive with nobody waiting on it -- possibly with an agent still
        spending tokens in it. Recorded rather than killed, for the same
        reason a timed-out session is: it holds the context that makes the
        failure diagnosable, and the reaper collects it if nobody comes back.
        """
        partial = self._partial
        if partial is None or partial.session_id is None or partial.item_id != record.item_id:
            return
        with contextlib.suppress(Exception):
            self.queue.record_abandoned_session(
                partial.session_id,
                record.item_id,
                reason=f"worker failed and left this session running: {reason}",
                session_url=None,
            )
            self._emit(
                record,
                "session_orphaned",
                detail=reason,
                session_id=partial.session_id,
            )

    def reap(self) -> ReapReport | None:
        """Collect sessions kept alive after a timeout that nobody returned to.

        Returns None when the host cannot reap -- the executor's `SessionHost`
        protocol deliberately does not include killing sessions, so a host
        that only creates and waits is a legitimate configuration, not an
        error.
        """
        if not (hasattr(self.devenv, "kill_session") and hasattr(self.devenv, "delete_session")):
            return None
        report = reap_abandoned_sessions(
            self.queue,
            self.devenv,  # type: ignore[arg-type]
            max_age=self.session_max_age,
            on_event=self.on_event,
        )
        if report.reaped or report.failed:
            log.info("session reaper: %s", report)
        return report

    def serve(
        self,
        *,
        poll_seconds: float = 15.0,
        stop: threading.Event | None = None,
        max_idle_polls: int | None = None,
    ) -> list[Outcome]:
        """Run until stopped, waiting for work rather than exiting without it.

        `run()` drains the backlog and returns, which is right for a one-shot
        invocation and wrong for a fleet: add an item an hour later and
        nothing claims it. This is the daemon.

        Control state is re-read on every pass, so pausing a project takes
        effect at the next item boundary without a restart -- and resuming it
        needs no restart either.
        """
        outcomes: list[Outcome] = []
        stop = stop or threading.Event()
        idle = 0
        while not stop.is_set():
            self.reap()
            try:
                outcome = self.run_once()
            except CapExhausted as exc:
                # Out of budget. Waiting is the only useful response, and the
                # park in ModelClient already knows for how long -- so sleep a
                # poll and re-ask rather than exiting the fleet.
                log.info("budget exhausted, waiting: %s", exc)
                outcome = None
            if outcome is None:
                idle += 1
                if max_idle_polls is not None and idle >= max_idle_polls:
                    return outcomes
                stop.wait(poll_seconds)
                continue
            idle = 0
            outcomes.append(outcome)
        return outcomes

    def run(self, limit: int | None = None) -> list[Outcome]:
        outcomes: list[Outcome] = []
        # Before claiming, not after: a run that exits early still leaves the
        # previous run's survivors collected.
        self.reap()
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

    def _keepalive(self, record: WorkRecord) -> None:
        """Extend the lease, and stop if it is no longer ours.

        The heartbeat has always returned whether the claim survived; nothing
        read it, so a worker that lost its claim carried on regardless and
        then reported a result for someone else's item. Reading the answer is
        the whole point of asking.

        The background heartbeat is checked first: it is what notices a loss
        during a stage, while this call is what turns that into a stop at the
        next boundary.
        """
        if self._heartbeat is not None and self._heartbeat.lost:
            raise ClaimLost(
                f"{record.item_id} is no longer owned by {self.owner}; "
                "its lease was refused while the attempt was still running"
            )
        if not self.queue.heartbeat(record.item_id, self.owner, project_id=self.project_id):
            raise ClaimLost(
                f"{record.item_id} is no longer owned by {self.owner}; "
                "its lease expired and another worker re-claimed it"
            )

    def _execute(self, record: WorkRecord) -> Outcome:
        outcome = Outcome(record.item_id, FAILED)
        # Kept on the instance so an unexpected failure can still report how
        # far the item got. Fabricating a fresh Outcome in the handler threw
        # that away, which mattered exactly when it was most wanted: after the
        # draft-PR checkpoint, "it died during review with the work already
        # committed" and "it died before touching anything" look identical
        # from an empty stage list.
        self._partial = outcome
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
                # Kept alive on purpose -- and recorded, so it is owned rather
                # than merely surviving. The reaper collects it if nobody
                # comes back to it.
                self.queue.record_abandoned_session(
                    session.id,
                    record.item_id,
                    reason=outcome.reason,
                    session_url=session.tab_url(self.ui_base_url) if self.ui_base_url else None,
                )
                # Nothing judged the work: the agent never finished, and it
                # is still holding the item in a session deliberately left
                # alive. The state is unchanged from before this taxonomy
                # existed -- moving it to pending would let a second worker
                # claim a tree a live agent is still writing to.
                outcome.stop = Stop(CRASHED, AGENT_TIMEOUT, detail=outcome.reason)
                return outcome
            if finished.exit_code != 0:
                outcome.reason = f"agent exited {finished.exit_code}"
                self._emit(record, "agent_failed", detail=outcome.reason, session_id=session.id)
                outcome.stop = Stop(CRASHED, WORKER_ERROR, detail=outcome.reason)
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
                outcome.stop = Stop(REFUSED, NO_TARGET, detail=outcome.reason)
                return outcome
            outcome.stages.append("changes")

            checked = self.checks.run(tree)
            failure = checked.detail
            outcome.stages.append("checks")
            if not checked.ok:
                stop = stop_for(checked)
                outcome.reason = failure
                self._emit(
                    record,
                    "checks_failed",
                    detail=failure[:2000],
                    session_id=session.id,
                    error_class=(
                        "disk_exhausted" if is_disk_exhaustion(failure) else stop.reason_kind
                    ),
                )
                if checked.fix:
                    self._emit(
                        record,
                        "fix_available",
                        detail="`" + " ".join(checked.fix) + "` is declared to clear this",
                        session_id=session.id,
                    )
                outcome.stop = stop
                outcome.state = stop.state
                return outcome
            self._emit(record, "checks_passed", session_id=session.id)
            self._keepalive(record)

            # The same graph check the direct executor makes, at the same
            # point and through the same call. Session mode reached its
            # checkpoint without re-reading the graph at all, so a plan
            # corrected while an agent was working produced a durable,
            # externally visible candidate for work that was no longer
            # eligible -- the one place the two executors disagreed about a
            # gate. The live agent is not killed: it has already reached a
            # safe boundary, and the item goes back to pending.
            admission = self.queue.readiness(record.item_id, project_id=self.project_id)
            if not admission.ready:
                outcome.reason = (
                    f"{record.item_id} was admitted at graph revision "
                    f"{record.admitted_revision} and {admission.explain()}; the candidate is "
                    "discarded and the item goes back to pending"
                )
                self._emit(
                    record,
                    "dependency_invalidated",
                    detail=outcome.reason,
                    session_id=session.id,
                )
                # Withheld, not failed. The graph moved under a live claim;
                # nothing judged the work.
                outcome.stop = Stop(WITHHELD, DEPENDENCY_INVALIDATED, detail=outcome.reason)
                outcome.state = PENDING
                return outcome

            # Checkpoint BEFORE the expensive gate.
            #
            # Review is the slowest and most failure-prone step, and it used
            # to happen before anything was committed -- so a worker killed
            # during it lost work that had already passed every cheap gate.
            # Committing and pushing here means the candidate survives the
            # worker: a draft PR is still there on restart, with its evidence,
            # for a human or a later attempt.
            #
            # Draft, not ready: an unreviewed candidate must never present
            # itself as reviewed. Marking it ready is what approval buys.
            self._commit(tree, record, checkpoint=True)
            outcome.stages.append("commit")
            self._emit(record, "checkpointed", detail=branch, session_id=session.id)
            if self.push:
                run_git(tree, "push", "-u", "origin", branch)
                outcome.stages.append("push")
            if self.github is not None and record.issue:
                outcome.pr_url = self._open_pr(record, branch, base, draft=True)
                if outcome.pr_url:
                    outcome.stages.append("draft-pr")
                    self._emit(
                        record, "draft_pr_opened", detail=outcome.pr_url, session_id=session.id
                    )

            verdict_text = self._review(record, tree, True, "", base=base)
            outcome.stages.append("review")
            verdict = APPROVED if verdict_text.strip().upper().startswith("APPROVED") else REJECTED
            outcome.verdict = verdict
            self._emit(
                record, f"review_{verdict}", detail=verdict_text[:2000], session_id=session.id
            )

            # The verdict goes on the PR either way. A rejected draft that
            # says why is a lead; a rejected draft that says nothing is
            # litter someone has to reconstruct.
            if outcome.pr_url and self.github is not None:
                self._record_verdict(record, outcome.pr_url, verdict, verdict_text)

            if verdict != APPROVED:
                outcome.reason = f"review rejected: {verdict_text.strip()[:500]}"
                outcome.stop = Stop(REFUSED, REVIEW_REJECTED, detail=outcome.reason)
                return outcome

            if outcome.pr_url and self.github is not None:
                self._mark_ready(record, outcome.pr_url)
                outcome.stages.append("pr")

            outcome.state = DONE
            outcome.stop = Stop(COMPLETED)
            self._emit(record, "done", detail=outcome.pr_url or branch, session_id=session.id)
            return outcome
        finally:
            # The worktree goes whatever happened. Its branch survives, so a
            # rejected attempt is still inspectable; leaving the tree behind
            # would just accumulate copies of the repo.
            worktree_bytes = self._worktree_bytes(tree)
            self._remove_worktree(tree, keep_branch=outcome.state == DONE)
            self._emit(
                record,
                "worktree_removed",
                detail=f"reclaimed {worktree_bytes} bytes",
                worktree_bytes=worktree_bytes,
            )

    # ------------------------------------------------------------- helpers

    def _describe_checks(self) -> str:
        if not self.checks.commands:
            return "- No automated checks are configured for this repository."
        return "\n".join(f"- `{shlex.join(list(c))}` must pass" for c in self.checks.commands)

    def _on_waiting(self, record: WorkRecord, session: Session) -> None:
        """The agent is asking a human something.

        Not an error and not completion — and, since Stage J, **not a
        heartbeat either**. This used to extend the lease and emit an event,
        so a lease whose whole purpose is to distinguish slow from dead was
        being used to hold open a human's inbox: nothing bounded it, nothing
        survived the worker dying, and the answer could only come from the
        process that happened to be attached.

        A durable hold is opened instead. It is a state of the item, it
        outlives this process, and it is answerable from a phone.

        The hold is best-effort. A queue that will not record it must not turn
        an agent asking a question into a failed item — the old behaviour
        (extend the lease, say so in the stream) is still there underneath and
        is still better than nothing.
        """
        self._keepalive(record)
        url = session.tab_url(self.ui_base_url) if self.ui_base_url else None
        self._emit(
            record,
            "waiting_for_input",
            session_id=session.id,
            detail="the agent is asking for input",
            url=url,
        )
        if self._held:
            return
        try:
            hold = self.queue.hold(
                record.item_id,
                # The agent's own question is not available here: the session
                # host reports *that* it is waiting, not what it asked. Said
                # plainly rather than invented, and the deep link is what a
                # person actually needs to see the prompt.
                question=(
                    "the agent is waiting for input in its terminal session"
                    + (f" — attach at {url}" if url else "")
                ),
                owner=self.owner,
                reason="the session host reported the agent is waiting for input",
                session_id=session.id,
                session_url=url,
                project_id=self.project_id,
            )
        except Exception as exc:  # noqa: BLE001 - a hold must not fail an item
            self._emit(record, "hold_failed", detail=str(exc)[:300], session_id=session.id)
            return
        self._held = True
        self._emit(
            record,
            "held",
            session_id=session.id,
            detail=(
                "the item is held on a question and no other worker can claim it; "
                f"answer with the resume token, expiring at {hold.expires_at:.0f}"
                if hold.expires_at
                else "the item is held on a question and no other worker can claim it"
            ),
            url=url,
        )

    def _review(
        self, record: WorkRecord, tree: Path, passed: bool, failure: str, base: str = ""
    ) -> str:
        if self.reviewer is None:
            # Checked before the diff is computed: there is no point building
            # a diff nobody will read. Says so rather than silently treating
            # unreviewed work as approved.
            return "REJECTED\nNo reviewer is configured, so nothing has reviewed this."
        # Against the BASE, not the working tree. `git diff HEAD` answers
        # "what is uncommitted?", and by the time a reviewer is called the
        # answer is always "nothing": the checkpoint before the expensive
        # gate has just committed all of it. So every session-mode reviewer
        # was shown an empty diff, and said the only correct thing about one:
        #
        #   "The supplied diff is empty, so it demonstrates none of that and
        #    cannot be judged as satisfying the request."
        #
        # Measured on a real 48-line change that had already passed its
        # checks. No session-mode item could ever have been approved.
        diff = run_git(tree, "diff", f"{base}...HEAD") if base else run_git(tree, "diff", "HEAD")
        prompt = REVIEW_PROMPT.format(
            brief=record.brief,
            diff=diff[:20000],
            checks="passed" if passed else failure,
        )
        try:
            response = self.reviewer.call("reviewer", [{"role": "user", "content": prompt}])
        except RequestRefused as exc:
            return f"REJECTED\nThe reviewer refused to answer: {exc}"
        from .executor import _reader_for, _text_of

        return _text_of(response.body, _reader_for(self.reviewer, "reviewer"))

    def _base_for(self, record: WorkRecord) -> tuple[str, str | None]:
        candidates = [
            spec.target_id
            for spec in record.dependency_specs()
            if spec.target_kind == LOCAL_WORK
            and (found := self.queue.get(spec.target_id, project_id=self.project_id))
            and found.branch
            and found.state == DONE
        ]
        if not candidates:
            return self.base_branch, None
        first = self.queue.get(candidates[0], project_id=self.project_id)
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

    @staticmethod
    def _worktree_bytes(tree: Path) -> int:
        """Allocated bytes, not logical size, without following symlinks."""
        total = 0
        try:
            for path in tree.rglob("*"):
                with contextlib.suppress(OSError):
                    stat = path.lstat()
                    total += getattr(stat, "st_blocks", 0) * 512
        except OSError:
            return total
        return total

    def reap_orphaned_worktrees(self) -> list[str]:
        """Remove this repo's worktrees whose items are no longer claimed."""
        removed: list[str] = []
        listing = run_git(self.repo, "worktree", "list", "--porcelain", check=False)
        for line in listing.splitlines():
            if not line.startswith("worktree "):
                continue
            tree = Path(line.removeprefix("worktree "))
            if tree.parent != self.worktrees:
                continue
            record = self.queue.get(tree.name, project_id=self.project_id)
            if record is None:
                record = next(
                    (
                        item
                        for item in self.queue.items(project_id=self.project_id)
                        if item.item_id.lower() == tree.name.lower()
                    ),
                    None,
                )
            if record is not None and record.state == CLAIMED and record.lease_until >= self.now():
                # A live claim owns its worktree. An expired claim does not:
                # the next claimant can build a fresh tree from the durable
                # branch, while retaining this one only leaks the crashed
                # worker's build output.
                continue
            size = self._worktree_bytes(tree)
            self._remove_worktree(tree, keep_branch=True)
            removed.append(tree.name)
            if record is not None:
                self._emit(
                    record,
                    "orphaned_worktree_reaped",
                    detail=f"reclaimed {size} bytes",
                    worktree_bytes=size,
                )
        return removed

    def _commit(
        self, tree: Path, record: WorkRecord, verdict: str = "", checkpoint: bool = False
    ) -> None:
        run_git(tree, "add", "-A")
        trailer = (
            "Reviewed: not yet — this is a checkpoint taken after the cheap "
            "gates passed and before review.\n"
            if checkpoint
            else f"Reviewer verdict:\n{verdict.strip()[:1500]}\n"
        )
        message = (
            f"{record.title}\n\n"
            f"{record.brief.strip()[:1500]}\n\n"
            f"{trailer}\n"
            f"harness-item: {record.item_id}\n"
        )
        run_git(tree, "commit", "-m", message)

    def _record_verdict(
        self, record: WorkRecord, pr_url: str, verdict: str, verdict_text: str
    ) -> None:
        """Put the reviewer's verdict on the pull request.

        Best-effort: a comment that fails must not lose work that succeeded.
        """
        if self.github is None:
            return
        body = f"**Review: {verdict.upper()}**\n\n{verdict_text.strip()[:5000]}"
        try:
            self.github.comment_pr(pr_url, body)
        except Exception as exc:  # noqa: BLE001
            self._emit(record, "pr_comment_failed", detail=str(exc))

    def _mark_ready(self, record: WorkRecord, pr_url: str) -> None:
        """Take the pull request out of draft. This is what approval buys.

        If it fails the work is not lost -- the draft is still there, with the
        approving verdict on it, and a human can press the button.
        """
        if self.github is None:
            return
        try:
            self.github.mark_pr_ready(pr_url)
        except Exception as exc:  # noqa: BLE001
            self._emit(record, "pr_ready_failed", detail=str(exc))

    def _open_pr(
        self, record: WorkRecord, branch: str, base: str, *, draft: bool = True
    ) -> str | None:
        github = self.github
        if github is None:  # pragma: no cover - guarded by the caller
            return None
        body = (
            f"{record.brief.strip()[:3000]}\n\n"
            "---\n\n"
            "**Not yet reviewed.** Opened as a draft after the cheap gates passed, "
            "so the work survives the worker that produced it. The reviewer's "
            "verdict is posted as a comment, and approval takes it out of draft.\n\n"
            f"Closes #{record.issue}\n"
        )
        try:
            existing = getattr(github, "find_open_pr", lambda _head: None)(branch)
            if existing:
                return str(existing)
            url = github.create_pr(
                title=record.title, body=body, head=branch, base=base, draft=draft
            )
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
        error_class: str | None = None,
        **data: Any,
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
                    "error_class": error_class,
                    "detail": detail,
                    # The deep link. This is what lets the UI put a human into
                    # the terminal that is asking them a question.
                    "session_id": session_id,
                    "session_url": url,
                    "project_id": self.project_id,
                    **data,
                }
            )
