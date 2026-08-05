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


def apply_edits(root: Path, edits: list[Edit]) -> list[str]:
    """Apply every edit, or none of them. Returns the paths changed.

    All-or-nothing because a half-applied set is worse than a rejected one: the
    checks would run against a tree no model intended, and the reviewer would
    read a diff nobody wrote. Every edit is validated against the *pending*
    content first, so two edits to one file are checked in sequence exactly as
    they will be applied.
    """
    if not edits:
        raise EditError("no edit blocks found")

    pending: dict[Path, str] = {}
    order: list[str] = []

    for index, edit in enumerate(edits, start=1):
        target = _resolve(root, edit.path)
        where = f"edit {index} ({edit.path})"

        if target in pending:
            current = pending[target]
        elif target.exists():
            current = target.read_text()
        elif edit.creates:
            current = ""
        else:
            raise EditError(f"{where}: no such file, and its SEARCH block is not empty")

        if edit.creates:
            if current and current != edit.replace:
                raise EditError(
                    f"{where}: an empty SEARCH block means create, but this file already exists"
                )
            pending[target] = edit.replace
        else:
            search = edit.search.rstrip("\n")
            at = _occurrences(current, search)
            if not at:
                raise EditError(
                    f"{where}: the SEARCH text does not occur in the file as whole lines. "
                    f"It must match exactly, including indentation."
                )
            if len(at) > 1:
                raise EditError(
                    f"{where}: the SEARCH text occurs {len(at)} times and is therefore not a "
                    f"location. Include enough surrounding lines to make it unique."
                )
            cut = at[0]
            pending[target] = (
                current[:cut] + edit.replace.rstrip("\n") + current[cut + len(search) :]
            )

        if edit.path not in order:
            order.append(edit.path)

    for target, content in pending.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return order
