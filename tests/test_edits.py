"""Changing a file by naming the text rather than counting the lines.

Every test here exists because of one measured run. rdpapp, 2026-08-05: four
model calls, all HTTP 200, a healthy gateway, and **0 of 2 items delivered**.
Neither model misunderstood its item. Both miscounted lines in a unified diff:

    T1: hunk ends 0 source and 7 result line(s) short of what its header declares
    T2: the last hunk supplies 1 fewer source line

An edit block has no line numbers, so that failure cannot be represented. What
remains is a different risk — applying the *wrong* text confidently — and most
of these tests are about refusing rather than applying.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.edits import Edit, EditError, apply_edits, parse_edits


def block(path: str, search: str, replace: str) -> str:
    return f"{path}\n<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"


# ------------------------------------------------------------- parsing


def test_an_edit_block_is_read_out_of_surrounding_prose() -> None:
    """Models explain themselves. The explanation is not an error."""
    edits = parse_edits(
        "I will rename the field.\n\n"
        + block("src/lib.rs", "let old = 1;", "let new = 1;")
        + "\n\nThat should do it."
    )
    assert len(edits) == 1
    assert edits[0].path == "src/lib.rs"
    assert edits[0].search.strip() == "let old = 1;"
    assert edits[0].replace.strip() == "let new = 1;"


def test_several_blocks_are_read_in_order() -> None:
    """Order matters: two edits to one file are applied in sequence."""
    edits = parse_edits(block("a.rs", "one", "1") + "\n" + block("b.rs", "two", "2"))
    assert [e.path for e in edits] == ["a.rs", "b.rs"]


def test_a_fenced_block_and_a_backticked_path_are_both_accepted() -> None:
    """Models emit both. Rejecting one is a fight not worth having."""
    edits = parse_edits(
        "`src/lib.rs`\n```rust\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n```"
    )
    assert len(edits) == 1
    assert edits[0].path == "src/lib.rs"


def test_no_blocks_is_not_an_error_at_parse_time() -> None:
    """ "Produced no edits" and "produced malformed edits" are different.

    The executor already reports the first. Collapsing them would hide a model
    that declined the item behind a message about syntax.
    """
    assert parse_edits("I don't think this item can be done.") == []


# ------------------------------------------------------------ applying


def test_an_edit_replaces_exactly_the_named_text(tmp_path: Path) -> None:
    target = tmp_path / "lib.rs"
    target.write_text("fn a() {}\nfn b() {}\nfn c() {}\n")
    changed = apply_edits(tmp_path, [Edit("lib.rs", "fn b() {}", "fn b() { todo!() }")])
    assert changed == ["lib.rs"]
    assert target.read_text() == "fn a() {}\nfn b() { todo!() }\nfn c() {}\n"


def test_two_edits_to_one_file_are_applied_in_sequence(tmp_path: Path) -> None:
    """The second is validated against the first's result, not the original."""
    target = tmp_path / "lib.rs"
    target.write_text("one\ntwo\n")
    apply_edits(tmp_path, [Edit("lib.rs", "one", "1"), Edit("lib.rs", "two", "2")])
    assert target.read_text() == "1\n2\n"


def test_an_empty_search_creates_a_file(tmp_path: Path) -> None:
    apply_edits(tmp_path, [Edit("new/deep/file.rs", "", "fn main() {}\n")])
    assert (tmp_path / "new/deep/file.rs").read_text() == "fn main() {}\n"


def test_indentation_must_match(tmp_path: Path) -> None:
    """Whitespace is part of the text. A near-miss is a miss.

    Being lenient here would mean guessing which of several similar lines the
    model meant, which is the ambiguity this format exists to remove.
    """
    target = tmp_path / "lib.rs"
    target.write_text("    indented = 1\n")
    with pytest.raises(EditError, match="does not occur"):
        apply_edits(tmp_path, [Edit("lib.rs", "indented = 1", "indented = 2")])


# ------------------------------------------------------------ refusing


def test_text_that_does_not_occur_is_refused(tmp_path: Path) -> None:
    """The model's mistake costs an attempt, never a corrupt tree."""
    target = tmp_path / "lib.rs"
    target.write_text("actual content\n")
    with pytest.raises(EditError, match="does not occur"):
        apply_edits(tmp_path, [Edit("lib.rs", "imagined content", "x")])
    assert target.read_text() == "actual content\n"


def test_text_occurring_twice_is_not_a_location(tmp_path: Path) -> None:
    """The dangerous case, and the reason first-match is not acceptable.

    Taking the first match is how an edit lands in the wrong function and
    passes review because the diff looked plausible. The count is reported so
    the model can widen its context rather than guess again.
    """
    target = tmp_path / "lib.rs"
    target.write_text("fn helper() {}\nfn other() {}\nfn helper() {}\n")
    with pytest.raises(EditError, match="occurs 2 times"):
        apply_edits(tmp_path, [Edit("lib.rs", "fn helper() {}", "fn helper() { todo!() }")])
    assert target.read_text().count("fn helper() {}") == 2


def test_a_missing_file_with_a_non_empty_search_is_refused(tmp_path: Path) -> None:
    """Distinguished from create on purpose.

    Treating "not found" as "create it" would turn a model's mistaken path
    into a new file nobody asked for, in a tree a reviewer then reads as
    intentional.
    """
    with pytest.raises(EditError, match="no such file"):
        apply_edits(tmp_path, [Edit("absent.rs", "something", "else")])
    assert not (tmp_path / "absent.rs").exists()


def test_creating_a_file_that_already_exists_is_refused(tmp_path: Path) -> None:
    (tmp_path / "there.rs").write_text("existing\n")
    with pytest.raises(EditError, match="already exists"):
        apply_edits(tmp_path, [Edit("there.rs", "", "replacement\n")])
    assert (tmp_path / "there.rs").read_text() == "existing\n"


def test_no_edits_at_all_is_refused(tmp_path: Path) -> None:
    with pytest.raises(EditError, match="no edit blocks"):
        apply_edits(tmp_path, [])


# ------------------------------------------------------- all or nothing


def test_one_bad_edit_prevents_every_edit(tmp_path: Path) -> None:
    """A half-applied set is worse than a rejected one.

    The checks would run against a tree no model intended, and the reviewer
    would read a diff nobody wrote. Note the good edit comes FIRST, so this
    fails unless validation completes before anything is written.
    """
    good = tmp_path / "good.rs"
    good.write_text("before\n")
    with pytest.raises(EditError):
        apply_edits(
            tmp_path,
            [Edit("good.rs", "before", "after"), Edit("bad.rs", "nothing", "x")],
        )
    assert good.read_text() == "before\n", "the valid edit was written despite the invalid one"


# ------------------------------------------------- the worktree boundary


@pytest.mark.parametrize(
    "escape",
    ["../outside.rs", "../../etc/passwd", "sub/../../outside.rs", "/etc/passwd"],
)
def test_a_path_outside_the_worktree_is_refused(tmp_path: Path, escape: str) -> None:
    """The agent names the file, so the agent can name `~/.ssh/id_rsa`.

    Part of the boundary #187 asks for, arriving here for a different reason.
    Only the file-write half: a check command still runs arbitrary commands.
    """
    root = tmp_path / "worktree"
    (root / "sub").mkdir(parents=True)
    outside = tmp_path / "outside.rs"
    outside.write_text("untouched\n")
    with pytest.raises(EditError, match="outside the working tree"):
        apply_edits(root, [Edit(escape, "", "owned\n")])
    assert outside.read_text() == "untouched\n"


def test_a_symlink_out_of_the_tree_is_refused(tmp_path: Path) -> None:
    """Resolution follows links, so a link is not a way around the check."""
    root = tmp_path / "worktree"
    root.mkdir()
    outside = tmp_path / "outside.rs"
    outside.write_text("untouched\n")
    (root / "link.rs").symlink_to(outside)
    with pytest.raises(EditError, match="outside the working tree"):
        apply_edits(root, [Edit("link.rs", "untouched", "owned")])
    assert outside.read_text() == "untouched\n"


def test_a_fragment_inside_a_longer_identifier_is_not_a_match(tmp_path: Path) -> None:
    """The failure that is hardest to catch in review.

    Substring matching would let `foo` replace part of `foobar`, producing code
    that still compiles and means something else — and a diff that looks
    deliberate. Matching is anchored to whole lines so this cannot happen.
    """
    target = tmp_path / "lib.rs"
    target.write_text("let foobar = 1;\n")
    with pytest.raises(EditError, match="does not occur"):
        apply_edits(tmp_path, [Edit("lib.rs", "let foo", "let baz")])
    assert target.read_text() == "let foobar = 1;\n"


def test_a_multi_line_search_matches_on_line_boundaries(tmp_path: Path) -> None:
    target = tmp_path / "lib.rs"
    target.write_text("a\nb\nc\n")
    apply_edits(tmp_path, [Edit("lib.rs", "a\nb", "x\ny\nz")])
    assert target.read_text() == "x\ny\nz\nc\n"


# ------------------------------------ the diff the harness computes itself


def test_the_harness_computes_the_line_numbers_from_the_text(tmp_path: Path) -> None:
    """The whole point of the format.

    The model names text; this works out `@@ -a,b +c,d @@` from content it has
    actually read. A header can no longer disagree with its body, because
    nothing is asked to count.
    """
    from agent_harness.edits import to_diff

    target = tmp_path / "lib.rs"
    target.write_text("\n".join(f"line {n}" for n in range(1, 21)) + "\n")
    diff = to_diff(tmp_path, [Edit("lib.rs", "line 10", "line 10 changed")])

    assert "--- a/lib.rs" in diff
    assert "+++ b/lib.rs" in diff
    assert "-line 10\n" in diff
    assert "+line 10 changed\n" in diff
    # The header is arithmetic nobody had to do: 3 lines of context each side.
    assert "@@ -7,7 +7,7 @@" in diff
    # And it must actually apply. This is what the executor hands the ladder.
    assert target.read_text().count("line 10 changed") == 0, "the tree was touched"


def test_rendering_a_diff_changes_nothing_on_disk(tmp_path: Path) -> None:
    """`to_diff` is used before the apply ladder runs, which does the writing.

    If it wrote too, the ladder would apply an already-applied patch and the
    item would fail for a reason the model had nothing to do with.
    """
    from agent_harness.edits import to_diff

    target = tmp_path / "lib.rs"
    target.write_text("before\n")
    to_diff(tmp_path, [Edit("lib.rs", "before", "after")])
    assert target.read_text() == "before\n"


def test_a_new_file_renders_as_an_addition(tmp_path: Path) -> None:
    from agent_harness.edits import to_diff

    diff = to_diff(tmp_path, [Edit("new.rs", "", "fn main() {}\n")])
    assert "--- /dev/null" in diff
    assert "+++ b/new.rs" in diff
    assert "+fn main() {}" in diff


def test_edits_that_change_nothing_render_no_diff(tmp_path: Path) -> None:
    """Well-formed and inert. Reported as no change, not as a broken patch."""
    from agent_harness.edits import to_diff

    (tmp_path / "lib.rs").write_text("same\n")
    assert to_diff(tmp_path, [Edit("lib.rs", "same", "same")]) == ""


def test_several_edits_to_one_file_render_one_file_diff(tmp_path: Path) -> None:
    from agent_harness.edits import to_diff

    target = tmp_path / "lib.rs"
    target.write_text("a\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk\nl\n")
    diff = to_diff(tmp_path, [Edit("lib.rs", "a", "A"), Edit("lib.rs", "l", "L")])
    assert diff.count("--- a/lib.rs") == 1, "one file, one header"
    assert "+A\n" in diff and "+L\n" in diff
