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

import os
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..budgets import Budget, Spend
from ..guard import CommandGuard, CommandRefused, Refusal
from ..model_client import ModelClient
from ..role_runners import API_VERSION, RoleRunRequest, RoleRunResult

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


#: How much of one command's output the model is shown. A runaway command must
#: not become the whole context window.
OUTPUT_LIMIT = 32_000

#: What a command that ran out of time reports. 124 is what `timeout(1)` uses,
#: and a return code the model already knows how to read beats an exception it
#: never sees.
TIMED_OUT = 124


class NotInstalled(RuntimeError):
    """`mini-swe-agent` is not installed. Said once, plainly, up front."""


class MalformedReply(RuntimeError):
    """A 200 that is not a chat completion.

    Distinct from "the model formatted its action wrongly", which is the
    loop's business and which it answers by asking again. A gateway that
    returns HTTP 200 with an error object in the body is **not** a model
    mistake, and letting it look like one buys three more calls against a
    broken endpoint and then reports `RepeatedFormatError` -- a diagnosis
    naming the wrong component.
    """


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


@lru_cache(maxsize=1)
def bundled() -> dict[str, Any]:
    """Their config, whole, read once.

    Read as a unit rather than field by field because the two halves have to
    agree: `agent.instance_template` tells the model to make a **tool call**,
    and `model.format_error_template` is what it is told when it does not.
    Taking the prompts from here and hand-copying the error template produced
    exactly the mismatch that cost bug #8 -- a model told to use tool calls,
    and then told, on its first mistake, to "provide EXACTLY ONE action in
    triple backticks". That is the text protocol's sentence, and it discards
    `{{error}}`, which is the only part saying what actually went wrong.
    """
    import minisweagent
    import yaml

    loaded = yaml.safe_load((Path(minisweagent.package_dir) / "config" / CONFIG).read_text())
    return {"agent": loaded.get("agent") or {}, "model": loaded.get("model") or {}}


def _bundled_model(key: str) -> str:
    return str(bundled()["model"][key])


#: Shell operators that separate one command from the next. Screening
#: `sh -c "..."` does not work -- `guard.py` says so itself: an interpreter's
#: script is a single opaque token, so `rm -rf /` inside one is invisible.
#: Measured: a refusal list naming `rm` did not stop `rm -rf /` until the
#: command was split. So the string is separated into the commands a shell
#: would actually run, and each is screened as the argv it will become.
#:
#: `&&` and `&` are handled separately: `&` separates only when it is not the
#: `>&` of a redirection, or `2>&1` would be cut in half. `(`, `)`, `{` and `}`
#: are here because `(rm -rf .)` is a subshell, and a segment beginning `(rm`
#: matches no pattern naming `rm`.
_SEPARATORS = ("||", ";;", ";", "|", "\n", "(", ")", "{", "}")

#: Programs whose *arguments* are themselves a command. `env rm -rf .`,
#: `nohup rm -rf .`, `xargs rm -rf` and `timeout 5 rm -rf .` all run `rm`, and
#: none of them has `rm` as argv[0] -- so a refusal list naming `rm` misses
#: every one. The tail is screened at each offset because a wrapper's own
#: options vary (`xargs -I {} rm`, `timeout -s KILL 5 rm`) and guessing where
#: they stop is how this misses one.
_WRAPPERS = frozenset(
    {
        "builtin",
        "chroot",
        "command",
        "env",
        "exec",
        "ionice",
        "nice",
        "nohup",
        "parallel",
        "script",
        "setsid",
        "stdbuf",
        "time",
        "timeout",
        "unbuffer",
        "watch",
        "xargs",
    }
)

#: `find . -exec rm -rf {} \;` deletes, and its argv[0] is `find`.
_EXEC_FLAGS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})

#: A heredoc body fed to one of these is a *script*, so it keeps being
#: screened line by line. Fed to anything else it is *data* -- the file being
#: written -- and screening it refuses a README that mentions `/etc/hosts`.
_INTERPRETERS = frozenset(
    {
        ".",
        "awk",
        "bash",
        "dash",
        "eval",
        "fish",
        "ksh",
        "node",
        "perl",
        "php",
        "python",
        "python3",
        "ruby",
        "sh",
        "source",
        "xargs",
        "zsh",
    }
)

#: How deep a substitution inside a substitution is followed. Bounded because
#: the input is agent-supplied and recursion over it should not be.
_MAX_DEPTH = 4

_HEREDOC = re.compile(r"<<-?\s*(?:(['\"])([^'\"]+)\1|([A-Za-z_][A-Za-z0-9_]*))")


def _heredocs(command: str) -> tuple[str, list[str]]:
    """The line without its heredoc bodies, and the bodies still worth reading.

    A heredoc body is not a sequence of commands, and screening it as one is a
    **false positive**: `cat > README.md <<'EOF' ... see /etc/hosts ... EOF`
    was refused for naming a path outside the tree, in a document. Measured
    here; the loop's own prompts teach that exact form for creating a file.

    The exception is a body fed to an interpreter, which really is a script.
    Those are returned to be screened, so nothing that was caught before stops
    being caught.
    """
    if "<<" not in command:
        return command, []
    lines = command.split("\n")
    kept: list[str] = []
    scripts: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        words = {os.path.basename(word) for word in re.findall(r"[\w./-]+", line)}
        interpreted = bool(words & _INTERPRETERS)
        for match in _HEREDOC.finditer(line):
            delimiter = match.group(2) or match.group(3)
            body: list[str] = []
            while index < len(lines) and lines[index].strip() != delimiter:
                body.append(lines[index])
                index += 1
            index += 1  # the terminator line itself
            if interpreted:
                scripts.append("\n".join(body))
    return "\n".join(kept), scripts


def _balanced(text: str, start: int) -> tuple[str, int]:
    """The body of a `(...)` opened just before `start`, and where it ends."""
    depth = 1
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "'":
            found = text.find("'", index + 1)
            index = len(text) if found == -1 else found + 1
            continue
        if char == '"':
            index, _ = _double_quoted(text, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:index], index + 1
        index += 1
    return text[start:], len(text)


def _double_quoted(text: str, start: int) -> tuple[int, list[str]]:
    """Where a double-quoted run ends, and the commands substituted inside it.

    Double quotes suppress word splitting, not substitution: `"$(rm -rf .)"`
    still runs `rm`. Single quotes suppress both, which is why they are handled
    by skipping rather than by scanning.
    """
    inner: list[str] = []
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index + 1, inner
        if text.startswith("$(", index):
            body, index = _balanced(text, index + 2)
            inner.append(body)
            continue
        if char == "`":
            found = text.find("`", index + 1)
            end = len(text) if found == -1 else found
            inner.append(text[index + 1 : end])
            index = min(end + 1, len(text))
            continue
        index += 1
    return len(text), inner


def _split(text: str) -> tuple[list[str], list[str]]:
    """Simple commands, and the command substitutions hiding among them.

    Quote-aware, which the previous `str.replace` was not: `echo "a && b"` is
    one command, and cutting it at the `&&` produced two fragments that were
    screened as if they were commands. Substitution-aware, which it also was
    not: `echo $(rm -rf .)` runs `rm`, and as a flat token list its program is
    `echo` and the `rm` is an argument nothing looks at.
    """
    pieces: list[str] = []
    nested: list[str] = []
    buf: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            buf.append(text[index : index + 2])
            index += 2
            continue
        if char == "'":
            found = text.find("'", index + 1)
            end = len(text) if found == -1 else found + 1
            buf.append(text[index:end])
            index = end
            continue
        if char == '"':
            end, inner = _double_quoted(text, index)
            nested.extend(inner)
            buf.append(text[index:end])
            index = end
            continue
        if text.startswith(("$(", "<(", ">("), index):
            body, index = _balanced(text, index + 2)
            nested.append(body)
            # The substitution's *result* is opaque; a space keeps the words
            # around it apart.
            buf.append(" ")
            continue
        if char == "`":
            found = text.find("`", index + 1)
            end = len(text) if found == -1 else found
            nested.append(text[index + 1 : end])
            buf.append(" ")
            index = min(end + 1, len(text))
            continue
        if char == "&":
            if not text.startswith("&&", index) and "".join(buf).rstrip().endswith((">", "<")):
                buf.append(char)  # `2>&1`, not a separator
                index += 1
                continue
            pieces.append("".join(buf))
            buf = []
            index += 2 if text.startswith("&&", index) else 1
            continue
        for separator in _SEPARATORS:
            if text.startswith(separator, index):
                pieces.append("".join(buf))
                buf = []
                index += len(separator)
                break
        else:
            buf.append(char)
            index += 1
    pieces.append("".join(buf))
    return pieces, nested


def _argv(piece: str) -> list[str]:
    piece = piece.strip()
    if not piece:
        return []
    try:
        argv = shlex.split(piece)
    except ValueError:
        # Unbalanced quotes. Screen the raw text rather than assume it is
        # harmless: an unparseable command is the last thing to trust.
        argv = [piece]
    # `VAR=x cmd` -- the assignment is not the program being run.
    while argv and "=" in argv[0] and not argv[0].startswith("/"):
        argv = argv[1:]
    return argv


def _unwrapped(argv: Sequence[str]) -> list[list[str]]:
    """The commands an argv runs that are not its own argv[0]."""
    extra: list[list[str]] = []
    if os.path.basename(argv[0]) in _WRAPPERS:
        extra.extend(list(argv[at:]) for at in range(1, len(argv)))
    for at, token in enumerate(argv):
        if token in _EXEC_FLAGS and at + 1 < len(argv):
            extra.append(list(argv[at + 1 :]))
    return extra


def _segments(command: str, depth: int = 0) -> list[list[str]]:
    """Every simple command inside a shell line, as argv.

    Best effort, and it must stay best effort in the safe direction: anything
    this cannot parse is handed to the guard *whole* rather than skipped, so a
    line it does not understand is screened conservatively instead of waved
    through. It is not a shell, and the worktree boundary -- not this -- is the
    guarantee.

    Four things it now sees that a `str.replace` on the separators could not:
    a separator inside quotes is not one; a command substitution is a command;
    a wrapper's arguments are a command; and a heredoc body is usually data.
    """
    text, scripts = _heredocs(command)
    pieces, nested = _split(text)
    out: list[list[str]] = []
    for piece in pieces:
        argv = _argv(piece)
        if argv:
            out.append(argv)
            out.extend(_unwrapped(argv))
    if depth < _MAX_DEPTH:
        for body in [*nested, *scripts]:
            out.extend(_segments(body, depth + 1))
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

    **What the assistant did is put back into its content.** The action lives
    in `tool_calls`, which is dropped here, and the observation comes back as a
    plain user turn -- so a model whose reply was a tool call and nothing else
    saw its own turns as *empty strings*: thirty rounds of `assistant: ""`
    followed by `user: <returncode>0 ...`, with no way to tell which output
    answered which command. Reconstructed from `extra.actions` rather than
    echoed as `tool_calls`, because a `tool_calls` message must be answered by
    a `tool` message carrying its id, and that id is exactly what this strips.
    """
    out: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = str(message.get("content", "") or "")
        extra = message.get("extra")
        actions = extra.get("actions") or [] if isinstance(extra, Mapping) else []
        if actions:
            ran = "\n".join(f"$ {action.get('command', '')}" for action in actions)
            content = f"{content}\n\n{ran}".strip() if content.strip() else ran
        out.append(
            {
                "role": role if role in _WIRE_ROLES else "user",
                "content": content,
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


def _choice(body: Any, redact: Callable[[str | None], str | None] | None = None) -> dict[str, Any]:
    """The first choice of a chat-completions body.

    Read here rather than through the preset's reader because the reader
    returns text, and a tool call is not text: `tool_calls` sits beside
    `content` and would be dropped.

    It used to swallow every exception and return an empty message, which made
    **a provider fault indistinguishable from a model mistake**: a gateway
    answering HTTP 200 with `{"error": ...}` produced no tool calls, the loop
    said "provide exactly one action", the gateway said the same thing again,
    and three calls later the run ended as `RepeatedFormatError` -- blaming the
    model for the endpoint. A body that is not a chat completion is now said
    out loud, with the beginning of what did arrive, redacted.
    """
    import json

    text = body if isinstance(body, str) else bytes(body or b"").decode(errors="replace")
    try:
        payload = json.loads(text)
        choice = payload["choices"][0]
    except Exception as exc:
        excerpt = (redact(text[:400]) if redact else text[:400]) or ""
        raise MalformedReply(
            f"the endpoint returned 200 and a body that is not a chat completion "
            f"({type(exc).__name__}): {excerpt!r}"
        ) from exc
    return choice if isinstance(choice, dict) else {}


def _text(stream: Any) -> str:
    """A captured stream as text, whichever way the platform handed it over."""
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode(errors="replace")
    return str(stream)


def _bounded(text: str, limit: int = OUTPUT_LIMIT) -> str:
    """A command's output, cut to fit, saying so.

    Two bugs in one line. It kept the **last** 32k, and which end matters
    depends entirely on the command: a compiler puts its first error at the
    top and a test runner puts its summary at the bottom, so keeping either end
    alone throws away the answer for half the commands an agent runs. And it
    was **unmarked** -- output that began mid-line, indistinguishable from a
    command that really did print that little, which is how a model concludes a
    file has three functions in it.

    So: both ends, and a sentence in the middle naming what is gone.
    """
    if len(text) <= limit:
        return text
    marker = "\n[... {} characters elided by the harness ...]\n"
    keep = limit - len(marker.format(len(text)))
    head = keep // 2
    tail = keep - head
    return text[:head] + marker.format(len(text) - keep) + text[-tail:]


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
    #: What the loop reports it has spent, and what its own `cost_limit` reads.
    #: The harness supplies that limit explicitly (zero when the item is
    #: unlimited), while `ModelClient` owns real pricing and the audit; this is
    #: that number, folded in per call.
    cost: float = 0.0
    #: Priced and unpriced calls, kept apart. **Unknown cost is not zero cost**
    #: (`budgets.py`): while `unpriced` is non-zero, `cost` is a LOWER BOUND,
    #: so a ceiling built on it can fail to fire but can never fire early.
    spend: Spend = field(default_factory=Spend)
    n_calls: int = 0
    config: Any = None
    #: Theirs, read from the same config the prompts come from. Hand-copied,
    #: these drifted: the error template told a model on the *tool call* path
    #: to "provide EXACTLY ONE action in triple backticks" -- the text
    #: protocol's sentence, and the exact mismatch of bug #8 -- and dropped
    #: `{{error}}`, the only part that says what was actually wrong.
    format_error_template: str = field(
        default_factory=lambda: _bundled_model("format_error_template")
    )
    observation_template: str = field(
        default_factory=lambda: _bundled_model("observation_template")
    )
    #: Per-call accounting owned by the caller. Reporting only when the loop
    #: returns loses every call made before a policy refusal or provider
    #: exception, which is exactly when an item most needs an honest total.
    on_usage: Callable[[Spend], None] | None = None

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
        # Billed before the body is read: a reply nobody could parse was still
        # paid for, and a ceiling that only counts the calls that went well is
        # not a ceiling.
        cost = self._bill(reply.body)
        choice = _choice(reply.body, self.client.redact)
        message = choice.get("message")
        message = message if isinstance(message, dict) else {}
        # Not caught: their loop answers a FormatError by telling the model
        # what it did wrong and asking again, which beats anything invented
        # here. `finish_reason` goes with it because their template uses it to
        # tell "you formatted it wrongly" from "you were cut off at the token
        # limit before you got to the tool call" -- two different corrections,
        # and without it a truncation is answered with the wrong one.
        actions = parse_toolcall_actions(
            _attributes(message.get("tool_calls") or []),
            format_error_template=self.format_error_template,
            template_kwargs={"finish_reason": choice.get("finish_reason")},
        )
        return {
            "role": "assistant",
            "content": message.get("content") or "",
            "extra": {"actions": actions, "cost": cost},
        }

    def _bill(self, body: Any) -> float:
        """What that call cost, folded in as it happens.

        Through `usage_for` so this and the audit rollup cannot disagree about
        what a call cost -- `model_client` says that is why it exists. An
        unpriced model contributes 0.0 here and increments `spend.unpriced`,
        which is the honest shape: the loop's ceiling is then a lower bound and
        `measurable` says so, rather than a zero pretending to be a price.
        """
        call = Spend()
        try:
            call.add_call(self.client.usage_for(self.role, body))
        except Exception:  # pragma: no cover - a reader that cannot read
            call.unpriced += 1
        self.spend.add(call)
        if self.on_usage is not None:
            self.on_usage(call)
        charged = call.usd
        self.cost += charged
        return charged

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

        The *content* is theirs, rendered from the config's own
        `observation_template`: it reports a long output as an explicit head, a
        tail and an `elided_chars` count, where the hand-written one silently
        handed over whatever survived. A model cannot tell truncated output
        from short output unless something says so.
        """
        from jinja2 import StrictUndefined, Template

        template = Template(self.observation_template, undefined=StrictUndefined)
        return [
            {
                "role": "user",
                "content": template.render(
                    output={"exception_info": None, **output}, **(template_vars or {})
                ),
            }
            for output in outputs
        ]

    def serialize(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "n_calls": self.n_calls,
            "cost_usd": round(self.cost, 6),
            "unpriced_calls": self.spend.unpriced,
            "cost_measurable": self.spend.measurable,
        }


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
    #: The commands that were refused, in order. The agent saw each one and
    #: could carry on; this is what happened.
    refusals: list[str] = field(default_factory=list)
    #: The rule that fired for each, which `refusals` alone does not say. An
    #: operator asking "why did this item flail" needs the pattern, not the
    #: count.
    refused: list[Refusal] = field(default_factory=list)
    #: Where a refusal goes to survive the process. Both lists above die with
    #: it, and `serialize` only ever reported a *number* -- so a run whose
    #: agent spent ten turns bouncing off the policy left nothing behind that
    #: said so. A deployment passes an event writer here; nothing is imported
    #: to reach one, because core must not learn this adapter's name.
    on_refusal: Callable[[str, Refusal], None] | None = None
    #: The standalone experiment lets a loop correct a refused command. The
    #: harness execution path does not: AGENTS.md rules a policy refusal
    #: terminal, so its runner enables this and the exception reaches the
    #: executor's existing blocked-by-policy handler.
    terminal_refusals: bool = False
    #: Item-scoped command progress. The callback owns persistence; this
    #: adapter supplies only the command and its eventual return code.
    on_command: Callable[[str, int | None], None] | None = None
    config: Any = None

    def execute(self, action: dict[str, Any], cwd: str = "") -> dict[str, Any]:
        import subprocess

        command = action.get("action") or action.get("command") or ""
        where = Path(cwd) if cwd else self.repo
        try:
            for argv in _segments(str(command)):
                self.guard.enforce(argv, cwd=where)
        except CommandRefused as refused:
            return self._refuse(str(command), refused)

        if self.on_command is not None:
            self.on_command(str(command), None)

        try:
            result = subprocess.run(  # noqa: S602 - screened above, agent-supplied by design
                str(command),
                shell=True,
                cwd=where,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as expired:
            # Unhandled, this left `execute` as a `TimeoutExpired` that the
            # loop does not catch: `DefaultAgent.run` records an exit message
            # and re-raises, so one hung test command ended the whole item with
            # a traceback instead of a turn. The agent is told what happened
            # instead, in the shape it reads every other result in, and can
            # run something cheaper.
            partial = _text(expired.stdout) + _text(expired.stderr)
            timed_out = {
                "output": _bounded(partial),
                "returncode": TIMED_OUT,
                "exception_info": (
                    f"the command was killed after {self.timeout}s by this "
                    "deployment's command timeout; any output above is partial"
                ),
            }
            if self.on_command is not None:
                self.on_command(str(command), TIMED_OUT)
            return timed_out

        full = result.stdout + result.stderr
        if self.on_command is not None:
            self.on_command(str(command), result.returncode)
        # Checked on the WHOLE output, then truncated for the model. The marker
        # has to be the first line, and keeping the last 32k of a command that
        # printed it and then a lot else threw it away -- an agent that had
        # finished could not say so, which is bug #5 wearing a different hat.
        self._check_finished({"output": full, "returncode": result.returncode})
        return {
            "output": _bounded(full),
            "returncode": result.returncode,
            "exception_info": None,
        }

    def _refuse(self, command: str, refused: CommandRefused) -> dict[str, Any]:
        """Record the refusal, and tell the agent enough to comply.

        Measured on a real rdpapp item: 40 model calls, `LimitsExceeded`, and
        **15 of the 40 turns spent being refused**. The loop was working — it
        created files — it ran out of steps bouncing off the policy. Most were
        the heredoc false positive `_segments` now avoids; the rest were
        genuine reaches outside the worktree, retried in variants because the
        refusal never said where the boundary *was*.

        So the message names the tree, says the rule is not transient, and
        counts the refusals so far. **The policy is unchanged** -- nothing here
        widens the boundary; it only stops the agent guessing at it.
        """
        self.refusals.append(command)
        self.refused.append(refused.refusal)
        if self.on_refusal is not None:
            self.on_refusal(command, refused.refusal)
        if self.terminal_refusals:
            raise refused
        return {
            "output": (
                f"REFUSED by this deployment's command policy: {refused}\n"
                f"This is policy, not a transient failure: the same command will be "
                f"refused again. Everything this item may read or write is inside "
                f"{self.repo} — use a path in there, or a different command.\n"
                f"Refusals so far this run: {len(self.refusals)}."
            ),
            "returncode": 1,
            "exception_info": None,
        }

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
            submission = _bounded("".join(lines[1:]))
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
        import platform

        return {
            **platform.uname()._asdict(),
            **os.environ,
            "cwd": str(self.repo),
            **kwargs,
        }

    def serialize(self) -> dict[str, Any]:
        """What the trajectory keeps. The refusals themselves, not a count.

        A count says an agent was stopped and nothing about by what. The rule
        that fired is the thing an operator changes.
        """
        return {
            "repo": str(self.repo),
            "refusals": [
                # `line` is what the agent asked for; `command` is the argv the
                # guard actually screened, which is a *segment* of it and the
                # reason a refusal can be surprising.
                {"line": line, **refusal.as_dict()}
                for line, refusal in zip(self.refusals, self.refused, strict=False)
            ],
        }


def build(
    client: ModelClient,
    repo: Path,
    *,
    guard: CommandGuard | None = None,
    step_limit: int = 40,
    role: str = IMPLEMENTER,
    budget: Budget | None = None,
    timeout: int = 300,
    on_refusal: Callable[[str, Refusal], None] | None = None,
    terminal_refusals: bool = False,
    on_command: Callable[[str, int | None], None] | None = None,
    on_usage: Callable[[Spend], None] | None = None,
) -> Any:
    """A loop wired to this harness's client, guard and budget.

    `step_limit` is bounded on purpose and is not a formality: an unbounded
    loop against a per-item budget is a way to spend the whole budget on one
    item, and `budgets.py` exists because that has happened.

    **A step limit is not the budget, and it was the only thing here.** The
    executor checks `budgets.check` at every boundary it has; a loop's boundary
    is the whole loop, so an item's wall-clock and spend ceilings were declared
    and then not enforced for the entire time the agent ran. Both are handed to
    the loop's own limits, which it applies before each call -- the boundary
    `budgets.py` asks for ("never kills work in flight"), inside the loop.

    The spend ceiling is a **lower bound** while any call is unpriced
    (`HarnessModel.spend`), so it can fail to fire and cannot fire early.
    """
    agent_class = _require()

    class BudgetAwareAgent(agent_class):  # type: ignore[misc, valid-type]
        """Keep an unknown-cost loop from enforcing a known-cost subtotal."""

        def query(self) -> dict[str, Any]:
            # mini-swe-agent compares its numeric ``cost`` with ``cost_limit``.
            # Once one call is unpriced that number is only a lower bound, and
            # budgets.py forbids stopping an item on a number nobody can
            # defend. Disable only the dollar limit; step and wall-clock
            # limits remain emergency controls, and the harness reports the
            # unenforceable spend ceiling at its next boundary.
            if self.model.spend.unpriced:
                self.config.cost_limit = 0.0
            return super().query()  # type: ignore[no-any-return]

    prompts = bundled()["agent"]
    limits: dict[str, Any] = {}
    if budget is not None and budget.seconds:
        # Zero means unlimited to the loop, so a positive sub-second remainder
        # must round up rather than silently remove the ceiling.
        import math

        limits["wall_time_limit_seconds"] = max(1, math.ceil(budget.seconds))
    # mini-swe-agent's own default is a finite dollar limit. The harness owns
    # the item budget, whose default is unlimited, so never let an adapter
    # default silently become a second ceiling.
    limits["cost_limit"] = (
        float(budget.spend_usd) if budget is not None and budget.spend_usd else 0.0
    )
    return BudgetAwareAgent(
        HarnessModel(client=client, role=role, on_usage=on_usage),
        HarnessEnvironment(
            repo=repo,
            guard=guard or CommandGuard(),
            timeout=timeout,
            on_refusal=on_refusal,
            terminal_refusals=terminal_refusals,
            on_command=on_command,
        ),
        system_template=prompts["system_template"],
        instance_template=prompts["instance_template"],
        step_limit=step_limit,
        **limits,
    )


@dataclass(frozen=True)
class MiniSweRoleRunner:
    """The installed adapter for core's generic role-runner contract."""

    name: str = "agent-loop"
    api_version: int = API_VERSION

    @property
    def version(self) -> str:
        from importlib.metadata import version

        return version("mini-swe-agent")

    def run(self, request: RoleRunRequest, /) -> RoleRunResult:
        if not request.writable:
            raise ValueError("this runner adapter does not yet provide a read-only environment")

        commands = 0

        def command(line: str, returncode: int | None) -> None:
            nonlocal commands
            if returncode is None:
                commands += 1
            if request.report is not None:
                request.report(
                    "runner_command_started" if returncode is None else "runner_command_finished",
                    line,
                    {"command_index": commands, "returncode": returncode},
                )

        def refused(line: str, refusal: Refusal) -> None:
            if request.report is not None:
                request.report("runner_command_refused", line, {"refusal": refusal.as_dict()})

        if request.report is not None:
            request.report(
                "runner_started",
                f"{self.name} {self.version} running {request.role}",
                {
                    "role": request.role,
                    "step_limit": request.step_limit,
                    "budget": request.budget.as_dict(),
                    "writable": request.writable,
                },
            )
        agent = build(
            request.client,
            request.repo,
            guard=request.guard,
            step_limit=request.step_limit,
            role=request.role,
            budget=request.budget,
            timeout=request.command_timeout,
            on_refusal=refused,
            terminal_refusals=True,
            on_command=command,
            on_usage=request.account,
        )
        raw = agent.run(
            request.task,
            project_id=request.project_id,
            item_id=request.item_id,
            attempt=request.attempt,
        )
        exit_status = str(raw.get("exit_status") or "")
        normalized = exit_status
        if exit_status == "Submitted":
            normalized = "completed"
        elif exit_status == "TimeExceeded":
            normalized = "wall_clock_limit"
        elif exit_status == "LimitsExceeded":
            cost_limit = float(getattr(agent.config, "cost_limit", 0.0) or 0.0)
            spent_out = bool(cost_limit and agent.model.cost >= cost_limit)
            normalized = "spend_limit" if spent_out else "step_limit"
        elif not exit_status:
            normalized = "failed"
        result = RoleRunResult(
            exit_status=normalized,
            submission=str(raw.get("submission") or ""),
            calls=int(agent.model.n_calls),
            spend=agent.model.spend,
        )
        if request.report is not None:
            request.report(
                "runner_finished",
                normalized,
                {"calls": result.calls, "commands": commands, "spend": result.spend.as_dict()},
            )
        return result


RUNNER = MiniSweRoleRunner()
