"""The whole loop, end to end, against a scripted model and a real repository.

Written after four integration bugs were each found by a ten-minute live run
against a real gateway, at real cost, one at a time:

1. the environment supplied no template variables, so the first render died
   with `'system' is undefined`;
2. commands were screened as `sh -c "..."`, which `guard.py` says is one
   opaque token -- a refusal list naming `rm` did not stop `rm -rf /`;
3. a hand-rolled transport sent the credential wrong, and every route came
   back `credential rejected`;
4. the model returned only `content`, so the loop found no actions in
   `extra` and executed **nothing** while appearing to run.

Every one of them is a wiring mistake findable in under a second without a
network. That is what this file is for. The model is scripted, the repository
is a real git repository in a temp directory, and the check command is a real
subprocess -- so the only thing not exercised is model quality, which is the
one thing a test cannot assert anyway.

The same principle `demo.py` already applies to the direct executor: replace
the transport, keep everything else.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_harness.adapters.minisweagent import HarnessEnvironment, HarnessModel, build
from agent_harness.guard import CommandGuard
from agent_harness.model_client import ModelClient, Response, Route

DONE = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def scripted(*commands: str) -> tuple[ModelClient, list[list[dict[str, str]]]]:
    """A client that replies with each command in turn, in the loop's format.

    Returns the client and the list of message-lists it was asked, so a test
    can assert what the loop actually saw -- including that observations came
    back to it, which is the difference between a loop and a sequence.
    """
    asked: list[list[dict[str, str]]] = []
    replies = iter(commands)

    def transport(route: Route, messages: Any, options: Any) -> Response:
        asked.append(list(messages))
        try:
            command = next(replies)
        except StopIteration:  # pragma: no cover - a test script ran out
            command = DONE
        import json

        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": f"Doing the next thing.\n\n"
                            f"```mswea_bash_command\n{command}\n```"
                        }
                    }
                ]
            }
        )
        return Response(status=200, headers={}, body=body)

    client = ModelClient(
        roles={"implementer": Route("scripted", "https://e.example", preset="chat-completions")},
        transport=transport,
    )
    return client, asked


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repository with a real failing check.

    Real because the bugs this file exists to catch were all in the seam
    between the loop and something real -- a shell, a filesystem, a subprocess.
    A mocked environment would have reproduced none of them.
    """
    where = tmp_path / "repo"
    where.mkdir()
    (where / "answer.txt").write_text("wrong\n")
    (where / "check.sh").write_text('#!/bin/sh\ngrep -q "^right$" answer.txt\n')
    (where / "check.sh").chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=where, check=True)
    subprocess.run(["git", "add", "-A"], cwd=where, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=where,
        check=True,
    )
    return where


def test_the_loop_reads_edits_and_verifies_until_the_check_passes(repo: Path) -> None:
    """The whole point of a loop, asserted end to end.

    Look, change, verify, finish -- with the check failing first and passing
    after, so the test cannot pass on a loop that never ran the command.
    """
    client, asked = scripted(
        "cat answer.txt",
        "./check.sh",
        "echo right > answer.txt",
        "./check.sh",
        DONE,
    )
    agent = build(client, repo)
    result = agent.run("Make ./check.sh pass.")

    assert result.get("exit_status") == "Submitted"
    assert (repo / "answer.txt").read_text() == "right\n", "the loop did not change the file"
    assert subprocess.run(["./check.sh"], cwd=repo).returncode == 0

    # It is a loop, not a sequence: each turn saw the previous output.
    seen = "\n".join(m["content"] for m in asked[-1] if isinstance(m.get("content"), str))
    assert "wrong" in seen, "the observation from `cat` never came back to the model"


def test_actions_actually_reach_the_environment(repo: Path) -> None:
    """The bug that made the loop run and do nothing.

    Actions are read from `extra.actions`; a model returning only `content`
    produces a loop that terminates looking healthy having executed nothing.
    """
    client, _ = scripted("echo touched > marker.txt", DONE)
    build(client, repo).run("Leave a marker.")
    assert (repo / "marker.txt").exists(), "no action was executed"


def test_a_refused_command_is_answered_and_the_loop_continues(repo: Path) -> None:
    """A refusal is a correction inside a loop, not a terminal outcome.

    The opposite of the executor's rule (#187), and deliberately so: here
    there IS a next turn, and the agent can read why and choose differently.
    """
    client, _ = scripted("rm -rf .", "echo recovered > answer.txt", DONE)
    agent = build(client, repo, guard=CommandGuard(refusals=("rm",)))
    result = agent.run("Try something forbidden, then do the job.")

    assert agent.env.refusals == ["rm -rf ."], "the refusal was not recorded"
    assert (repo / "check.sh").exists(), "a refused command was executed anyway"
    assert (repo / "answer.txt").read_text() == "recovered\n", "the loop did not carry on"
    assert result.get("exit_status") == "Submitted"


def test_a_refusal_hidden_in_a_compound_command_is_still_caught(repo: Path) -> None:
    """`sh -c` screening was a no-op. This is the regression guard."""
    client, _ = scripted("echo safe && rm -rf .", DONE)
    agent = build(client, repo, guard=CommandGuard(refusals=("rm",)))
    agent.run("Try to smuggle it past.")
    assert agent.env.refusals, "the compound command was not screened"
    assert (repo / "check.sh").exists()


def test_the_step_limit_bounds_a_loop_that_will_not_stop(repo: Path) -> None:
    """An unbounded loop against a per-item budget spends all of it on one item."""
    client, _ = scripted(*(["echo still going"] * 50))
    agent = build(client, repo, step_limit=4)
    agent.run("Never finish.")
    assert agent.model.n_calls <= 5, f"ran {agent.model.n_calls} turns past a limit of 4"


def test_every_call_is_billed_to_a_role_the_deployment_can_re_route(repo: Path) -> None:
    """Not a model name. That is what makes PUT /api/roles possible."""
    client, _ = scripted(DONE)
    agent = build(client, repo)
    agent.run("Finish immediately.")
    assert agent.model.role == "implementer"
    assert agent.model.n_calls == 1


def test_the_environment_supplies_what_the_loops_templates_render(repo: Path) -> None:
    """`'system' is undefined` killed a live run before its first model call."""
    variables = HarnessEnvironment(repo=repo).get_template_vars()
    assert "system" in variables, "the loop's own templates reference this"
    assert variables["cwd"] == str(repo)


def test_a_malformed_reply_is_handed_back_to_the_model_not_raised(repo: Path) -> None:
    """Their loop already knows how to say "give me exactly one action".

    Catching it here would replace a correction the model can act on with an
    error nobody sees.
    """
    import json

    def transport(route: Route, messages: Any, options: Any) -> Response:
        return Response(
            status=200,
            headers={},
            body=json.dumps({"choices": [{"message": {"content": "no command at all"}}]}),
        )

    client = ModelClient(
        roles={"implementer": Route("m", "https://e.example", preset="chat-completions")},
        transport=transport,
    )
    model = HarnessModel(client=client)
    from minisweagent.exceptions import FormatError

    with pytest.raises(FormatError):
        model.query([{"role": "user", "content": "go"}])
