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

import logging
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

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


def session_host_probe(host: Any) -> Probe:
    """A probe that reads from the session host, and creates nothing.

    `list_sessions` is the cheapest call that proves both halves of what
    matters: the host is reachable, and the token is accepted. Creating a
    session would prove the same thing and leave a PTY behind, which is not
    a readiness check but a side effect.
    """

    def probe() -> tuple[bool, str]:
        try:
            sessions = host.list_sessions()
        except Exception as exc:  # noqa: BLE001 - any failure is the same answer
            # Truncated, and never echoing a token: the detail is for a human
            # deciding what to fix, not a place to leak a credential.
            return (False, f"the session host refused a read: {str(exc)[:200]}")
        return (True, f"reachable and authenticated, {len(sessions)} live session(s)")

    return probe


def clean_checks_probe(project: Any, *, timeout: float = 900.0) -> Probe:
    """Run configured argv checks in a detached worktree of base_branch.

    This is intentionally opt-in: a clean build can be as expensive as the
    work itself, but when requested it must happen before any agent spends
    tokens and its failure must name the command that failed.
    """

    def probe() -> tuple[bool, str]:
        work_dir = getattr(project, "work_dir", None)
        commands = list(getattr(project, "checks", None) or [])
        if not work_dir or not commands:
            return (True, "no checks configured")
        root = Path(work_dir)
        if not (root / ".git").exists():
            return (False, f"cannot probe checks: {work_dir} is not a git repository")
        temp = Path(tempfile.mkdtemp(prefix="harness-check-", dir=str(root.parent)))
        try:
            temp.rmdir()
            base = getattr(project, "base_branch", "main")
            added = subprocess.run(
                ["git", "-C", str(root), "worktree", "add", "--detach", str(temp), base],
                capture_output=True,
                text=True,
                check=False,
            )
            if added.returncode != 0:
                return (False, f"could not create clean worktree: {added.stderr.strip()[:300]}")
            for raw in commands:
                from .runtime import validate_check_command

                try:
                    validate_check_command(raw)
                    argv = shlex.split(raw)
                    result = subprocess.run(
                        argv,
                        cwd=temp,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    return (False, f"`{raw}` timed out after {timeout:.0f}s on the base branch")
                except ValueError as exc:
                    return (False, str(exc))
                if result.returncode != 0:
                    tail = (result.stdout + result.stderr).strip().splitlines()[-40:]
                    return (False, f"`{raw}` failed on base branch:\n" + "\n".join(tail))
            return (True, f"{len(commands)} check(s) pass on {base}")
        finally:
            subprocess.run(
                ["git", "-C", str(root), "worktree", "remove", "--force", str(temp)],
                capture_output=True,
                text=True,
                check=False,
            )
            # `git worktree add` can fail after creating the directory but
            # before registering it, in which case `worktree remove` has
            # nothing to remove. The probe must not become another source of
            # orphaned trees.
            shutil.rmtree(temp, ignore_errors=True)

    return probe


BASE_RUNNING = "running"
BASE_PASSED = "passed"
BASE_FAILED = "failed"


@dataclass
class BaseCheckRun:
    """One run of a project's check list against its base branch."""

    project_id: str
    state: str
    started_at: float
    finished_at: float | None = None
    ok: bool | None = None
    detail: str = ""


class BaseChecks:
    """Base-branch check runs, kept off the request thread.

    Running the suite inside the HTTP request repeated the mistake `stop`
    made: for any project the check is useful on, the suite takes minutes, so
    the request outlives every proxy timeout and returns a transport error
    while the build carries on with nowhere to report. The obvious response to
    that error -- retry -- then started a *second* concurrent build instead of
    joining the first.

    So a run is started, joined if one is already going, and read back later.
    The result is remembered, which is what lets preflight answer honestly
    about a check list without rebuilding the world on every poll.
    """

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._runs: dict[str, BaseCheckRun] = {}

    def status(self, project_id: str) -> BaseCheckRun | None:
        with self._lock:
            return self._runs.get(project_id)

    def start(self, project: Any, *, timeout: float = 900.0) -> BaseCheckRun:
        """Begin a run, or return the one already in flight."""
        project_id = str(getattr(project, "project_id", "default"))
        with self._lock:
            current = self._runs.get(project_id)
            if current is not None and current.state == BASE_RUNNING:
                # Joined, not duplicated. Two builds of the same tree are two
                # worktrees and twice the disk for one answer.
                return current
            run = BaseCheckRun(project_id=project_id, state=BASE_RUNNING, started_at=self._now())
            self._runs[project_id] = run
        probe = clean_checks_probe(project, timeout=timeout)
        threading.Thread(
            target=self._execute,
            args=(run, probe),
            name=f"harness-base-checks-{project_id}",
            daemon=True,
        ).start()
        return run

    def _execute(self, run: BaseCheckRun, probe: Probe) -> None:
        try:
            ok, detail = probe()
        except Exception as exc:  # noqa: BLE001 - a probe must not kill its thread
            log.warning("base checks: %s raised", run.project_id, exc_info=True)
            ok, detail = False, f"base checks could not run: {exc}"
        with self._lock:
            run.ok = ok
            run.detail = detail
            run.state = BASE_PASSED if ok else BASE_FAILED
            run.finished_at = self._now()


def last_base_result_probe(checks: BaseChecks, project_id: str) -> Probe:
    """Report the most recent base-checks run, without starting one.

    Never runs the suite itself: a read of readiness has to stay a read. An
    answer that has not been obtained yet is reported as not-ready with the
    call that would obtain it, which is more use than a check that blocks.
    """

    def probe() -> tuple[bool, str]:
        run = checks.status(project_id)
        if run is None:
            return (
                False,
                "base checks have not been run; POST /api/projects/"
                f"{project_id}/preflight/base to start one",
            )
        if run.state == BASE_RUNNING:
            return (False, f"base checks started {run.started_at:.0f} and are still running")
        return (bool(run.ok), run.detail)

    return probe


@dataclass(frozen=True)
class Answer:
    """What one model said when asked a trivial question."""

    ok: bool
    detail: str
    seconds: float


#: Asks one route's model for a minimal completion. Injected, like every
#: other probe here, so a readiness gate can be tested without a network.
Ask = Callable[[Any], Answer]


@dataclass
class RoleReachability:
    """Which configured models actually answered, and which did not.

    Three buckets rather than a boolean because the middle one is a different
    decision: a model that answers late is usable and must not be refused,
    while a model that does not answer at all can complete nothing.
    """

    answered: dict[str, str] = field(default_factory=dict)
    slow: dict[str, str] = field(default_factory=dict)
    silent: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        # Failures first: they are what the reader has to act on.
        everything = {**self.silent, **self.slow, **self.answered}
        return "; ".join(f"{role}: {detail}" for role, detail in everything.items())


def remembered(ask: Ask, *, ttl: float = 60.0, now: Callable[[], float] = time.time) -> Ask:
    """The same answer for a short while, per model and endpoint.

    Readiness is polled — a dashboard refreshing every few seconds would
    otherwise turn a reachability probe into a load generator against the
    model endpoint, and bill for it. Whether an endpoint serves a model does
    not change between two polls seconds apart, so it is asked at most once a
    minute per route.
    """
    seen: dict[tuple[str, str], tuple[float, Answer]] = {}

    def cached(route: Any) -> Answer:
        key = (str(getattr(route, "model", "")), str(getattr(route, "endpoint", "")))
        found = seen.get(key)
        if found is not None and found[0] > now():
            return found[1]
        answer = ask(route)
        seen[key] = (now() + ttl, answer)
        return answer

    return cached


def role_reachability_probe(
    routes: Mapping[str, Any],
    ask: Ask,
    *,
    timeout: float = 10.0,
    patience: float = 20.0,
) -> Callable[[], RoleReachability]:
    """Does each configured role's model actually answer?

    Preflight used to check that a reviewer was *routed*, which is true of a
    model that will never reply. An endpoint can advertise a model in
    `/models` and serve nothing behind it; the harness then discovered that
    once per item, after the planner and implementer had been paid for, at
    the cost of the whole retry ladder — six attempts with escalating backoff
    is fifteen to twenty minutes of wall clock, per item, spent establishing a
    condition that was true before the run started.

    In parallel, because the roles are independent and the point is to be
    quick. `timeout` is the budget a healthy model is expected to meet;
    `patience` is the deadline after which it is treated as no answer at all.
    Between the two it is reported as slow and **not** failed: a model that
    replies late is usable, and refusing it would be this gate overreaching.

    Daemon threads, deliberately: an ask that never returns must not hold the
    report open, and must not hold the *process* open either. A pooled worker
    is joined at interpreter exit, which would make one wedged endpoint delay
    every shutdown of the service.
    """

    def probe() -> RoleReachability:
        report = RoleReachability()
        answers: dict[str, Answer] = {}

        def record(role: str, route: Any) -> None:
            try:
                answers[role] = ask(route)
            except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
                answers[role] = Answer(False, f"could not be asked: {str(exc)[:160]}", 0.0)

        threads = [
            threading.Thread(
                target=record, args=(role, route), name=f"harness-probe-{role}", daemon=True
            )
            for role, route in routes.items()
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + patience
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

        for role, route in routes.items():
            model = str(getattr(route, "model", "?"))
            answer = answers.get(role)
            if answer is None:
                report.silent[role] = f"{model} did not answer within {patience:g}s"
            elif not answer.ok:
                # Always named, whether or not the asker already did: a
                # failure that does not say which model failed does not say
                # what to change.
                detail = answer.detail
                report.silent[role] = detail if detail.startswith(model) else f"{model}: {detail}"
            elif answer.seconds > timeout:
                report.slow[role] = f"{answer.detail} after {answer.seconds:.0f}s"
            else:
                report.answered[role] = f"{answer.detail} in {answer.seconds:.1f}s"
        return report

    return probe


def disk_space_probe(path: str, floor_gb: float = 0.0) -> tuple[bool, str]:
    """Free space on the volume holding a project's checkout."""
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return (False, f"could not measure free space at {path}: {exc}")
    gib = 1024**3
    free_gb = usage.free / gib
    total_gb = usage.total / gib
    ok = floor_gb <= 0 or free_gb >= floor_gb
    detail = f"{free_gb:.1f} GiB free of {total_gb:.1f} GiB on volume holding {path}"
    if floor_gb > 0:
        detail += f"; configured floor {floor_gb:.1f} GiB"
    return (ok, detail)


def preflight_project(
    project: Any,
    *,
    has_fleet: bool,
    reviewer_route: Any = None,
    reviewer_independent: tuple[bool, str] | None = None,
    role_probe: Callable[[], RoleReachability] | None = None,
    session_host: Probe | None = None,
    git_probe: Callable[[str], tuple[bool, str]] = _is_git_repo,
    github_probe: Callable[[str], tuple[bool, str]] = _gh_can_write,
    checks_probe: Probe | None = None,
    disk_probe: Callable[[str, float], tuple[bool, str]] = disk_space_probe,
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
    if checks_probe is not None:
        ok, detail = checks_probe()
        checks.append(Check("base checks", ok, detail))

    # Only when one is configured. A deployment with no session host is
    # already caught by the workers check above, and adding a second failing
    # check for the same fact would just make the summary noisier.
    if session_host is not None:
        ok, detail = session_host()
        checks.append(
            Check(
                "session host",
                ok,
                detail if ok else f"{detail} — agents run as sessions on it, so none can start",
            )
        )

    work_dir = getattr(project, "work_dir", None)
    if work_dir:
        ok, detail = git_probe(work_dir)
        checks.append(Check("checkout", ok, detail))
        disk_ok, disk_detail = disk_probe(
            work_dir, float(getattr(project, "min_free_disk_gb", 0.0) or 0.0)
        )
        checks.append(Check("disk space", disk_ok, disk_detail))
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

    if role_probe is not None:
        reach = role_probe()
        if reach.silent:
            # Blocking, because a fleet whose reviewer cannot answer can
            # complete nothing: every item runs to the last step and fails
            # there, having already paid for the plan and the implementation.
            checks.append(
                Check(
                    "role reachability",
                    False,
                    f"{reach.summary()} — the model is configured but not answering, "
                    "so every item would fail at that stage after being paid for",
                )
            )
        elif reach.answered or reach.slow:
            checks.append(Check("role reachability", True, reach.summary()))
        if reach.slow:
            # A warning: a slow model is usable, and a preflight budget is not
            # a statement about how long a real completion may take.
            checks.append(
                Check(
                    "model latency",
                    False,
                    "; ".join(f"{role}: {detail}" for role, detail in reach.slow.items())
                    + " — slow to answer a one-token prompt, so every call pays that first",
                    blocking=False,
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
