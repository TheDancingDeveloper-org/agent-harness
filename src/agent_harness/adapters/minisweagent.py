"""Run an item through a real agent loop, keeping this harness's gates.

The direct executor gives a model **one turn**: a planner picks files, a
context is assembled, one implementer call is parsed and applied. Measured
against rdpapp over four passes, that delivered nothing — and the failures
moved each time the *format* was patched rather than the loop. Edit blocks
instead of diffs, indentation tolerance, quoting the file back: each was a
real improvement and each was a substitute for letting the model look.

Measured 2026-08-05, same item, same models, same gateway, the only variable
being the loop:

| | direct executor | a loop |
|---|---|---|
| turns | 1 | 31 |
| reached the check command | once in four passes | yes |
| **`cargo test`** | never green | **passed** |

So the loop is adopted rather than rebuilt. Writing our own would spend weeks
producing something worse than a loop that scores >74% on SWE-bench, to solve
a problem two protocol implementations solve.

**What this adapter is for is the seam.** `mini-swe-agent` defines `Model` and
`Environment` as protocols, each one method wide, so this supplies both:

- `HarnessModel` routes every call through `ModelClient`, which keeps the
  fallback chains, the retry ladder, per-endpoint parking, failure
  classification, pricing and the recorded answer. The loop does not learn
  what a provider is.
- `HarnessEnvironment` puts every command the agent runs through
  `CommandGuard`, so the refusal list and the worktree boundary hold for an
  agent's shell exactly as they do for a check command.

What is deliberately **not** enforced here: which files inside the repository
the agent touches. Owner ruling, 2026-08-05 — an agent using the whole
repository to reach an outcome is how work actually gets done, and the
reviewer gate is where "it changed something the item did not ask for" is
judged. The guard bounds what is *dangerous*, not what is *untidy*.

Opt-in, per `AGENTS.md`: nothing in core imports this, and the dependency is
an extra. A build without `mini-swe-agent` installed loads none of it and
says so plainly rather than failing somewhere later.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..guard import CommandGuard, CommandRefused
from ..model_client import ModelClient

#: The role this loop's calls are billed and routed under. The same name the
#: direct executor uses, so a deployment does not have to configure a second
#: one and the audit does not grow a second word for the same job.
IMPLEMENTER = "implementer"

#: How the loop says it has finished. Theirs, quoted: the agent is told to
#: echo it, and an environment that does not notice produces an agent that
#: cannot stop.
SUBMIT_MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"

#: Their tool-calling config, and their own default for `mini`. The prompts
#: must match the protocol: `default.yaml` tells the model to emit
#: ```mswea_bash_command``` fences, which is the *text* path. Pairing those
#: prompts with tool calls produced five straight replies with no tool call
#: and a `RepeatedFormatError` -- the model did exactly what it was told, and
#: it was told the wrong thing.
CONFIG = "mini.yaml"


class NotInstalled(RuntimeError):
    """`mini-swe-agent` is not installed. Said once, plainly, up front."""


def _require() -> Any:
    """Import the loop, or explain exactly what to do about it.

    Lazily, and never at module import: `AGENTS.md` requires an adapter to be
    loadable without its dependency present, so that a deployment that does
    not use it pays nothing and a deployment that misconfigures it gets one
    legible sentence instead of an ImportError from three frames down.
    """
    try:
        from minisweagent.agents.default import DefaultAgent
    except ImportError as exc:  # pragma: no cover - exercised by absence
        raise NotInstalled(
            "the mini-swe-agent execution mode needs the `agent-loop` extra: "
            "`uv sync --extra agent-loop`. Nothing else in the harness requires it."
        ) from exc
    return DefaultAgent


#: Shell operators that separate one command from the next. Screening
#: `sh -c "..."` does not work -- `guard.py` says so itself: an interpreter's
#: script is a single opaque token, so `rm -rf /` inside one is invisible.
#: Measured: a refusal list naming `rm` did not stop `rm -rf /` until the
#: command was split. So the string is separated into the commands a shell
#: would actually run, and each is screened as the argv it will become.
_SEPARATORS = ("&&", "||", ";", "|", "\n")


def _segments(command: str) -> list[list[str]]:
    """Every simple command inside a shell line, as argv.

    Best effort, and it must stay best effort in the safe direction: anything
    this cannot parse is handed to the guard *whole* rather than skipped, so a
    line it does not understand is screened conservatively instead of waved
    through. Quoting, substitution and redirection defeat exact parsing --
    that is why the worktree boundary, not this, is the guarantee.
    """
    import shlex

    text = command
    for separator in _SEPARATORS:
        text = text.replace(separator, "\x00")

    out: list[list[str]] = []
    for piece in text.split("\x00"):
        piece = piece.strip()
        if not piece:
            continue
        try:
            argv = shlex.split(piece)
        except ValueError:
            # Unbalanced quotes. Screen the raw text rather than assume it is
            # harmless: an unparseable command is the last thing to trust.
            argv = [piece]
        # `VAR=x cmd` -- the assignment is not the program being run.
        while argv and "=" in argv[0] and not argv[0].startswith("/"):
            argv = argv[1:]
        if argv:
            out.append(argv)
    return out


#: Roles a chat-completions endpoint defines. The loop uses `exit` for its own
#: bookkeeping, which is its business and not a role any API knows.
_WIRE_ROLES = frozenset({"system", "user", "assistant", "tool"})


def _for_the_wire(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The conversation as a chat API defines it, and nothing more.

    The loop keeps bookkeeping on its messages -- `extra` carrying the parsed
    actions, an `exit` role when it finishes. An endpoint is entitled to reject
    a message object with fields it does not define, and one did: a live run
    was refused on all three routes with `upstream_rejected`, which the
    classifier correctly called a refusal and correctly did not retry.

    LiteLLM strips these, which is why the spike never saw it and the first
    integrated run did. Stripping here rather than in `ModelClient` because
    this is the loop's shape, and core should not learn it.

    An unknown role becomes `user`: dropping the message would silently lose a
    turn of context, and inventing a role is how this failed in the first
    place.
    """
    out: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        out.append(
            {
                "role": role if role in _WIRE_ROLES else "user",
                "content": str(message.get("content", "")),
            }
        )
    return out


def _attributes(value: Any) -> Any:
    """JSON as objects, because their parser reads `call.function.name`.

    It was written against LiteLLM's response objects, which use attribute
    access. Our transport returns JSON, so a dict reaches it and fails with
    `'dict' object has no attribute 'function'`.

    Converted rather than reimplemented: their parser also validates the tool
    name and the argument JSON, and a second copy of that would be a second
    thing to keep in step with a format that is theirs.
    """
    from types import SimpleNamespace

    if isinstance(value, dict):
        return SimpleNamespace(**{k: _attributes(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_attributes(item) for item in value]
    return value


def _message_of(body: Any) -> dict[str, Any]:
    """The assistant message from a chat-completions body.

    Read here rather than through the preset's reader because the reader
    returns text, and a tool call is not text: `tool_calls` sits beside
    `content` and would be dropped. Conservative -- an unreadable body is an
    empty message, and the loop then reports a format error rather than this
    layer raising something the loop cannot answer.
    """
    import json

    try:
        payload = json.loads(body if isinstance(body, str) else bytes(body).decode())
        message = payload["choices"][0]["message"]
        return message if isinstance(message, dict) else {}
    except Exception:
        return {}


@dataclass
class HarnessModel:
    """`mini-swe-agent`'s `Model`, answered by this harness's client.

    Everything the loop would otherwise get from LiteLLM comes from
    `ModelClient` instead: a role rather than a model name, so the map can be
    re-routed live; a chain, so one dead model does not stall an item; the
    retry ladder that never retries a spend cap; and the answer recorded to
    the event stream. The loop is not told any of this and does not need to
    be.
    """

    client: ModelClient
    role: str = IMPLEMENTER
    #: What the loop reports it has spent. `ModelClient` owns real pricing and
    #: writes it to the audit; this exists only because the loop's own limits
    #: read it, and a wrong number here would stop an item early.
    cost: float = 0.0
    n_calls: int = 0
    config: Any = None
    #: Their format, quoted from their own default config. Held here so a
    #: change on their side is one string to update rather than a hunt.
    action_regex: str = r"```mswea_bash_command\s*\n(.*?)\n```"
    format_error_template: str = (
        "Please always provide EXACTLY ONE action in triple backticks, "
        "found {{actions|length}} actions."
    )
    observation_template: str = "<returncode>{{output.returncode}}</returncode>\n{{output.output}}"

    def query(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        """One turn, as a tool call.

        **Tool calls, not text parsing.** `mini-swe-agent` v2 says so itself --
        "we strongly recommend to use toolcalls instead" -- and the difference
        is not cosmetic. Measured against `gpt-5.6`: with the legacy
        text-regex path the model returned four turns' worth of `THOUGHT:`
        prose in one reply and a single action, and that action was the finish
        marker. It submitted having executed nothing, on turn one. The spike
        that worked used the tool-calling path.

        The tool definition is theirs (`BASH_TOOL`), passed as a call option,
        which `JsonChatRequest.render` already forwards into the payload -- so
        no protocol change was needed to reach it.
        """
        from minisweagent.models.utils.actions_toolcall import BASH_TOOL, parse_toolcall_actions

        reply = self.client.call(
            self.role,
            _for_the_wire(messages),
            tools=[BASH_TOOL],
            tool_choice="auto",
            **kwargs,
        )
        self.n_calls += 1
        message = _message_of(reply.body)
        # Not caught: their loop answers a FormatError by telling the model
        # what it did wrong and asking again, which beats anything invented
        # here.
        actions = parse_toolcall_actions(
            _attributes(message.get("tool_calls") or []),
            format_error_template=self.format_error_template,
        )
        return {
            "role": "assistant",
            "content": message.get("content") or "",
            "extra": {"actions": actions},
        }

    def format_message(self, **kwargs: Any) -> dict[str, Any]:
        return dict(kwargs)

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        return {"n_model_calls": self.n_calls, "model_cost": self.cost}

    def format_observation_messages(
        self,
        message: dict[str, Any],
        outputs: list[dict[str, Any]],
        template_vars: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """An observation is a plain user turn.

        Their tool-call formatter pairs each observation with the `tool_call_id`
        it answers. That is the correct shape, and it is not used here because
        `_for_the_wire` strips a message to `role` and `content` -- a `tool`
        message without its id is a malformed request, which is the failure
        that produced `upstream_rejected`. Giving the output back as a user
        turn keeps the conversation well-formed at the cost of the pairing,
        which the model does not need when there is one tool.
        """
        return [
            {
                "role": "user",
                "content": f"<returncode>{o.get('returncode')}</returncode>\n{o.get('output', '')}",
            }
            for o in outputs
        ]

    def serialize(self) -> dict[str, Any]:
        return {"role": self.role, "n_calls": self.n_calls}


@dataclass
class HarnessEnvironment:
    """`mini-swe-agent`'s `Environment`, screened by this harness's guard.

    Every command the agent runs arrives at `execute`, which is the whole
    reason this adapter is small: one method is the entire attack surface, so
    the guard that already screens check commands screens an agent's shell on
    the same terms and with the same refusal list.

    A refusal is returned to the agent as output rather than raised. That is
    the opposite of the executor's rule — there, a refusal is terminal — and
    deliberately so: inside a loop the agent can read why and choose another
    command, which is the whole point of it being a loop. The item is not
    silently permitted to proceed; the refusal is recorded, and a loop that
    keeps trying refused commands runs out of steps.
    """

    repo: Path
    guard: CommandGuard = field(default_factory=CommandGuard)
    timeout: int = 300
    refusals: list[str] = field(default_factory=list)
    config: Any = None

    def execute(self, action: dict[str, Any], cwd: str = "") -> dict[str, Any]:
        import subprocess

        command = action.get("action") or action.get("command") or ""
        where = Path(cwd) if cwd else self.repo
        try:
            for argv in _segments(str(command)):
                self.guard.enforce(argv, cwd=where)
        except CommandRefused as refused:
            self.refusals.append(str(command))
            return {
                "output": f"REFUSED by this deployment's command policy: {refused}",
                "returncode": 1,
            }

        result = subprocess.run(  # noqa: S602 - screened above, agent-supplied by design
            str(command),
            shell=True,
            cwd=where,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        output = {
            "output": (result.stdout + result.stderr)[-32_000:],
            "returncode": result.returncode,
        }
        self._check_finished(output)
        return output

    def _check_finished(self, output: dict[str, Any]) -> None:
        """Raise `Submitted` when the agent says it is done.

        Their protocol, not ours: the loop ends when a command's output starts
        with the submit marker, and an environment that does not raise it
        produces an agent that cannot finish. Measured -- a scripted loop that
        should have stopped after one turn ran to its 40-step limit instead,
        which in a real run is 40 model calls to reach an outcome it had
        already decided.

        Kept a faithful copy of `LocalEnvironment._check_finished` rather than
        an interpretation. The marker and the return-code condition are theirs
        to change.
        """
        from minisweagent.exceptions import Submitted

        lines = str(output.get("output", "")).lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == SUBMIT_MARKER and output["returncode"] == 0:
            submission = "".join(lines[1:])
            raise Submitted(
                {
                    "role": "exit",
                    "content": submission,
                    "extra": {"exit_status": "Submitted", "submission": submission},
                }
            )

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        """What the loop's prompt templates interpolate.

        Its own templates reference `system` and the rest of `platform.uname()`,
        so an environment that supplies only a working directory fails at the
        first render with `'system' is undefined` -- measured, and it fails
        before any model call, which is at least the cheap end of wrong.

        Kept identical to the loop's own `LocalEnvironment` rather than
        curated: the templates are theirs, and guessing which variables they
        will reference next is how this breaks again on an upgrade.
        """
        import os
        import platform

        return {
            **platform.uname()._asdict(),
            **os.environ,
            "cwd": str(self.repo),
            **kwargs,
        }

    def serialize(self) -> dict[str, Any]:
        return {"repo": str(self.repo), "refusals": len(self.refusals)}


def build(
    client: ModelClient,
    repo: Path,
    *,
    guard: CommandGuard | None = None,
    step_limit: int = 40,
    role: str = IMPLEMENTER,
) -> Any:
    """A loop wired to this harness's client and guard.

    `step_limit` is bounded on purpose and is not a formality: an unbounded
    loop against a per-item budget is a way to spend the whole budget on one
    item, and `budgets.py` exists because that has happened.
    """
    import minisweagent
    import yaml

    agent_class = _require()
    bundled = yaml.safe_load((Path(minisweagent.package_dir) / "config" / CONFIG).read_text())[
        "agent"
    ]
    return agent_class(
        HarnessModel(client=client, role=role),
        HarnessEnvironment(repo=repo, guard=guard or CommandGuard()),
        system_template=bundled["system_template"],
        instance_template=bundled["instance_template"],
        step_limit=step_limit,
    )
