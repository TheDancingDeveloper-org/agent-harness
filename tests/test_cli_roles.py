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

import logging
from pathlib import Path
from typing import Any

import pytest

from agent_harness.__main__ import _http_transport, main
from agent_harness.api import ROLE_MAP_KEY
from agent_harness.model_client import Route
from agent_harness.work import WorkQueue

ENDPOINT = "https://models.example/v1"


def route(model: str, endpoint: str = ENDPOINT) -> dict[str, str]:
    return {"model": model, "endpoint": endpoint, "provider": "claw-bay"}


def run_cli(db: Path, work: Path, **flags: Any) -> int:
    argv = ["--db", str(db), "run", "--repo", "owner/name", "--work", str(work)]
    for name, value in flags.items():
        flag = f"--{name.replace('_', '-')}"
        # Store-true flags carry no value; passing one turns the next argument
        # into a positional and argparse then blames the wrong thing.
        argv += [flag] if value is True else [flag, value]
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


def test_a_stored_route_still_overrides_the_flag_for_its_own_role(cli: Path, caplog: Any) -> None:
    """Merging must not become "the command line wins" -- re-routing a role
    live is the reason the map is stored at all.

    The note goes to the LOG, not stdout (#153): it changes which model a run
    calls, and stdout is block-buffered into a pipe, so the warning arrived out
    of order when it arrived at all and vanished entirely when the process was
    killed. Two runs were spent on a model the operator had replaced."""
    db = cli / "harness.sqlite"
    WorkQueue(str(db)).set_setting(ROLE_MAP_KEY, {"reviewer": route("stored-reviewer")})

    with caplog.at_level(logging.WARNING, logger="agent_harness.__main__"):
        run_cli(db, cli, planner="p", implementer="i", reviewer="typed-reviewer", endpoint=ENDPOINT)

    stored = WorkQueue(str(db)).get_setting(ROLE_MAP_KEY) or {}
    assert stored["reviewer"]["model"] == "stored-reviewer"
    assert "overrides the command line" in caplog.text
    assert "stored-reviewer" in caplog.text and "typed-reviewer" in caplog.text
    assert "--reroute" in caplog.text, "and how to make the command line win"


def test_the_roles_the_flags_supplied_are_reported(cli: Path, caplog: Any) -> None:
    db = cli / "harness.sqlite"
    WorkQueue(str(db)).set_setting(ROLE_MAP_KEY, {"reviewer": route("claude-sonnet-4-6")})

    with caplog.at_level(logging.INFO, logger="agent_harness.__main__"):
        run_cli(
            db, cli, planner="p", implementer="i", reviewer="claude-sonnet-4-6", endpoint=ENDPOINT
        )

    assert "no route for" in caplog.text
    assert "planner" in caplog.text and "implementer" in caplog.text


def test_reroute_makes_the_command_line_win_and_stores_it(cli: Path) -> None:
    """Without this, changing a model on a database that has ever run meant
    editing the settings table by hand or deleting the queue (#153)."""
    db = cli / "harness.sqlite"
    WorkQueue(str(db)).set_setting(ROLE_MAP_KEY, {"reviewer": route("stored-reviewer")})

    run_cli(
        db,
        cli,
        planner="p",
        implementer="i",
        reviewer="typed-reviewer",
        endpoint=ENDPOINT,
        reroute=True,
    )

    stored = WorkQueue(str(db)).get_setting(ROLE_MAP_KEY) or {}
    assert stored["reviewer"]["model"] == "typed-reviewer"


def test_a_complete_stored_map_needs_no_role_flags_at_all(cli: Path) -> None:
    """The flags were simultaneously required to start and inert once given: a
    database holding a complete map still refused with "no model configured",
    naming the very roles it had routes for (#153)."""
    db = cli / "harness.sqlite"
    WorkQueue(str(db)).set_setting(
        ROLE_MAP_KEY,
        {"planner": route("p"), "implementer": route("i"), "reviewer": route("r")},
    )

    code = run_cli(db, cli, endpoint=ENDPOINT)

    assert code == 0


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


@pytest.mark.parametrize(
    ("wire_error", "normalized"),
    [
        ("timeout", TimeoutError),
        ("network", ConnectionError),
    ],
)
def test_http_transport_normalizes_wire_errors_for_model_retries(
    monkeypatch: pytest.MonkeyPatch,
    wire_error: str,
    normalized: type[Exception],
) -> None:
    import httpx

    def fail(*_args: object, **_kwargs: object) -> None:
        request = httpx.Request("POST", ENDPOINT)
        if wire_error == "timeout":
            raise httpx.ReadTimeout("late", request=request)
        raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(httpx.Client, "request", fail)
    transport = _http_transport("test-key")

    with pytest.raises(normalized):
        transport(Route("model", ENDPOINT), [], {})


# ------------------------------------------------------- protocol selection


def test_the_protocol_each_role_speaks_is_reported(cli: Path, capsys: Any) -> None:
    """An operator should not have to read the source to find out what shape
    of request their fleet is about to send."""
    db = cli / "harness.sqlite"

    assert run_cli(db, cli, planner="p", implementer="i", reviewer="r", endpoint=ENDPOINT) == 0

    out = capsys.readouterr().out
    assert "protocol: chat-completions" in out
    assert "classifier=generic" in out


def test_an_unknown_preset_refuses_before_anything_is_claimed(cli: Path, capsys: Any) -> None:
    """Discovering that a preset is not installed on the first model call means
    discovering it after the item is claimed and the attempt is spent."""
    db = cli / "harness.sqlite"

    code = run_cli(
        db, cli, planner="p", implementer="i", reviewer="r", endpoint=ENDPOINT, preset="not-a-thing"
    )

    assert code == 2
    err = capsys.readouterr().err
    assert "not-a-thing" in err
    # ...and it names what does exist, rather than only what does not.
    assert "generic" in err and "claw-bay" in err
    assert WorkQueue(str(db)).control()[0] != "running"


def test_an_endpoint_host_is_reported_as_a_suggestion_and_acted_on_by_nobody(
    cli: Path, capsys: Any
) -> None:
    """Detection from a hostname is a good hint and a terrible decision
    procedure. It is printed; nothing is chosen."""
    db = cli / "harness.sqlite"
    vendor_endpoint = "https://api.theclawbay.com/v1"

    assert (
        run_cli(db, cli, planner="p", implementer="i", reviewer="r", endpoint=vendor_endpoint) == 0
    )

    assert "matches the 'claw-bay' preset" in capsys.readouterr().out
    stored = WorkQueue(str(db)).get_setting(ROLE_MAP_KEY) or {}
    # Nothing wrote a preset into the map on the strength of a hostname.
    assert all(not route.get("preset") for route in stored.values())


def test_the_transport_renders_the_request_the_preset_describes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half of Stage B that was hardcoded: the URL, the payload keys and
    the credential header now come from the route, so a second wire protocol is
    configuration rather than another branch in the CLI."""
    import httpx

    from agent_harness import protocols

    sent: dict[str, Any] = {}

    def capture(self: Any, method: str, url: str, **kwargs: Any) -> Any:
        sent.update({"method": method, "url": url, **kwargs})
        return httpx.Response(200, text="{}", request=httpx.Request(method, url))

    monkeypatch.setenv(protocols.PRESET_PATH_ENV, "fixture-plugin=preset_plugin:PRESET")
    monkeypatch.setattr(httpx.Client, "request", capture)
    transport = _http_transport("shared-key")

    transport(
        Route("a-model", ENDPOINT, preset="fixture-plugin"),
        [{"role": "user", "content": "x"}],
        {"role": "implementer", "timeout": 5.0, "temperature": 0.1},
    )

    assert sent["url"] == f"{ENDPOINT}/v2/generate"
    assert sent["json"]["model_id"] == "a-model"
    assert sent["json"]["turns"] == [{"role": "user", "content": "x"}]
    assert sent["json"]["temperature"] == 0.1
    # Transport instructions never reach the model as completion parameters.
    assert "timeout" not in sent["json"] and "role" not in sent["json"]
    assert sent["headers"]["x-api-key"] == "shared-key"
    assert "Authorization" not in sent["headers"]


def test_the_cli_agent_default_is_the_executor_s() -> None:
    """They disagreed, and the CLI's was the one that could not write.

    `session_executor.DEFAULT_AGENT_COMMAND` carries `--permission-mode
    acceptEdits`; the CLI's `--agent` default did not. So `run --session-host`
    without the flag produced an agent that was refused every Edit, reported
    no changes, and read as a model that had considered the task and declined.

    Measured: it cost a full agent run, and the agent's own account of the
    refusal sat in a scrollback nothing reads.

    Asserted against the source rather than a parsed value, because the defect
    was *a second default existing at all* — a literal here that happens to
    match today would drift again tomorrow.
    """
    import inspect

    from agent_harness import __main__
    from agent_harness.session_executor import DEFAULT_AGENT_COMMAND

    source = inspect.getsource(__main__)
    assert 'default=" ".join(DEFAULT_AGENT_COMMAND)' in source, (
        "the CLI must take the executor's agent command, not declare its own"
    )
    assert '"--permission-mode"' in inspect.getsource(
        __import__("agent_harness.session_executor", fromlist=["x"])
    ), "and that command must still grant edit permission"
    assert "--permission-mode" in " ".join(DEFAULT_AGENT_COMMAND)


# ------------------------------------- an escalation is not a failed run


def _outcome(item_id: str, *, ok: bool, disposition: str = "", reason: str = "") -> Any:
    from agent_harness.executor import Outcome
    from agent_harness.outcomes import Stop

    out = Outcome(item_id, "done" if ok else "blocked", reason=reason)
    if disposition:
        out.stop = Stop(disposition, detail=reason)
    return out


def test_an_escalated_item_is_marked_for_a_person_not_as_a_failure() -> None:
    """Measured on rdpapp R7: a correct refusal was announced as `FAIL R7`."""
    from agent_harness.__main__ import run_summary
    from agent_harness.outcomes import ESCALATED

    lines = run_summary(
        [_outcome("R7", ok=False, disposition=ESCALATED, reason="needs a live Postgres")]
    )

    assert lines[0].startswith("  YOU R7:")
    assert "FAIL" not in "\n".join(lines)
    assert "waiting on you, not on a retry: R7" in lines[-1]


def test_a_real_failure_still_reads_as_one() -> None:
    from agent_harness.__main__ import run_summary
    from agent_harness.outcomes import REFUSED

    lines = run_summary([_outcome("R2", ok=False, disposition=REFUSED, reason="review rejected")])

    assert lines[0].startswith("  FAIL R2:")
    assert "waiting on you" not in "\n".join(lines)


def test_an_escalation_does_not_colour_the_exit_status() -> None:
    """A queue of well-formed questions is not a broken run."""
    from agent_harness.__main__ import _is_failure
    from agent_harness.outcomes import ESCALATED, REFUSED

    assert _is_failure(_outcome("R7", ok=False, disposition=ESCALATED)) is False
    assert _is_failure(_outcome("R2", ok=False, disposition=REFUSED)) is True
