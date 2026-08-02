"""Run one work item: plan it, implement it, check it, review it, propose it.

This is the loop. It claims an item, drives three model roles over it, and
either opens a pull request or hands the item back — and it records every
step as an event so the dashboard can answer "what is it doing?" without
anyone opening a log.

The ordering is the part worth defending:

    plan -> implement -> apply -> CHEAP CHECK -> review -> push -> PR

**Cheap checks run before the expensive reviewer call.** A patch that does
not apply, or does not compile, cannot be worth a review; spending a model
call to be told so is paying the most expensive gate to catch what the
cheapest one already caught.

**The branch is pushed before the PR is opened, and both happen after the
review passes.** Work that has survived every gate is durable before
anything else can lose it — a killed worker after that point loses nothing,
because the branch is on the remote.

**Nothing is ever committed to the default branch.** Every item produces a
branch and a proposal, so a wrong answer is reviewable rather than landed.

Everything external is injected — the model transport, the git runner, the
GitHub client, the clock. That is not test scaffolding for its own sake: it
means this module can be exercised end to end against a real temporary
repository with a scripted model, which is the only way to know the loop
works without spending money to find out.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model_client import CapExhausted, ModelClient, RequestRefused
from .work import (
    DONE,
    FAILED,
    PENDING,
    ClaimLost,
    WorkQueue,
    WorkRecord,
    worker_identity,
)

PLANNER = "planner"
IMPLEMENTER = "implementer"
REVIEWER = "reviewer"

#: A unified diff in a fenced block, or bare. Models produce both, and a
#: harness that only accepts one form throws away work that was correct.
_FENCED_DIFF = re.compile(r"```(?:diff|patch)?\s*\n(.*?)```", re.DOTALL)
_DIFF_START = re.compile(r"^(diff --git |--- |\+\+\+ )", re.MULTILINE)

APPROVED = "approved"
REJECTED = "rejected"


class GitError(RuntimeError):
    pass


@dataclass
class Outcome:
    """What happened to one item, and why."""

    item_id: str
    state: str
    reason: str = ""
    branch: str | None = None
    #: What the branch was cut from. Not always the default branch: work
    #: that depends on other work is stacked on it.
    base: str | None = None
    #: The hosted session the agent ran in, when it ran as one. This is
    #: a deep link: it is how a human attaches to the terminal doing the
    #: work, from any device.
    session_id: str | None = None
    pr_url: str | None = None
    verdict: str | None = None
    stages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state == DONE


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def extract_diff(reply: str) -> str | None:
    """Pull a unified diff out of a model reply.

    Prefers a fenced block, because a model that fences its diff has said
    where the patch ends and the commentary begins. Falls back to slicing
    from the first diff header — a correct patch preceded by prose is still
    a correct patch, and rejecting it would discard real work over
    formatting.
    """
    for block in _FENCED_DIFF.findall(reply):
        if _DIFF_START.search(block):
            return _ensure_trailing_newline(block)
    match = _DIFF_START.search(reply)
    if match:
        return _ensure_trailing_newline(reply[match.start() :])
    return None


def _ensure_trailing_newline(text: str) -> str:
    # `git apply` rejects a patch whose final line has no newline, which is
    # an infuriating way to lose an otherwise-good diff.
    return text if text.endswith("\n") else text + "\n"


#: The tolerance ladder, in the order it is tried.
#:
#: MEASURED, not assumed. Against seven realistic ways a model damages a
#: diff, the first rung that rescued each was:
#:
#:     well-formed (3 context lines)   git apply
#:     minimal hunk header             --unidiff-zero
#:     wrong hunk line numbers         --unidiff-zero
#:     trailing whitespace on +        --unidiff-zero
#:     trailing whitespace on -        patch --fuzz
#:     wrong indentation in context    patch --fuzz
#:     overstated hunk counts          nothing
#:     missing leading space           nothing
#:
#: `--ignore-whitespace` and `--3way` rescued **nothing** and are therefore
#: absent: a rung that never fires is a subprocess per failure buying no
#: patches. The two that do the work were missing from the obvious ladder.
APPLY_LADDER: tuple[tuple[str, list[str]], ...] = (
    ("git apply", ["git", "apply", "-"]),
    # Understated/hand-written hunk headers are the single most common
    # model error, and this is what forgives them.
    ("git apply --unidiff-zero", ["git", "apply", "--unidiff-zero", "-"]),
)

#: The fuzzy fallback, DISABLED by default. `patch` matches loosely, which
#: means it can place a hunk in the WRONG location and report success --
#: producing a plausible, wrong result rather than an honest failure.
#:
#: That is not theoretical. The first realistic end-to-end run of this
#: harness hit it: an item whose diff was generated against its
#: dependency's tree was applied to a base without that dependency, and
#: `--fuzz=3` "succeeded" by dropping the change in the nearest similar
#: place. Cheap checks passed, because the misplacement was in code the
#: tests did not cover.
#:
#: It rescues genuine whitespace damage, so it is kept -- but opt-in, and
#: the rung that applied a patch is always recorded, because "this only
#: applied fuzzily" is something a reviewer needs to be told.
FUZZY_RUNG = ("patch --fuzz=3", ["patch", "-p1", "-l", "--fuzz=3", "--no-backup-if-mismatch"])


def apply_diff(repo: Path, diff: str, *, allow_fuzzy: bool = False) -> tuple[bool, str]:
    """Apply a patch, tolerating the ways models damage diffs.

    Returns (applied, how) — `how` names the rung that worked, or the
    collected errors if none did. Which rung was needed is worth recording:
    a fleet suddenly relying on the fuzzy fallback is a fleet whose
    implementer has got worse at diffs.

    `allow_fuzzy` enables the last-resort `patch` rung. Off by default,
    because a misplaced hunk that reports success is worse than a failure.
    """
    ladder = (*APPLY_LADDER, FUZZY_RUNG) if allow_fuzzy else APPLY_LADDER
    errors = []
    for label, args in ladder:
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                args if args[0] != "git" else ["git", "-C", str(repo), *args[1:]],
                input=diff,
                capture_output=True,
                text=True,
                cwd=str(repo),
            )
        except FileNotFoundError:
            # `patch` is not installed everywhere. Missing it costs the last
            # rung, not the whole apply.
            errors.append(f"{label}: not available")
            continue
        if result.returncode == 0:
            return True, label
        detail = (result.stderr or result.stdout).strip().splitlines()
        errors.append(f"{label}: {detail[-1]}" if detail else label)
    return False, "; ".join(dict.fromkeys(errors))


@dataclass
class Checks:
    """Cheap verification, run before the reviewer is paid to have an opinion.

    Commands are the caller's — the harness has no idea what your project
    builds with, and guessing would tie it to one ecosystem.
    """

    commands: Sequence[Sequence[str]] = ()
    timeout: float = 900.0

    def run(self, repo: Path) -> tuple[bool, str]:
        for command in self.commands:
            result = subprocess.run(  # noqa: S603 - caller-supplied argv, no shell
                list(command),
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            if result.returncode != 0:
                tail = (result.stdout + result.stderr).strip().splitlines()[-40:]
                return False, f"`{' '.join(command)}` failed:\n" + "\n".join(tail)
        return True, ""


PLAN_PROMPT = """\
You are planning one unit of work. Do not write code yet.

{brief}

Write a short plan: which files you expect to change, what the change is, and
how it will be verified. If the task cannot be done as stated — because it is
ambiguous, contradicts the codebase, or depends on something absent — say so
plainly and explain what is missing instead of inventing a way forward.
"""

IMPLEMENT_PROMPT = """\
Implement this change and reply with a unified diff and nothing else.

{brief}

Your plan:
{plan}

Repository context:
{context}

Reply with a single unified diff (`diff --git` / `---` / `+++` / `@@`) that
applies cleanly at the repository root. No commentary outside the diff.
"""

REVIEW_PROMPT = """\
Review this change. You did not write it.

The task:
{brief}

The diff:
```diff
{diff}
```

Cheap checks: {checks}

Answer with a first line of exactly APPROVED or REJECTED, then your reasoning.
Reject if the change does not do what the task asked, breaks something, or
claims to do more than it does. Approving work that does not do what was
asked is the expensive failure here; an unnecessary rejection costs one
retry.
"""


class Executor:
    """Drives work items through the model roles."""

    def __init__(
        self,
        queue: WorkQueue,
        client: ModelClient,
        repo: Path,
        *,
        checks: Checks | None = None,
        github: Any | None = None,
        base_branch: str = "main",
        branch_prefix: str = "harness/",
        allow_fuzzy_apply: bool = False,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        push: bool = True,
        now: Callable[[], float] = time.time,
        context_provider: Callable[[WorkRecord], str] | None = None,
    ) -> None:
        self.queue = queue
        self.client = client
        self.repo = Path(repo)
        self.checks = checks or Checks()
        self.github = github
        self.base_branch = base_branch
        self.branch_prefix = branch_prefix
        self.allow_fuzzy_apply = allow_fuzzy_apply
        self.on_event = on_event
        self.push = push
        self.now = now
        self.context_provider = context_provider or (lambda _record: "")
        self.owner = worker_identity()

    # ------------------------------------------------------------- driving

    def run_once(self) -> Outcome | None:
        """Claim one item and take it as far as it goes. None if nothing is
        available."""
        record = self.queue.claim(self.owner)
        if record is None:
            return None
        try:
            outcome = self._execute(record)
        except ClaimLost as exc:
            # Deliberately no release: the item is not ours to finish. The
            # new owner is working on it right now, and reporting anything
            # here would overwrite a live claim.
            self._emit(record, "claim_lost", detail=str(exc))
            return None
        except CapExhausted as exc:
            # Out of budget. Hand the item back untouched rather than
            # burning an attempt on something that was never tried.
            self._emit(record, "budget_exhausted", detail=str(exc))
            self.queue.release(record.item_id, PENDING, error=f"budget: {exc}", owner=self.owner)
            raise
        except Exception as exc:  # noqa: BLE001 - one item must not kill the loop
            self._emit(record, "error", detail=str(exc))
            self.queue.release(record.item_id, FAILED, error=str(exc), owner=self.owner)
            return Outcome(record.item_id, FAILED, reason=str(exc))
        self.queue.release(
            record.item_id,
            outcome.state,
            error=outcome.reason or None,
            branch=outcome.branch,
            pr_url=outcome.pr_url,
            owner=self.owner,
        )
        return outcome

    def run(self, limit: int | None = None) -> list[Outcome]:
        """Work items until there are none left, or `limit` is reached."""
        outcomes: list[Outcome] = []
        while limit is None or len(outcomes) < limit:
            try:
                outcome = self.run_once()
            except CapExhausted:
                # The endpoint is parked; continuing would just re-park it.
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
        """
        if not self.queue.heartbeat(record.item_id, self.owner):
            raise ClaimLost(
                f"{record.item_id} is no longer owned by {self.owner}; "
                "its lease expired and another worker re-claimed it"
            )

    def _execute(self, record: WorkRecord) -> Outcome:
        outcome = Outcome(record.item_id, FAILED)
        self._emit(record, "started")

        # 1. Plan. Cheap, once per item, and the highest-leverage call.
        plan = self._call(record, PLANNER, PLAN_PROMPT.format(brief=record.brief))
        outcome.stages.append("plan")
        self._keepalive(record)

        # 2. Implement.
        reply = self._call(
            record,
            IMPLEMENTER,
            IMPLEMENT_PROMPT.format(
                brief=record.brief,
                plan=plan,
                context=self.context_provider(record),
            ),
        )
        outcome.stages.append("implement")
        diff = extract_diff(reply)
        if not diff:
            self._emit(record, "no_diff")
            outcome.reason = "the implementer returned no diff"
            return outcome

        # 3. Apply, on a branch of its own, based on whatever this item
        #    actually depends on.
        branch = f"{self.branch_prefix}{record.item_id.lower()}"
        base, stacked_on = self._base_for(record)
        self._prepare_branch(branch, base)
        outcome.branch = branch
        outcome.base = base
        if stacked_on:
            self._emit(record, "stacked", detail=f"based on {base} ({stacked_on})")
        applied, how = apply_diff(self.repo, diff, allow_fuzzy=self.allow_fuzzy_apply)
        outcome.stages.append("apply")
        if not applied:
            self._emit(record, "apply_failed", detail=how)
            outcome.reason = f"the diff did not apply: {how}"
            self._abandon_branch(branch)
            return outcome
        self._emit(record, "applied", detail=how)
        self._keepalive(record)

        # 4. Cheap checks BEFORE the expensive reviewer call. Paying a model
        #    to tell us the build is broken is paying the dearest gate to
        #    catch what the cheapest one already did.
        passed, failure = self.checks.run(self.repo)
        outcome.stages.append("checks")
        if not passed:
            self._emit(record, "checks_failed", detail=failure[:2000])
            outcome.reason = failure
            self._abandon_branch(branch)
            return outcome
        self._emit(record, "checks_passed")
        self._keepalive(record)

        # 5. Review, by a different role (and ideally a different vendor).
        verdict_text = self._call(
            record,
            REVIEWER,
            REVIEW_PROMPT.format(
                brief=record.brief,
                diff=diff[:20000],
                checks="passed" if passed else failure,
            ),
        )
        outcome.stages.append("review")
        verdict = APPROVED if verdict_text.strip().upper().startswith("APPROVED") else REJECTED
        outcome.verdict = verdict
        self._emit(record, f"review_{verdict}", detail=verdict_text[:2000])
        if verdict != APPROVED:
            outcome.reason = f"review rejected: {verdict_text.strip()[:500]}"
            self._abandon_branch(branch)
            return outcome

        # 6. Commit, and make it durable before anything else can lose it.
        self._commit(record, verdict_text)
        outcome.stages.append("commit")
        if self.push:
            run_git(self.repo, "push", "-u", "origin", branch)
            outcome.stages.append("push")
            self._emit(record, "pushed", detail=branch)

        # 7. Propose. Never land: a wrong answer must stay reviewable.
        if self.github is not None and record.issue:
            outcome.pr_url = self._open_pr(record, branch, verdict_text, base)
            outcome.stages.append("pr")

        outcome.state = DONE
        self._emit(record, "done", detail=outcome.pr_url or branch)
        return outcome

    # ------------------------------------------------------------- helpers

    def _call(self, record: WorkRecord, role: str, prompt: str) -> str:
        self._emit(record, "calling", detail=role)
        try:
            response = self.client.call(role, [{"role": "user", "content": prompt}])
        except RequestRefused as exc:
            raise RuntimeError(f"{role} refused: {exc}") from exc
        return _text_of(response.body)

    def _base_for(self, record: WorkRecord) -> tuple[str, str | None]:
        """Which branch this item's work should sit on top of.

        An item that depends on another was almost certainly written against
        that other item's result. Basing it on the default branch instead
        means applying a diff to a tree missing the very change it assumes,
        which either fails outright or — worse, with fuzzy matching on —
        succeeds in the wrong place.

        So dependent work is STACKED: it branches from the dependency's
        branch, and its pull request targets that branch rather than the
        base. The stack unwinds naturally as each PR merges.

        Only one dependency can be stacked on. With several unmerged
        dependency branches there is no single correct base without merging
        them, so the first is used and the fact is reported rather than
        hidden.
        """
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

    def _prepare_branch(self, branch: str, base: str | None = None) -> None:
        run_git(self.repo, "checkout", base or self.base_branch)
        run_git(self.repo, "checkout", "-B", branch)

    def _abandon_branch(self, branch: str) -> None:
        """Throw away a failed attempt cleanly.

        Without this a failed patch stays in the working tree and the next
        item inherits it, so one bad diff quietly contaminates everything
        after it.
        """
        run_git(self.repo, "checkout", "--", ".", check=False)
        run_git(self.repo, "clean", "-fd", check=False)
        run_git(self.repo, "checkout", self.base_branch, check=False)
        run_git(self.repo, "branch", "-D", branch, check=False)

    def _commit(self, record: WorkRecord, verdict: str) -> None:
        run_git(self.repo, "add", "-A")
        message = (
            f"{record.title}\n\n"
            f"{record.brief.strip()[:1500]}\n\n"
            f"Reviewer verdict:\n{verdict.strip()[:1500]}\n\n"
            f"harness-item: {record.item_id}\n"
        )
        run_git(self.repo, "commit", "-m", message)

    def _open_pr(self, record: WorkRecord, branch: str, verdict: str, base: str) -> str | None:
        github = self.github
        if github is None:  # pragma: no cover - guarded by the caller
            return None
        body = (
            f"{record.brief.strip()[:3000]}\n\n"
            f"---\n\n**Reviewer verdict**\n\n{verdict.strip()[:3000]}\n\n"
            f"Closes #{record.issue}\n"
        )
        try:
            url = github.create_pr(
                title=record.title,
                body=body,
                head=branch,
                base=base,
            )
            return str(url) if url else None
        except Exception as exc:  # noqa: BLE001 - a PR failure must not lose the work
            self._emit(record, "pr_failed", detail=str(exc))
            return None

    def _emit(self, record: WorkRecord, stage: str, detail: str | None = None) -> None:
        if self.on_event is None:
            return
        # Telemetry is never load-bearing: a broken sink must not fail an
        # item that otherwise succeeded.
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
                }
            )


def _text_of(body: Any) -> str:
    """Best-effort extraction of assistant text from a provider response.

    Handles the OpenAI and Anthropic shapes, and falls back to the raw body
    — a reply this build cannot parse is still evidence, and discarding it
    would turn a parsing gap into a silent empty answer.
    """
    import json as _json

    if isinstance(body, bytes):
        body = body.decode(errors="replace")
    if not isinstance(body, str):
        return str(body)
    try:
        payload = _json.loads(body)
    except ValueError:
        return body
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") or {}
            if isinstance(message, dict) and message.get("content"):
                return str(message["content"])
        content = payload.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and first.get("text"):
                return str(first["text"])
    return body
