"""Resolver for `external:github-issue:OWNER/REPO#NUMBER` dependency targets.

**Opt-in.** Nothing in the core imports this. `graph.py` knows the *name*
`github-issue` and the module path to import when a plan uses it, and imports
it lazily at that moment — the same shape as the CLI's `--adapter oxidex`
choice. A deployment that never writes an external GitHub target never loads
this file, and the core never learns what a GitHub issue looks like.

What makes this an adapter rather than core is the format knowledge in one
function: `OWNER/REPO#NUMBER`, and the fact that a closed issue means done.
Both are GitHub's conventions, not the harness's.

The honest-answer rule from `oxidex` applies here too. This resolver returns
three states and guesses at none of them:

    satisfied    the issue exists and is closed
    blocked      the issue exists and is open
    unresolved   the issue could not be found, or `gh` could not be reached

`unresolved` is not a failure to be smoothed over. A required target the
resolver cannot see is a blocker, and reporting "probably fine" would
reintroduce exactly the assumption the typed graph exists to remove.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence

from ..graph import BLOCKED, SATISFIED, UNRESOLVED, ExternalTarget, ResolverOutcome

__all__ = ["parse_identity", "resolve", "resolver"]

#: `owner/repo#123`. Deliberately strict: a target this cannot read stays
#: unresolved and says so, rather than being turned into a repository guess.
_IDENTITY = re.compile(r"^(?P<repo>[^/\s#]+/[^/\s#]+)#(?P<number>\d+)$")

Runner = Callable[[Sequence[str]], str]


def parse_identity(identity: str) -> tuple[str, int] | None:
    match = _IDENTITY.match(identity.strip())
    if match is None:
        return None
    return match.group("repo"), int(match.group("number"))


def resolve(target: ExternalTarget, runner: Runner | None = None) -> ResolverOutcome:
    """Ask GitHub whether one issue is closed.

    `runner` is injected so this is testable without a network, a credential
    or a `gh` binary — the same contract `github.GitHub` uses.
    """
    parsed = parse_identity(target.identity)
    if parsed is None:
        return ResolverOutcome(
            UNRESOLVED,
            f"{target.identity!r} is not an OWNER/REPO#NUMBER reference, so this "
            "resolver cannot say what it points at",
        )
    repo, number = parsed
    run = runner or _gh
    try:
        out = run(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "-R",
                repo,
                "--json",
                "number,state,title,url",
            ]
        )
    except Exception as exc:  # noqa: BLE001 - unreachable is a state, not a crash
        return ResolverOutcome(UNRESOLVED, f"gh could not read {repo}#{number}: {exc}")
    try:
        raw = json.loads(out or "{}")
    except ValueError:
        return ResolverOutcome(UNRESOLVED, f"gh returned unreadable output for {repo}#{number}")
    state = str(raw.get("state") or "").lower()
    url = raw.get("url") or f"{repo}#{number}"
    if not state:
        return ResolverOutcome(UNRESOLVED, f"gh reported no state for {repo}#{number}")
    if state == "closed":
        return ResolverOutcome(SATISFIED, f"{url} is closed")
    return ResolverOutcome(BLOCKED, f"{url} is {state}")


def resolver(runner: Runner | None = None) -> Callable[[ExternalTarget], ResolverOutcome]:
    """The factory `graph.load_resolver` looks for by name."""

    def _resolve(target: ExternalTarget) -> ResolverOutcome:
        return resolve(target, runner)

    return _resolve


def _gh(args: Sequence[str]) -> str:
    import subprocess

    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        args, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh failed")
    return result.stdout
