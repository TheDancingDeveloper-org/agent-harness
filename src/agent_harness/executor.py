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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model_client import CapExhausted, ModelClient, RequestRefused, RetryExhausted
from .work import (
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


#: How much repository the implementer is shown. Big enough that a small
#: project arrives whole, small enough not to dominate the prompt.
DEFAULT_CONTEXT_BUDGET = 60_000

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
            omitted.append(
                (
                    path,
                    "named target exceeds remaining content budget"
                    if explicit
                    else "content budget exhausted",
                )
            )
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
        artifacts: Path | None = None,
        project_id: str = DEFAULT_PROJECT,
    ) -> None:
        self.queue = queue
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
        try:
            outcome = self._execute_with_heartbeat(record)
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
            partial = self._partial_for(record)
            self.queue.release(
                record.item_id,
                PENDING,
                error=f"budget: {exc}",
                branch=partial.branch if partial else None,
                pr_url=partial.pr_url if partial else None,
                owner=self.owner,
                consume_attempt=False,
                project_id=self.project_id,
            )
            raise
        except RetryExhausted as exc:
            self._emit(record, "retry_exhausted", detail=str(exc), error_class=exc.kind)
            partial = self._partial_for(record)
            self.queue.release(
                record.item_id,
                PENDING,
                error=str(exc),
                branch=partial.branch if partial else None,
                pr_url=partial.pr_url if partial else None,
                owner=self.owner,
                project_id=self.project_id,
            )
            if partial is not None:
                partial.state = PENDING
                partial.reason = str(exc)
                return partial
            return Outcome(record.item_id, PENDING, reason=str(exc))
        except Exception as exc:  # noqa: BLE001 - one item must not kill the loop
            self._emit(record, "error", detail=str(exc))
            partial = self._partial_for(record)
            self.queue.release(
                record.item_id,
                FAILED,
                error=str(exc),
                branch=partial.branch if partial else None,
                pr_url=partial.pr_url if partial else None,
                owner=self.owner,
                project_id=self.project_id,
            )
            if partial is not None:
                partial.state = FAILED
                partial.reason = str(exc)
                return partial
            return Outcome(record.item_id, FAILED, reason=str(exc))
        self.queue.release(
            record.item_id,
            outcome.state,
            error=outcome.reason or None,
            branch=outcome.branch,
            pr_url=outcome.pr_url,
            owner=self.owner,
            project_id=self.project_id,
        )
        return outcome

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

    def _execute(self, record: WorkRecord) -> Outcome:
        outcome = Outcome(record.item_id, FAILED)
        # Retained so a reviewer-side exception can still return the durable
        # checkpoint's branch/PR to the queue and caller.
        self._partial = outcome
        self._emit(record, "started")

        # Resolved first, and deliberately before the implementer is called:
        # the working tree still holds the previous item's branch, so a model
        # shown the tree writes context lines for a file its patch will never
        # meet. The branch itself is still cut later, so an item that produces
        # no usable diff leaves no branch behind.
        base, stacked_on = self._base_for(record)
        self._base = base

        # 1. Plan. Cheap, once per item, and the highest-leverage call.
        planner_reply = self._call(record, PLANNER, PLAN_PROMPT.format(brief=record.brief))
        planner = parse_planner_result(planner_reply)
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

        # 2. Implement.
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
        reply = self._call(
            record,
            IMPLEMENTER,
            IMPLEMENT_PROMPT.format(
                brief=record.brief,
                plan=planner.plan,
                context=context.text,
            ),
        )
        outcome.stages.append("implement")
        diff = extract_diff(reply)
        if not diff:
            self._emit(record, "no_diff")
            outcome.reason = "the implementer returned no diff"
            return outcome

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
            return outcome

        # 4. Apply, on a branch of its own, based on whatever this item
        #    actually depends on.
        branch = f"{self.branch_prefix}{record.item_id.lower()}"
        self._prepare_branch(branch, base)
        outcome.branch = branch
        outcome.base = base
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
            )
            outcome.reason = f"the diff did not apply to {base}: {how}" + (
                f" (patch kept at {kept})" if kept else ""
            )
            self._abandon_branch(branch)
            return outcome
        self._emit(record, "applied", detail=how)
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
        passed, failure = self.checks.run(self.repo)
        outcome.stages.append("checks")
        if not passed:
            self._emit(
                record,
                "checks_failed",
                detail=failure[:2000],
                error_class="disk_exhausted" if is_disk_exhaustion(failure) else None,
            )
            outcome.reason = failure
            self._abandon_branch(branch)
            return outcome
        self._emit(record, "checks_passed")
        self._keepalive(record)

        # The graph is re-checked here, at the last cheap point before review
        # spends money and the checkpoint makes anything durable. `claim` checked it
        # once, minutes ago; correcting a plan while work is in flight is a
        # normal thing for an operator to do, and an item that is no longer
        # eligible must not land on the strength of a stale check.
        unmet = self.queue.unmet_dependencies(record.item_id, project_id=self.project_id)
        if unmet:
            outcome.reason = (
                f"{record.item_id} now depends on {', '.join(unmet)}, which "
                "is not done; the candidate is discarded and the item goes back to pending"
            )
            self._emit(record, "dependency_invalidated", detail=outcome.reason)
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
        if self.push:
            run_git(self.repo, "push", "-u", "origin", branch)
            outcome.stages.append("push")
            self._emit(record, "pushed", detail=branch)
        if self.github is not None and record.issue:
            outcome.pr_url = self._open_pr(record, branch, base, draft=True)
            if outcome.pr_url:
                outcome.stages.append("draft-pr")
                self._emit(record, "draft_pr_opened", detail=outcome.pr_url)

        # 7. Review, by a different role -- and ideally a different vendor,
        #    which `ModelClient.reviewer_independence()` now reports on rather
        #    than leaving to a comment nobody reads.
        verdict_text = self._call(
            record,
            REVIEWER,
            REVIEW_PROMPT.format(
                brief=record.brief,
                diff=applied_diff[:20000],
                checks="passed" if passed else failure,
            ),
        )
        outcome.stages.append("review")
        verdict = APPROVED if verdict_text.strip().upper().startswith("APPROVED") else REJECTED
        outcome.verdict = verdict
        self._emit(record, f"review_{verdict}", detail=verdict_text[:2000])
        if outcome.pr_url and self.github is not None:
            self._record_verdict(record, outcome.pr_url, verdict, verdict_text)
        if verdict != APPROVED:
            outcome.reason = f"review rejected: {verdict_text.strip()[:500]}"
            return outcome

        # 8. Approval takes the durable draft out of draft. Never land: a
        #    wrong answer must stay reviewable.
        if outcome.pr_url and self.github is not None:
            self._mark_ready(record, outcome.pr_url)
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
            dependency
            for dependency in record.depends_on
            if (found := self.queue.get(dependency, project_id=self.project_id))
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
    ) -> None:
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
                    "error_class": error_class,
                    "detail": detail,
                    "project_id": self.project_id,
                }
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
