"""Stage E1 deterministic change-protocol comparison.

The alternatives in this module are deliberately test-only.  They consume the
same generated repository, tasks and in-process transport, while production
continues to use the unified-diff path in :mod:`agent_harness.executor`.

What is held constant is the model's *intent*: every protocol is asked for the
same change to the same tree, judged by the same reviewer prompt, and scored by
the same detector.  What varies is only how that intent is encoded, and each
protocol is given the failure its own encoding makes possible -- an unplaceable
hunk, a search string matching twice, a file written twice in one reply, and an
elided region.  Comparing the encodings on their own characteristic failures is
the point; a shared failure would measure the fixture, not the protocol.

The counts here are outcomes over a scripted case mix.  They say what each
encoding does when a given failure occurs.  They say nothing about how often
any provider produces one.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from agent_harness.executor import Checks, apply_diff, extract_diff, validate_diff
from agent_harness.model_client import Route
from stage_a_support import (
    DeterministicTransport,
    FixtureRepository,
    Reply,
    generated_repository,
    git,
)


@dataclass(frozen=True)
class Change:
    kind: str
    path: str
    content: str | None = None
    source: str | None = None
    search: str | None = None
    replacement: str | None = None


@dataclass(frozen=True)
class ExperimentCase:
    name: str
    brief: str
    expected: tuple[Change, ...]
    response: tuple[Change, ...] | None = None
    special: str | None = None
    #: (path, text) that the task did not authorise changing.  Losing any of
    #: it means the change landed over something, not where it was asked for.
    preserve: tuple[tuple[str, str], ...] = ()
    #: (path, text) the change must produce when the response encodes the
    #: expected change.  Absent means it landed somewhere else.
    landed: tuple[tuple[str, str], ...] = ()


#: What the apply step concluded.  ``unusable`` is a response that could not be
#: read as this protocol's change format at all; ``refused`` is a response that
#: parsed and was then declined rather than guessed at.
APPLIED, UNUSABLE, REFUSED = "applied", "unusable", "refused"


@dataclass(frozen=True)
class ApplyResult:
    status: str
    detail: str
    repair_steps: int = 0
    repair_scan_bytes: int = 0

    @property
    def applied(self) -> bool:
        return self.status == APPLIED


@dataclass(frozen=True)
class Attempt:
    case: str
    applied: bool
    clean: bool
    unusable: bool
    refused: bool
    repaired: bool
    repair_steps: int
    repair_scan_bytes: int
    checks_passed: bool | None
    wrong_location: bool
    wrong_location_reviewed: bool
    reviewer_called: bool
    reviewer_rejected: bool
    input_tokens: int
    output_tokens: int
    reviewer_input_tokens: int
    reviewer_output_tokens: int
    apply_ms: float
    checks_ms: float
    cheap_gate_ms: float
    create_correct: bool | None
    delete_correct: bool | None
    rename_correct: bool | None


@dataclass(frozen=True)
class Summary:
    attempts: int
    clean_applications: int
    applied: int
    wrong_locations: int
    wrong_locations_reviewed: int
    unusable_responses: int
    validator_refusals: int
    check_failures: int
    repairs: int
    repair_steps: int
    repair_scan_bytes: int
    input_tokens: int
    output_tokens: int
    reviewer_calls: int
    reviewer_input_tokens: int
    reviewer_output_tokens: int
    reviewer_rejections: int
    create_correct: int
    create_total: int
    delete_correct: int
    delete_total: int
    rename_correct: int
    rename_total: int
    apply_total_ms: float
    checks_total_ms: float
    cheap_gate_total_ms: float


class ChangeProtocol(Protocol):
    name: str

    def prompt(self, case: ExperimentCase) -> str: ...

    def response(self, repo: Path, case: ExperimentCase) -> str: ...

    def apply(self, repo: Path, response: str) -> ApplyResult: ...


HEADER = '"""Repeated-text fixture; this header must remain first."""\n\n'
REPEATED = (
    HEADER
    + "def first() -> str:\n"
    + '    marker = "same"\n'
    + "    return marker\n\n\n"
    + "def second() -> str:\n"
    + '    marker = "same"\n'
    + "    return marker\n"
)
REPEATED_CHANGED = REPEATED.replace(
    'def second() -> str:\n    marker = "same"',
    'def second() -> str:\n    marker = "changed"',
)
OPERATIONS_HEADER = '"""Arithmetic operations; this docstring must remain first."""'
OPERATIONS = (
    OPERATIONS_HEADER + "\n\n\ndef add(left: int, right: int) -> int:\n    return left + right\n"
)
MULTIPLY = "\n\ndef multiply(left: int, right: int) -> int:\n    return left * right\n"
OPERATIONS_WITH_MULTIPLY = OPERATIONS + MULTIPLY

#: The elision a model writes instead of reproducing a region it believes is
#: unchanged.  In a diff or a search block that region is the anchor, so the
#: elision destroys the statement of *where*.  In a whole-file replacement the
#: same region is the payload, so the elision is indistinguishable from a
#: complete answer until something else looks at the tree.
ELISION = "# ... unchanged ...\n"
OPERATIONS_ELIDED = OPERATIONS_HEADER + "\n\n" + ELISION + MULTIPLY

#: Headers whose position is the wrong-placement detector for this fixture.
HEADERS_FIRST: tuple[tuple[str, str], ...] = (
    ("src/mathkit/operations.py", OPERATIONS_HEADER),
    ("src/mathkit/repeated.py", HEADER),
)
ADD_DEFINITION = "def add(left: int, right: int) -> int:"
FIRST_MARKER = 'def first() -> str:\n    marker = "same"'
SECOND_CHANGED = 'def second() -> str:\n    marker = "changed"'


def _cases() -> tuple[ExperimentCase, ...]:
    multiply = Change(
        "replace",
        "src/mathkit/operations.py",
        content=OPERATIONS_WITH_MULTIPLY,
        search=OPERATIONS,
        replacement=OPERATIONS_WITH_MULTIPLY,
    )
    operations_survive = (
        ("src/mathkit/operations.py", OPERATIONS_HEADER),
        ("src/mathkit/operations.py", ADD_DEFINITION),
    )
    return (
        ExperimentCase(
            "modify",
            "Add a typed multiply function after add without moving the module docstring.",
            (multiply,),
            preserve=operations_survive,
            landed=(("src/mathkit/operations.py", "def multiply(left: int, right: int) -> int:"),),
        ),
        ExperimentCase(
            "repeated-text",
            "Change only second()'s repeated marker to changed; preserve first() and the header.",
            (
                Change(
                    "replace",
                    "src/mathkit/repeated.py",
                    content=REPEATED_CHANGED,
                    search='def second() -> str:\n    marker = "same"',
                    replacement=SECOND_CHANGED,
                ),
            ),
            preserve=(("src/mathkit/repeated.py", FIRST_MARKER),),
            landed=(("src/mathkit/repeated.py", SECOND_CHANGED),),
        ),
        ExperimentCase(
            "create",
            "Create RELEASE.txt with the fixture release marker.",
            (Change("create", "RELEASE.txt", content="fixture-release\n"),),
        ),
        ExperimentCase(
            "delete",
            "Delete src/mathkit/deprecated.py.",
            (Change("delete", "src/mathkit/deprecated.py"),),
        ),
        ExperimentCase(
            "rename",
            "Rename docs/OLD.md to docs/ARCHIVE.md without changing its content.",
            (Change("rename", "docs/ARCHIVE.md", source="docs/OLD.md"),),
        ),
        ExperimentCase(
            "repairable",
            "Update the example output from add to multiply.",
            (
                Change(
                    "replace",
                    "examples/example.txt",
                    content="multiply(6, 7) -> 42\n",
                    search="add(2, 3) -> 5\n",
                    replacement="multiply(6, 7) -> 42\n",
                ),
            ),
            special="repairable",
        ),
        ExperimentCase(
            "unsafe-ambiguous",
            "Add multiply without guessing a location in an existing file.",
            (multiply,),
            special="unsafe",
            preserve=operations_survive,
        ),
        ExperimentCase(
            "elided-context",
            "Add multiply after add; the reply elides the region it believes unchanged.",
            (multiply,),
            special="elision",
            preserve=operations_survive,
        ),
        ExperimentCase(
            "malformed",
            "Add multiply using the requested response protocol.",
            (multiply,),
            special="malformed",
            preserve=operations_survive,
        ),
        ExperimentCase(
            "reviewer-reject",
            "Change the historical-notes heading to Archived historical notes.",
            (
                Change(
                    "replace",
                    "docs/OLD.md",
                    content="# Archived historical notes\n",
                    search="# Historical notes\n",
                    replacement="# Archived historical notes\n",
                ),
            ),
            response=(
                Change(
                    "replace",
                    "docs/OLD.md",
                    content="# Historical notes changed\n",
                    search="# Historical notes\n",
                    replacement="# Historical notes changed\n",
                ),
            ),
        ),
    )


def _generated_repository(root: Path) -> FixtureRepository:
    fixture = generated_repository(root)
    repeated = root / "src/mathkit/repeated.py"
    repeated.write_text(REPEATED)
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    git(root, "add", ".gitignore", "src/mathkit/repeated.py")
    git(root, "commit", "-q", "-m", "add adversarial repeated-text fixture")
    return fixture


def _safe_path(repo: Path, raw: str) -> Path:
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe relative path: {raw!r}")
    candidate = repo.joinpath(*path.parts)
    if candidate.is_symlink() or any(
        parent.is_symlink() for parent in candidate.parents if parent != repo
    ):
        raise ValueError(f"symlink path is not accepted: {raw!r}")
    return candidate


def _apply_changes(repo: Path, changes: Sequence[Change]) -> None:
    for change in changes:
        target = _safe_path(repo, change.path)
        if change.kind in {"replace", "create"}:
            if change.content is None:
                raise ValueError(f"{change.kind} has no content")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.content)
        elif change.kind == "delete":
            target.unlink()
        elif change.kind == "rename":
            if change.source is None:
                raise ValueError("rename has no source")
            source = _safe_path(repo, change.source)
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
        else:
            raise ValueError(f"unknown change kind: {change.kind}")


def _render_diff(repo: Path, changes: Sequence[Change]) -> str:
    _apply_changes(repo, changes)
    try:
        return _observable_diff(repo)
    finally:
        git(repo, "reset", "--hard", "HEAD")
        git(repo, "clean", "-fd")


def _observable_diff(repo: Path) -> str:
    """Include untracked creations without staging their contents."""
    git(repo, "add", "-N", "--", ".")
    return git(repo, "diff", "--binary", "--find-renames", "HEAD")


def _overcount_first_hunk(diff: str) -> str:
    header = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    def add_one(match: re.Match[str]) -> str:
        return (
            f"@@ -{match.group(1)},{int(match.group(2) or 1) + 1} "
            f"+{match.group(3)},{int(match.group(4) or 1) + 1} @@"
        )

    damaged, count = header.subn(add_one, diff, count=1)
    if count != 1:
        raise AssertionError("repair case did not produce one count-bearing hunk")
    return damaged


class UnifiedDiffProtocol:
    name = "unified_diff"

    def prompt(self, case: ExperimentCase) -> str:
        return (
            "Implement the task. Reply with one unified diff rooted at the repository "
            "and no prose.\n"
            f"Task: {case.brief}"
        )

    def response(self, repo: Path, case: ExperimentCase) -> str:
        if case.special == "unsafe":
            return (
                "diff --git a/src/mathkit/operations.py b/src/mathkit/operations.py\n"
                "--- a/src/mathkit/operations.py\n"
                "+++ b/src/mathkit/operations.py\n"
                "@@ -0,0 +1,2 @@\n"
                "+def multiply(left: int, right: int) -> int:\n"
                "+    return left * right\n"
            )
        if case.special == "elision":
            # The anchor the hunk needs is exactly the region the reply elided.
            return (
                "diff --git a/src/mathkit/operations.py b/src/mathkit/operations.py\n"
                "--- a/src/mathkit/operations.py\n"
                "+++ b/src/mathkit/operations.py\n"
                "@@ -1,3 +1,7 @@\n"
                f" {OPERATIONS_HEADER}\n"
                f" {ELISION.rstrip()}\n"
                "     return left + right\n"
                "+\n"
                "+\n"
                "+def multiply(left: int, right: int) -> int:\n"
                "+    return left * right\n"
            )
        if case.special == "malformed":
            return "I changed the file but cannot provide a patch."
        diff = _render_diff(repo, case.response or case.expected)
        return _overcount_first_hunk(diff) if case.special == "repairable" else diff

    def apply(self, repo: Path, response: str) -> ApplyResult:
        diff = extract_diff(response)
        if diff is None:
            return ApplyResult(UNUSABLE, "response contains no unified diff")
        fatal = [problem for problem in validate_diff(diff) if problem.fatal]
        if fatal:
            return ApplyResult(REFUSED, "; ".join(map(str, fatal)))
        applied, how = apply_diff(repo, diff)
        if not applied:
            return ApplyResult(REFUSED, how)
        repaired = "recounted" in how
        return ApplyResult(
            APPLIED,
            how,
            repair_steps=int(repaired),
            repair_scan_bytes=len(diff.encode()) if repaired else 0,
        )


def _json_payload(response: str, *, key: str) -> tuple[list[Mapping[str, Any]], int]:
    stripped = response.strip()
    repair_steps = 0
    fenced = re.fullmatch(r"```json\s*\n(.*)\n```", stripped, re.DOTALL)
    if fenced is not None:
        stripped = fenced.group(1)
        repair_steps = 1
    raw = json.loads(stripped)
    if not isinstance(raw, dict) or set(raw) != {key} or not isinstance(raw[key], list):
        raise ValueError(f"expected one {key!r} list")
    rows = raw[key]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"every {key} entry must be an object")
    return rows, repair_steps


def _whole_rows(changes: Sequence[Change]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for change in changes:
        if change.kind in {"replace", "create"}:
            assert change.content is not None
            rows.append({"operation": "write", "path": change.path, "content": change.content})
        elif change.kind == "delete":
            rows.append({"operation": "delete", "path": change.path})
        else:
            assert change.source is not None
            rows.append({"operation": "rename", "from": change.source, "path": change.path})
    return rows


class WholeFileProtocol:
    name = "whole_file"

    def prompt(self, case: ExperimentCase) -> str:
        return (
            "Implement the task. Reply with JSON containing a files list. Each entry uses write "
            "with path/content, delete with path, or rename with from/path. Return complete "
            "content "
            f"for every named write.\nTask: {case.brief}"
        )

    def response(self, repo: Path, case: ExperimentCase) -> str:
        del repo
        if case.special == "malformed":
            return "not JSON"
        if case.special == "elision":
            rows = [
                {
                    "operation": "write",
                    "path": "src/mathkit/operations.py",
                    "content": OPERATIONS_ELIDED,
                }
            ]
        elif case.special == "unsafe":
            rows = _whole_rows(case.expected)
            assert rows[0]["operation"] == "write"
            rows.append({**rows[0], "content": OPERATIONS})
        else:
            rows = _whole_rows(case.response or case.expected)
        payload = json.dumps({"files": rows}, separators=(",", ":"), sort_keys=True)
        return f"```json\n{payload}\n```" if case.special == "repairable" else payload

    def apply(self, repo: Path, response: str) -> ApplyResult:
        try:
            rows, repair_steps = _json_payload(response, key="files")
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            return ApplyResult(UNUSABLE, str(error))
        try:
            changes: list[Change] = []
            touched: set[str] = set()
            for row in rows:
                operation = row.get("operation")
                path = row.get("path")
                if not isinstance(operation, str) or not isinstance(path, str):
                    raise ValueError("operation and path must be strings")
                _safe_path(repo, path)
                source = row.get("from")
                names = {path}
                if operation == "rename":
                    if not isinstance(source, str):
                        raise ValueError("rename requires from")
                    _safe_path(repo, source)
                    names.add(source)
                if touched & names:
                    raise ValueError("one response touches a path more than once")
                touched.update(names)
                if operation == "write" and isinstance(row.get("content"), str):
                    target = _safe_path(repo, path)
                    changes.append(
                        Change(
                            "replace" if target.exists() else "create", path, content=row["content"]
                        )
                    )
                elif operation == "delete" and _safe_path(repo, path).is_file():
                    changes.append(Change("delete", path))
                elif operation == "rename" and isinstance(source, str):
                    if not _safe_path(repo, source).is_file() or _safe_path(repo, path).exists():
                        raise ValueError("rename source/destination state is invalid")
                    changes.append(Change("rename", path, source=source))
                else:
                    raise ValueError("invalid whole-file operation")
            _apply_changes(repo, changes)
        except (OSError, TypeError, ValueError) as error:
            return ApplyResult(REFUSED, str(error))
        return ApplyResult(
            APPLIED,
            "validated whole-file operations",
            repair_steps=repair_steps,
            repair_scan_bytes=len(response.encode()) if repair_steps else 0,
        )


def _search_rows(changes: Sequence[Change]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for change in changes:
        if change.kind == "replace":
            assert change.search is not None and change.replacement is not None
            rows.append(
                {
                    "operation": "replace",
                    "path": change.path,
                    "search": change.search,
                    "replacement": change.replacement,
                }
            )
        elif change.kind == "create":
            assert change.content is not None
            rows.append({"operation": "create", "path": change.path, "content": change.content})
        elif change.kind == "delete":
            rows.append({"operation": "delete", "path": change.path})
        else:
            assert change.source is not None
            rows.append({"operation": "rename", "from": change.source, "path": change.path})
    return rows


class SearchReplaceProtocol:
    name = "search_replace"

    def prompt(self, case: ExperimentCase) -> str:
        return (
            "Implement the task. Reply with JSON containing a blocks list. Replacements require "
            "path/search/replacement and the search must match exactly once. Create, delete and "
            f"rename use explicit operations.\nTask: {case.brief}"
        )

    def response(self, repo: Path, case: ExperimentCase) -> str:
        del repo
        if case.special == "malformed":
            return json.dumps({"edits": []})
        if case.special == "elision":
            rows = [
                {
                    "operation": "replace",
                    "path": "src/mathkit/operations.py",
                    "search": OPERATIONS_HEADER + "\n\n" + ELISION,
                    "replacement": OPERATIONS_ELIDED,
                }
            ]
        elif case.special == "unsafe":
            rows = [
                {
                    "operation": "replace",
                    "path": "src/mathkit/repeated.py",
                    "search": 'marker = "same"',
                    "replacement": 'marker = "changed"',
                }
            ]
        else:
            rows = _search_rows(case.response or case.expected)
        payload = json.dumps({"blocks": rows}, separators=(",", ":"), sort_keys=True)
        return f"```json\n{payload}\n```" if case.special == "repairable" else payload

    def apply(self, repo: Path, response: str) -> ApplyResult:
        try:
            rows, repair_steps = _json_payload(response, key="blocks")
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            return ApplyResult(UNUSABLE, str(error))
        try:
            changes: list[Change] = []
            touched: set[str] = set()
            for row in rows:
                operation = row.get("operation")
                path = row.get("path")
                if not isinstance(operation, str) or not isinstance(path, str):
                    raise ValueError("operation and path must be strings")
                target = _safe_path(repo, path)
                source = row.get("from")
                names = {path}
                if operation == "rename":
                    if not isinstance(source, str):
                        raise ValueError("rename requires from")
                    _safe_path(repo, source)
                    names.add(source)
                if touched & names:
                    raise ValueError("one response touches a path more than once")
                touched.update(names)
                if operation == "replace":
                    search, replacement = row.get("search"), row.get("replacement")
                    if (
                        not isinstance(search, str)
                        or not search
                        or not isinstance(replacement, str)
                    ):
                        raise ValueError("replace requires non-empty search and string replacement")
                    current = target.read_text()
                    if current.count(search) != 1:
                        raise ValueError("search text must match exactly once")
                    changes.append(
                        Change(
                            "replace",
                            path,
                            content=current.replace(search, replacement),
                            search=search,
                            replacement=replacement,
                        )
                    )
                elif operation == "create" and isinstance(row.get("content"), str):
                    if target.exists():
                        raise ValueError("create target already exists")
                    changes.append(Change("create", path, content=row["content"]))
                elif operation == "delete" and target.is_file():
                    changes.append(Change("delete", path))
                elif operation == "rename" and isinstance(source, str):
                    if not _safe_path(repo, source).is_file() or target.exists():
                        raise ValueError("rename source/destination state is invalid")
                    changes.append(Change("rename", path, source=source))
                else:
                    raise ValueError("invalid search/replace operation")
            _apply_changes(repo, changes)
        except (OSError, TypeError, ValueError) as error:
            return ApplyResult(REFUSED, str(error))
        return ApplyResult(
            APPLIED,
            "validated unique-match operations",
            repair_steps=repair_steps,
            repair_scan_bytes=len(response.encode()) if repair_steps else 0,
        )


def _mock_tokens(text: str) -> int:
    """Stable mock-route tokens: words and individual punctuation marks.

    There is no provider tokenizer in an in-process route.  This explicit
    tokenizer makes relative protocol volume reproducible without presenting
    it as vendor billing usage.
    """
    return len(re.findall(r"\w+|[^\w\s]", text, re.UNICODE))


def _transport_reply(transport: DeterministicTransport, model: str, prompt: str) -> str:
    response = transport(
        Route(model, f"fixture://{model}"),
        [{"role": "user", "content": prompt}],
        {},
    )
    raw: Any = json.loads(response.body or "{}")
    content = raw["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise AssertionError("fixture transport returned non-string content")
    return content


def _changed_paths(repo: Path) -> set[str]:
    _observable_diff(repo)
    return set(git(repo, "diff", "--name-only", "--find-renames", "HEAD").splitlines())


def _intended_paths(changes: Sequence[Change]) -> set[str]:
    paths = {change.path for change in changes}
    paths.update(change.source for change in changes if change.source is not None)
    return paths


def _wrong_location(repo: Path, case: ExperimentCase) -> bool:
    """Whether an applied change touched anywhere but where it was asked for.

    Three separable ways that happens, all of which the fixture makes visible:
    a path the task never named changed; a header that had to remain first no
    longer is; or text the task did not authorise changing is gone, which is
    what an edit landing *over* existing code looks like afterwards.
    """
    if not _changed_paths(repo) <= _intended_paths(case.expected):
        return True
    for path, header in HEADERS_FIRST:
        target = repo / path
        if target.is_file() and not target.read_text().startswith(header):
            return True
    required = case.preserve + (case.landed if case.response is None else ())
    for path, snippet in required:
        target = repo / path
        if not target.is_file() or snippet not in target.read_text():
            return True
    return False


def _operation_correctness(repo: Path, case: ExperimentCase) -> tuple[bool | None, ...]:
    create = (
        (repo / "RELEASE.txt").is_file()
        and (repo / "RELEASE.txt").read_text() == "fixture-release\n"
        if case.name == "create"
        else None
    )
    delete = not (repo / "src/mathkit/deprecated.py").exists() if case.name == "delete" else None
    rename = (
        not (repo / "docs/OLD.md").exists()
        and (repo / "docs/ARCHIVE.md").read_text() == "# Historical notes\n"
        if case.name == "rename"
        else None
    )
    return create, delete, rename


def _review_prompt(case: ExperimentCase, repo: Path) -> str:
    return (
        "Review the resulting tree against the task. Reply APPROVED only when the exact requested "
        "change is present and no unrelated location changed.\n"
        f"Task: {case.brief}\nResulting diff:\n{_observable_diff(repo)}"
    )


def _run_attempt(
    protocol: ChangeProtocol, case: ExperimentCase, root: Path
) -> tuple[Attempt, str | None]:
    fixture = _generated_repository(root)
    expected_diff = _render_diff(fixture.root, case.expected)
    response_text = protocol.response(fixture.root, case)
    implement_prompt = protocol.prompt(case)
    transport = DeterministicTransport({"implementer": Reply(response_text)})

    started = time.perf_counter_ns()
    delivered = _transport_reply(transport, "implementer", implement_prompt)
    result = protocol.apply(fixture.root, delivered)
    applied_at = time.perf_counter_ns()
    checks_ok: bool | None = None
    if result.applied:
        checks_ok, _detail = Checks(commands=[list(command) for command in fixture.checks]).run(
            fixture.root
        )
    checked_at = time.perf_counter_ns()

    wrong_location = result.applied and _wrong_location(fixture.root, case)
    reviewer_called = result.applied and bool(checks_ok)
    reviewer_rejected = False
    reviewer_input = reviewer_output = 0
    review_prompt: str | None = None
    if reviewer_called:
        review_prompt = _review_prompt(case, fixture.root)
        verdict = "APPROVED" if _observable_diff(fixture.root) == expected_diff else "REJECTED"
        review_transport = DeterministicTransport({"reviewer": Reply(verdict)})
        review_reply = _transport_reply(review_transport, "reviewer", review_prompt)
        reviewer_input = _mock_tokens(review_prompt)
        reviewer_output = _mock_tokens(review_reply)
        reviewer_rejected = review_reply == "REJECTED"

    create, delete, rename = _operation_correctness(fixture.root, case)
    apply_ms = (applied_at - started) / 1_000_000
    checks_ms = (checked_at - applied_at) / 1_000_000
    attempt = Attempt(
        case=case.name,
        applied=result.applied,
        clean=result.applied and result.repair_steps == 0,
        unusable=result.status == UNUSABLE,
        refused=result.status == REFUSED,
        repaired=result.applied and result.repair_steps > 0,
        repair_steps=result.repair_steps,
        repair_scan_bytes=result.repair_scan_bytes,
        checks_passed=checks_ok,
        wrong_location=wrong_location,
        wrong_location_reviewed=wrong_location and reviewer_called,
        reviewer_called=reviewer_called,
        reviewer_rejected=reviewer_rejected,
        input_tokens=_mock_tokens(implement_prompt),
        output_tokens=_mock_tokens(delivered),
        reviewer_input_tokens=reviewer_input,
        reviewer_output_tokens=reviewer_output,
        apply_ms=apply_ms,
        checks_ms=checks_ms,
        cheap_gate_ms=apply_ms + checks_ms,
        create_correct=create,
        delete_correct=delete,
        rename_correct=rename,
    )
    return attempt, review_prompt


def _summarize(attempts: Sequence[Attempt]) -> Summary:
    def operation(name: str) -> tuple[int, int]:
        values = [getattr(attempt, name) for attempt in attempts]
        present = [value for value in values if value is not None]
        return sum(value is True for value in present), len(present)

    create_correct, create_total = operation("create_correct")
    delete_correct, delete_total = operation("delete_correct")
    rename_correct, rename_total = operation("rename_correct")
    return Summary(
        attempts=len(attempts),
        clean_applications=sum(attempt.clean for attempt in attempts),
        applied=sum(attempt.applied for attempt in attempts),
        wrong_locations=sum(attempt.wrong_location for attempt in attempts),
        wrong_locations_reviewed=sum(attempt.wrong_location_reviewed for attempt in attempts),
        unusable_responses=sum(attempt.unusable for attempt in attempts),
        validator_refusals=sum(attempt.refused for attempt in attempts),
        check_failures=sum(attempt.checks_passed is False for attempt in attempts),
        repairs=sum(attempt.repaired for attempt in attempts),
        repair_steps=sum(attempt.repair_steps for attempt in attempts),
        repair_scan_bytes=sum(attempt.repair_scan_bytes for attempt in attempts),
        input_tokens=sum(attempt.input_tokens for attempt in attempts),
        output_tokens=sum(attempt.output_tokens for attempt in attempts),
        reviewer_calls=sum(attempt.reviewer_called for attempt in attempts),
        reviewer_input_tokens=sum(attempt.reviewer_input_tokens for attempt in attempts),
        reviewer_output_tokens=sum(attempt.reviewer_output_tokens for attempt in attempts),
        reviewer_rejections=sum(attempt.reviewer_rejected for attempt in attempts),
        create_correct=create_correct,
        create_total=create_total,
        delete_correct=delete_correct,
        delete_total=delete_total,
        rename_correct=rename_correct,
        rename_total=rename_total,
        apply_total_ms=sum(attempt.apply_ms for attempt in attempts),
        checks_total_ms=sum(attempt.checks_ms for attempt in attempts),
        cheap_gate_total_ms=sum(attempt.cheap_gate_ms for attempt in attempts),
    )


def test_stage_e1_protocol_experiment(tmp_path: Path) -> None:
    """Compare the protocols without installing either alternative in core."""
    protocols: tuple[ChangeProtocol, ...] = (
        UnifiedDiffProtocol(),
        WholeFileProtocol(),
        SearchReplaceProtocol(),
    )
    cases = _cases()
    all_attempts: dict[str, list[Attempt]] = {protocol.name: [] for protocol in protocols}
    review_prompts: dict[str, dict[str, str]] = {}
    for protocol in protocols:
        for case in cases:
            attempt, review_prompt = _run_attempt(
                protocol, case, tmp_path / f"{protocol.name}-{case.name}"
            )
            all_attempts[protocol.name].append(attempt)
            if review_prompt is not None:
                review_prompts.setdefault(case.name, {})[protocol.name] = review_prompt

    summaries = {name: _summarize(attempts) for name, attempts in all_attempts.items()}
    if os.environ.get("STAGE_E1_REPORT") == "1":
        diagnostic = {
            name: [asdict(attempt) for attempt in attempts]
            for name, attempts in all_attempts.items()
        }
        print("STAGE_E1_ATTEMPTS=" + json.dumps(diagnostic, sort_keys=True))
    # Held identical across protocols: every protocol answers the same ten
    # cases, is asked to encode the same intent, and is judged the same way.
    for summary in summaries.values():
        assert summary.attempts == 10
        assert summary.unusable_responses == 1
        assert summary.repairs == 1
        assert summary.repair_steps == 1
        assert (summary.create_correct, summary.create_total) == (1, 1)
        assert (summary.delete_correct, summary.delete_total) == (1, 1)
        assert (summary.rename_correct, summary.rename_total) == (1, 1)
        assert summary.reviewer_rejections == 1
        assert summary.apply_total_ms > 0
        assert summary.checks_total_ms > 0

    # Where they differ, and why. The elided-context case is the whole
    # comparison: the same model failure is an unusable anchor in the two
    # located protocols and a complete-looking payload in the unlocated one.
    diff_summary = summaries["unified_diff"]
    search_summary = summaries["search_replace"]
    whole_summary = summaries["whole_file"]
    for located in (diff_summary, search_summary):
        assert located.applied == 7
        assert located.clean_applications == 6
        assert located.validator_refusals == 2
        assert located.wrong_locations == 0
        assert located.check_failures == 0
        assert located.reviewer_calls == 7
    assert whole_summary.applied == 8
    assert whole_summary.clean_applications == 7
    assert whole_summary.validator_refusals == 1
    assert whole_summary.wrong_locations == 1
    assert whole_summary.check_failures == 1
    assert whole_summary.reviewer_calls == 7

    # The cheap gate contained the corrupted tree, but only after it was
    # written: no protocol let a misplaced change reach paid review.
    for summary in summaries.values():
        assert summary.wrong_locations_reviewed == 0

    elided = {
        name: next(attempt for attempt in attempts if attempt.case == "elided-context")
        for name, attempts in all_attempts.items()
    }
    assert not elided["unified_diff"].applied and elided["unified_diff"].refused
    assert not elided["search_replace"].applied and elided["search_replace"].refused
    assert elided["whole_file"].applied
    assert elided["whole_file"].checks_passed is False
    assert elided["whole_file"].wrong_location
    assert not elided["whole_file"].reviewer_called

    # D9 remains open: holding the reviewer prompt byte-for-byte constant
    # prevents this protocol decision from silently deciding that prompt.
    for prompts in review_prompts.values():
        assert len(set(prompts.values())) == 1
    assert set(review_prompts) == {
        "modify",
        "repeated-text",
        "create",
        "delete",
        "rename",
        "repairable",
        "reviewer-reject",
    }
    assert all(len(prompts) == 3 for prompts in review_prompts.values())

    # The experiment's safety boundary: only this test module implements the
    # alternatives. Production still names unified diffs as its sole response.
    production = (Path(__file__).parents[1] / "src/agent_harness/executor.py").read_text()
    assert "Implement this change and reply with a unified diff and nothing else." in production
    assert "WholeFileProtocol" not in production and "SearchReplaceProtocol" not in production

    if os.environ.get("STAGE_E1_REPORT") == "1":
        printable = {name: asdict(summary) for name, summary in summaries.items()}
        print("STAGE_E1_MEASUREMENTS=" + json.dumps(printable, sort_keys=True))


def test_wrong_location_detector_reports_each_way_a_change_can_miss(tmp_path: Path) -> None:
    """A zero wrong-location rate is only evidence if the detector can fire.

    Each branch of the detector is shown reporting a tree that a protocol
    could plausibly produce, so the comparison's zeroes are earned rather than
    a property of a detector that never says no.
    """
    cases = {entry.name: entry for entry in _cases()}
    modify, repeated = cases["modify"], cases["repeated-text"]

    def tree(name: str, changes: Sequence[Change]) -> FixtureRepository:
        fixture = _generated_repository(tmp_path / name)
        _apply_changes(fixture.root, changes)
        return fixture

    def rewrite(path: str, content: str) -> tuple[Change, ...]:
        return (Change("replace", path, content=content),)

    correct = tree("correct", modify.expected)
    assert not _wrong_location(correct.root, modify)

    unnamed = tree("unnamed-path", modify.expected)
    (unnamed.root / "examples/example.txt").write_text("multiply(6, 7) -> 42\n")
    assert _wrong_location(unnamed.root, modify)

    above = tree(
        "above-the-header",
        rewrite("src/mathkit/operations.py", MULTIPLY.lstrip("\n") + OPERATIONS),
    )
    assert _wrong_location(above.root, modify)

    over = tree("over-existing-code", rewrite("src/mathkit/operations.py", OPERATIONS_ELIDED))
    assert _wrong_location(over.root, modify)
    passed, _detail = Checks(commands=[list(command) for command in over.checks]).run(over.root)
    assert not passed

    other_twin = REPEATED.replace(FIRST_MARKER, FIRST_MARKER.replace('"same"', '"changed"'))
    twin = tree("the-other-twin", rewrite("src/mathkit/repeated.py", other_twin))
    assert _wrong_location(twin.root, repeated)
