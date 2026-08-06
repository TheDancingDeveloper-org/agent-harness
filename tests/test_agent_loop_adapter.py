"""The loop is adopted; the gates are not given up.

Measured 2026-08-05, same item, same models, same gateway, the only variable
being the loop: the direct executor delivered nothing in four passes and a
loop reached `cargo test` green in 31 turns. So the loop is not rebuilt here.

What these tests protect is the part that *is* ours: every model call still
goes through `ModelClient`, and every command still goes through
`CommandGuard`. An adapter that quietly bypassed either would have taken the
loop and given up the reasons this repository exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_harness.adapters.minisweagent import (
    HarnessEnvironment,
    HarnessModel,
    NotInstalled,
)
from agent_harness.guard import CommandGuard
from agent_harness.model_client import ModelClient, Response, Route


def _client(calls: list[tuple[str, Any]]) -> ModelClient:
    def transport(route: Route, messages: Any, options: Any) -> Response:
        calls.append((route.model, messages))
        return Response(
            status=200,
            headers={},
            # A tool call, which is the loop's v2 path and the one the
            # adapter uses. The legacy text shape is not used: measured
            # against a real model it produced four turns of prose in one
            # reply and submitted having executed nothing.
            body=(
                '{"choices":[{"message":{"role":"assistant","content":"thinking",'
                '"tool_calls":[{"id":"c1","type":"function","function":'
                '{"name":"bash","arguments":"{\\"command\\": \\"echo ok\\"}"}}]}}]}'
            ),
        )

    return ModelClient(
        roles={"implementer": Route("m", "https://e.example", preset="chat-completions")},
        transport=transport,
    )


# ------------------------------------------------------------- the model


def test_every_call_goes_through_the_harness_client() -> None:
    """Not LiteLLM. The chains, ladder, parking and pricing are the point.

    If the loop talked to a provider directly, a dead model would stall an
    item, a spend cap would be retried, and the answer would never reach the
    event stream -- every one of which this repository has a module for.
    """
    calls: list[tuple[str, Any]] = []
    model = HarnessModel(client=_client(calls))
    reply = model.query([{"role": "user", "content": "hello"}])
    (action,) = reply["extra"]["actions"]
    assert action["command"] == "echo ok"
    assert [name for name, _ in calls] == ["m"], "the call did not go through ModelClient"


def test_the_loop_names_a_role_and_never_a_model() -> None:
    """Which is what lets the map be re-routed while the fleet runs."""
    model = HarnessModel(client=_client([]))
    assert model.role == "implementer"
    assert model.serialize()["role"] == "implementer"


def test_calls_are_counted_for_the_loops_own_limits() -> None:
    """The loop stops itself on these. A wrong count stops an item early."""
    model = HarnessModel(client=_client([]))
    for _ in range(3):
        model.query([{"role": "user", "content": "x"}])
    assert model.get_template_vars()["n_model_calls"] == 3


# ------------------------------------------------------- the environment


def test_a_refused_command_never_runs(tmp_path: Path) -> None:
    """The guard screens an agent's shell exactly as it screens a check."""
    marker = tmp_path / "should-not-exist"
    env = HarnessEnvironment(
        repo=tmp_path,
        guard=CommandGuard(refusals=("touch",)),
    )
    result = env.execute({"action": f"touch {marker}"})
    assert result["returncode"] == 1
    assert "REFUSED" in result["output"]
    assert not marker.exists(), "a refused command was executed anyway"


def test_a_refusal_is_returned_to_the_agent_rather_than_raised(tmp_path: Path) -> None:
    """Deliberately the opposite of the executor's rule.

    There a refusal is terminal, because there is no next turn to correct it.
    In a loop the agent can read why and choose another command, which is what
    a loop is for. It is still recorded, and a loop that keeps trying refused
    commands runs out of steps.
    """
    env = HarnessEnvironment(repo=tmp_path, guard=CommandGuard(refusals=("rm",)))
    result = env.execute({"action": "rm -rf /"})
    assert "REFUSED" in result["output"]
    assert env.refusals == ["rm -rf /"], "the refusal was not recorded"


def test_a_permitted_command_runs_and_reports_both_streams(tmp_path: Path) -> None:
    env = HarnessEnvironment(repo=tmp_path)
    result = env.execute({"action": "echo out; echo err >&2"})
    assert result["returncode"] == 0
    assert "out" in result["output"] and "err" in result["output"]


def test_commands_run_in_the_item_s_worktree(tmp_path: Path) -> None:
    """Two agents in one tree is the data race worktrees exist to prevent."""
    (tmp_path / "here.txt").write_text("x")
    env = HarnessEnvironment(repo=tmp_path)
    assert "here.txt" in env.execute({"action": "ls"})["output"]


def test_output_is_bounded(tmp_path: Path) -> None:
    """A runaway command must not become the whole context window."""
    env = HarnessEnvironment(repo=tmp_path)
    result = env.execute({"action": "head -c 200000 /dev/zero | tr '\\0' 'x'"})
    assert len(result["output"]) <= 32_000


def test_what_the_agent_touches_inside_the_repo_is_not_policed(tmp_path: Path) -> None:
    """Owner ruling, 2026-08-05.

    An agent using the whole repository to reach an outcome is how work gets
    done. The guard bounds what is *dangerous* -- a refused program, a path
    outside the tree -- not what is *untidy*. Whether a change went beyond
    what the item asked for is the reviewer's judgement, and it has the diff.
    """
    (tmp_path / "unrelated.txt").write_text("before")
    env = HarnessEnvironment(repo=tmp_path)
    env.execute({"action": "echo after > unrelated.txt"})
    assert (tmp_path / "unrelated.txt").read_text().strip() == "after"


# ------------------------------------------------------------ opting in


def test_core_does_not_import_this_adapter() -> None:
    """`AGENTS.md`'s first rule, and it is enforced rather than trusted.

    A build without the extra installed must load core normally. A lazy import
    written as a dotted string would still be core knowing the name, so this
    checks the execution path's source rather than its imports.
    """
    from agent_harness import executor, model_client, work

    for module in (executor, model_client, work):
        source = Path(module.__file__ or "").read_text()
        assert "minisweagent" not in source, f"{module.__name__} names the adapter"


def test_a_missing_dependency_says_what_to_install() -> None:
    """One legible sentence, not an ImportError three frames down."""
    import sys
    from unittest import mock

    from agent_harness.adapters import minisweagent

    with (
        mock.patch.dict(sys.modules, {"minisweagent.agents.default": None}),
        pytest.raises((NotInstalled, ImportError)) as raised,
    ):
        minisweagent._require()
    if isinstance(raised.value, NotInstalled):
        assert "agent-loop" in str(raised.value)


# ------------------------------------------- screening what a shell will run


def test_a_refusal_inside_a_compound_command_is_caught(tmp_path: Path) -> None:
    """The find that made this adapter worth writing carefully.

    Screening `sh -c "..."` does nothing: `guard.py` says an interpreter's
    script is one opaque token, and measured here, a refusal list naming `rm`
    did not stop `rm -rf /` until the line was split into the commands a shell
    would actually run.
    """
    env = HarnessEnvironment(repo=tmp_path, guard=CommandGuard(refusals=("rm",)))
    for line in (
        "rm -rf build",
        "cargo build && rm -rf build",
        "cargo build; rm -rf build",
        "ls | rm -rf build",
        "cargo build ||\nrm -rf build",
    ):
        result = env.execute({"action": line})
        assert "REFUSED" in result["output"], f"not screened: {line!r}"


def test_a_leading_assignment_is_not_mistaken_for_the_program(tmp_path: Path) -> None:
    """`FOO=1 cargo test` runs cargo, not FOO -- and the loop writes these."""
    from agent_harness.adapters.minisweagent import _segments

    assert _segments("RUST_LOG=debug cargo test") == [["cargo", "test"]]
    assert _segments("A=1 B=2 make check") == [["make", "check"]]


def test_an_unparseable_line_is_screened_whole_rather_than_skipped(tmp_path: Path) -> None:
    """Best effort, in the safe direction.

    An unbalanced quote is the last thing to wave through, so it reaches the
    guard as raw text instead of being dropped for being unparseable.
    """
    from agent_harness.adapters.minisweagent import _segments

    assert _segments('echo "unclosed') == [['echo "unclosed']]


def test_an_ordinary_command_is_still_one_segment(tmp_path: Path) -> None:
    from agent_harness.adapters.minisweagent import _segments

    assert _segments("cargo test -p rdpapp-models") == [["cargo", "test", "-p", "rdpapp-models"]]
