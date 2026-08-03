"""Can this project actually finish an item?

A queue can be resumed into a state where every item is doomed: no reviewer,
so every review fails closed; no git checkout, so no worktree; no GitHub write
credential, so nothing can reach a pull request. The fleet claims work, spends
money, and fails every item — while the API reports `running`.

The expensive part is not the failure. It is that a nonproductive fleet and a
productive one look identical from outside until the bill arrives, so nobody
looks for hours.

So: check before claiming, name what is missing, and refuse rather than start.
Checks that would merely reduce quality warn; checks that make the definition
of done unreachable block.

Every probe is injected. Reading the real filesystem and shelling out to `gh`
inside a check makes the check untestable, and an untestable readiness gate is
the last thing that should be trusted.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: A probe answers one question about the world. Returning (ok, detail) rather
#: than raising keeps a failing probe from looking like a broken harness.
Probe = Callable[[], "tuple[bool, str]"]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    #: Blocking means the definition of done is unreachable, not merely that
    #: quality suffers. Only blocking checks refuse a start.
    blocking: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "blocking": self.blocking,
        }


@dataclass
class Preflight:
    project_id: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not any(c.blocking and not c.ok for c in self.checks)

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.blocking and not c.ok]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.blocking and not c.ok]

    def summary(self) -> str:
        if self.ready:
            warned = len(self.warnings)
            return "ready" + (f", {warned} warning(s)" if warned else "")
        return "; ".join(f"{c.name}: {c.detail}" for c in self.blockers)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "ready": self.ready,
            "summary": self.summary(),
            "checks": [c.as_dict() for c in self.checks],
        }


def _is_git_repo(path: str) -> tuple[bool, str]:
    directory = Path(path)
    if not directory.exists():
        return (False, f"{path} does not exist")
    if not (directory / ".git").exists():
        return (False, f"{path} is not a git repository")
    return (True, path)


def _gh_can_write(repo: str) -> tuple[bool, str]:
    """Whether `gh` can actually write to the repo.

    Checked by asking GitHub, not by looking for a token: a token that exists
    and lacks the scope produces exactly the failure this gate is for, at the
    point where an agent has already done the work.
    """
    if not shutil.which("gh"):
        return (False, "the gh CLI is not installed")
    try:
        result = subprocess.run(  # noqa: S603
            ["gh", "api", f"repos/{repo}", "--jq", ".permissions.push"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (False, f"could not ask GitHub: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        return (False, detail[0][:160] if detail else "gh refused the request")
    if result.stdout.strip() != "true":
        return (False, f"no push permission on {repo}")
    return (True, f"push access to {repo}")


def preflight_project(
    project: Any,
    *,
    has_fleet: bool,
    reviewer_route: Any = None,
    reviewer_independent: tuple[bool, str] | None = None,
    git_probe: Callable[[str], tuple[bool, str]] = _is_git_repo,
    github_probe: Callable[[str], tuple[bool, str]] = _gh_can_write,
) -> Preflight:
    """Everything that must hold before this project can produce a pull request."""
    checks: list[Check] = []

    # Without a fleet, `start` can only set a flag. That is the false-running
    # state: the API says running, nothing claims, and the two are
    # indistinguishable from outside.
    checks.append(
        Check(
            "workers",
            has_fleet,
            "a worker pool is attached"
            if has_fleet
            else "no worker pool is attached, so starting would set a flag nobody acts on",
        )
    )

    work_dir = getattr(project, "work_dir", None)
    if work_dir:
        ok, detail = git_probe(work_dir)
        checks.append(Check("checkout", ok, detail))
    else:
        checks.append(
            Check("checkout", False, "no work_dir is configured, so no worktree can be made")
        )

    repo = getattr(project, "repo", None)
    if repo:
        ok, detail = github_probe(repo)
        checks.append(
            Check(
                "github write",
                ok,
                detail if ok else f"{detail} — items cannot reach a pull request",
            )
        )
    else:
        checks.append(
            Check("github write", False, "no repo is configured, so no pull request can be opened")
        )

    # No reviewer means every review fails closed, so every item fails. That
    # is worse than not starting: it spends the implementer's tokens first.
    checks.append(
        Check(
            "reviewer",
            reviewer_route is not None,
            "a reviewer role is routed"
            if reviewer_route is not None
            else "no reviewer is routed; review fails closed, so every item would fail "
            "after paying for the implementation",
        )
    )

    if reviewer_independent is not None:
        independent, why = reviewer_independent
        # A warning, not a blocker: running one model is a legitimate
        # deliberate choice about your own budget.
        checks.append(Check("reviewer independence", independent, why, blocking=False))

    project_checks = list(getattr(project, "checks", None) or [])
    checks.append(
        Check(
            "checks",
            bool(project_checks),
            f"{len(project_checks)} check(s) run before review"
            if project_checks
            else "no checks configured — nothing verifies a diff before the reviewer sees it",
            blocking=False,
        )
    )

    return Preflight(project_id=getattr(project, "project_id", "?"), checks=checks)
