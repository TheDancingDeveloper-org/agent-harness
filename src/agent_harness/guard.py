"""Commands the harness will not run, and the tree it will not run them outside of.

The harness gives every item its own git worktree and never commits to the
default branch. Neither of those constrains what a *command* does. `verify:` in
a plan is argv the harness executes (`plan.py` says so, and names
`verify: rm -rf build` as the thing that would look like any other line on the
page); `$HARNESS_AGENT_COMMAND` is argv the harness launches; a project's checks
are argv the harness runs before it pays a reviewer. All three are read from
documents and configuration that a model may have written, and until this module
existed all three were run as given.

**This is a deterministic guard, in the same family as "a claim is a lease" and
"checks run before the reviewer".** It is screening, not instruction: nothing
here asks a model to behave, and the refusal holds whether the model is right,
wrong or adversarial. Telling the implementer not to reach for `sudo` is asking
the least reliable component in the system to enforce the constraint.

Two rules, and they are separate on purpose:

*A refusal list.* Patterns matched against argv. Configured per deployment,
because "what must never run here" is a property of the deployment and not of
this framework. The built-in default (`DEFAULT_REFUSALS`) is deliberately tiny
and names nothing belonging to any workload — see its own note.

*A path boundary.* Every guarded command runs in a directory the harness chose
(the item's worktree, or the repository under adoption). Any argument naming a
path outside that directory is refused. This is what makes `~/.ssh`, `/etc` and
`rm -rf /` unreachable through a command the harness runs, without the guard
having to enumerate them.

**What this is not, stated plainly.** It is not a sandbox and it is not process
isolation — the session host owns those, and reimplementing them here would be a
second, worse copy. It screens the argv the harness itself executes. It cannot
see what an agent then types inside its own PTY session, and it cannot read
inside an inline program: `sh -c '...'` and `python -c '...'` are one argv token
to this module, so a deployment that runs a shell as a check has an unscreened
shell. Add the interpreter to the refusal list if that matters to you; it is not
in the built-in default because refusing it would break check commands this
repository's own suite uses, and a default that breaks legitimate use gets
turned off, which protects nobody.

**This is the command half, and only that.** Confining *file writes* to the
worktree on the in-process path is `edits.py`'s job, on its own branch. The two
are complementary and neither is the other's fallback: a write that never goes
through a command is invisible here, and a command that never writes a file is
invisible there.

**Pattern matching is a reduction, not a guarantee.** A list of patterns bounds
the obvious reaches, not the clever ones. It is worth having for the same reason
a lease is worth having: it converts a class of catastrophic outcomes into a
legible refusal. It does not convert them into impossibilities.

**A refusal is terminal (owner decision, 2026-08-05).** The command is blocked,
the item stops, and it records `blocked_by_policy` with the rule that refused it.
It is *not* handed back to the agent as a correction to retry. The decision was
taken on three grounds: it is the safest of the two (a guard that answers is a
guard that can be probed), it is the cheapest (no second agent turn is bought),
and it cannot loop. **The cost the owner accepted is real and is not hidden
here:** an agent that reaches for a forbidden command when a permitted
equivalent existed loses the whole item, and a person has to look at it. That is
paid deliberately, and `doctor` reports the policy so the person paying it can
see what it is.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .outcomes import BLOCKED, BLOCKED_BY_POLICY, COMMAND_BLOCKED, PATH_ESCAPE, Stop

#: Where a deployment's policy is stored, as one setting rather than a column.
#: It is a property of the deployment — the same worktrees, the same host, the
#: same credentials on disk — rather than of one project's plan.
GUARD_KEY = "command_guard"

#: The built-in default, and the argument for every line of it.
#:
#: It is short because a long default is a default nobody read, and because a
#: refusal list is **configuration**: only the deployment knows what its own
#: machine must never be asked to do. Nothing here names a language, an
#: ecosystem, a tool, a repository or a workload, and nothing here is a
#: judgement about a particular project's habits.
#:
#: * privilege escalation — nothing the harness runs on an item's behalf needs
#:   another user's authority, so a command asking for it is either a mistake or
#:   an escape, and neither should proceed unattended;
#: * host lifecycle — no verification step takes the machine down, and a fleet
#:   that reboots its own host loses every other item in flight;
#: * a force push — the harness's entire contract is *propose a branch*. A
#:   forced update destroys history the harness did not write, on a remote it
#:   shares with people. The ordinary push it does need is untouched.
DEFAULT_REFUSALS: tuple[str, ...] = (
    "sudo",
    "su",
    "doas",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "git push --force",
    "git push -f",
    "git push --force-with-lease",
)


@dataclass(frozen=True)
class Refusal:
    """One command, the rule that refused it, and what that means for the item.

    Carries the rule rather than a sentence about it, so an operator can see
    *which* line of policy fired without reading the guard's source, and so a
    deployment can tell "my own rule caught this" from "the built-in default
    caught this".
    """

    reason_kind: str
    #: The pattern, or the boundary, in the form it was configured in.
    rule: str
    detail: str
    command: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason_kind": self.reason_kind,
            "rule": self.rule,
            "detail": self.detail,
            "command": list(self.command),
        }

    def stop(self) -> Stop:
        """The `Stop` this refusal produces. Terminal, and it says why.

        `blocked`, not `failed`: the item needs a person to widen the policy or
        to rewrite the work, and an item sitting in `failed` is one nothing
        looking for work that needs a person will ever find. The attempt is
        consumed because it happened — an agent ran, and money was spent — but
        the state is what stops it, not the attempt count: `blocked` is never
        re-claimed, which is what "terminal" means here.
        """
        return Stop(
            BLOCKED_BY_POLICY,
            self.reason_kind,
            detail=self.detail,
            state=BLOCKED,
            consumes_attempt=True,
        )


class CommandRefused(Exception):
    """Raised where a refused command would have been run.

    An exception rather than a return value because the refusal has to abort an
    attempt from inside `Checks.run`, several frames below anything that can
    decide an item's fate — and because a blocked command is **not a gate's
    answer**. The gate never ran. Making it a sixth check outcome would say a
    check had an opinion about the diff, which it did not.

    It is caught at each executor's boundary and turned into `refusal.stop()`
    before anything else sees it, so it never surfaces as an unexplained
    failure. `self.refusal` carries everything the queue records.
    """

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.detail)
        self.refusal = refusal


def _basename(program: str) -> str:
    return os.path.basename(program.replace("\\", "/"))


def _token_matches(pattern_token: str, argv_token: str) -> bool:
    """Whether one pattern token matches one argv token.

    Three forms, each closing a way of writing the same thing:
    exact (`--force`), attached value (`--output=/x` matches `--output`), and
    bundled short flags (`-f` matches `-rf`, which is the same flag written
    the way people actually write it).
    """
    if pattern_token == argv_token:
        return True
    if argv_token.startswith(pattern_token + "="):
        return True
    short = (
        len(pattern_token) == 2
        and pattern_token.startswith("-")
        and argv_token.startswith("-")
        and not pattern_token.startswith("--")
        and not argv_token.startswith("--")
    )
    return short and pattern_token[1] in argv_token[1:]


def _pattern_matches(pattern: Sequence[str], argv: Sequence[str]) -> bool:
    """`git push --force` matches any argv running git, pushing, forced.

    The program is matched by basename as well as in full, so an absolute path
    to it is not an evasion. The remaining tokens are matched **anywhere** in
    the rest of the argv rather than in position, so inserting `-c foo` or
    naming a remote first does not slip past a rule.
    """
    if not pattern or not argv:
        return False
    head, rest = pattern[0], pattern[1:]
    if head != argv[0] and head != _basename(argv[0]):
        return False
    return all(any(_token_matches(token, given) for given in argv[1:]) for token in rest)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _path_like(token: str) -> str:
    """The path a token names, or empty.

    A token is treated as naming a path when it contains a separator or starts
    with `~`. `--flag=VALUE` and `key=VALUE` are unwrapped first, because
    `dd of=/dev/sda` hides its path in the second half of one token.
    """
    value = token
    if "=" in token:
        head, _, tail = token.partition("=")
        if head.startswith("-") or head.replace("-", "_").isidentifier():
            value = tail
    if not value:
        return ""
    if value.startswith("~") or "/" in value or "\\" in value:
        return value
    return ""


@dataclass(frozen=True)
class CommandGuard:
    """The policy, and the one operation that applies it.

    Immutable and cheap: it is built once per run and consulted per command,
    and nothing about a command it screened is remembered here — the record of
    a refusal belongs on the item, not in a process that may not outlive it.
    """

    #: The deployment's own patterns. Empty is not "no guard": see `defaults`.
    refusals: tuple[str, ...] = ()
    #: Whether `DEFAULT_REFUSALS` is in force as well. On, because a deployment
    #: that has not thought about this yet should still not be running `sudo`.
    defaults: bool = True
    #: Whether a guarded command's arguments must stay inside the directory it
    #: runs in. On for the same reason.
    confine: bool = True
    #: Whether this deployment configured anything at all. Read by `doctor`,
    #: which reports an unconfigured guard as *not configured* rather than as a
    #: pass — a guard nobody enabled is not a guard, even when it happens to be
    #: refusing things.
    configured: bool = False

    @property
    def patterns(self) -> tuple[str, ...]:
        return (DEFAULT_REFUSALS if self.defaults else ()) + self.refusals

    @property
    def active(self) -> bool:
        return bool(self.patterns) or self.confine

    def describe(self) -> str:
        parts = [f"{len(self.patterns)} refusal pattern(s)"]
        parts.append("paths confined to the item's tree" if self.confine else "paths unconfined")
        if not self.defaults:
            parts.append("built-in defaults off")
        return "; ".join(parts)

    # -------------------------------------------------------------- applying

    def screen(self, argv: Sequence[str], *, cwd: Path | str | None = None) -> Refusal | None:
        """The whole guard, as one question: may the harness run this?

        `cwd` is both the directory the command will run in and the boundary it
        may not reach outside. They are the same thing on purpose — the harness
        always runs a guarded command in the tree that command is about, and a
        boundary configured separately from the working directory is one more
        thing that can be set to the wrong value.
        """
        given = tuple(str(part) for part in argv)
        if not given:
            return None
        for pattern in self.patterns:
            tokens = shlex.split(pattern)
            if _pattern_matches(tokens, given):
                return Refusal(
                    COMMAND_BLOCKED,
                    pattern,
                    f"`{shlex.join(given)}` is refused by policy `{pattern}`; "
                    "the harness will not run it on an agent's behalf",
                    command=given,
                )
        if not self.confine or cwd is None:
            return None
        root = Path(cwd).resolve()
        # argv[0] is exempt: it names the program, and an absolute path to an
        # interpreter is how a check is normally written. What it is *asked to
        # do* is every other token, and those are screened.
        for token in given[1:]:
            candidate = _path_like(token)
            if not candidate:
                continue
            if candidate.startswith("~"):
                return Refusal(
                    PATH_ESCAPE,
                    str(root),
                    f"`{shlex.join(given)}` names {candidate!r}, which is outside the "
                    f"item's tree {root}",
                    command=given,
                )
            resolved = (root / candidate).resolve()
            if not _within(resolved, root):
                return Refusal(
                    PATH_ESCAPE,
                    str(root),
                    f"`{shlex.join(given)}` names {candidate!r}, which resolves to "
                    f"{resolved} — outside the item's tree {root}",
                    command=given,
                )
        return None

    def enforce(self, argv: Sequence[str], *, cwd: Path | str | None = None) -> None:
        """`screen`, raising. The form every call site inside a run wants."""
        refusal = self.screen(argv, cwd=cwd)
        if refusal is not None:
            raise CommandRefused(refusal)

    # ----------------------------------------------------------- configuring

    @classmethod
    def from_settings(cls, stored: Mapping[str, Any] | None) -> CommandGuard:
        """Build from the stored setting. Absent means unconfigured, not off.

        An unreadable or half-written setting is **not** silently ignored:
        every field falls back to the safe value, and `configured` reflects
        whether a deployment actually wrote something, which is what doctor
        reports.
        """
        if not stored:
            return cls()
        raw: Iterable[Any] = stored.get("refusals") or ()
        refusals = tuple(str(item) for item in raw if str(item).strip())
        return cls(
            refusals=refusals,
            defaults=bool(stored.get("defaults", True)),
            confine=bool(stored.get("confine", True)),
            configured=True,
        )

    def as_settings(self) -> dict[str, Any]:
        return {
            "refusals": list(self.refusals),
            "defaults": self.defaults,
            "confine": self.confine,
        }


#: The guard a caller that has not been given one uses. Not a null object: the
#: built-in default is in force, so a code path nobody wired a guard into is
#: still guarded. `doctor` says out loud that it is the default.
DEFAULT_GUARD = CommandGuard()


def _default_guard() -> CommandGuard:
    return DEFAULT_GUARD


def guard_field() -> Any:
    """A dataclass field defaulting to `DEFAULT_GUARD`."""
    return field(default_factory=_default_guard)
