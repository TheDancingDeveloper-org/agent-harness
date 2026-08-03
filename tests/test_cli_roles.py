"""What `agent-harness run` does with the role map before it spends anything.

A stored map is how a role is re-routed live, so it has to win. But a stored
map holding only *some* roles used to suppress the flags for the rest: the CLI
printed "the stored map is in force", set the project running, claimed an item
and then failed it with `no route for role 'planner'`. An operator who supplied
a complete configuration got a failed item for their trouble.

These tests run the real CLI with an empty queue, so nothing is claimed, no
model is called and no repository is touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_harness.__main__ import main
from agent_harness.api import ROLE_MAP_KEY
from agent_harness.work import WorkQueue

ENDPOINT = "https://models.example/v1"


def route(model: str, endpoint: str = ENDPOINT) -> dict[str, str]:
    return {"model": model, "endpoint": endpoint, "provider": "claw-bay"}


def run_cli(db: Path, work: Path, **flags: str) -> int:
    argv = ["--db", str(db), "run", "--repo", "owner/name", "--work", str(work)]
    for name, value in flags.items():
        argv += [f"--{name.replace('_', '-')}", value]
    return main(argv)


@pytest.fixture()
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("HARNESS_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_partial_stored_map_is_filled_from_the_command_line(cli: Path) -> None:
    """The bug: a map holding only `reviewer` left planner unrouted."""
    db = cli / "harness.sqlite"
    WorkQueue(str(db)).set_setting(ROLE_MAP_KEY, {"reviewer": route("claude-sonnet-4-6")})

    code = run_cli(
        db,
        cli,
        planner="gpt-5.6",
        implementer="gpt-5.6",
        reviewer="claude-sonnet-4-6",
        endpoint=ENDPOINT,
    )

    assert code == 0
    stored = WorkQueue(str(db)).get_setting(ROLE_MAP_KEY)
    assert stored is not None
    assert set(stored) == {"planner", "implementer", "reviewer"}
    assert stored["planner"]["model"] == "gpt-5.6"
    # The role that WAS stored keeps the stored route.
    assert stored["reviewer"]["model"] == "claude-sonnet-4-6"


def test_a_stored_route_still_overrides_the_flag_for_its_own_role(cli: Path, capsys: Any) -> None:
    """Merging must not become "the command line wins" -- re-routing a role
    live is the reason the map is stored at all."""
    db = cli / "harness.sqlite"
    WorkQueue(str(db)).set_setting(ROLE_MAP_KEY, {"reviewer": route("stored-reviewer")})

    run_cli(db, cli, planner="p", implementer="i", reviewer="typed-reviewer", endpoint=ENDPOINT)

    stored = WorkQueue(str(db)).get_setting(ROLE_MAP_KEY) or {}
    assert stored["reviewer"]["model"] == "stored-reviewer"
    out = capsys.readouterr().out
    assert "overrides the command line" in out and "reviewer" in out


def test_the_roles_the_flags_supplied_are_reported(cli: Path, capsys: Any) -> None:
    db = cli / "harness.sqlite"
    WorkQueue(str(db)).set_setting(ROLE_MAP_KEY, {"reviewer": route("claude-sonnet-4-6")})

    run_cli(db, cli, planner="p", implementer="i", reviewer="claude-sonnet-4-6", endpoint=ENDPOINT)

    out = capsys.readouterr().out
    assert "no route for" in out
    assert "planner" in out and "implementer" in out


def test_an_unusable_stored_route_refuses_before_anything_is_claimed(
    cli: Path, capsys: Any
) -> None:
    """A route with no endpoint cannot make a call. Finding that out on the
    first model call means finding it out after the item is claimed."""
    db = cli / "harness.sqlite"
    queue = WorkQueue(str(db))
    queue.set_setting(
        ROLE_MAP_KEY,
        {
            "planner": {"model": "p", "endpoint": "", "provider": "claw-bay"},
            "implementer": route("i"),
            "reviewer": route("r"),
        },
    )

    code = run_cli(db, cli, planner="p", implementer="i", reviewer="r", endpoint=ENDPOINT)

    assert code == 2
    assert "no usable route" in capsys.readouterr().err
    # And crucially: it did not set the project running.
    assert WorkQueue(str(db)).control()[0] != "running"


def test_with_no_stored_map_the_flags_are_seeded(cli: Path) -> None:
    db = cli / "harness.sqlite"

    assert run_cli(db, cli, planner="p", implementer="i", reviewer="r", endpoint=ENDPOINT) == 0

    stored = WorkQueue(str(db)).get_setting(ROLE_MAP_KEY) or {}
    assert {name: r["model"] for name, r in stored.items()} == {
        "planner": "p",
        "implementer": "i",
        "reviewer": "r",
    }
