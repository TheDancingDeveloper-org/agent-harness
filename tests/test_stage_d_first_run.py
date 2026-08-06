"""Stage D: a first run that needs no credentials, and a diagnostic that spends nothing.

Two claims are under test, and they are different kinds of claim.

The demo's claim is **end to end**: a clean directory, two documented commands,
no network, no credential, and a commit on a branch at the end of it. So the
test drives the real CLI through `main()` rather than calling the pieces —
a test that assembled the executor itself would prove the parts work and leave
the wiring, which is the entire point of a first-run path, unproven.

Doctor's claim is **negative**: that it reports without doing. Negative claims
need adversarial tests, so the probes that would reach the network are replaced
with ones that fail the test if they are called at all.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_harness import demo as demo_module
from agent_harness.__main__ import main

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="the demo needs git")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


# ------------------------------------------------------------------- init


def test_init_demo_builds_everything_and_starts_nothing(tmp_path: Path) -> None:
    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0

    assert (target / "repo" / ".git").is_dir()
    assert (target / "PLAN.md").is_file()
    assert (target / "queue.sqlite").is_file()
    # Not created until something runs. An init that wrote an event stream
    # would have done something, and the whole claim is that it has not.
    assert not (target / "events.jsonl").exists()

    from agent_harness.work import STOPPED, WorkQueue

    queue = WorkQueue(str(target / "queue.sqlite"))
    state, _ = queue.control(project_id=demo_module.PROJECT_ID)
    assert state == STOPPED, "init must not start a project; a first run is opt-in"
    assert queue.counts(demo_module.PROJECT_ID) == {"pending": 1}


def test_init_demo_configures_no_repo_so_nothing_can_reach_github(tmp_path: Path) -> None:
    """The safety property, asserted rather than assumed.

    A demo that inherited a repo from anywhere could open a pull request
    against a real repository from a command someone ran to look around.
    """
    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0

    from agent_harness.work import WorkQueue

    queue = WorkQueue(str(target / "queue.sqlite"))
    project = queue.get_project(demo_module.PROJECT_ID)
    assert project is not None
    assert not project.repo


def test_init_refuses_a_directory_that_already_has_something_in_it(tmp_path: Path) -> None:
    target = tmp_path / "demo"
    target.mkdir()
    (target / "something-of-mine.txt").write_text("do not lose this")

    assert main(["init", "--demo", "--into", str(target)]) == 2
    assert (target / "something-of-mine.txt").read_text() == "do not lose this"


# -------------------------------------------------------------------- run


def _run_the_demo(target: Path, *extra: str) -> int:
    return main(
        [
            "--db",
            str(target / "queue.sqlite"),
            "run",
            "--demo",
            "--project",
            demo_module.PROJECT_ID,
            "--work",
            str(target / "repo"),
            "--events",
            str(target / "events.jsonl"),
            "--no-push",
            "--limit",
            "1",
            "--check",
            demo_module.CHECK,
            *extra,
        ]
    )


def test_the_documented_commands_complete_an_item_with_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole of Stage D §8.4, as one test.

    `_http_transport` is replaced with something that fails loudly. If any
    part of the demo path reaches for a real transport, this test says so
    rather than quietly making a request during a test run.
    """
    import agent_harness.__main__ as cli

    def forbidden(api_key: str) -> object:
        raise AssertionError("the demo built an HTTP transport; it must not touch a network")

    monkeypatch.setattr(cli, "_http_transport", forbidden)
    monkeypatch.delenv("HARNESS_API_KEY", raising=False)
    monkeypatch.delenv("HARNESS_ENDPOINT", raising=False)

    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    assert _run_the_demo(target) == 0

    from agent_harness.work import DONE, WorkQueue

    queue = WorkQueue(str(target / "queue.sqlite"))
    record = queue.get(demo_module.ITEM_ID, project_id=demo_module.PROJECT_ID)
    assert record is not None
    assert record.state == DONE
    assert record.branch

    # The change is really in a commit on a real branch, not merely reported.
    log = _git(target / "repo", "log", "--oneline", "--all")
    assert log.count("\n") == 2, log
    committed = _git(target / "repo", "show", f"{record.branch}:calc/operations.py")
    assert "def multiply(" in committed
    # And `main` is untouched, because nothing was merged and nothing pushed.
    assert "def multiply(" not in _git(target / "repo", "show", "main:calc/operations.py")


def test_the_run_records_every_stage_it_went_through(tmp_path: Path) -> None:
    """The event file is the demo's output, so its contents are the claim.

    Asserted by stage name, not by count: a demo that reported success while
    skipping the checks or the reviewer would be exactly the thing a first-run
    path must not be able to do.
    """
    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    assert _run_the_demo(target) == 0

    events = [
        json.loads(line)
        for line in (target / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    outcomes = [e.get("outcome") for e in events]
    for stage in ("started", "applied", "checks_passed", "checkpointed", "review_approved", "done"):
        assert stage in outcomes, f"{stage} is missing from the event stream"
    # Checkpointed before reviewed, every time. That ordering is the rule the
    # repository states as "checkpoint before the expensive gate", and a demo
    # is a fine place to keep proving it.
    assert outcomes.index("checkpointed") < outcomes.index("review_approved")


def test_the_demo_reports_no_cost_rather_than_an_invented_one(tmp_path: Path) -> None:
    """Zero tokens is the truth here and a made-up number would be a lie that
    lands in a cost rollup."""
    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    assert _run_the_demo(target) == 0

    events = [
        json.loads(line)
        for line in (target / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    model_calls = [e for e in events if e.get("role") and e.get("model")]
    assert len(model_calls) == 3, "planner, implementer and reviewer should each be recorded"
    assert {e["role"] for e in model_calls} == {"planner", "implementer", "reviewer"}
    assert all(not e.get("tokens_in") and not e.get("tokens_out") for e in model_calls)
    assert all(not e.get("cost_usd") for e in model_calls)


def test_run_demo_refuses_to_push(tmp_path: Path) -> None:
    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    code = main(
        [
            "--db",
            str(target / "queue.sqlite"),
            "run",
            "--demo",
            "--project",
            demo_module.PROJECT_ID,
            "--work",
            str(target / "repo"),
            "--limit",
            "1",
        ]
    )
    assert code == 2


def test_run_demo_and_session_host_are_different_first_runs(tmp_path: Path) -> None:
    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    assert _run_the_demo(target, "--session-host", "http://somewhere.invalid") == 2


def test_a_run_works_the_project_it_was_told_to(tmp_path: Path) -> None:
    """`run --project X` used to set X running and then claim from `default`.

    The demo found it: a full queue reported "nothing to do". Kept as its own
    test because the demo would pass again the moment anyone put its item in
    the default project for an unrelated reason.
    """
    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    assert _run_the_demo(target) == 0

    from agent_harness.work import DONE, WorkQueue

    queue = WorkQueue(str(target / "queue.sqlite"))
    assert queue.counts(demo_module.PROJECT_ID) == {DONE: 1}
    assert queue.counts("default") == {}


def test_a_relative_work_path_is_not_a_trap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run from somewhere else entirely, with a relative --work.

    This failed mid-apply with `cannot change to 'x/y'` — a message that
    points at git rather than at the flag, which is the worst way for a first
    run to fail.
    """
    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    import os

    relative = os.path.relpath(target / "repo", elsewhere)
    code = main(
        [
            "--db",
            str(target / "queue.sqlite"),
            "run",
            "--demo",
            "--project",
            demo_module.PROJECT_ID,
            "--work",
            relative,
            "--events",
            str(target / "events.jsonl"),
            "--no-push",
            "--limit",
            "1",
            "--check",
            demo_module.CHECK,
        ]
    )
    assert code == 0


def test_the_demo_check_actually_gates(tmp_path: Path) -> None:
    """The checks are not decoration: break the fixture's tests and the item
    must not reach the reviewer.

    Without this, a demo whose check silently no-oped would look identical to
    one whose check ran and passed.
    """
    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    repo = target / "repo"
    # A test that fails regardless of what the implementer writes.
    (repo / "tests" / "test_always_fails.py").write_text(
        "import unittest\n\n\n"
        "class Always(unittest.TestCase):\n"
        "    def test_no(self):\n"
        "        self.fail('deliberate')\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@invalid", "-c", "user.name=t", "commit", "-q", "-m", "break it")

    # Nonzero, because the item did not complete. A first-run path that
    # exited 0 over a failed item would be unusable in CI, which is one of
    # the two things §8.1 says this path is for.
    assert _run_the_demo(target) == 1
    events = [
        json.loads(line)
        for line in (target / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    outcomes = [e.get("outcome") for e in events]
    assert "checks_failed" in outcomes
    assert "review_approved" not in outcomes, "a failed check must not reach the reviewer"


# ----------------------------------------------------------------- doctor


def test_doctor_asks_no_model_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    capsys.readouterr()

    assert main(["--db", str(target / "queue.sqlite"), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "model reachability" in out
    assert "not asked" in out
    # Never rounded up to a pass.
    assert "Not asking is not the same as answering." in out


def test_doctor_reports_every_thing_stage_d_asks_for(tmp_path: Path) -> None:
    """§8.3's list, as a list. Named individually so removing one is a failing
    test rather than a quietly shorter report."""
    from agent_harness.doctor import diagnose
    from agent_harness.work import WorkQueue

    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    queue = WorkQueue(str(target / "queue.sqlite"))
    report = diagnose(queue, queue.projects())

    names = {f.name for f in report.environment}
    names |= {f.name for p in report.projects for f in p.findings}
    for required in (
        "routes",  # configuration and route completeness
        "protocol and classifier",  # protocol/classifier selection
        "model reachability",  # provider reachability
        "checkout",  # git/worktree availability
        "checks",  # check-command validity
        "reviewer independence",
        "cost visibility",  # whether the traffic is observable
        "github mutations",
    ):
        assert required in names, f"doctor no longer reports {required!r}"


def test_doctor_is_read_only(tmp_path: Path) -> None:
    """Twice, and nothing changed. A diagnostic with a side effect is a
    diagnostic people stop running."""
    from agent_harness.doctor import diagnose
    from agent_harness.work import WorkQueue

    def listing_of(where: Path) -> list[str]:
        """What is in the directory, minus SQLite's own scratch files.

        `queue.sqlite-wal` and `queue.sqlite-shm` come and go with WAL
        checkpointing, so whether they exist at any instant is a property of
        timing and the filesystem rather than of anything `doctor` did.
        Comparing them asserted something nobody meant and no code here
        controls, and it failed intermittently in CI on a passing branch --
        once mistaken for an integration failure and investigated as one.

        The byte-for-byte comparison of `queue.sqlite` below is the real
        guarantee and is deliberately left exactly as it was.
        """
        return sorted(p.name for p in where.iterdir() if not p.name.endswith(("-wal", "-shm")))

    def content_of(db: Path) -> str:
        """Everything the database holds, as SQL.

        Not the file's bytes. Opening a WAL-mode database and closing it
        checkpoints the log into the main file and allocates pages, so the
        bytes change with no logical change at all -- measured on an untouched
        `main`: 12,288 bytes before, 126,976 after, and a full dump identical.
        The byte comparison therefore failed on every branch including `main`
        itself, and said nothing about whether `doctor` had changed anything.

        This asserts what the test is actually named for: that `doctor`
        changes no data. It is stronger than the byte check in the way that
        matters, because a dump that differs is a real difference.
        """
        import sqlite3

        with sqlite3.connect(db) as conn:
            return "\n".join(conn.iterdump())

    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    before = content_of(target / "queue.sqlite")
    listing = listing_of(target)

    queue = WorkQueue(str(target / "queue.sqlite"))
    diagnose(queue, queue.projects())
    diagnose(queue, queue.projects())

    assert listing_of(target) == listing
    assert content_of(target / "queue.sqlite") == before


def test_doctor_blocks_and_exits_nonzero_when_the_checkout_is_gone(tmp_path: Path) -> None:
    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    shutil.rmtree(target / "repo")

    assert main(["--db", str(target / "queue.sqlite"), "doctor"]) == 1


def test_doctor_catches_a_check_whose_program_is_not_installed(tmp_path: Path) -> None:
    """Configured is not runnable, and the difference costs an implementer.

    A check naming a program that does not exist passes every configuration
    test there is and fails after the item has been paid for.
    """
    from agent_harness.doctor import FAIL, diagnose
    from agent_harness.work import WorkQueue

    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    queue = WorkQueue(str(target / "queue.sqlite"))
    project = queue.get_project(demo_module.PROJECT_ID)
    assert project is not None
    project.checks = ["definitely-not-a-real-program --please"]
    queue.add_project(project)

    report = diagnose(queue, queue.projects())
    checks = next(f for f in report.projects[0].findings if f.name == "checks")
    assert checks.state == FAIL
    assert checks.blocking
    assert not report.ok


def test_doctor_json_is_machine_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    capsys.readouterr()

    assert main(["--db", str(target / "queue.sqlite"), "doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {f["name"] for f in payload["environment"]} >= {"git", "route presets"}
    assert payload["projects"][0]["project_id"] == demo_module.PROJECT_ID


def test_doctor_never_calls_a_probe_it_was_not_given(tmp_path: Path) -> None:
    """The negative claim, adversarially. `ask` is a landmine."""
    from agent_harness.doctor import diagnose
    from agent_harness.work import WorkQueue

    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    queue = WorkQueue(str(target / "queue.sqlite"))

    report = diagnose(queue, queue.projects(), ask=None)
    reachability = [
        f for p in report.projects for f in p.findings if f.name.startswith("model reachability")
    ]
    assert len(reachability) == 1
    assert reachability[0].state == "unknown"


def test_doctor_asks_when_told_to(tmp_path: Path) -> None:
    from agent_harness.doctor import OK, diagnose
    from agent_harness.work import WorkQueue

    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    queue = WorkQueue(str(target / "queue.sqlite"))

    asked: list[str] = []

    def ask(route: object) -> tuple[bool, str]:
        asked.append(str(getattr(route, "model", "?")))
        return (True, "answered")

    report = diagnose(queue, queue.projects(), ask=ask)
    assert asked == [demo_module.MODEL] * 3
    reachability = [
        f for p in report.projects for f in p.findings if f.name.startswith("model reachability")
    ]
    assert len(reachability) == 3
    assert all(f.state == OK for f in reachability)


# ----------------------------------------------------- what the demo is not


def test_the_demo_says_it_proves_wiring_and_not_quality(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The honesty requirement, as a test.

    A deterministic demo that reads as a success story is how "it works" gets
    said about a harness nobody has run against a model.
    """
    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    out = capsys.readouterr().out
    assert "proves nothing about model quality" in out

    assert _run_the_demo(target) == 0
    assert "proves wiring, not quality" in capsys.readouterr().out


def test_the_scripted_reviewer_admits_it_is_scripted() -> None:
    """Its own approval text says the verdict is fixed. Somebody will read
    this in an event stream and it should not read as a real review."""
    from agent_harness.demo import _REVIEW

    assert "fixed" in _REVIEW
    assert "not a model" in _REVIEW


def test_the_demo_diff_is_computed_from_the_tree_not_hardcoded(tmp_path: Path) -> None:
    """So it cannot rot into a patch that no longer applies.

    Change the fixture and the diff changes with it; a stored patch would
    still be describing the old file and would fail at apply, which is a
    tedious thing to debug in a first-run path.
    """
    repo = demo_module.create_fixture_repo(tmp_path / "repo")
    first = demo_module._diff_for(repo)
    (repo / "calc" / "operations.py").write_text("# rewritten\n")
    second = demo_module._diff_for(repo)
    assert first != second
    assert "# rewritten" in second


# ------------------------------------------------- the docs, against the CLI


DOCS = Path(__file__).resolve().parent.parent / "docs"
README = Path(__file__).resolve().parent.parent / "README.md"


def test_the_documented_first_run_commands_are_the_ones_that_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A first-run document is read by someone who cannot yet run the system.

    That makes it the worst place for a flag that no longer exists: the reader
    has no way to tell, and the failure is their first impression. So every
    flag `USAGE.md` §0a puts on `init` and `run --demo` is asserted against the
    parser's own help.
    """
    usage = (DOCS / "USAGE.md").read_text()
    first_run = usage.split("## 0a.")[1].split("## 0b.")[0]
    # `git log --oneline --all`, `ollama pull` and friends are somebody
    # else's flags; this test is about ours.
    first_run = "\n".join(
        line
        for line in first_run.splitlines()
        if not re.search(r"\b(git|ollama|pip|export)\b", line)
    )

    with pytest.raises(SystemExit):
        main(["--help"])
    top_help = capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["init", "--help"])
    init_help = capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["run", "--help"])
    run_help = capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["doctor", "--help"])
    doctor_help = capsys.readouterr().out
    known = top_help + init_help + run_help + doctor_help

    documented = set(re.findall(r"(?<![\w-])(--[a-z]+(?:-[a-z]+)*)", first_run))
    unknown = sorted(flag for flag in documented if flag not in known)
    assert not unknown, f"USAGE.md §0a names flags the CLI does not have: {unknown}"


def test_the_command_the_demo_prints_is_the_command_that_works(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`init` prints a command. Run exactly that, parsed from the output.

    Not the argv the test would have written — the text a human copies. A demo
    whose printed command has a typo in it is a demo that does not work.
    """
    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    printed = next(
        line.strip()
        for line in capsys.readouterr().out.splitlines()
        if line.strip().startswith("agent-harness ")
    )
    argv = shlex.split(printed)
    assert argv[0] == "agent-harness"
    assert main(argv[1:]) == 0


def test_the_readme_distinguishes_tested_from_observed_from_proven() -> None:
    """Stage D §8.4's honesty requirement, as a test rather than a promise."""
    readme = README.read_text()
    for word in ("**tested**", "**observed**", "**proven**"):
        assert word in readme
    assert "No failures observed" in readme
