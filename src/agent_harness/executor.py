"""Run one work item: plan it, implement it, check it, review it, propose it.

This is the loop. It claims an item, drives three model roles over it, and
either opens a pull request or hands the item back — and it records every
step as an event so the dashboard can answer "what is it doing?" without
anyone opening a log.

The ordering is the part worth defending:

    plan -> implement -> apply -> CHEAP CHECK -> checkpoint -> draft PR -> review

**Cheap checks run before the expensive reviewer call.** A patch that does
not apply, or does not compile, cannot be worth a review; spending a model
call to be told so is paying the most expensive gate to catch what the
cheapest one already caught.

**The branch is checkpointed and pushed before the expensive reviewer
call.** Work that has passed the cheap gates is durable before a reviewer
timeout or worker death can lose it. The pull request remains a draft and is
explicitly marked unreviewed until the reviewer approves it.

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
import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import attempts as A
from .budgets import SPEND as BUDGET_SPEND
from .budgets import WALL_CLOCK as BUDGET_WALL_CLOCK
from .budgets import Budget, BudgetExceeded, Spend, budget_for
from .budgets import check as budget_check
from .graph import LOCAL_WORK
from .model_client import CapExhausted, ModelClient, RequestRefused, RetryExhausted
from .outcomes import (
    BUDGET_EXHAUSTED,
    CLAIM_LOST,
    COMPLETED,
    CONTEXT_UNAVAILABLE,
    CRASHED,
    DECIDED,
    DEPENDENCY_INVALIDATED,
    ESCALATE,
    ESCALATED,
    FAIL,
    FIX_AVAILABLE,
    ITEM_SPEND,
    ITEM_WALL_CLOCK,
    NO_TARGET,
    PASSED,
    PATCH_REJECTED,
    PROVIDER_EXHAUSTED,
    REFUSED,
    RETRY,
    REVIEW_REJECTED,
    WITHHELD,
    WORKER_ERROR,
    CheckResult,
    Stop,
    stop_for,
)
from .work import (
    BLOCKED,
    DEFAULT_PROJECT,
    DONE,
    FAILED,
    HELD,
    PENDING,
    ClaimLost,
    LeaseHeartbeat,
    WorkQueue,
    WorkRecord,
    worker_identity,
)

#: Which reason kind each ceiling reports. Named here so the executor never
#: has to branch on a ceiling string.
BUDGET_REASON = {BUDGET_WALL_CLOCK: ITEM_WALL_CLOCK, BUDGET_SPEND: ITEM_SPEND}

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
    #: What stopped it, in the taxonomy `outcomes.py` defines. `state` is the
    #: queue's word for where the item ended up; this is why. They are
    #: different questions — `failed` covers both a reviewer's rejection and a
    #: crashed worker, and those want different responses from a human.
    stop: Stop | None = None
    #: A question this item is waiting on, when it asked rather than guessed.
    #: Carried rather than acted on immediately because a hold **keeps the
    #: claim** (D12) — so it replaces the release at the end of an attempt
    #: instead of following one, which would have found the item unclaimed.
    ask: str = ""

    @property
    def ok(self) -> bool:
        return self.state == DONE

    @property
    def disposition(self) -> str:
        return self.stop.disposition if self.stop is not None else ""

    @property
    def reason_kind(self) -> str:
        return self.stop.reason_kind if self.stop is not None else ""


def _planner_artefact(planner: PlannerResult) -> dict[str, Any]:
    """The planner's answer, as something a later process can read back.

    Every field, because a partial round trip would silently downgrade a
    resumed attempt's context — and a resumed attempt that plans worse than a
    fresh one is a resumption nobody should want.
    """
    return {
        "plan": planner.plan,
        "targets": [
            {
                "path": target.path,
                "reason": target.reason,
                "usable": target.usable,
                "uncertainty": target.uncertainty,
            }
            for target in planner.targets
        ],
        "cannot_identify_target": planner.cannot_identify_target,
        "uncertainties": list(planner.uncertainties),
    }


def _planner_from(artefact: Mapping[str, Any]) -> PlannerResult:
    return PlannerResult(
        plan=str(artefact.get("plan") or ""),
        targets=tuple(
            PlannerTarget(
                path=str(entry.get("path") or ""),
                reason=str(entry.get("reason") or ""),
                usable=bool(entry.get("usable", True)),
                uncertainty=entry.get("uncertainty"),
            )
            for entry in artefact.get("targets") or []
        ),
        cannot_identify_target=artefact.get("cannot_identify_target"),
        uncertainties=tuple(artefact.get("uncertainties") or []),
    )


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


#: How much repository the implementer is shown. Big enough that a small
#: project arrives whole, small enough not to dominate the prompt.
#:
#: A **default**, not a constant: a repository whose files are larger than this
#: cannot be worked on at all until it is raised, and no number chosen here is
#: right for every project. `run --context-budget` and `HARNESS_CONTEXT_BUDGET`
#: set it.
DEFAULT_CONTEXT_BUDGET = 60_000

#: Why a file was left out, as tokens rather than prose. The difference is
#: load-bearing: running out of room for context nobody asked for is normal,
#: and being unable to supply a file the planner *named* means the implementer
#: is about to be asked to edit something it cannot see.
TARGET_OVER_BUDGET = "named target exceeds remaining content budget"
BUDGET_SPENT = "content budget exhausted"

#: What a coordinator says that a worker should act on. `decision` is absent
#: because that is what an **escalation** is recorded as, addressed to an
#: operator: telling the implementer that a human has been asked to decide
#: invites it to guess the answer, which is the opposite of what escalating is
#: for. A test asserts the escalation body never reaches a prompt.
GUIDANCE_TYPES = frozenset({"answer", "correction"})

#: Who a message has to be for. Unaddressed traffic is the room talking to
#: everyone; anything with an explicit recipient list is for those recipients,
#: and a worker reading someone else's mail is how an escalation to a human
#: becomes a hint to a model.
GUIDANCE_AUDIENCE = frozenset({"worker", "agent", "all"})

#: How much of the room reaches the prompt. A conversation is unbounded and a
#: prompt is not; the most recent few are the ones that still describe the
#: item as it now stands.
GUIDANCE_MESSAGES = 3
GUIDANCE_CHARS = 4000

GUIDANCE_PROMPT = """
The project coordinator has looked at this item and said the following. It is
advice from something that can see the whole project, not an instruction that
overrides the task or the checks — and it may be wrong. Where it conflicts
with the brief, the brief wins.
{messages}
"""

#: The stages a worker reports into the coordination room. Every one is a
#: setback a person watching the run would react to, and every one is named
#: in `COORDINATION-PLANE.md` §6 as an oversight trigger. Successes are
#: absent on purpose: a coordinator reading `checks_passed` pays a model to
#: conclude that nothing is wrong.
#: One room per work item, per COORDINATION-PLANE.md §5. Everything used to
#: land in the general room, so a coordinator polling one project read every
#: item's traffic interleaved — unreadable past two concurrent items, and
#: impossible to reason about per item.
ITEM_ROOM_PREFIX = "work:"


def item_room(item_id: str) -> str:
    """The room one item's traffic belongs in."""
    return f"{ITEM_ROOM_PREFIX}{item_id}"


def _signature(stage: str, evidence: Mapping[str, Any]) -> str:
    """A stable fingerprint of *what went wrong*, ignoring when.

    Two failures with the same signature are the same failure happening
    again, which is the single most useful thing a coordinator can know and
    the one thing it could not previously tell: shown three identical
    observations it answered three times, at model prices, having no notion
    it had seen any of them before.

    Deliberately excludes anything that moves on its own — attempt numbers,
    episode, timestamps, worker identity — because a fingerprint that changes
    every attempt makes every repetition look novel, which is the bug.
    """
    import hashlib

    parts = [stage]
    for key in ("check", "outcome", "verdict", "rungs", "base"):
        parts.append(str(evidence.get(key, "")))
    # Output is included but normalised: build tools print paths and line
    # numbers that are stable, and durations and temp directories that are
    # not. Keeping the first lines keeps the diagnosis and drops the noise.
    output = str(evidence.get("output") or evidence.get("review") or "")
    parts.append("\n".join(output.splitlines()[:20]))
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


def _attachments(paths: Sequence[str]) -> tuple[Any, ...]:
    """Real files, referenced rather than inlined.

    An attachment is content-addressed on purpose: bodies are kept forever and
    blobs are not, so the message keeps enough to find and verify one and
    nothing else. A file that has since gone is simply not attached — a
    coordinator that cannot open it is no worse off than one that was never
    told, and a broken reference is worse than both.
    """
    import hashlib

    from .coordination import Attachment

    found = []
    for raw in paths:
        path = Path(str(raw))
        try:
            body = path.read_bytes()
        except OSError:
            continue
        found.append(
            Attachment(
                digest=f"sha256:{hashlib.sha256(body).hexdigest()}",
                size=len(body),
                media_type="text/x-diff" if path.suffix == ".patch" else "text/plain",
                location=str(path),
            )
        )
    return tuple(found)


LEDGER_TRIGGERS = frozenset(
    {
        "checks_failed",
        "apply_failed",
        "review_rejected",
        "no_diff",
        "no_changes",
        "context_unavailable",
        "held",
        "budget_exhausted",
        "agent_timeout",
    }
)

#: Files that are never worth spending the budget on, whatever their size.
_UNINTERESTING = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".woff", ".woff2")


@dataclass(frozen=True)
class PlannerTarget:
    """One file the planner expects the implementer to change."""

    path: str
    reason: str
    usable: bool = True
    uncertainty: str | None = None


@dataclass(frozen=True)
class PlannerResult:
    """Validated planner guidance, never authority to edit a path."""

    plan: str
    targets: tuple[PlannerTarget, ...] = ()
    cannot_identify_target: str | None = None
    uncertainties: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextPolicy:
    """Configured repository-context policy.

    Core deliberately has no list of workload build directories. Deployments
    can name generated paths here; an empty file needs no such convention and
    is omitted automatically unless the planner explicitly targets it.
    """

    budget: int = DEFAULT_CONTEXT_BUDGET
    generated_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextSelection:
    """The observable result of selecting implementer context."""

    text: str
    files: tuple[str, ...]
    budget: int
    characters: int
    truncated: bool
    omitted: tuple[tuple[str, str], ...]
    fallback_relevance: bool
    targets: tuple[PlannerTarget, ...]


def parse_planner_result(reply: str) -> PlannerResult:
    """Parse the planner's structured response without trusting it.

    A legacy or malformed answer remains useful as prose, but it is explicitly
    marked uncertain and supplies no target paths. This keeps a formatting
    failure from becoming arbitrary file-read authority.
    """
    candidate = reply.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        raw = json.loads(candidate)
    except (TypeError, ValueError):
        detail = "planner response was not valid structured JSON"
        return PlannerResult(
            plan=reply.strip() or "The planner returned no implementation plan.",
            cannot_identify_target=detail,
            uncertainties=(detail,),
        )
    if not isinstance(raw, dict):
        detail = "planner response must be a JSON object"
        return PlannerResult(plan=str(raw), cannot_identify_target=detail, uncertainties=(detail,))

    uncertainties: list[str] = []
    plan = raw.get("plan")
    if not isinstance(plan, str) or not plan.strip():
        uncertainties.append("planner result has no non-empty `plan`")
        plan = "The planner returned no usable implementation plan."
    cannot = raw.get("cannot_identify_target")
    if cannot is not None and (not isinstance(cannot, str) or not cannot.strip()):
        uncertainties.append("`cannot_identify_target` must be null or a non-empty string")
        cannot = "planner did not provide a valid target-identification result"

    targets: list[PlannerTarget] = []
    target_rows = raw.get("targets", [])
    if not isinstance(target_rows, list):
        uncertainties.append("planner `targets` must be a list")
        target_rows = []
    for index, row in enumerate(target_rows):
        if not isinstance(row, dict):
            uncertainties.append(f"planner target {index} is not an object")
            continue
        path, reason = row.get("path"), row.get("reason")
        if not isinstance(path, str) or not path.strip():
            uncertainties.append(f"planner target {index} has no non-empty path")
            continue
        if not isinstance(reason, str) or not reason.strip():
            uncertainties.append(f"planner target {path!r} has no non-empty reason")
            continue
        targets.append(PlannerTarget(path.strip(), reason.strip()))
    if cannot and targets:
        uncertainties.append("planner named targets while also saying it could not identify one")
    if not cannot and not targets:
        uncertainties.append("planner neither named a target nor explained why it could not")
        cannot = "planner did not identify a target"
    return PlannerResult(
        plan=plan.strip(),
        targets=tuple(targets),
        cannot_identify_target=cannot,
        uncertainties=tuple(uncertainties),
    )


def _normalise_target(repo: Path, path: str) -> tuple[str | None, str | None]:
    """Return a repository-relative target, or an explicit uncertainty."""
    raw = Path(path)
    if raw.is_absolute():
        return None, "absolute paths are outside the repository contract"
    root = repo.resolve()
    candidate = (root / raw).resolve(strict=False)
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None, "path escapes the repository"
    normalised = relative.as_posix()
    if normalised in {"", "."}:
        return None, "path identifies the repository, not a file"
    return normalised, None


def _configured_generated(path: str, configured: Sequence[str]) -> bool:
    candidate = Path(path)
    return any(
        candidate == Path(prefix) or Path(prefix) in candidate.parents for prefix in configured
    )


def select_repo_context(
    repo: Path,
    record: Any = None,
    *,
    planner: PlannerResult | None = None,
    policy: ContextPolicy | None = None,
    ref: str | None = None,
) -> ContextSelection:
    """Select target-first repository context and retain selection evidence."""
    policy = policy or ContextPolicy()
    planner = planner or PlannerResult(
        plan="No structured planner result was supplied.",
        cannot_identify_target="no structured planner result was supplied",
    )
    read: Callable[[str], str]
    try:
        if ref:
            tracked = [
                p for p in run_git(repo, "ls-tree", "-r", "--name-only", ref).splitlines() if p
            ]
            read = lambda path: run_git(repo, "show", f"{ref}:{path}")  # noqa: E731
        else:
            tracked = [p for p in run_git(repo, "ls-files").splitlines() if p.strip()]
            read = lambda path: (repo / path).read_text(encoding="utf-8")  # noqa: E731
    except GitError:
        return ContextSelection("", (), policy.budget, 0, False, (), False, ())
    if not tracked:
        return ContextSelection("", (), policy.budget, 0, False, (), False, ())

    tracked_set = set(tracked)
    validated: list[PlannerTarget] = []
    named: list[str] = []
    omitted: list[tuple[str, str]] = []
    for target in planner.targets:
        path, problem = _normalise_target(repo, target.path)
        if problem or path is None:
            validated.append(PlannerTarget(target.path, target.reason, False, problem))
            omitted.append((target.path, problem or "invalid target"))
            continue
        if path not in tracked_set:
            problem = "target is missing from the selected repository revision"
            validated.append(PlannerTarget(path, target.reason, False, problem))
            omitted.append((path, problem))
            continue
        try:
            if not ref and not (repo / path).is_file():
                raise OSError("target is not a regular file")
        except OSError as exc:
            problem = str(exc)
            validated.append(PlannerTarget(path, target.reason, False, problem))
            omitted.append((path, problem))
            continue
        validated.append(PlannerTarget(path, target.reason))
        if path not in named:
            named.append(path)

    # Planner targets are first. Surrounding context is a deterministic path
    # relevance fallback, not a claim that the heuristic knows the answer.
    brief = " ".join(
        [
            str(getattr(record, "title", "") or ""),
            str(getattr(record, "brief", "") or ""),
            planner.plan,
            *(f"{target.path} {target.reason}" for target in validated),
        ]
    ).lower()
    terms = {term for term in re.findall(r"[a-z0-9][a-z0-9_.-]+", brief) if len(term) > 2}

    def relevance(path: str) -> tuple[int, int, str]:
        components = set(re.findall(r"[a-z0-9][a-z0-9_.-]+", path.lower()))
        return (-len(terms & components), len(path), path)

    fallback = sorted(
        (
            path
            for path in tracked
            if path not in named and not path.lower().endswith(_UNINTERESTING)
        ),
        key=relevance,
    )

    parts: list[str] = []
    supplied: list[str] = []
    spent = 0
    truncated = False
    for path in [*named, *fallback]:
        explicit = path in named
        if not explicit and _configured_generated(path, policy.generated_paths):
            omitted.append((path, "configured generated artefact"))
            continue
        # Reading a worktree symlink follows it, which could turn an innocent
        # context selection into arbitrary access outside the repository. Git
        # stores the link target, not the pointed-to bytes; the implementer
        # needs neither, so links stay visible in the listing only.
        if (repo / path).is_symlink():
            omitted.append((path, "symbolic link content is not supplied"))
            continue
        try:
            body = read(path)
        except (OSError, UnicodeDecodeError, GitError):
            omitted.append((path, "binary or unreadable"))
            continue
        if not explicit and not body:
            omitted.append((path, "empty file"))
            continue
        block = f"--- {path} ---\n{body}\n"
        if spent + len(block) > policy.budget:
            truncated = True
            omitted.append((path, TARGET_OVER_BUDGET if explicit else BUDGET_SPENT))
            continue
        parts.append(block)
        supplied.append(path)
        spent += len(block)

    # Listing comes after file content and is bounded by the same budget. It
    # is orientation, not a reason to evict a file the planner named.
    heading = "Files in this repository:\n"
    listing = heading
    for path in tracked:
        line = f"  {path}\n"
        if spent + len(listing) + len(line) > policy.budget:
            truncated = True
            break
        listing += line
    if spent + len(listing) <= policy.budget:
        parts.append(listing)
        spent += len(listing)

    return ContextSelection(
        text="\n".join(parts),
        files=tuple(supplied),
        budget=policy.budget,
        characters=spent,
        truncated=truncated,
        omitted=tuple(omitted),
        fallback_relevance=bool(fallback),
        targets=tuple(validated),
    )


def repo_context(
    repo: Path,
    record: Any = None,
    *,
    budget: int = DEFAULT_CONTEXT_BUDGET,
    ref: str | None = None,
) -> str:
    """What the repository looks like, for a model that cannot read it.

    The implementer is asked for a diff that "applies cleanly at the
    repository root". Without this it was asked that while being shown
    nothing, so it could not write context lines -- it did not know what they
    were -- and emitted hunks claiming the file was empty. `--unidiff-zero`
    then inserted them at line one, above module docstrings and imports, with
    every check still green.

    Files named in the brief come first and whole: they are the ones being
    edited, and a patch against a file the model has only seen the name of is
    a patch written blind. The rest fills the remaining budget smallest-first,
    on the grounds that many small files tell a model more about a codebase's
    conventions than one large one.

    `ref` is the commit the patch will actually be applied to, and reading
    from it rather than the working tree is the whole point: the tree still
    holds the *previous* item's branch at this stage, so a model shown the
    tree writes context lines for a file the patch will never meet.
    """
    return select_repo_context(
        repo,
        record,
        policy=ContextPolicy(budget=budget),
        ref=ref,
    ).text


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


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

#: Lines that legitimately sit between hunks: file headers and git metadata.
_BETWEEN_HUNKS = (
    "diff --git ",
    "--- ",
    "+++ ",
    "index ",
    "old mode ",
    "new mode ",
    # Every one of these is emitted by `git diff` and none of them was
    # recognised, so a patch that CREATED a file -- the ordinary way to add a
    # module -- could be rejected as corrupt with `line inside a hunk starts
    # with 'n'`. Found by running a real backlog: the first item that tried to
    # add a file failed here.
    "new file mode ",
    "deleted file mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
)


@dataclass(frozen=True)
class PatchProblem:
    """Something structurally wrong with a model's diff.

    `fatal` separates damage no rung of the ladder can repair -- a truncated
    or mis-prefixed hunk, which is what `git apply` reports as "corrupt patch
    at line N" -- from damage that IS routinely repaired, notably hunk headers
    whose line counts are wrong. Only the first kind is worth refusing over;
    refusing the second would throw away work `--unidiff-zero` rescues every
    day.
    """

    line: int
    detail: str
    fatal: bool = True

    def __str__(self) -> str:
        return f"line {self.line}: {self.detail}"


def validate_diff(diff: str) -> list[PatchProblem]:
    """Structural problems in a unified diff, cheapest possible check.

    Not a re-implementation of `git apply` — it says nothing about whether the
    patch matches the tree, which is the *repository's* business. It answers
    the different question the failure log could not: was the model's output
    even a diff? A patch that fails because it was truncated mid-hunk and one
    that fails because it was written against a different base look identical
    in `git apply: error: corrupt patch at line 549`, and the fix for them is
    not the same.
    """
    problems: list[PatchProblem] = []
    lines = diff.splitlines()
    if not any(_HUNK_HEADER.match(line) for line in lines):
        # No hunks usually means the model answered in prose, which is the
        # failure this check is for. But a rename, a delete or a mode change
        # is a complete patch with nothing to hunk, so the presence of a real
        # file header is what separates "no diff" from "no content".
        if any(line.startswith("diff --git ") for line in lines) and any(
            line.startswith(("rename from ", "deleted file mode ", "old mode ", "new file mode "))
            for line in lines
        ):
            return []
        return [PatchProblem(1, "no hunk header (`@@ -a,b +c,d @@`) anywhere in the reply")]

    old_left = new_left = 0
    hunk_line = 0
    counting = False
    for number, line in enumerate(lines, start=1):
        header = _HUNK_HEADER.match(line)
        if header:
            if counting and (old_left or new_left):
                # Another hunk follows, so nothing was cut off: the header
                # simply over-declared. `recount_hunks` derives the right
                # counts from the body, so this is reported and not refused --
                # which is what this class of damage was always documented to
                # be, and was not.
                problems.append(
                    PatchProblem(
                        hunk_line,
                        f"hunk ends {old_left} source and {new_left} result line(s) "
                        "short of what its header declares",
                        fatal=False,
                    )
                )
            old_left = int(header.group(2) or 1)
            new_left = int(header.group(4) or 1)
            hunk_line, counting = number, True
            continue
        if not counting:
            continue
        if not (old_left or new_left):
            # The hunk is complete. Anything after it is commentary or the
            # next file's header, and neither is this hunk's problem.
            counting = False
            continue
        if line.startswith("\\"):  # "\ No newline at end of file"
            continue
        if line.startswith(" ") or line == "":
            old_left, new_left = max(0, old_left - 1), max(0, new_left - 1)
        elif line.startswith("-"):
            old_left = max(0, old_left - 1)
        elif line.startswith("+"):
            new_left = max(0, new_left - 1)
        elif line.startswith(_BETWEEN_HUNKS):
            # The next file starts while this hunk is unfinished: the header
            # claimed more lines than the hunk contains. A whole file follows,
            # so the reply did not stop -- the counts are wrong and derivable,
            # so this is recounted rather than refused.
            problems.append(
                PatchProblem(
                    hunk_line,
                    f"hunk header declares {old_left} more source and {new_left} more "
                    "result line(s) than the hunk contains",
                    fatal=False,
                )
            )
            counting = False
        else:
            problems.append(
                PatchProblem(
                    number,
                    "line inside a hunk starts with "
                    f"{line[:1]!r}, not ' ', '+' or '-': {line[:60]!r}",
                )
            )
            counting = False
    if counting and (old_left or new_left):
        # Short at end of input, which is two different faults wearing one
        # symptom, and they must not be treated alike.
        #
        # A context line counts on BOTH sides. So a header that over-counted
        # trailing context -- the common model error, observed on four
        # consecutive runs -- is short by the SAME amount on each side, its
        # body is complete and internally consistent, and `recount_hunks`
        # repairs it exactly.
        #
        # A reply genuinely cut off mid-hunk loses a mix of `+`, `-` and
        # context, so the two shortfalls differ. Recounting that would make
        # the header agree with a partial body and the patch would apply --
        # landing, say, a deletion whose replacement never arrived. That is
        # the "succeeds in the wrong place" failure the ladder refuses
        # elsewhere, so it stays fatal.
        recountable = old_left == new_left
        problems.append(
            PatchProblem(
                hunk_line,
                f"the last hunk supplies {old_left} fewer source and {new_left} fewer result "
                "line(s) than its header declares, and the patch then ends"
                + ("" if recountable else " — the reply was cut off mid-hunk"),
                fatal=not recountable,
            )
        )
    return problems


def recount_hunks(diff: str) -> str:
    """Rewrite each hunk header's line counts from the body beneath it.

    A model that miscounts its own hunk is the single most common way a
    correct patch is thrown away. Measured: four consecutive runs of one item
    produced a byte-for-byte correct body under `@@ -1,10 +1,21 @@` for a file
    with nine lines -- one too many on each side, because the trailing newline
    was counted as a line. `git apply` parses by the declared counts, reaches
    the end of input a line early, and reports `corrupt patch`. Neither
    `--unidiff-zero` nor `patch --fuzz` helps: there is nothing to be tolerant
    *with* once the parser has run out of input.

    This is safe in a way the tolerance ladder is not, and the distinction
    matters. The body is the truth -- the counts are a derivable property of
    it, so recomputing them guesses at nothing. Contrast a `@@ -0,0` header
    against a file that has content (#133), where the header carried the only
    statement about *where* the lines belong: there, nothing could be
    recomputed, and the patch is refused instead.

    Headers that are already right are rewritten to the same values, so this
    is a no-op on a well-formed diff.
    """
    lines = diff.splitlines(keepends=True)
    out: list[str] = []
    pending: int | None = None  # index in `out` of a header awaiting its counts
    old = new = 0

    def flush() -> None:
        if pending is None:
            return
        header = _HUNK_HEADER.match(out[pending])
        if header is None:  # pragma: no cover - only reached via a bad index
            return
        # Only when they actually disagree. A header written `@@ -1 +1 @@` is
        # correct and means one line; rewriting it to `@@ -1,1 +1,1 @@` would
        # change a byte of every well-formed patch for no reason.
        declared_old = int(header.group(2) or 1)
        declared_new = int(header.group(4) or 1)
        if (declared_old, declared_new) == (old, new):
            return
        tail = out[pending][header.end() :]
        out[pending] = f"@@ -{header.group(1)},{old} +{header.group(3)},{new} @@{tail}"

    for line in lines:
        if _HUNK_HEADER.match(line):
            flush()
            out.append(line)
            pending, old, new = len(out) - 1, 0, 0
            continue
        if pending is not None:
            if line.startswith("\\"):
                # "\ No newline at end of file" annotates the line above and
                # counts towards neither side. Ending the hunk here would
                # recount it short.
                out.append(line)
                continue
            if line.startswith("+"):
                new += 1
            elif line.startswith("-"):
                old += 1
            elif line.startswith((" ", "\n")) or line.strip() == "":
                # A context line. An empty line in a diff is a context line
                # whose single leading space some models drop.
                old += 1
                new += 1
            else:
                # A file header or trailing commentary: this hunk is over.
                flush()
                pending = None
        out.append(line)
    flush()
    return "".join(out)


#: How much of a damaged patch goes into the event, and how much of it around
#: the first problem. Bounded on purpose: the event store is a record of what
#: happened, not a place to put a four-thousand-line diff, and the damage
#: starts where the first problem is.
DIAGNOSTIC_CONTEXT_LINES = 3
MAX_DIAGNOSTIC_CHARS = 2000


def _bounded(problems: list[PatchProblem], diff: str) -> str:
    """The findings, plus the few lines where the patch went wrong."""
    lines = diff.splitlines()
    anchor = next((p for p in problems if p.fatal), problems[0])
    low = max(1, anchor.line - DIAGNOSTIC_CONTEXT_LINES)
    high = min(len(lines), anchor.line + DIAGNOSTIC_CONTEXT_LINES)
    excerpt = "\n".join(f"{n:>5}| {lines[n - 1][:200]}" for n in range(low, high + 1))
    findings = "\n".join(str(p) for p in problems[:5])
    return f"{findings}\n\n{excerpt}"[:MAX_DIAGNOSTIC_CHARS]


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


def _header_path(line: str) -> str:
    """The path off a `---`/`+++` line, without git's trailing timestamp."""
    return line[4:].split("\t", 1)[0].strip()


def _stripped(path: str) -> str | None:
    """The file `-p1` writes to: one leading component off, as git strips it.

    None when that cannot be read off the header -- a quoted, escaped path or
    one with no prefix at all. Not knowing which file a hunk targets is a
    reason to say nothing about it, never a reason to guess.
    """
    if path.startswith('"') or "/" not in path:
        return None
    return path.partition("/")[2] or None


def unplaceable_hunks(repo: Path, diff: str) -> list[str]:
    """Hunks whose header claims a placement the working tree disproves.

    `@@ -0,0 +...` says the old file has no lines at this point: the shape of
    a file creation. Against a file that exists with content in it, that is
    not a header with its counts slightly wrong -- it is false about the tree,
    and it carries no information about *where* the new lines belong.

    Measured, live: an implementer emitted `@@ -0,0 +1,4 @@` against a
    `calc.py` opening with a module docstring. `git apply` refuses it, but
    `--unidiff-zero` -- the rung that exists to forgive hand-written headers
    -- takes the absent context at face value and inserts the lines at line 1,
    above the docstring, which becomes a bare string expression and leaves
    `__doc__` as None. The checks stay green, because Python does not care
    where a string literal sits, so the item goes to review as a success with
    damage the harness introduced rather than the model.

    The difference from the headers the ladder is *for* is that this one is
    checkable without applying anything, and no rung can recover what it threw
    away. A creation the patch declares honestly (`--- /dev/null`, or `new
    file mode`) is not this: git decides for itself whether the file is
    already there, and refuses either way.

    It does cost one honest case, measured: `git diff -U0` emits exactly this
    header for a line prepended to a file that has content. Nothing in the
    patch distinguishes that from a model that meant "somewhere in this file"
    and wrote the emptiest header it knew, and one of the two is damage no
    gate can see -- so both are refused and the implementer is asked again.
    A prepend that carries one line of context is accepted by the first rung.
    """
    problems: list[str] = []
    target: str | None = None
    previous = ""
    creating = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            target, creating = None, False
        elif line.startswith("new file mode "):
            creating = True
        elif line.startswith("--- "):
            creating = creating or _header_path(line) == "/dev/null"
        elif line.startswith("+++ ") and previous.startswith("--- "):
            # Paired with its `---` so that a *removed* line reading like a
            # file header cannot re-target the hunks that follow it.
            target = None if creating else _stripped(_header_path(line))
        else:
            header = _HUNK_HEADER.match(line)
            if header is not None and target and (header.group(1), header.group(2)) == ("0", "0"):
                path = repo / target
                body = path.read_bytes() if path.is_file() else b""
                if body:
                    count = body.count(b"\n") + (0 if body.endswith(b"\n") else 1)
                    problems.append(
                        f"{target}: `{header.group(0)}` says the file is empty, but it has "
                        f"{count} line(s) — say where these lines go, with a line of "
                        "context or a header naming the line they follow"
                    )
        previous = line
    return list(dict.fromkeys(problems))


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
    unplaceable = unplaceable_hunks(repo, diff)
    if unplaceable:
        # Refused before the first rung rather than after the last, because
        # the rungs below would "succeed" at this: `--unidiff-zero` inserts a
        # `-0,0` hunk at line 1 of whatever file it names, and reports a clean
        # apply. That is the same trade as the fuzzy rung above -- tolerance
        # bought by discarding the evidence of where a hunk belongs -- and it
        # is refused for the same reason, one rung earlier because here the
        # patch is provably wrong before anything is run.
        return False, "; ".join(unplaceable)
    ladder = (*APPLY_LADDER, FUZZY_RUNG) if allow_fuzzy else APPLY_LADDER
    # Every rung gets the patch as written first, then with its hunk counts
    # recomputed. Recounting is not tolerance -- the counts are derivable from
    # the body, so nothing is guessed -- but it goes second so a diff that was
    # already correct is applied exactly as the model wrote it.
    recounted = recount_hunks(diff)
    variants = [("", diff)] if recounted == diff else [("", diff), (" (recounted)", recounted)]
    errors = []
    for label, args in ladder:
        for suffix, candidate in variants:
            try:
                result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    args if args[0] != "git" else ["git", "-C", str(repo), *args[1:]],
                    input=candidate,
                    capture_output=True,
                    text=True,
                    cwd=str(repo),
                )
            except FileNotFoundError:
                # `patch` is not installed everywhere. Missing it costs the
                # rung, not the whole apply -- and not the other variant.
                errors.append(f"{label}: not available")
                break
            if result.returncode == 0:
                return True, label + suffix
            detail = (result.stderr or result.stdout).strip().splitlines()
            errors.append(f"{label}{suffix}: {detail[-1]}" if detail else label + suffix)
    return False, "; ".join(dict.fromkeys(errors))


@dataclass
class Checks:
    """Cheap verification, run before the reviewer is paid to have an opinion.

    Commands are the caller's — the harness has no idea what your project
    builds with, and guessing would tie it to one ecosystem.

    `run` returns a typed `CheckResult`. It still unpacks as `(ok, detail)`,
    so a caller that only wants the bit keeps working; a caller that wants to
    know *which kind* of not-ok this is can now ask.

    **The classification is structural, never semantic.** The harness reads
    how the subprocess ended — timed out, could not be started, ran out of
    disk, exited non-zero — and never reads a project's output to guess what
    its failure meant. Guessing at another ecosystem's messages is precisely
    how a generic harness stops being one, and a misread failure here would
    turn a real defect into a retry.
    """

    commands: Sequence[Sequence[str]] = ()
    timeout: float = 900.0
    #: `command index or program name -> argv that is believed to clear it`.
    #: Declared by the caller, because only the caller knows that
    #: `ruff format` fixes what `ruff format --check` reports. Recorded when
    #: the check fails; **never run** — see `outcomes.CheckResult.fix`.
    fixes: Mapping[str, Sequence[str]] = field(default_factory=dict)

    def _fix_for(self, command: Sequence[str]) -> tuple[str, ...]:
        for key in (" ".join(command), command[0] if command else ""):
            found = self.fixes.get(key)
            if found:
                return tuple(found)
        return ()

    def run(self, repo: Path) -> CheckResult:
        for command in self.commands:
            argv = list(command)
            label = " ".join(argv)
            try:
                result = subprocess.run(  # noqa: S603 - caller-supplied argv, no shell
                    argv,
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                # The question was not answered. That is not the same as the
                # answer being no, and holding it against the item would fail
                # sound work because a machine was busy.
                return CheckResult(
                    RETRY,
                    f"`{label}` did not finish within {self.timeout:g}s",
                    command=tuple(argv),
                )
            except OSError as exc:
                # The program is missing or not executable. No diff fixes
                # that and no retry clears it; it is a deployment fault
                # wearing a check's clothes.
                return CheckResult(
                    ESCALATE,
                    f"`{label}` could not be started: {exc}",
                    command=tuple(argv),
                )
            if result.returncode != 0:
                tail = (result.stdout + result.stderr).strip().splitlines()[-40:]
                detail = f"`{label}` failed:\n" + "\n".join(tail)
                if is_disk_exhaustion(detail):
                    # The machine is out of room. Every subsequent item fails
                    # the same way, and each one pays a planner and an
                    # implementer first.
                    return CheckResult(ESCALATE, detail, command=tuple(argv))
                fix = self._fix_for(argv)
                if fix:
                    return CheckResult(FIX_AVAILABLE, detail, command=tuple(argv), fix=fix)
                return CheckResult(FAIL, detail, command=tuple(argv))
        return PASSED


def is_disk_exhaustion(detail: str) -> bool:
    """Whether a command reported an explicit filesystem-capacity failure."""
    lowered = detail.lower()
    return any(
        signature in lowered
        for signature in ("no space left on device", "disk quota exceeded", "enospc")
    )


PLAN_PROMPT = """\
You are planning one unit of work. Do not write code yet.

{brief}
{listing}
Reply with one JSON object and no commentary:

{{
  "plan": "a short implementation and verification plan",
  "targets": [{{"path": "relative/path", "reason": "why it is a target"}}],
  "cannot_identify_target": null
}}

Order targets by importance. If the task is ambiguous, contradicts the
codebase, or depends on something absent, return an empty targets list and put
the reason in `cannot_identify_target` instead of inventing a path.
"""

#: The repository's tracked paths, for the role whose entire job is naming
#: some of them. Paths only, never content: the planner has to know what
#: exists, and the implementer is the one that needs to read it.
PLAN_LISTING_PROMPT = """
Files in this repository:
{listing}
"""

IMPLEMENT_PROMPT = """\
Implement this change and reply with a unified diff and nothing else.

{brief}

Your plan:
{plan}

Repository context:
{context}
{unavailable}{checks}{guidance}{prior}
Reply with a single unified diff (`diff --git` / `---` / `+++` / `@@`) that
applies cleanly at the repository root. No commentary outside the diff.
"""

#: Supporting targets the budget could not carry. Named rather than silently
#: dropped, and paired with an instruction not to edit them — a model that
#: knows a file exists but has not read it will otherwise write a plausible
#: hunk against it, which is the failure this whole path exists to prevent.
STARVED_PROMPT = """
The planner also named these files, and they were too large to include. You
have NOT seen them, so do not change them — if the task cannot be done without
changing one, say so instead of guessing at its contents:
{paths}
"""

#: The gates, told to the writer as well as the marker. Empty when a project
#: configured none, so a run without checks reads exactly as it did before.
CHECKS_PROMPT = """
These commands run on your diff before any reviewer sees it, and a non-zero
exit refuses the change:
{commands}
"""

#: Why the previous attempt was refused. **This is a new attempt informed by
#: the last refusal, not a resumption of it** — the item is re-planned against
#: the current brief exactly as before (D11), and nothing here is treated as
#: progress. Without it a retry repeats the same mistake blind: measured on
#: rdpapp, three of four attempts at one item were refused by the same
#: formatter, each one unaware the last had been.
PRIOR_FAILURE_PROMPT = """
A previous attempt at this task was refused. You are starting again from the
current brief, not continuing that attempt — but do not reproduce the fault:

{error}
"""

REVIEW_PROMPT = """\
Review this change. You did not write it, and your job is not to be agreeable.

Assume it is wrong until the diff shows otherwise. Most changes that fail
review fail because they do something *adjacent* to what was asked, or claim
more than they did — not because they are obviously broken.

The task:
{brief}

The diff, which is the change **in full** — it was produced from the
repository, so any file not appearing in it is unchanged:
```diff
{diff}
```
{context}
{checks}

## Answer

First line exactly APPROVED or REJECTED. Then, in order:

1. **What I verified** — the specific things you actually checked against the
   task. If you cannot name any, that is a REJECTED.
2. **What I could not verify** — anything the change claims that *what you were
   given* does not show. Say it, do not assume it.

   **Read the files above before writing this section.** They are the touched
   files in full, at their post-change state, so questions like "is this the
   only caller", "is there another path that bypasses it" and "does this
   signature fit its callers" are answerable — answer them. Only a file listed
   as not included is genuinely unavailable to you, and "the diff does not
   show it" is not a reason when the file does.
3. **Why** — one paragraph.

## Reject if

- It does not do what the task asked, or does more than the task asked.
- It claims an effect the change does not demonstrate.
- It changes something unrelated, however small.

## Do not reject if

- **The task's scope is narrower than the problem.** Scope is the task's to
  set, not yours. Where the task says what is *out* of scope, judge the change
  against what it asked for; that the wider problem remains afterwards is not a
  fault in this work. Note it under "what I could not verify" so a person sees
  it — a note is the right size for that, and a rejection is not.
- The task cannot be judged from what you were given — but only after you have
  looked at what you were given. Wanting evidence that was in front of you is
  not grounds to reject, and neither is wanting evidence the task did not ask
  for.

Approving work that does not do what was asked is the expensive failure here:
it reaches a pull request, a human reads it as reviewed, and the cost lands
much later. An unnecessary rejection costs one retry.
"""

#: What the checks were and who ran them. The reviewer used to be handed the
#: bare word `passed`, which reads as the author's claim about their own work
#: — and a reviewer told to assume the work is wrong will discount it, as one
#: did: "I cannot verify that the file still type-checks, because no build
#: output was provided beyond the claim 'Checks: passed'." The harness ran
#: them, on this exact tree, before the reviewer was called. Saying so is a
#: fact, not a reassurance.
REVIEW_CHECKS_PROMPT = """
These commands were run by the harness on this exact tree, after the change
was applied and before you were called. All of them exited zero:
{commands}
"""

#: When a project has configured none. Said plainly, because "no checks" is
#: information a reviewer should weigh, and silence would let it assume there
#: were some.
REVIEW_NO_CHECKS_PROMPT = """
This project has no checks configured, so nothing has been run against this
change. You are the only gate it has.
"""

#: The files the diff touched, as they now stand. Without this the reviewer is
#: asked whether a change is wired in correctly while holding only the change,
#: and "the task cannot be judged from what you were given" — which the prompt
#: lists as grounds to reject — becomes true by construction rather than by
#: fault. What is missing is named, because a reviewer that does not know its
#: view is partial will treat it as complete.
REVIEW_CONTEXT_PROMPT = """
The files it touched, as they now stand:
{files}{omitted}
"""


def review_context(repo: Path, diff: str, budget: int) -> str:
    """The touched files as they now stand, for the reviewer.

    The reviewer is asked whether a change is wired in where it should be
    and whether anything unrelated moved, and was given only the change.
    Measured on rdpapp: two of the three reasons in a rejection were "the
    diff does not show whether …", which no diff ever can. That is a gate
    rejecting for the shape of its own prompt.

    The same budget bounds it, and a file too large to include is **named
    as absent** rather than quietly left out — a reviewer that believes a
    partial view is complete is worse than one that knows it is partial.
    """
    paths: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            path = line[4:].strip()
            path = path[2:] if path.startswith("b/") else path
            if path and path not in paths:
                paths.append(path)
    if not paths:
        return ""

    blocks: list[str] = []
    missing: list[str] = []
    spent = 0
    for path in paths:
        try:
            body = (repo / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            missing.append(f"  {path} — could not be read")
            continue
        block = f"--- {path} ---\n{body}\n"
        if spent + len(block) > budget:
            missing.append(f"  {path} — {len(body)} characters, too large to include")
            continue
        blocks.append(block)
        spent += len(block)
    omitted = "\nNot included, so you have not seen them:\n" + "\n".join(missing) if missing else ""
    return REVIEW_CONTEXT_PROMPT.format(files="\n".join(blocks), omitted=omitted)


def review_checks_prompt(commands: Sequence[Sequence[str]]) -> str:
    """Which commands passed, and that the harness ran them."""
    commands = [" ".join(command) for command in commands if command]
    if not commands:
        return REVIEW_NO_CHECKS_PROMPT
    return REVIEW_CHECKS_PROMPT.format(commands="\n".join(f"  {command}" for command in commands))


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
        context_policy: ContextPolicy | None = None,
        ledger: Any | None = None,
        ask_when_uncertain: bool = False,
        artifacts: Path | None = None,
        project_id: str = DEFAULT_PROJECT,
        durability: str | None = None,
    ) -> None:
        self.queue = queue
        #: How often this worker makes progress durable. None takes the
        #: queue's own setting. A policy rather than a constant because the
        #: deterministic demo wants the cheapest and a fleet wants the
        #: strictest, and one number cannot be both.
        self.durability = durability
        # Which project's queue this worker serves. Items are keyed by
        # (project_id, item_id), so a worker that does not say which project
        # it is for claims from `default` -- which is nobody's project once
        # more than one exists.
        self.project_id = project_id
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
        # Defaults to showing the repository. An empty context meant asking a
        # model for a patch that "applies cleanly" against files it had never
        # seen, which it cannot do and which nothing said out loud.
        self.context_provider = context_provider
        self.context_policy = context_policy or ContextPolicy()
        #: Where this worker reports setbacks so a coordinator can read them.
        #: None is the ordinary case and changes nothing.
        self.ledger = ledger
        #: Whether a worker that cannot proceed asks instead of failing.
        #: Off by default: a hold keeps the claim, so an unwatched fleet is
        #: better served by failing fast than by tying up a worker until the
        #: hold expires.
        self.ask_when_uncertain = ask_when_uncertain
        #: Which go at this item the worker is on. `attempts` cannot serve:
        #: `requeue` resets it to zero deliberately, so three consecutive
        #: failures of the same item all reported as attempt 0, deduplicated
        #: to a single message, and a coordinator saw one bad day instead of
        #: the pattern it exists to notice. Measured, not theorised.
        self._episode = 0
        #: The ref the current item's patch will be applied to. Resolved
        #: before the implementer is called, because that is what it needs to
        #: be looking at.
        self._base: str | None = None
        # Where a patch that could not be applied is kept. Supplied, never
        # guessed: the core owns no directory layout. Without it the reply is
        # gone the moment the item fails, and the only way to see what the
        # model actually produced is to pay for it again.
        self.artifacts = Path(artifacts) if artifacts is not None else None
        self.owner = worker_identity()
        self._partial: Outcome | None = None
        self._heartbeat: LeaseHeartbeat | None = None
        #: This attempt's spend, folded from the model client's own events.
        #: Reset per item in `run_once`.
        self._spend = Spend()
        self._budget_cache: Budget | None = None
        self._unenforceable_said: set[str] = set()

    # ------------------------------------------------------------- driving

    def _execute_with_heartbeat(self, record: WorkRecord) -> Outcome:
        """One attempt, with its claim kept alive for the whole of it.

        A check suite is perfectly capable of outlasting the lease on its own,
        so the heartbeat covers the stages and not merely the gaps between
        them.
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
        """Claim one item and take it as far as it goes. None if nothing is
        available."""
        record = self.queue.claim(self.owner, project_id=self.project_id)
        if record is None:
            return None
        # Per item, not per worker: two items in one process must not share a
        # spend total or inherit each other's ceilings.
        self._spend = Spend()
        self._budget_cache = None
        self._unenforceable_said = set()
        try:
            outcome = self._execute_with_heartbeat(record)
        except ClaimLost as exc:
            # Deliberately no release: the item is not ours to finish. The
            # new owner is working on it right now, and reporting anything
            # here would overwrite a live claim.
            self._emit(record, "claim_lost", detail=str(exc), error_class=CLAIM_LOST)
            # Not ours to record either. Whatever `exit` mode buffered belongs
            # to an attempt somebody else now owns.
            self.queue.attempts_log.discard()
            return None
        except CapExhausted as exc:
            # Out of budget. Hand the item back untouched rather than
            # burning an attempt on something that was never tried.
            self._emit(record, "budget_exhausted", detail=str(exc))
            self._persist_spend(record)
            self.queue.attempts_log.flush(self.durability)
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
        except BudgetExceeded as exc:
            # A budget stop is not a failure of the work and not an exhausted
            # attempt ladder. It is a policy decision this deployment made,
            # and it needs a person to raise the ceiling or let the item go --
            # so it lands in `blocked`, with the ceiling named.
            #
            # It deliberately does NOT park the endpoint. `WINDOW_CAP` and
            # `TERMINAL_CAP` are a provider's statement about our budget and
            # belong in the never-retry set; this is our statement about one
            # item, and parking a shared endpoint because one item was
            # expensive would be that conflation made real.
            self._emit(
                record,
                "budget_exceeded",
                detail=exc.detail,
                error_class=BUDGET_REASON[exc.ceiling],
            )
            self._persist_spend(record)
            self.queue.attempts_log.flush(self.durability)
            stop = Stop(ESCALATED, BUDGET_REASON[exc.ceiling], detail=exc.detail)
            partial = self._partial_for(record)
            self.queue.release(
                record.item_id,
                stop.state or BLOCKED,
                error=exc.detail,
                branch=partial.branch if partial else None,
                pr_url=partial.pr_url if partial else None,
                owner=self.owner,
                consume_attempt=False,
                disposition=stop.disposition,
                reason_kind=stop.reason_kind,
                project_id=self.project_id,
            )
            if partial is not None:
                partial.state = BLOCKED
                partial.reason = exc.detail
                partial.stop = stop
                return partial
            return Outcome(record.item_id, BLOCKED, reason=exc.detail, stop=stop)
        except RetryExhausted as exc:
            self._emit(record, "retry_exhausted", detail=str(exc), error_class=exc.kind)
            self.queue.attempts_log.flush(self.durability)
            self._persist_spend(record)
            partial = self._partial_for(record)
            # A provider that would not answer is not this item's fault, so
            # the disposition says `withheld`. The attempt is still consumed,
            # exactly as before: whether it should be is D11, and Stage K
            # names distinctions rather than re-deciding accounting.
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
            self.queue.attempts_log.flush(self.durability)
            self._persist_spend(record)
            partial = self._partial_for(record)
            # Crashed, not refused. Nothing was decided about the item; an
            # exception happened while deciding, and a human reading the
            # queue should be looking at the harness rather than the diff.
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
        # Whatever `exit` mode buffered lands now, however the attempt ended.
        # A failed attempt's record is as useful as a successful one's -- more
        # so, since it is the one somebody will read.
        self.queue.attempts_log.flush(self.durability)
        self._persist_spend(record)
        # A decision was reached, so the attempt stops being resumable. Only a
        # worker that was KILLED leaves a position to continue from; one that
        # reached a verdict decided, and resuming into that decision would make
        # an operator's retry replay the rejection it was retrying.
        #
        # `withheld` is the exception and the point: a spend cap, or a provider
        # that would not answer, decided nothing about the item, so the next
        # claim continues rather than restarts. That is where the cost saving
        # under real-world failure actually lives.
        if outcome.disposition in DECIDED:
            self.queue.attempts_log.seal(self.project_id, record.item_id, record.attempts)
        # `consume_attempt` follows the disposition, so an item nobody
        # attempted and an item waiting on a person do not spend one. Every
        # disposition that consumed an attempt before still does.
        if outcome.ask:
            # A held item is not released: the claim, the worktree and the
            # context are exactly what an answer resumes into (D12). Failing
            # to hold is not fatal — the item falls through to the ordinary
            # release below and is retried, which is what happened before any
            # of this existed.
            with contextlib.suppress(Exception):
                self._hold(record, outcome)
                return outcome
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

    def _hold(self, record: WorkRecord, outcome: Outcome) -> None:
        """Ask, in the room and on the item, and keep the claim.

        Two halves of one feature that had never been introduced:
        `message_type: "question"` existed in the ledger and no worker sent
        one, while `holds.py` independently implemented an item waiting on a
        person. A question in the room survives everything; a hold survives
        the worker dying. Neither alone is what a stuck agent needs.
        """
        hold = self.queue.hold(
            record.item_id,
            question=outcome.ask,
            reason=outcome.reason or "the worker could not proceed without an answer",
            owner=self.owner,
            project_id=self.project_id,
        )
        self._emit(record, "held", detail=outcome.ask)
        if self.ledger is None:
            return
        from .coordination import Submission

        with contextlib.suppress(Exception):
            self.ledger.append(
                Submission(
                    project_id=self.project_id,
                    room_id=item_room(record.item_id),
                    sender_id=self.owner,
                    sender_role="worker",
                    message_type="question",
                    body=outcome.ask,
                    item_id=record.item_id,
                    attempt=record.attempts,
                    idempotency_key=(
                        f"ask:{self.owner}:{self.project_id}:{record.item_id}:{self._episode}"
                    ),
                    # The resume token is deliberately absent. Answering is an
                    # action through the command service, which looks the token
                    # up itself — a token in a room is a token anything that can
                    # read the room may spend.
                    payload={"stage": "held", "expires_at": hold.expires_at},
                )
            )

    def _persist_spend(self, record: WorkRecord) -> None:
        """Add this attempt's cost to the item's running total, once.

        Accumulated across attempts because the ceiling bounds the *item*: a
        total that reset on every re-claim would bound one attempt, and an
        item that crashes in a loop would then never reach any ceiling at all.
        """
        if not (self._spend.priced or self._spend.unpriced):
            return
        spent, unpriced = self._spend.usd, self._spend.unpriced
        self._spend = Spend()
        with contextlib.suppress(Exception):
            self.queue.add_spend(record.item_id, spent, unpriced, project_id=self.project_id)

    def _partial_for(self, record: WorkRecord) -> Outcome | None:
        partial = self._partial
        if partial is not None and partial.item_id == record.item_id:
            return partial
        return None

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

    def _budget(self, record: WorkRecord) -> Budget:
        if self._budget_cache is None:
            self._budget_cache = budget_for(self.queue.get_project(self.project_id), record)
        return self._budget_cache

    def _budget_stop(self, record: WorkRecord) -> None:
        """Refuse to go past a ceiling this item declared.

        Called at the boundaries that already exist rather than from a timer.
        A budget stop must never kill work in flight: stopping mid-stage
        destroys the context and leaves a half-finished worktree, which is the
        reasoning `work.py` already gives about pause semantics.
        """
        budget = self._budget(record)
        if not budget.bounded:
            return
        started = record.first_started_at or self.now()
        verdict = budget_check(budget, elapsed=self.now() - started, spend=self._spend)
        for ceiling, why in verdict.unenforceable:
            # Said once per stop, and said as a fact rather than a warning
            # nobody reads: a ceiling that cannot be checked is not a ceiling
            # that was met.
            if ceiling not in self._unenforceable_said:
                self._unenforceable_said.add(ceiling)
                self._emit(record, "budget_unenforceable", detail=why)
        if verdict.exceeded is not None:
            raise verdict.exceeded

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
        # The heartbeat proves the process is alive; it proves nothing about
        # progress. This is the boundary where "alive" and "within budget"
        # stop being the same question.
        self._budget_stop(record)

    def _context_for(self, record: WorkRecord, planner: PlannerResult) -> ContextSelection:
        if self.context_provider is not None:
            text = self.context_provider(record)
            return ContextSelection(
                text=text,
                files=(),
                budget=len(text),
                characters=len(text),
                truncated=False,
                omitted=(),
                fallback_relevance=False,
                targets=planner.targets,
            )
        return select_repo_context(
            self.repo,
            record,
            planner=planner,
            policy=self.context_policy,
            ref=self._base,
        )

    def _resume_point(
        self, record: WorkRecord, log: A.AttemptLog, attempt: int, mode: str
    ) -> A.Resume:
        """Where this attempt starts, and whether it may start there at all.

        Two things happen here that are easy to conflate. The first is reading
        the durable position. The second is **checking that the position is
        still an answer to the question that was asked** — `WorkQueue.add`
        rewrites `title`, `brief` and `depends_on` on live claimed rows, so a
        worker can be briefed from one revision and judged against another.
        An attempt whose brief moved has its position thrown away and starts
        again from the planner, loudly.
        """
        resume = log.resume(self.project_id, record.item_id, attempt)
        if resume.brief is None:
            log.begin(
                self.project_id,
                record.item_id,
                attempt,
                title=record.title,
                brief=record.brief,
                depends_on=list(record.depends_on),
                admitted_revision=record.admitted_revision,
            )
            return resume

        pinned = resume.brief
        moved = pinned.brief != record.brief or list(pinned.depends_on) != list(record.depends_on)
        if moved and resume.resumable:
            self._emit(
                record,
                "brief_moved",
                detail=(
                    f"{record.item_id} was briefed at graph revision "
                    f"{pinned.admitted_revision} and the plan has changed since; the "
                    f"durable position at {resume.at!r} is discarded and the attempt "
                    "starts again from the planner against the current brief"
                ),
            )
            log.abandon(self.project_id, record.item_id, attempt)
            log.begin(
                self.project_id,
                record.item_id,
                attempt,
                title=record.title,
                brief=record.brief,
                depends_on=list(record.depends_on),
                admitted_revision=record.admitted_revision,
            )
            return A.Resume(attempt=attempt)
        if resume.open_intents:
            # `sync` durability caught a crash mid-effect. Said out loud
            # because the alternative -- a push that may or may not have
            # landed, discovered later by someone reading git -- is the exact
            # thing that mode is bought for.
            self._emit(
                record,
                "effect_unconfirmed",
                detail=(
                    "began and did not confirm: " + ", ".join(resume.open_intents) + "; "
                    "the effect may have half-happened and is re-attempted"
                ),
            )
        return resume

    def _execute(self, record: WorkRecord) -> Outcome:
        outcome = Outcome(record.item_id, FAILED)
        # Retained so a reviewer-side exception can still return the durable
        # checkpoint's branch/PR to the queue and caller.
        self._partial = outcome
        self._emit(record, "started")

        log = self.queue.attempts_log
        attempt = record.attempts
        mode = log.mode_for(self.durability)
        resume = self._resume_point(record, log, attempt, mode)

        # Resolved first, and deliberately before the implementer is called:
        # the working tree still holds the previous item's branch, so a model
        # shown the tree writes context lines for a file its patch will never
        # meet. The branch itself is still cut later, so an item that produces
        # no usable diff leaves no branch behind.
        base, stacked_on = self._base_for(record)
        self._base = base

        # 1. Plan. Cheap, once per item, and the highest-leverage call.
        planner = _planner_from(resume.artefact(A.PLANNED)) if resume.skips(A.PLANNED) else None
        if planner is not None:
            self._emit(record, "resumed", detail=f"{A.PLANNED} (mode={mode})")
        else:
            planner_reply = self._call(
                record,
                PLANNER,
                PLAN_PROMPT.format(brief=record.brief, listing=self._repo_listing()),
            )
            planner = parse_planner_result(planner_reply)
            log.record(
                self.project_id,
                record.item_id,
                attempt,
                A.PLANNED,
                _planner_artefact(planner),
                admitted_revision=record.admitted_revision,
                mode=mode,
            )
        self._emit(
            record,
            "planner_targets",
            detail=json.dumps(
                {
                    "targets": [
                        {"path": target.path, "reason": target.reason} for target in planner.targets
                    ],
                    "cannot_identify_target": planner.cannot_identify_target,
                    "uncertainties": list(planner.uncertainties),
                },
                sort_keys=True,
            ),
        )
        outcome.stages.append("plan")
        self._keepalive(record)

        # 2. Implement. Skipped whole when a durable diff already exists: this
        #    is the call the stage exists to stop re-paying for.
        if resume.skips(A.IMPLEMENTED):
            stored = str(resume.artefact(A.IMPLEMENTED).get("diff") or "")
            outcome.stages.append("implement")
            self._emit(record, "resumed", detail=f"{A.IMPLEMENTED} (mode={mode})")
            return self._from_diff(
                record, outcome, planner, stored, base, stacked_on, resume, log, attempt, mode
            )

        if planner.cannot_identify_target and self.ask_when_uncertain:
            # The planner has said, in as many words, that the task is
            # ambiguous, contradicts the codebase, or depends on something
            # absent. Today the harness records that and carries on to pay an
            # implementer that has no target — which is guessing, and
            # `AGENTS.md` is explicit that the ambiguous case must reach a
            # person rather than be guessed at.
            #
            # Opt-in, because a hold keeps the claim: a fleet nobody is
            # watching would rather fail an item in seconds than tie a worker
            # up until the hold expires.
            outcome.ask = (
                f"The planner could not identify what to change: {planner.cannot_identify_target}"
            )
            outcome.reason = outcome.ask
            outcome.stop = Stop(ESCALATED, NO_TARGET, detail=outcome.ask, consumes_attempt=False)
            outcome.state = HELD
            return outcome

        context = self._context_for(record, planner)
        self._emit(
            record,
            "context_selected",
            detail=json.dumps(
                {
                    "planner_targets": [
                        {
                            "path": target.path,
                            "reason": target.reason,
                            "usable": target.usable,
                            "uncertainty": target.uncertainty,
                        }
                        for target in context.targets
                    ],
                    "files": list(context.files),
                    "character_budget": context.budget,
                    "characters": context.characters,
                    "truncated": context.truncated,
                    "omitted": [
                        {"path": path, "reason": reason} for path, reason in context.omitted
                    ],
                    "fallback_relevance": context.fallback_relevance,
                },
                sort_keys=True,
            ),
        )
        starved = [path for path, reason in context.omitted if reason == TARGET_OVER_BUDGET]
        # The planner is asked to order its targets by importance, so the first
        # usable one is the file the work is *in*; the rest are supporting.
        # Losing the first is fatal — the implementer would be asked to change
        # a file it has not been shown, and it will answer rather than refuse,
        # because models do not decline for want of evidence. Losing a
        # supporting file is not fatal, and blocking the item over it would
        # make a large repository unworkable for the sake of a file nobody was
        # going to edit.
        primary = next((target.path for target in context.targets if target.usable), None)
        if starved and (primary is None or primary in starved):
            outcome.reason = (
                "the planner's target(s) "
                + ", ".join(f"{path} ({self._size_of(path)})" for path in starved)
                + f" do not fit the context budget of {context.budget} characters, "
                "so the implementer would be asked to change a file it cannot see. "
                "Raise --context-budget (or HARNESS_CONTEXT_BUDGET), or split the file."
            )
            self._emit(record, "context_unavailable", detail=outcome.reason)
            outcome.stop = Stop(
                ESCALATED,
                CONTEXT_UNAVAILABLE,
                detail=outcome.reason,
                state=BLOCKED,
                # A ceiling this deployment set stopped it; the item did not
                # fail. Spending an attempt on the same file every claim would
                # exhaust it for a condition no attempt can change.
                consumes_attempt=False,
            )
            outcome.state = BLOCKED
            return outcome
        reply = self._call(
            record,
            IMPLEMENTER,
            IMPLEMENT_PROMPT.format(
                brief=record.brief,
                plan=planner.plan,
                context=context.text,
                # The reviewer is told what the checks said; the implementer
                # was never told what they are. So a diff is refused by a
                # formatter the model was not shown, which costs an attempt and
                # a model call to discover something the harness knew before it
                # asked. Naming the commands is not weakening the gate: the
                # gate still runs, and still refuses.
                checks=self._checks_prompt(),
                guidance=self._guidance(record),
                prior=self._prior_failure_prompt(record),
                unavailable=self._starved_prompt(starved),
            ),
        )
        outcome.stages.append("implement")
        diff = extract_diff(reply)
        if not diff:
            self._emit(record, "no_diff")
            outcome.reason = "the implementer returned no diff"
            outcome.stop = Stop(REFUSED, NO_TARGET, detail=outcome.reason)
            return outcome
        log.record(
            self.project_id,
            record.item_id,
            attempt,
            A.IMPLEMENTED,
            {"diff": diff},
            admitted_revision=record.admitted_revision,
            mode=mode,
        )
        return self._from_diff(
            record, outcome, planner, diff, base, stacked_on, resume, log, attempt, mode
        )

    def _from_diff(
        self,
        record: WorkRecord,
        outcome: Outcome,
        planner: PlannerResult,
        diff: str,
        base: str,
        stacked_on: str | None,
        resume: A.Resume,
        log: A.AttemptLog,
        attempt: int,
        mode: str,
    ) -> Outcome:
        """Everything from a diff in hand to a verdict.

        Split out because it is the whole of the resumable half: an attempt
        that already has a durable diff enters here rather than at the planner,
        and one that has just paid for a diff falls through into it. Two
        entrances, one body — a second copy of the apply/check/review sequence
        would be a second place for the gates to drift.
        """
        # 3. Parse the patch BEFORE touching git. A reply that is not a
        #    well-formed diff is a model failure, and it is worth saying so:
        #    it looks identical to a patch written against the wrong base once
        #    it reaches `git apply: error: corrupt patch at line 549`, and the
        #    two are fixed in completely different places.
        problems = validate_diff(diff)
        fatal = [p for p in problems if p.fatal]
        if problems:
            self._emit(
                record,
                "patch_malformed" if fatal else "patch_suspect",
                detail=_bounded(problems, diff),
            )
        if fatal:
            kept = self._preserve_patch(record, diff)
            outcome.stages.append("parse")
            outcome.reason = (
                "the implementer did not produce a usable diff — "
                + "; ".join(str(p) for p in fatal[:3])
                + (f" (patch kept at {kept})" if kept else "")
            )
            outcome.stop = Stop(REFUSED, PATCH_REJECTED, detail=outcome.reason)
            return outcome

        # 4. Apply, on a branch of its own, based on whatever this item
        #    actually depends on.
        branch = f"{self.branch_prefix}{record.item_id.lower()}"
        outcome.branch = branch
        outcome.base = base

        if resume.skips(A.CHECKPOINTED):
            # The commit already exists in git, which is the strongest durable
            # artefact there is. Re-applying would either conflict with itself
            # or produce an empty diff; checking the branch out is the resume.
            checkpoint = resume.artefact(A.CHECKPOINTED)
            outcome.pr_url = checkpoint.get("pr_url") or None
            self._emit(record, "resumed", detail=f"{A.CHECKPOINTED} (mode={mode})")
            run_git(self.repo, "checkout", "-q", branch)
            applied_diff = run_git(self.repo, "diff", f"{base}...{branch}") or diff
            outcome.stages.extend(("apply", "checks", "commit"))
            return self._review_stage(
                record, outcome, applied_diff, branch, base, resume, log, attempt, mode
            )

        self._prepare_branch(branch, base)
        if stacked_on:
            self._emit(record, "stacked", detail=f"based on {base} ({stacked_on})")
        applied, how = apply_diff(self.repo, diff, allow_fuzzy=self.allow_fuzzy_apply)
        outcome.stages.append("apply")
        if not applied:
            # The patch parsed, so this is the repository disagreeing with it,
            # not damaged model output. Keep it anyway: whoever diagnoses this
            # needs the diff that failed, and re-generating it costs a call
            # and produces a different patch.
            kept = self._preserve_patch(record, diff)
            self._emit(
                record,
                "apply_failed",
                detail=how + (f"\npatch kept at {kept}" if kept else ""),
                evidence={
                    "base": base,
                    # Which rungs of the tolerance ladder were tried, and what
                    # each said. "corrupt patch at line 22" and "patch does not
                    # apply" are different diagnoses and lead different places.
                    "rungs": how,
                    "files": [str(kept)] if kept else [],
                },
            )
            outcome.reason = f"the diff did not apply to {base}: {how}" + (
                f" (patch kept at {kept})" if kept else ""
            )
            outcome.stop = Stop(REFUSED, PATCH_REJECTED, detail=outcome.reason)
            self._abandon_branch(branch)
            return outcome
        self._emit(record, "applied", detail=how)
        log.record(
            self.project_id,
            record.item_id,
            attempt,
            A.APPLIED,
            {"branch": branch, "base": base, "how": how},
            admitted_revision=record.admitted_revision,
            mode=mode,
        )
        # What landed, not what was proposed. The tolerance ladder exists to
        # rescue malformed patches, so the text a model produced and the change
        # it produced are routinely different -- a `@@ -0,0` hunk that git
        # apply would refuse becomes a real insertion with real context. The
        # reviewer has to see the second one: reviewing the first rejects good
        # work for an artefact of the plumbing, and, worse, makes the gate
        # structurally unable to catch a diff that claims more than it did.
        applied_diff = run_git(self.repo, "diff", "HEAD") or diff
        self._keepalive(record)

        # 5. Cheap checks BEFORE the expensive reviewer call. Paying a model
        #    to tell us the build is broken is paying the dearest gate to
        #    catch what the cheapest one already did.
        checked = self.checks.run(self.repo)
        failure = checked.detail
        outcome.stages.append("checks")
        if not checked.ok:
            stop = stop_for(checked)
            self._emit(
                record,
                "checks_failed",
                detail=failure[:2000],
                evidence={
                    "check": " ".join(checked.command) if checked.command else "",
                    "outcome": checked.outcome,
                    # The tail is where a build tool prints what it objected
                    # to, which is the part a person reads first.
                    "output": failure[-4000:],
                    "fix_declared": " ".join(checked.fix) if checked.fix else "",
                },
                # The gate's own word for what happened, so a client branches
                # on a token rather than on English. `disk_exhausted` is kept
                # exactly as it was: it is a narrower, older fact and the
                # audit layer already counts it.
                error_class=("disk_exhausted" if is_disk_exhaustion(failure) else stop.reason_kind),
            )
            if checked.fix:
                # Recorded, and not run. The item still fails; somebody now
                # knows what would clear it.
                self._emit(
                    record,
                    "fix_available",
                    detail="`" + " ".join(checked.fix) + "` is declared to clear this",
                )
            outcome.reason = failure
            outcome.stop = stop
            outcome.state = stop.state
            self._abandon_branch(branch)
            return outcome
        self._emit(record, "checks_passed")
        # Recorded, though it makes resumption no cheaper: re-running a
        # project's checks is idempotent and costs no model call, so a resumed
        # attempt runs them again rather than trusting a result from a tree
        # that may have been rebuilt. What this row buys is the report — how
        # far an attempt got, for the cost accounting in the evidence.
        log.record(
            self.project_id,
            record.item_id,
            attempt,
            A.CHECKED,
            {},
            admitted_revision=record.admitted_revision,
            mode=mode,
        )
        self._keepalive(record)

        # The graph is re-checked here, at the last cheap point before review
        # spends money and the checkpoint makes anything durable. `claim` checked it
        # once, minutes ago; correcting a plan while work is in flight is a
        # normal thing for an operator to do, and an item that is no longer
        # eligible must not land on the strength of a stale check.
        #
        # Deliberately the same `readiness` call admission made, over the same
        # authoritative graph revision, so the two cannot drift apart. The
        # agent is NOT killed: it has already finished, the branch is
        # abandoned, and the item returns to pending with the reason recorded.
        admission = self.queue.readiness(record.item_id, project_id=self.project_id)
        if not admission.ready:
            outcome.reason = (
                f"{record.item_id} was admitted at graph revision "
                f"{record.admitted_revision} and {admission.explain()}; the candidate is "
                "discarded and the item goes back to pending"
            )
            self._emit(record, "dependency_invalidated", detail=outcome.reason)
            # And the resumable position goes with it. The plan the attempt was
            # briefed with is no longer the plan, so the diff it produced is an
            # answer to a question nobody is asking; resuming into it would be
            # exactly the silent judgement against a newer brief that §7.4
            # forbids.
            log.abandon(self.project_id, record.item_id, attempt)
            outcome.stop = Stop(WITHHELD, DEPENDENCY_INVALIDATED, detail=outcome.reason)
            outcome.state = PENDING
            self._abandon_branch(branch)
            return outcome

        # 6. Checkpoint BEFORE the expensive gate, matching session mode.
        #    Review is the slowest and most failure-prone call. Work that has
        #    passed every cheap gate must survive a worker dying during it,
        #    but must not present itself as reviewed until approval exists.
        self._commit(record, checkpoint=True)
        outcome.stages.append("commit")
        self._emit(record, "checkpointed", detail=branch)
        # Recorded immediately, and BEFORE the external effects below. The
        # commit is the durable artefact; a crash during the push must resume
        # from the commit rather than from the diff.
        log.record(
            self.project_id,
            record.item_id,
            attempt,
            A.CHECKPOINTED,
            {
                "branch": branch,
                "base": base,
                "sha": run_git(self.repo, "rev-parse", "HEAD").strip(),
            },
            admitted_revision=record.admitted_revision,
            mode=mode,
        )
        # Neither of the two effects below is idempotent, so each is bracketed
        # in `sync` mode: a row left in `attempt_intents` is a crash caught in
        # the one window where the effect may have half-happened. In the other
        # modes this is a no-op and the window is simply invisible, which is
        # the honest description of what those modes buy.
        if self.push:
            log.opening(self.project_id, record.item_id, attempt, "push", branch, mode=mode)
            run_git(self.repo, "push", "-u", "origin", branch)
            log.closed(self.project_id, record.item_id, attempt, "push", mode=mode)
            outcome.stages.append("push")
            self._emit(record, "pushed", detail=branch)
        if self.github is not None and record.issue:
            log.opening(self.project_id, record.item_id, attempt, "draft_pr", branch, mode=mode)
            outcome.pr_url = self._open_pr(record, branch, base, draft=True)
            log.closed(self.project_id, record.item_id, attempt, "draft_pr", mode=mode)
            if outcome.pr_url:
                outcome.stages.append("draft-pr")
                self._emit(record, "draft_pr_opened", detail=outcome.pr_url)
                log.record(
                    self.project_id,
                    record.item_id,
                    attempt,
                    A.CHECKPOINTED,
                    {
                        "branch": branch,
                        "base": base,
                        "sha": run_git(self.repo, "rev-parse", "HEAD").strip(),
                        "pr_url": outcome.pr_url,
                    },
                    admitted_revision=record.admitted_revision,
                    mode=mode,
                )

        return self._review_stage(
            record, outcome, applied_diff, branch, base, resume, log, attempt, mode
        )

    def _review_stage(
        self,
        record: WorkRecord,
        outcome: Outcome,
        applied_diff: str,
        branch: str,
        base: str,
        resume: A.Resume,
        log: A.AttemptLog,
        attempt: int,
        mode: str,
    ) -> Outcome:
        """The expensive gate, and what approval buys.

        Reached from two places: an attempt that just checkpointed, and one
        resumed at a checkpoint a previous worker made. Both must run exactly
        the same gate — a resumed attempt that reviewed more cheaply would be
        this stage weakening the thing the whole pipeline exists to protect.
        """
        # 7. Review, by a different role -- and ideally a different vendor,
        #    which `ModelClient.reviewer_independence()` now reports on rather
        #    than leaving to a comment nobody reads.
        if resume.skips(A.REVIEWED):
            # A verdict already exists. Re-asking would spend the most
            # expensive call in the pipeline to re-derive an answer we have,
            # and -- since a model is not deterministic -- might get a
            # different one, which would make a crash a way to shop for a
            # verdict.
            recorded = resume.artefact(A.REVIEWED)
            verdict_text = str(recorded.get("text") or "")
            self._emit(record, "resumed", detail=f"{A.REVIEWED} (mode={mode})")
        else:
            verdict_text = self._call(
                record,
                REVIEWER,
                REVIEW_PROMPT.format(
                    brief=record.brief,
                    diff=applied_diff[:20000],
                    context=self._review_context(applied_diff),
                    # Always passing by here: a non-passing check returned
                    # above. What the reviewer needs is *which* commands
                    # passed, and that the harness rather than the author ran
                    # them.
                    checks=self._review_checks_prompt(),
                ),
            )
        outcome.stages.append("review")
        verdict = APPROVED if verdict_text.strip().upper().startswith("APPROVED") else REJECTED
        log.record(
            self.project_id,
            record.item_id,
            attempt,
            A.REVIEWED,
            {"verdict": verdict, "text": verdict_text[:4000]},
            admitted_revision=record.admitted_revision,
            mode=mode,
        )
        outcome.verdict = verdict
        self._emit(
            record,
            f"review_{verdict}",
            detail=verdict_text[:2000],
            # The verdict in full, not the 500 characters the queue keeps. A
            # reviewer's reasoning is the single most useful thing a
            # coordinator can read about a refused item, and truncating it
            # mid-sentence — which is what `last_error` does — throws away the
            # part that says what to do differently.
            evidence={"verdict": verdict, "review": verdict_text[:8000]},
        )
        if outcome.pr_url and self.github is not None:
            self._record_verdict(record, outcome.pr_url, verdict, verdict_text)
        if verdict != APPROVED:
            outcome.reason = f"review rejected: {verdict_text.strip()[:500]}"
            outcome.stop = Stop(REFUSED, REVIEW_REJECTED, detail=outcome.reason)
            return outcome

        # 8. Approval takes the durable draft out of draft. Never land: a
        #    wrong answer must stay reviewable.
        if outcome.pr_url and self.github is not None:
            self._mark_ready(record, outcome.pr_url)
            outcome.stages.append("pr")

        outcome.state = DONE
        outcome.stop = Stop(COMPLETED)
        self._emit(record, "done", detail=outcome.pr_url or branch)
        return outcome

    # ------------------------------------------------------------- helpers

    def _call(self, record: WorkRecord, role: str, prompt: str) -> str:
        self._emit(record, "calling", detail=role)
        self._budget_stop(record)
        try:
            response = self.client.call(role, [{"role": "user", "content": prompt}])
        except RequestRefused as exc:
            raise RuntimeError(f"{role} refused: {exc}") from exc
        # Folded in as it happens, not reconstructed afterwards. An item that
        # blows its ceiling on the implementer must not reach the reviewer,
        # and a total assembled at the end could only ever say so too late.
        with contextlib.suppress(Exception):
            self._spend.add_call(self.client.usage_for(role, response.body))
        return _text_of(response.body, _reader_for(self.client, role))

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

    def _review_context(self, diff: str) -> str:
        return review_context(self.repo, diff, self.context_policy.budget)

    def _review_checks_prompt(self) -> str:
        return review_checks_prompt(self.checks.commands)

    def _starved_prompt(self, starved: Sequence[str]) -> str:
        """Supporting targets that did not fit, named so they are not guessed at."""
        if not starved:
            return ""
        return STARVED_PROMPT.format(
            paths="\n".join(f"  {path} ({self._size_of(path)})" for path in starved)
        )

    def _repo_listing(self) -> str:
        """The tracked paths, for the planner.

        The planner's whole job is to name files, and it was given only the
        brief — so it could only name a path the brief had already quoted, and
        anything else was either a guess or an honest `cannot_identify_target`.
        Measured on rdpapp: an item that described a surface rather than a file
        got "I do not have the repository tree or actual file paths", and the
        implementer then received relevance-guessed files instead of the one
        the work was in.

        Paths only. The planner needs to know what exists; reading it is the
        implementer's job and it has its own budget for that.
        """
        try:
            tracked = [path for path in run_git(self.repo, "ls-files").splitlines() if path]
        except GitError:
            return ""
        if not tracked:
            return ""
        lines: list[str] = []
        spent = 0
        for path in tracked:
            line = f"  {path}\n"
            if spent + len(line) > self.context_policy.budget:
                lines.append(f"  … and {len(tracked) - len(lines)} more not shown\n")
                break
            lines.append(line)
            spent += len(line)
        return PLAN_LISTING_PROMPT.format(listing="".join(lines).rstrip("\n"))

    def _prior_failure_prompt(self, record: WorkRecord) -> str:
        """Why the last attempt was refused, for the attempt replacing it.

        Bounded, because a check's output can be a whole build log and the
        useful part is at the top, where the tool says what it objected to.
        """
        error = (record.last_error or "").strip()
        if not error:
            return ""
        return PRIOR_FAILURE_PROMPT.format(error=error[:4000])

    def _checks_prompt(self) -> str:
        """The project's checks, as the implementer's prompt renders them."""
        commands = [" ".join(command) for command in self.checks.commands if command]
        if not commands:
            return ""
        return CHECKS_PROMPT.format(commands="\n".join(f"  {command}" for command in commands))

    def _size_of(self, path: str) -> str:
        """How big a file the budget could not fit, for the message that says so.

        A number nobody can read is not evidence, and "does not fit" without
        one leaves the reader guessing whether the budget is off by a little or
        by two orders of magnitude. Unreadable is reported as unknown rather
        than as zero: a size of 0 would be a measurement, and this is not one.
        """
        try:
            return f"{(self.repo / path).stat().st_size} bytes"
        except OSError:
            return "size unknown"

    def _prepare_branch(self, branch: str, base: str | None = None) -> None:
        """A clean tree at `base`, on a branch of this item's own.

        The discard is not belt-and-braces. `_abandon_branch` cleans up after
        an attempt that *ended*; a worker that was killed mid-apply ended
        nothing, and leaves its half-applied diff in the working tree. Without
        this, the next attempt carries those changes across the checkout and
        its own patch then fails to apply against a tree that already contains
        it — which reads as "the model wrote a bad diff" and is not.
        """
        run_git(self.repo, "checkout", "--", ".", check=False)
        run_git(self.repo, "clean", "-fd", check=False)
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

    def _commit(self, record: WorkRecord, verdict: str = "", checkpoint: bool = False) -> None:
        run_git(self.repo, "add", "-A")
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
        run_git(self.repo, "commit", "-m", message)

    def _open_pr(
        self, record: WorkRecord, branch: str, base: str, *, draft: bool = True
    ) -> str | None:
        github = self.github
        if github is None:  # pragma: no cover - guarded by the caller
            return None
        body = (
            f"{record.brief.strip()[:3000]}\n\n"
            "---\n\n**Not yet reviewed.** Opened as a draft after the cheap "
            "gates passed so the work survives the worker that produced it. "
            "The reviewer's verdict is posted as a comment, and approval "
            "takes it out of draft.\n\n"
            f"Closes #{record.issue}\n"
        )
        try:
            existing = getattr(github, "find_open_pr", lambda _head: None)(branch)
            if existing:
                return str(existing)
            url = github.create_pr(
                title=record.title,
                body=body,
                head=branch,
                base=base,
                draft=draft,
            )
            return str(url) if url else None
        except Exception as exc:  # noqa: BLE001 - a PR failure must not lose the work
            self._emit(record, "pr_failed", detail=str(exc))
            return None

    def _record_verdict(
        self, record: WorkRecord, pr_url: str, verdict: str, verdict_text: str
    ) -> None:
        if self.github is None:
            return
        body = f"**Review: {verdict.upper()}**\n\n{verdict_text.strip()[:5000]}"
        try:
            self.github.comment_pr(pr_url, body)
        except Exception as exc:  # noqa: BLE001
            self._emit(record, "pr_comment_failed", detail=str(exc))

    def _mark_ready(self, record: WorkRecord, pr_url: str) -> None:
        if self.github is None:
            return
        try:
            self.github.mark_pr_ready(pr_url)
        except Exception as exc:  # noqa: BLE001
            self._emit(record, "pr_ready_failed", detail=str(exc))

    def _preserve_patch(self, record: WorkRecord, diff: str) -> str | None:
        """Keep a patch that failed, and return where it went.

        Returns None when no artifact directory was configured — the harness
        does not invent one, because it owns no directory layout. The bounded
        diagnostic in the event is written either way, so a deployment without
        artifacts still knows what happened; it just cannot replay it.
        """
        if self.artifacts is None:
            return None
        try:
            self.artifacts.mkdir(parents=True, exist_ok=True)
            path = self.artifacts / f"{record.item_id}-{int(self.now())}.patch"
            path.write_text(diff)
        except OSError as exc:
            # Diagnostics are never load-bearing. Losing the artifact must not
            # turn a failed item into a crashed worker.
            self._emit(record, "patch_not_kept", detail=str(exc))
            return None
        return str(path)

    def _emit(
        self,
        record: WorkRecord,
        stage: str,
        detail: str | None = None,
        error_class: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        if self.on_event is not None:
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
                        "error_class": error_class,
                        "detail": detail,
                        "project_id": self.project_id,
                    }
                )
        self._say(record, stage, detail, evidence or {})

    def _guidance(self, record: WorkRecord) -> str:
        """What the coordinator has said about this item, for the next go.

        The return path, and the first thing in this design that closes a
        loop rather than improving observation. Until now a coordinator's
        conclusion went into a room that nothing read, so a correct and
        specific diagnosis — measured: "the `?` operator is being used on a
        `Result<_, String>` in a function whose error type expects
        `StdError`" — reached nobody who could act on it.

        **This is not resumption.** No prior diff is fed back, the item is
        re-planned against the current brief exactly as before, and a guided
        attempt that fails consumes an attempt exactly as an unguided one
        does (proposal Q1). Guidance is information, not absolution: if a
        guided attempt were free, a coordinator could grant an item unlimited
        attempts by advising it.
        """
        if self.ledger is None:
            return ""
        with contextlib.suppress(Exception):
            said = self.ledger.read(
                self.project_id,
                item_room(record.item_id),
                after=0,
                audience="worker",
            )
            recent = [
                m
                for m in said
                if m.sender_role == "oversight"
                and m.message_type in GUIDANCE_TYPES
                and (not m.recipients or GUIDANCE_AUDIENCE & set(m.recipients))
            ][-GUIDANCE_MESSAGES:]
            if not recent:
                return ""
            body = "\n".join(f"  - {m.body.strip()[:GUIDANCE_CHARS]}" for m in recent)
            return GUIDANCE_PROMPT.format(messages=body)
        return ""

    def _say(
        self,
        record: WorkRecord,
        stage: str,
        detail: str | None,
        evidence: Mapping[str, Any],
    ) -> None:
        """Report a setback into the coordination room, if there is one.

        A coordinator can only act on what it was told, and until now no
        worker told it anything: the ledger existed and nothing in the run
        loop had ever written to it. These are the stages
        `COORDINATION-PLANE.md` §6 names as triggers — failure, exhaustion,
        lack of progress — and they are the ones a person watching would
        react to.

        Successes are deliberately absent. A coordinator that reads every
        `checks_passed` pays a model to conclude that nothing is wrong, once
        per item, forever.
        """
        if stage == "started":
            self._episode += 1
        if self.ledger is None or stage not in LEDGER_TRIGGERS:
            return
        from .coordination import Submission

        # Never load-bearing, for the same reason telemetry is not: the
        # coordination plane is designed to be safe when absent, so a broken
        # ledger must not turn a working item into a failed one.
        with contextlib.suppress(Exception):
            self.ledger.append(
                Submission(
                    project_id=self.project_id,
                    room_id=item_room(record.item_id),
                    sender_id=self.owner,
                    sender_role="worker",
                    message_type="observation",
                    body=f"{stage}: {(detail or '').strip()[:2000]}",
                    item_id=record.item_id,
                    attempt=record.attempts,
                    # One message per (worker, item, episode, stage). A
                    # resumed attempt re-reaching a stage it already reported
                    # is the same observation, so replay cannot make the room
                    # look busier than the fleet is — while three separate
                    # goes at one item stay three separate observations.
                    idempotency_key=(
                        f"worker:{self.owner}:{self.project_id}:{record.item_id}:"
                        f"{self._episode}:{stage}"
                    ),
                    payload={
                        "stage": stage,
                        "attempts": record.attempts,
                        "episode": self._episode,
                        # The evidence a person would open. A coordinator told
                        # only "checks_failed" can conclude nothing and says so:
                        # asked to act on three identical failures it replied
                        # "I was not given the diff, file paths, or any evidence
                        # of what needs changing", and escalated instead of
                        # routing around the problem. It was right to.
                        **{k: v for k, v in evidence.items() if k != "files"},
                        # What went wrong, as a fingerprint. Lets a reader tell
                        # "again" from "something new" without re-reasoning.
                        "signature": _signature(stage, evidence),
                    },
                    attachments=_attachments(evidence.get("files") or ()),
                )
            )


def _reader_for(client: Any, role: str) -> Any:
    """The response reader belonging to the route a role is served by.

    Defensive because this is a *convenience*: a route with an unresolvable
    preset, or a client that predates presets, still gets an answer out of the
    fallback below rather than failing a call that already succeeded.
    """
    try:
        return client.route_for(role).resolve().reader
    except Exception:  # noqa: BLE001 - any failure means "no configured reader"
        return None


def _text_of(body: Any, reader: Any = None) -> str:
    """Best-effort extraction of assistant text from a provider response.

    The route's own reader is asked first, so a gateway that puts its reply
    somewhere unusual is a preset's configuration rather than another branch
    here. Failing that, the two shapes in common use, and failing those the raw
    body — a reply this build cannot parse is still evidence, and discarding it
    would turn a parsing gap into a silent empty answer.
    """
    import json as _json

    if reader is not None:
        try:
            found = reader.text(body)
        except Exception:  # noqa: BLE001 - a reader is never load-bearing
            found = None
        if found:
            return str(found)

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
