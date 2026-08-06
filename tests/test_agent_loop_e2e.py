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
   `extra` and executed **nothing** while appearing to run;
5. no `Submitted` was raised on the finish marker, so a loop that should have
   stopped after one turn ran to its step limit -- in a real run, forty model
   calls to reach an outcome it had already decided;
6. the loop's own bookkeeping (`extra`, an `exit` role) reached the wire, and
   the gateway refused every route with `upstream_rejected`.

The fifth and sixth were found here, in under a second each. The sixth had
already cost a live run before this file existed.

A second pass, after a real rdpapp item came back `LimitsExceeded` with 40
model calls and **15 of the 40 turns refused by the guard** — a loop that was
working, creating files, and running out of steps being told no:

7. `_segments` split a line by replacing its separators, which is not a shell.
   A heredoc *body* was chopped into argv and screened as commands, so writing
   a source file — the most common thing a coding agent does — was refused for
   containing `&&`, `|`, or a bare `/`. Measured over the shapes of that run,
   6 of 9 file-writes were refused before and 0 after;
8. and in the other direction, the same splitter could not see `$(...)`,
   backticks, `env`/`xargs`/`nohup`/`timeout`, `find -exec` or a subshell, so
   `echo $(rm -rf .)` ran with a refusal list naming `rm`. 3 of 7 genuinely
   dangerous lines were refused before, 7 of 7 after;
9. `HarnessModel.cost` was never written to, so the loop's own spend ceiling
   could not fire and a per-item budget reached nothing that runs long;
10. `subprocess.TimeoutExpired` was unhandled, so one hung command ended the
    item with a traceback rather than a turn;
11. output truncation kept the last 32k, unmarked — which threw away the head
    of every compiler error and, when the agent said it had finished and then
    printed anything, the finish marker itself;
12. the assistant's own turns reached the wire empty, because the action lived
    only in the `tool_calls` this strips: thirty outputs and no way to tell
    which command produced which;
13. a gateway answering HTTP 200 with an error body was reported as the model
    failing to format an action, three calls later;
14. and the correction handed back when no tool call arrived was the *text*
    protocol's — "provide EXACTLY ONE action in triple backticks" — which is
    bug #8 above, surviving in the one message that only appears when things
    have already gone wrong.

Every one of them is a wiring mistake findable in under a second without a
network. That is what this file is for. The model is scripted, the repository
is a real git repository in a temp directory, and the check command is a real
subprocess -- so the only thing not exercised is model quality, which is the
one thing a test cannot assert anyway.

The same principle `demo.py` already applies to the direct executor: replace
the transport, keep everything else.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_harness.adapters.minisweagent import (
    HarnessEnvironment,
    HarnessModel,
    MalformedReply,
    build,
)
from agent_harness.budgets import Budget
from agent_harness.guard import CommandGuard, Refusal
from agent_harness.model_client import ModelClient, Response, Route
from agent_harness.pricing import Price, PriceTable

DONE = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def scripted(
    *commands: str,
    prices: PriceTable | None = None,
    tokens: int = 0,
) -> tuple[ModelClient, list[list[dict[str, str]]]]:
    """A client that replies with each command in turn, in the loop's format.

    Returns the client and the list of message-lists it was asked, so a test
    can assert what the loop actually saw -- including that observations came
    back to it, which is the difference between a loop and a sequence.

    `tokens` and `prices` make the reply *cost* something. A real endpoint
    reports usage on every call and a real deployment has a price table; a
    scripted model that reports neither is the reason a spend ceiling could sit
    in the code untested while never firing.
    """
    asked: list[list[dict[str, str]]] = []
    sent: list[dict[str, Any]] = []
    replies = iter(commands)

    def transport(route: Route, messages: Any, options: Any) -> Response:
        asked.append(list(messages))
        sent.append(dict(options))
        try:
            command = next(replies)
        except StopIteration:  # pragma: no cover - a test script ran out
            command = DONE

        # A tool call, which is what the loop's v2 path expects. The legacy
        # text-regex shape is deliberately not used here: measured against a
        # real model, it returned four turns of prose in one reply and
        # submitted having executed nothing.
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "doing the next thing",
                            "tool_calls": [
                                {
                                    "id": f"call_{len(asked)}",
                                    "type": "function",
                                    "function": {
                                        "name": "bash",
                                        "arguments": json.dumps({"command": command}),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": tokens, "completion_tokens": tokens},
            }
        )
        return Response(status=200, headers={}, body=body)

    client = ModelClient(
        roles={"implementer": Route("scripted", "https://e.example", preset="chat-completions")},
        transport=transport,
        prices=prices,
    )
    client.sent_options = sent  # type: ignore[attr-defined]
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


def test_what_reaches_the_wire_is_a_chat_message_and_nothing_else(repo: Path) -> None:
    """The suspect behind a live `upstream_rejected` from the gateway.

    The loop keeps its own bookkeeping on messages -- `extra` carrying parsed
    actions, and an `exit` role when it finishes. Those are its business. A
    chat-completions endpoint is entitled to reject a message object with
    fields it does not define, and LiteLLM strips them where our transport
    would pass them straight through.

    So this asserts the shape we send, not the shape we hold: every message
    reaching the transport must have exactly `role` and `content`, and a role
    the API actually defines.
    """
    client, asked = scripted("cat answer.txt", "./check.sh", DONE)
    build(client, repo).run("Look, check, finish.")

    assert asked, "no call reached the transport"
    for turn in asked:
        for message in turn:
            assert set(message) == {"role", "content"}, f"extra fields on the wire: {message}"
            assert message["role"] in {"system", "user", "assistant"}, message["role"]
            assert isinstance(message["content"], str)


def test_the_bash_tool_is_offered_on_every_call(repo: Path) -> None:
    """Tool calls, not text parsing -- and this is what makes it one.

    Measured against a real model on the legacy text-regex path: it returned
    four turns' worth of `THOUGHT:` prose in a single reply and one action,
    and that action was the finish marker. It submitted on turn one having
    executed nothing. `mini-swe-agent` v2 recommends tool calls for this
    reason; a run that quietly stopped sending the tool would regress to it.
    """
    client, _ = scripted("ls", DONE)
    build(client, repo).run("Look around, then finish.")

    options = client.sent_options  # type: ignore[attr-defined]
    assert options, "nothing reached the transport"
    for sent in options:
        names = [t["function"]["name"] for t in sent.get("tools", [])]
        assert names == ["bash"], f"the bash tool was not offered: {sent.get('tools')}"


def test_an_observation_goes_back_as_a_well_formed_turn(repo: Path) -> None:
    """A `tool` message needs its `tool_call_id`, and ours are stripped.

    `_for_the_wire` reduces a message to `role` and `content`, so a `tool`
    role would arrive without the id it must answer -- a malformed request,
    which is what `upstream_rejected` was. The output goes back as a user
    turn instead: the pairing is lost, which costs nothing when there is one
    tool, and the conversation stays valid.
    """
    client, asked = scripted("cat answer.txt", DONE)
    build(client, repo).run("Read it, then finish.")

    last = asked[-1]
    assert all(m["role"] in {"system", "user", "assistant"} for m in last), last
    seen = "\n".join(m["content"] for m in last)
    assert "wrong" in seen, "the command output never returned to the model"


def test_the_prompts_match_the_protocol() -> None:
    """Prompts and protocol must agree, and once they did not.

    `default.yaml` instructs the model to emit ```mswea_bash_command``` fences
    -- the text path. Pairing it with tool calls produced five consecutive
    replies containing no tool call and a `RepeatedFormatError`. The model did
    what it was told; it was told the wrong thing.

    Asserted against the config actually loaded, so the two cannot drift apart
    silently again.
    """
    from pathlib import Path

    import minisweagent
    import yaml

    from agent_harness.adapters.minisweagent import CONFIG

    text = (Path(minisweagent.package_dir) / "config" / CONFIG).read_text()
    prompts = yaml.safe_load(text)["agent"]
    joined = prompts["system_template"] + prompts["instance_template"]
    assert "mswea_bash_command" not in joined, "these prompts ask for the TEXT protocol"
    assert "tool call" in joined.lower(), "these prompts do not ask for a tool call"


# ------------------------------------------- what a shell really runs (#2, again)
#
# The `sh -c` find was fixed by splitting a line on its separators. That
# split was a `str.replace`, which is not a shell: it cannot see a quote, it
# cannot see a substitution, and it cannot see that a heredoc body is a
# document rather than a script. Each of the following ran, or was refused,
# for one of those reasons -- against a real repository and a real shell.


def test_a_command_substitution_cannot_smuggle_a_refused_program(repo: Path) -> None:
    """`$(...)` is a command, and it was screened as an argument.

    `echo $(rm -rf .)` reaches the shell as `echo`, `$(rm`, `-rf`, `.)` -- the
    program is `echo`, so a refusal list naming `rm` matched nothing and the
    deletion ran. Backticks are the same hole spelled the older way.
    """
    for line in ("echo $(rm -rf .)", "echo `rm -rf .`", 'echo "$(rm -rf .)"'):
        client, _ = scripted(line, DONE)
        agent = build(client, repo, guard=CommandGuard(refusals=("rm",)))
        agent.run("Try to smuggle it past.")
        assert agent.env.refusals == [line], f"not screened: {line!r}"
        assert (repo / "check.sh").exists(), f"a refused command ran anyway: {line!r}"


def test_a_wrapper_does_not_launder_a_refused_program(repo: Path) -> None:
    """`env rm` runs `rm`, and its argv[0] is `env`.

    A pattern list matches the program. Every one of these runs a refused
    program under another program's name, which is the cheapest possible way
    around a refusal list and needs no cleverness at all.
    """
    for line in ("env rm -rf .", "nohup rm -rf .", "ls | xargs rm -rf", "timeout 5 rm -rf ."):
        client, _ = scripted(line, DONE)
        agent = build(client, repo, guard=CommandGuard(refusals=("rm",)))
        agent.run("Try to launder it.")
        assert agent.env.refusals == [line], f"not screened: {line!r}"
        assert (repo / "check.sh").exists(), f"a refused command ran anyway: {line!r}"


def test_find_exec_is_the_command_it_names(repo: Path) -> None:
    """`find . -exec rm -rf {} \\;` deletes. Its program is `find`."""
    client, _ = scripted("find . -name '*.sh' -exec rm -rf {} \\;", DONE)
    agent = build(client, repo, guard=CommandGuard(refusals=("rm",)))
    agent.run("Tidy up.")
    assert agent.env.refusals, "the -exec command was not screened"
    assert (repo / "check.sh").exists(), "a refused command ran anyway"


def test_a_subshell_is_not_a_program_called_open_paren(repo: Path) -> None:
    """`(rm -rf .)` split into no segments a pattern could match."""
    for line in ("(rm -rf .)", "{ rm -rf . ; }", "ls & rm -rf ."):
        client, _ = scripted(line, DONE)
        agent = build(client, repo, guard=CommandGuard(refusals=("rm",)))
        agent.run("Try a subshell.")
        assert agent.env.refusals == [line], f"not screened: {line!r}"
        assert (repo / "check.sh").exists(), f"a refused command ran anyway: {line!r}"


def test_a_separator_inside_quotes_is_not_a_second_command(repo: Path) -> None:
    """A false positive is a bug too, and this one refused ordinary text.

    `sudo` is a built-in refusal, so a message *mentioning* it after a `;`
    inside quotes was cut out of its quotes and screened as a command. The
    agent then loses a turn to a refusal for writing a sentence.
    """
    client, _ = scripted('echo "step 1; sudo needed?" "no" > notes.txt', "cat notes.txt", DONE)
    agent = build(client, repo)
    agent.run("Leave a note.")

    assert agent.env.refusals == [], f"refused ordinary text: {agent.env.refusals}"
    assert "sudo needed?" in (repo / "notes.txt").read_text()


def test_a_heredoc_body_is_the_file_being_written(repo: Path) -> None:
    """The loop's own prompts teach `cat <<'EOF' > file`. It was refused.

    Every line of the *document* was screened as a command, so writing a README
    that mentions `/etc/hosts` tripped the worktree boundary and writing one
    that begins a line with `sudo` tripped the refusal list. The body is data.
    """
    client, _ = scripted(
        "cat > README.md <<'EOF'\nsee /etc/hosts for details\nsudo is never needed\nEOF",
        DONE,
    )
    agent = build(client, repo)
    agent.run("Write the README.")

    assert agent.env.refusals == [], f"refused a document: {agent.env.refusals}"
    assert "see /etc/hosts for details" in (repo / "README.md").read_text()


#: One turn from the measured rdpapp run, reconstructed: the agent writing a
#: source file. The *body* contains `&&`, `|` and paths, and the old splitter
#: chopped it into argv and screened each fragment as a command.
WRITES_A_SOURCE_FILE = """cat > src/legacy_sqlite.rs <<'EOF'
// see docs/schema.md and /usr/share/doc/sqlite3 for the column list
pub fn keep(col: &str, kind: &str) -> bool {
    (kind == "TEXT" || kind == "BLOB") && !col.is_empty()
}

pub fn mask(bits: u32) -> u32 {
    bits | 0x1 | 0x2
}

pub fn ratio(hit: f64, total: f64) -> f64 {
    hit / total
}
EOF"""


def test_writing_a_source_file_is_not_fifteen_refusals(repo: Path) -> None:
    """The measured failure: 40 calls, `LimitsExceeded`, 15 turns refused.

    The loop was working -- it created files -- and it ran out of steps being
    refused for writing them. `&&` and `|` inside a heredoc body are Rust, not
    shell, and the body's mention of `/usr/share/doc` is a comment rather than
    a reach outside the tree. Writing a file is the single most common thing a
    coding agent does, which is what made this the expensive one.
    """
    client, _ = scripted(WRITES_A_SOURCE_FILE, "cat src/legacy_sqlite.rs", DONE)
    (repo / "src").mkdir()
    agent = build(client, repo)
    result = agent.run("Write the module.")

    assert agent.env.refusals == [], f"refused a source file: {agent.env.refusals}"
    written = (repo / "src" / "legacy_sqlite.rs").read_text()
    assert "&& !col.is_empty()" in written, "the file was not written by the real shell"
    assert "bits | 0x1" in written
    # The dominant mechanism, measured: a bare `/` on a body line -- division,
    # a comment rule, a path fragment -- resolved to `/` and tripped path
    # confinement. Ordinary arithmetic was reaching outside the worktree.
    assert "hit / total" in written
    assert result.get("exit_status") == "Submitted"


def test_the_boundary_still_holds_around_the_heredoc(repo: Path) -> None:
    """Not weakened: the redirection target is screened even when the body is not.

    The heredoc body is data; where the file lands is not. Stated plainly: this
    passed before the heredoc change as well. It is here to prove the change did
    not buy its false-positive fix with a hole, not to demonstrate a bug.
    """
    client, _ = scripted("cat > /tmp/schema-lines <<'EOF'\nhello\nEOF", DONE)
    agent = build(client, repo)
    agent.run("Write it outside the tree.")
    assert agent.env.refusals, "a write outside the worktree was allowed"


def test_a_reach_outside_the_worktree_is_refused_and_says_where_the_edge_is(
    repo: Path,
) -> None:
    """Refusing is correct. Refusing without saying why costs the next turn too.

    The measured run retried variants of `... > /tmp/schema-lines && cat
    /tmp/schema-lines` because nothing in the refusal named the boundary. This
    changes the *message*, not the policy: the tree is named, the rule is
    stated as permanent, and the count says how much of the budget has gone.
    """
    client, asked = scripted(
        "echo x > /tmp/schema-lines && cat /tmp/schema-lines",
        "echo x > ./schema-lines",
        DONE,
    )
    agent = build(client, repo)
    result = agent.run("Stage the schema somewhere.")

    assert agent.env.refusals == ["echo x > /tmp/schema-lines && cat /tmp/schema-lines"]
    assert agent.env.refused[0].reason_kind == "path_escape"

    # The message the model was actually given, on the turn after the refusal.
    told = "\n".join(m["content"] for m in asked[1])
    assert "REFUSED" in told
    assert str(repo) in told, "the refusal never says where the boundary is"
    assert "refused again" in told, "the agent is not told the rule is permanent"
    # And it complied on the next turn, inside the tree.
    assert (repo / "schema-lines").read_text() == "x\n"
    assert result.get("exit_status") == "Submitted"


def test_a_quoted_separator_is_not_a_second_command_but_a_real_one_still_is(
    repo: Path,
) -> None:
    """Both directions in one test, because only having one is how this broke.

    `echo "a && b"` is one command printing a string; `echo safe && rm -rf .`
    is two, and the second must still be refused.

    Stated plainly: this one passed before the fix too, and only by luck --
    cutting `echo "a && b"` at the `&&` left both halves with an unbalanced
    quote, and the unparseable-line fallback made each a single token that
    matched no pattern. Add a second quoted argument (see the `sudo` test
    above) and the same accident refuses ordinary text. It is pinned here
    because the luck is not a property anyone should rely on.
    """
    client, _ = scripted('echo "a && b" > quoted.txt', DONE)
    agent = build(client, repo, guard=CommandGuard(refusals=("rm",)))
    agent.run("Print a string containing a separator.")
    assert agent.env.refusals == [], "refused a quoted separator"
    assert (repo / "quoted.txt").read_text() == "a && b\n"

    client, _ = scripted("echo safe && rm -rf .", DONE)
    agent = build(client, repo, guard=CommandGuard(refusals=("rm",)))
    agent.run("Try to smuggle it past.")
    assert agent.env.refusals == ["echo safe && rm -rf ."], "a real separator was missed"
    assert (repo / "check.sh").exists()


def test_a_heredoc_fed_to_an_interpreter_is_still_a_script(repo: Path) -> None:
    """The guard is not weakened to fix the false positive above.

    `bash <<EOF` really does execute its body, so that body keeps being
    screened line by line. Only a body going somewhere else is treated as data.
    """
    client, _ = scripted("bash <<'EOF'\nrm -rf .\nEOF", DONE)
    agent = build(client, repo, guard=CommandGuard(refusals=("rm",)))
    agent.run("Try a script.")

    assert agent.env.refusals, "an interpreted heredoc body was not screened"
    assert (repo / "check.sh").exists(), "a refused command ran anyway"


# --------------------------------------------------- what the model is shown


def test_a_command_that_runs_out_of_time_is_a_turn_and_not_a_crash(repo: Path) -> None:
    """`subprocess.TimeoutExpired` was not handled anywhere.

    It left `execute`, and the loop does not catch it: `DefaultAgent.run`
    records an exit message and re-raises, so one hung test command ended the
    whole item with a traceback and no submission. The agent is told instead,
    in the same shape as every other result, and picks something cheaper.
    """
    client, asked = scripted("sleep 30", "echo right > answer.txt", "./check.sh", DONE)
    agent = build(client, repo, timeout=1)
    result = agent.run("Run the slow thing, then the job.")

    assert result.get("exit_status") == "Submitted"
    assert (repo / "answer.txt").read_text() == "right\n", "the loop did not carry on"
    seen = "\n".join(m["content"] for m in asked[-1])
    assert "124" in seen, "the model was never told the command timed out"
    assert "timeout" in seen.lower()


def test_the_submit_marker_survives_a_long_command(repo: Path) -> None:
    """Truncation kept the LAST 32k, and the marker has to be the FIRST line.

    So a command that said it was finished and then printed anything sizeable
    had its own submission cut off, and the loop ran on to its step limit --
    bug #5 again, reached by a different road. The marker is looked for in the
    whole output; only what the model is *shown* is bounded.
    """
    client, _ = scripted(f"{DONE}; seq 1 200000", *(["echo still going"] * 10))
    agent = build(client, repo, step_limit=8)
    result = agent.run("Finish, noisily.")

    assert result.get("exit_status") == "Submitted", "the finish marker was truncated away"
    assert agent.model.n_calls == 1, f"ran {agent.model.n_calls} turns after finishing"


def test_truncated_output_says_so_and_keeps_both_ends(repo: Path) -> None:
    """Unmarked truncation is a model reading a fragment as the whole thing.

    And which end to keep is not a matter of taste: a compiler puts its first
    error at the top, a test runner puts its summary at the bottom. Keeping one
    end silently throws the answer away for half the commands an agent runs.
    """
    client, asked = scripted("seq 1 200000", DONE)
    build(client, repo).run("Look at a lot of output.")

    observation = asked[-1][-1]["content"]
    assert "elided" in observation, "the model cannot tell this output was cut"
    assert '"1\\n2\\n3' in observation, "the head is gone"
    assert "199999" in observation, "the tail is gone"
    assert len(observation) < 80_000


def test_the_model_can_tell_which_command_produced_which_output(repo: Path) -> None:
    """`tool_calls` is dropped on the way to the wire, and nothing replaced it.

    The observation comes back as a plain user turn -- correct, because a `tool`
    turn needs the id this strips -- but the assistant turn it answers held the
    action only in `tool_calls`. A model that replies with a tool call and no
    prose therefore saw its own turns as empty strings, and could not tell
    which of thirty outputs answered which of thirty commands.
    """
    client, asked = scripted("cat answer.txt", "ls", DONE)
    build(client, repo).run("Look twice.")

    said = [m["content"] for m in asked[-1] if m["role"] == "assistant"]
    assert any("cat answer.txt" in s for s in said), f"the commands are not in the record: {said}"
    assert any("ls" in s for s in said)
    assert all(s.strip() for s in said), "an empty assistant turn"


# ------------------------------------------------- ceilings that actually fire


def test_a_spend_ceiling_stops_a_loop_that_will_not_stop(repo: Path) -> None:
    """`HarnessModel.cost` was never written to, so no spend ceiling could fire.

    The loop reads a cost off each reply's `extra` and stops itself at
    `cost_limit`; nothing put one there, so `cost` was 0.0 for the life of every
    run and the ONLY bound on an agent was its step count. A per-item spend
    budget was declared, and then not enforced for the whole time the agent ran.
    """
    prices = PriceTable(version="test", prices={"scripted": Price(1.0, 1.0)})
    client, _ = scripted(*(["echo still going"] * 20), prices=prices, tokens=1_000_000)
    agent = build(client, repo, step_limit=20, budget=Budget(spend_usd=3.0))
    result = agent.run("Never finish.")

    assert result.get("exit_status") == "LimitsExceeded"
    assert agent.model.n_calls == 2, f"spent past the ceiling for {agent.model.n_calls} calls"
    assert agent.model.cost == pytest.approx(4.0)
    assert agent.model.spend.measurable, "every call was priced; this should be enforceable"


def test_an_unpriced_call_is_not_a_free_one(repo: Path) -> None:
    """`budgets.py`'s rule, kept here: unknown cost is not zero cost.

    An unpriced model must leave the ceiling reported as a lower bound rather
    than quietly satisfied -- otherwise a run against a model nobody priced
    looks comfortably inside a budget it may have blown.
    """
    client, _ = scripted("ls", DONE, tokens=1000)
    agent = build(client, repo)
    agent.run("Look, then finish.")

    assert agent.model.spend.unpriced == 2
    assert not agent.model.spend.measurable, "an unpriced call must not read as measured"
    assert agent.model.serialize()["cost_measurable"] is False


def test_one_unpriced_call_makes_the_loop_s_dollar_ceiling_unenforceable(repo: Path) -> None:
    """A known subtotal is not an enforceable total after one unknown call."""
    replies = iter(("echo first", "echo second", DONE))
    calls = 0

    def transport(route: Route, messages: Any, options: Any) -> Response:
        nonlocal calls
        del route, messages, options
        calls += 1
        command = next(replies)
        payload: dict[str, Any] = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "working",
                        "tool_calls": [
                            {
                                "id": f"call_{calls}",
                                "type": "function",
                                "function": {
                                    "name": "bash",
                                    "arguments": json.dumps({"command": command}),
                                },
                            }
                        ],
                    }
                }
            ]
        }
        if calls > 1:
            payload["usage"] = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
        return Response(200, {}, json.dumps(payload))

    client = ModelClient(
        roles={"implementer": Route("scripted", "https://e.example")},
        transport=transport,
        prices=PriceTable(version="test", prices={"scripted": Price(1.0, 1.0)}),
    )
    agent = build(client, repo, step_limit=5, budget=Budget(spend_usd=1.0))

    result = agent.run("Finish despite an unenforceable dollar total.")

    assert result.get("exit_status") == "Submitted"
    assert calls == 3
    assert agent.model.spend.unpriced == 1
    assert agent.model.spend.usd == pytest.approx(4.0)


def test_a_wall_clock_budget_reaches_the_loop(repo: Path) -> None:
    """The item's ceilings were never handed to the thing that runs long.

    `budgets.check` runs at the boundaries the executor has; a loop's boundary
    is the whole loop, so a wall-clock ceiling could not be applied until the
    agent had already finished. The loop applies its own before every call,
    which is the boundary `budgets.py` asks for.
    """
    client, _ = scripted(DONE)
    agent = build(client, repo, budget=Budget(seconds=90, spend_usd=2.5))
    assert agent.config.wall_time_limit_seconds == 90
    assert agent.config.cost_limit == 2.5

    other, _ = scripted(DONE)
    unlimited = build(other, repo)
    assert unlimited.config.wall_time_limit_seconds == 0, "a budget nobody set is not a ceiling"
    assert unlimited.config.cost_limit == 0.0, (
        "the loop library's finite default must not override an unlimited item budget"
    )


# ------------------------------------------- a refusal that outlives the run


def test_a_refusal_leaves_a_record_the_process_does_not_own(repo: Path) -> None:
    """It was a list of strings on an object that dies with the run.

    `serialize` reported a *count*, so a run whose agent spent ten turns
    bouncing off the policy left nothing saying which rule it hit. The rule is
    the thing an operator would change.
    """
    seen: list[tuple[str, Refusal]] = []
    client, _ = scripted("rm -rf .", "echo recovered > answer.txt", DONE)
    agent = build(
        client,
        repo,
        guard=CommandGuard(refusals=("rm",)),
        on_refusal=lambda command, refusal: seen.append((command, refusal)),
    )
    result = agent.run("Try something forbidden, then do the job.")

    assert [command for command, _ in seen] == ["rm -rf ."]
    assert seen[0][1].rule == "rm"
    assert seen[0][1].reason_kind == "command_blocked"
    (recorded,) = agent.env.serialize()["refusals"]
    assert recorded["rule"] == "rm", "the trajectory does not say what refused it"
    assert recorded["reason_kind"] == "command_blocked"
    assert recorded["line"] == "rm -rf ."
    assert recorded["command"] == ["rm", "-rf", "."], "the argv the guard screened"
    assert result.get("exit_status") == "Submitted"


# ------------------------------- telling a provider's fault from a model's


def test_the_format_error_asks_for_the_protocol_the_prompts_teach(repo: Path) -> None:
    """Bug #8, in the one message that only appears when things go wrong.

    The prompts come from `mini.yaml` and ask for a tool call. The correction
    handed back when no tool call arrives was hand-copied from the *text*
    protocol -- "provide EXACTLY ONE action in triple backticks" -- and it
    dropped `{{error}}`, the only part saying what was actually wrong. A model
    that has just failed is the last one to give directions to the wrong path.
    """

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
    from minisweagent.exceptions import FormatError

    with pytest.raises(FormatError) as raised:
        HarnessModel(client=client).query([{"role": "user", "content": "go"}])

    correction = raised.value.messages[0]["content"]
    assert "backticks" not in correction, "the tool-call path is told to use the text path"
    assert "bash" in correction, "the correction never names the tool to call"
    assert "No tool calls found" in correction, "the actual error is thrown away"


def test_a_gateway_error_arriving_as_a_200_is_not_blamed_on_the_model(repo: Path) -> None:
    """An unreadable body became an empty message, which looks like a bad model.

    A gateway answering HTTP 200 with an error object produced no tool calls;
    the loop said "give me exactly one action", got the same error again, and
    ended as `RepeatedFormatError` after three calls -- a wrong diagnosis, and
    two more calls against a broken endpoint to reach it.
    """
    calls: list[int] = []

    def transport(route: Route, messages: Any, options: Any) -> Response:
        calls.append(1)
        return Response(
            status=200,
            headers={},
            body=json.dumps({"error": {"message": "upstream is on fire", "code": "bad_gateway"}}),
        )

    client = ModelClient(
        roles={"implementer": Route("m", "https://e.example", preset="chat-completions")},
        transport=transport,
    )
    agent = build(client, repo)
    with pytest.raises(MalformedReply) as raised:
        agent.run("Go.")

    assert "upstream is on fire" in str(raised.value), "what arrived is not in the message"
    assert len(calls) == 1, f"asked a broken endpoint {len(calls)} times"
