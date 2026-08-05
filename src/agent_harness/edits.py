"""Changing a file by naming the text, not by counting the lines.

A unified diff asks a model to do arithmetic it cannot check: `@@ -401,7
+401,12 @@` claims how many lines the hunk consumes and produces, and the
model has to get both right, blind, having read the file once in a prompt.
When it miscounts by one the patch is refused and the whole item is lost.

Measured against rdpapp, 2026-08-05, two items in one run:

    T1: hunk ends 0 source and 7 result line(s) short of what its header declares
    T2: the last hunk supplies 1 fewer source line

Neither model misunderstood the task. Both miscounted. The failure scales with
file size, which is why a 704 KB source file is the worst case: anchoring a
hunk inside it means reproducing surrounding context exactly, from memory of a
prompt, with no way to check.

An edit block removes the arithmetic entirely:

    path/to/file.rs
    <<<<<<< SEARCH
    the exact text to find
    =======
    the exact text to put there
    >>>>>>> REPLACE

There are no line numbers, so there is nothing to miscount. The text either
occurs in the file or it does not, and *that* is a question with a definite
answer the harness can check before changing anything.

**Ambiguity is refused, never guessed.** Text occurring twice is not an edit
location; picking the first is how a patch lands in the wrong function and
passes review because the diff looked plausible. Both the empty-search
(create) and not-found cases are reported in the terms the executor already
acts on, so a bad edit costs an attempt rather than corrupting a tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SEARCH = "<<<<<<< SEARCH"
DIVIDER = "======="
REPLACE = ">>>>>>> REPLACE"

#: A path line, then a fenced block. The path may be bare or wrapped in
#: backticks, because models emit both and rejecting one is a fight not worth
#: having when the intent is unambiguous.
_BLOCK = re.compile(
    r"^[ \t]*`?(?P<path>[^\n`]+?)`?[ \t]*\n"
    r"(?:```[a-zA-Z0-9_+-]*\n)?"
    r"[ \t]*<<<<<<< SEARCH[ \t]*\n"
    r"(?P<search>.*?)"
    r"^[ \t]*=======[ \t]*\n"
    r"(?P<replace>.*?)"
    r"^[ \t]*>>>>>>> REPLACE[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


class EditError(Exception):
    """An edit that cannot be applied safely. Never applied partially."""


def _occurrences(text: str, needle: str) -> list[int]:
    """Where `needle` occurs in `text`, on **whole-line boundaries only**.

    Plain substring matching is not safe enough for this job. `foo` occurs
    inside `foobar`, so an edit naming a short identifier would replace part of
    a longer one and produce code that still compiles and means something else
    — the failure that is hardest to see in review, because the diff looks
    deliberate.

    A match must therefore begin at the start of a line and end at the end of
    one. That is also the mental model the format implies: *these lines become
    those lines*, not *this fragment becomes that fragment*.

    The cost is that a model which drops the leading indentation gets a
    refusal. That is the intended trade — the alternative is inferring which
    of several similar lines was meant, and inference is exactly what this
    format exists to remove. The error says so, so the next attempt can fix it.
    """
    if not needle:
        return []
    found: list[int] = []
    start = 0
    while (at := text.find(needle, start)) != -1:
        starts_line = at == 0 or text[at - 1] == "\n"
        end = at + len(needle)
        ends_line = end == len(text) or text[end] == "\n"
        if starts_line and ends_line:
            found.append(at)
        start = at + 1
    return found


def _reindented(text: str, needle: str) -> tuple[int, int, str] | None:
    """One region matching `needle` ignoring how far each line is indented.

    Returns `(start, end, indent_delta)` for a **unique** match, else None.

    Why this is not the guessing the exact matcher refuses. A model that
    reproduces the right lines but shifts them all by two spaces has named a
    location correctly and formatted it wrongly; refusing that costs an item
    for a mistake with no ambiguity in it. What is still refused is anything
    with a *choice* in it: the stripped lines must correspond one for one, the
    relative shape must be preserved (every line moves by the same amount),
    and it must match in exactly one place. Two candidates is still not a
    location, and is still refused.

    Blank lines are compared as blank regardless of trailing whitespace,
    because an editor stripping it is not a difference anybody means.
    """
    want = needle.split("\n")
    have = text.split("\n")
    if not want:
        return None
    stripped = [line.strip() for line in want]

    hits: list[int] = []
    for first in range(len(have) - len(want) + 1):
        window = have[first : first + len(want)]
        if [line.strip() for line in window] != stripped:
            continue
        # Every line must move by the same amount, or this is a different
        # shape wearing the same words -- which is a choice, so it is refused.
        deltas = {
            len(line) - len(line.lstrip()) - (len(w) - len(w.lstrip()))
            for line, w in zip(window, want, strict=True)
            if line.strip()
        }
        if len(deltas) == 1:
            hits.append(first)

    if len(hits) != 1:
        return None
    first = hits[0]
    shift = next(
        len(line) - len(line.lstrip()) - (len(w) - len(w.lstrip()))
        for line, w in zip(have[first : first + len(want)], want, strict=True)
        if line.strip()
    )
    start = sum(len(line) + 1 for line in have[:first])
    end = start + sum(len(line) + 1 for line in have[first : first + len(want)]) - 1
    return start, end, " " * shift if shift > 0 else ""


def _nearest(text: str, needle: str, span: int = 6) -> str:
    """The file's own text where the SEARCH nearly matched, quoted back.

    A failed match tells the model it was wrong and nothing more, so the retry
    is another guess at text it has already misremembered. Measured on rdpapp:
    the same item failed this way on three consecutive attempts, each time on
    a block whose *first* line was present and whose later lines were not.

    Quoting the real text turns an unactionable refusal into a correction. The
    model does not have to remember the file -- it is being shown it.

    Deliberately bounded, and deliberately silent when there is nothing useful
    to say: a guess at the wrong place, quoted confidently, would be worse
    than no hint at all.
    """
    want = [line.strip() for line in needle.split("\n") if line.strip()]
    have = text.split("\n")
    if not want:
        return ""
    stripped = [line.strip() for line in have]
    try:
        anchor = stripped.index(want[0])
    except ValueError:
        return " No line of it matches the file; check the path and re-read the file."

    # How far the model got before diverging, so the message can say where.
    matched = 0
    for offset, wanted in enumerate(want):
        if anchor + offset >= len(stripped) or stripped[anchor + offset] != wanted:
            break
        matched = offset + 1

    window = have[anchor : anchor + max(len(want), span)]
    quoted = "\n".join(window)
    return (
        f" Its first {matched} line(s) match at line {anchor + 1}, then it diverges. "
        f"The file actually contains, from that point:\n{quoted}\n"
        f"Reproduce that text exactly in SEARCH."
    )


@dataclass(frozen=True)
class Edit:
    """One exact-text replacement in one file."""

    path: str
    search: str
    replace: str

    @property
    def creates(self) -> bool:
        """An empty search means "this file is new".

        Distinguished from a failed match on purpose: creating a file and
        failing to find text are different outcomes, and collapsing them would
        turn a model's mistake into a silently created file.
        """
        return self.search.strip() == ""


def parse_edits(text: str) -> list[Edit]:
    """Every edit block in `text`, in order. Prose around them is ignored.

    Returns an empty list rather than raising when there are none: "the model
    produced no edits" is a real outcome the executor already reports, and it
    is not the same as "the model produced malformed edits".
    """
    return [
        Edit(
            path=match.group("path").strip(),
            search=match.group("search"),
            replace=match.group("replace"),
        )
        for match in _BLOCK.finditer(text)
    ]


def _resolve(root: Path, path: str) -> Path:
    """A path inside `root`, or an error.

    The agent names the file, so the agent can name `../../.ssh/id_rsa`. This
    is the boundary that stops it: resolved, then checked to be under the root
    that was handed in. Symlinks resolve too, so a link out of the tree is
    caught rather than followed.
    """
    target = (root / path).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        raise EditError(f"{path} is outside the working tree and will not be written") from None
    return target


def plan_edits(root: Path, edits: list[Edit]) -> dict[str, tuple[str, str]]:
    """Validate every edit and return `path -> (before, after)`.

    Nothing is written. Separated from applying so the same validation serves
    both callers: the one that changes the tree, and the one that renders a
    diff without touching it.

    All-or-nothing because a half-applied set is worse than a rejected one: the
    checks would run against a tree no model intended, and the reviewer would
    read a diff nobody wrote. Every edit is validated against the *pending*
    content first, so two edits to one file are checked in sequence exactly as
    they will be applied.
    """
    if not edits:
        raise EditError("no edit blocks found")

    pending: dict[Path, str] = {}
    original: dict[Path, str] = {}
    order: list[str] = []

    for index, edit in enumerate(edits, start=1):
        target = _resolve(root, edit.path)
        where = f"edit {index} ({edit.path})"

        if target in pending:
            current = pending[target]
        elif target.exists():
            current = target.read_text()
            original.setdefault(target, current)
        elif edit.creates:
            current = ""
            original.setdefault(target, "")
        else:
            raise EditError(f"{where}: no such file, and its SEARCH block is not empty")
        original.setdefault(target, current)

        if edit.creates:
            if current and current != edit.replace:
                raise EditError(
                    f"{where}: an empty SEARCH block means create, but this file already exists"
                )
            pending[target] = edit.replace
        else:
            search = edit.search.rstrip("\n")
            replace = edit.replace.rstrip("\n")
            at = _occurrences(current, search)
            if len(at) > 1:
                raise EditError(
                    f"{where}: the SEARCH text occurs {len(at)} times and is therefore not a "
                    f"location. Include enough surrounding lines to make it unique."
                )
            if at:
                cut = at[0]
                pending[target] = current[:cut] + replace + current[cut + len(search) :]
            else:
                # Exact match failed. Before refusing, allow the one difference
                # that carries no ambiguity: the same lines, uniquely located,
                # indented differently. The replacement is shifted by the same
                # amount so the result keeps the file's own indentation rather
                # than the model's.
                loose = _reindented(current, search)
                if loose is None:
                    raise EditError(
                        f"{where}: the SEARCH text does not occur in the file as whole "
                        f"lines, even ignoring indentation." + _nearest(current, search)
                    )
                start, end, shift = loose
                shifted = "\n".join(
                    (shift + line) if line.strip() else line for line in replace.split("\n")
                )
                pending[target] = current[:start] + shifted + current[end:]

        if edit.path not in order:
            order.append(edit.path)

    # Keyed by the path as the model wrote it, in the order it first named
    # each file, so a diff reads the way the edits were given.
    by_path: dict[str, tuple[str, str]] = {}
    for path in order:
        target = _resolve(root, path)
        by_path[path] = (original.get(target, ""), pending[target])
    return by_path


def apply_edits(root: Path, edits: list[Edit]) -> list[str]:
    """Apply every edit, or none of them. Returns the paths changed."""
    planned = plan_edits(root, edits)
    for path, (_, after) in planned.items():
        target = _resolve(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(after)
    return list(planned)


def to_diff(root: Path, edits: list[Edit]) -> str:
    """The unified diff these edits describe, without touching the tree.

    This is the point of the format. The model names text; *this* computes the
    line numbers, from content it has actually read. A hunk header can no
    longer disagree with its body, because nothing is asked to count.

    Rendering a diff rather than writing the files keeps every gate downstream
    exactly as it was -- the patch validator, the apply ladder, the checks, the
    reviewer and the commit all still see a diff, and none of them needs to
    learn a second way for changes to arrive.
    """
    import difflib

    out: list[str] = []
    for path, (before, after) in plan_edits(root, edits).items():
        if before == after:
            continue
        out.extend(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{path}" if before else "/dev/null",
                tofile=f"b/{path}",
                n=3,
            )
        )
        if out and not out[-1].endswith("\n"):
            out.append("\n")
    return "".join(out)
