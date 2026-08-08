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


def _is_clean_tree(path: str) -> tuple[bool, str]:
    """Whether the checkout holds work the harness would destroy.

    A headless run works **in place**: every attempt begins by discarding the
    working tree — `git checkout -- .` then `git clean -fd` — so it can put the
    item's branch on a known state. That is right for the harness's own
    leftovers and catastrophic for a person's: a modified tracked file is
    reverted, and an untracked one is deleted, neither recoverably.

    Nothing said so until a real import nearly lost 136 uncommitted files
    including two whole crates (#147). So it is a refusal, before the first
    claim, rather than a sentence in a document.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", path, "status", "--porcelain"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - defensive
        return (False, f"could not read the checkout's status: {exc}")
    if result.returncode != 0:
        return (False, f"could not read the checkout's status: {result.stderr.strip()[:200]}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return (True, "clean")
    modified = sum(1 for line in lines if not line.startswith("??"))
    untracked = len(lines) - modified
    return (
        False,
        f"{modified} uncommitted change(s) and {untracked} untracked path(s) in {path}. "
        "A run discards both — tracked files are reverted and untracked ones are "
        "DELETED — so this work would be lost and could not be recovered. Commit or "
        "stash it, or pass --allow-dirty if it is genuinely disposable.",
    )


#: How far behind its upstream a base may be before the run is refused. A few
#: commits behind is the ordinary state of any branch and blocking on it would
#: make the check noise nobody reads. Two dozen is a different repository.
STALE_BASE_LIMIT = 25


def _git(path: str, *args: str, timeout: float = 60.0) -> tuple[int, str, str]:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", path, *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (1, "", str(exc))
    return (result.returncode, result.stdout.strip(), result.stderr.strip())


def _base_is_current(path: str, base: str) -> tuple[bool, str]:
    """Whether the base branch still resembles the line of work it came from.

    **This is the one wrong configuration every downstream stage reports as a
    success.** The agent works, the checks pass, the reviewer approves and the
    commit lands — onto a branch nobody is developing on any more. There is no
    failure to notice, so nothing notices, and the cost is not one item but
    every item in the run.

    Measured on `rdpapp`: a base cut from a local working tree turned out to be
    121 commits behind `origin/master` and 27 ahead, carrying an alternate
    implementation that was never promoted. Six items were delivered onto it
    before a human who knew the lineage said so (#180).

    Deliberately quiet about what it cannot answer. A branch with no upstream,
    a repository with no remote, an unreachable remote — none of those are
    evidence that the base is stale, and reporting them as failures would train
    people to pass the override flag by default.
    """
    upstreams = _upstreams_for(path, base)
    if not upstreams:
        return (True, f"nothing to compare {base} against: no upstream and no remote default")

    # Behind *every* candidate is the finding. A base cut locally for one run
    # legitimately tracks nothing, and a repository can have several remotes
    # only one of which is authoritative — so being current with any one of
    # them is enough to believe the base is on a live line of work.
    #
    # The first draft of this returned early when a branch tracked nothing and
    # the repository had more than one remote, which is exactly the shape of
    # the case it was written for: `harness/base` tracked nothing, `rdpapp` has
    # a Forgejo `origin` and a GitHub secondary, and the check said nothing at
    # all while the base sat 121 commits behind.
    results: list[tuple[int | None, str]] = []
    for upstream in upstreams:
        remote = upstream.split("/", 1)[0]
        code, _, err = _git(path, "fetch", "--quiet", remote, timeout=120.0)
        if code == 0 and _git(path, "merge-base", base, upstream)[0] != 0:
            # No common ancestor: a different project that happens to be a
            # remote of this checkout, not a line of work this base could have
            # come from. Counting it would let an unrelated remote with two
            # commits in it vouch for a base that is a hundred behind the one
            # that matters.
            continue
        if code != 0:
            # Unreachable is a fact about the network, not about the base.
            results.append((None, f"could not reach {remote}: {err[:100]}"))
            continue
        code, counts, _ = _git(path, "rev-list", "--left-right", "--count", f"{base}...{upstream}")
        if code != 0 or "\t" not in counts:
            results.append((None, f"could not compare {base} against {upstream}"))
            continue
        ahead_s, _, behind_s = counts.partition("\t")
        try:
            ahead, behind = int(ahead_s), int(behind_s.strip())
        except ValueError:  # pragma: no cover - git's output is two integers
            results.append((None, f"could not read {base}...{upstream}"))
            continue
        results.append(
            (
                behind,
                f"{behind} commit(s) behind {upstream}" + (f" and {ahead} ahead" if ahead else ""),
            )
        )

    measured = [(behind, text) for behind, text in results if behind is not None]
    if not measured:
        return (True, f"could not compare {base}: " + "; ".join(text for _, text in results))

    closest, detail = min(measured, key=lambda pair: pair[0])
    if closest == 0:
        return (True, f"{base} is current with {detail.split(' behind ', 1)[-1]}")
    where = f"{base} is " + "; ".join(text for _, text in measured)
    if closest <= STALE_BASE_LIMIT:
        return (True, where)
    return (
        False,
        f"{where}. Work based here lands on a lineage that has moved on, and every "
        "stage after this one — the agent, the checks, the reviewer, the commit — "
        "will report success while it happens. Rebase or cut a new base from the "
        "current head, or pass --allow-stale-base if this really is the line of work.",
    )


def _upstreams_for(path: str, base: str) -> list[str]:
    """Every remote ref this base could reasonably be measured against.

    Its own tracking branch when it has one, since that is the answer the
    person who made the branch already gave. Otherwise every remote's default
    head — plural on purpose, because "which remote is authoritative" is not a
    question a preflight check can answer and not one it should guess at.
    """
    code, upstream, _ = _git(path, "rev-parse", "--abbrev-ref", f"{base}@{{upstream}}")
    if code == 0 and upstream:
        return [upstream]
    code, remotes, _ = _git(path, "remote")
    heads = []
    for remote in (name.strip() for name in remotes.splitlines() if name.strip()):
        code, head, _ = _git(path, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD")
        if code == 0 and head:
            heads.append(head)
    return heads


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
    role_runner: Probe | None = None,
    git_probe: Callable[[str], tuple[bool, str]] = _is_git_repo,
    github_probe: Callable[[str], tuple[bool, str]] = _gh_can_write,
    checks_probe: Probe | None = None,
    disk_probe: Callable[[str, float], tuple[bool, str]] = disk_space_probe,
    clean_probe: Callable[[str], tuple[bool, str]] = _is_clean_tree,
    allow_dirty: bool = False,
    base_probe: Callable[[str, str], tuple[bool, str]] = _base_is_current,
    allow_stale_base: bool = False,
    execution_environment: Probe | None = None,
    remote_required: bool = True,
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

    if role_runner is not None:
        ok, detail = role_runner()
        checks.append(Check("role runner", ok, detail))

    if execution_environment is not None:
        ok, detail = execution_environment()
        checks.append(Check("execution environment", ok, detail))

    work_dir = getattr(project, "work_dir", None)
    if work_dir:
        ok, detail = git_probe(work_dir)
        checks.append(Check("checkout", ok, detail))
        # Only worth asking of something that is a git checkout at all —
        # otherwise the answer is a confusing second failure about the same
        # fact the check above already reported.
        if ok:
            clean_ok, clean_detail = clean_probe(work_dir)
            checks.append(
                Check(
                    "clean checkout",
                    clean_ok or allow_dirty,
                    clean_detail
                    if clean_ok
                    else f"{clean_detail}{' Allowed by --allow-dirty.' if allow_dirty else ''}",
                )
            )
            base = str(getattr(project, "base_branch", "") or "")
            if base:
                base_ok, base_detail = base_probe(work_dir, base)
                checks.append(
                    Check(
                        "base branch",
                        base_ok or allow_stale_base,
                        base_detail
                        if base_ok
                        else f"{base_detail}"
                        + (" Allowed by --allow-stale-base." if allow_stale_base else ""),
                    )
                )
        disk_ok, disk_detail = disk_probe(
            work_dir, float(getattr(project, "min_free_disk_gb", 0.0) or 0.0)
        )
        checks.append(Check("disk space", disk_ok, disk_detail))
    else:
        checks.append(
            Check("checkout", False, "no work_dir is configured, so no worktree can be made")
        )

    repo = getattr(project, "repo", None)
    if repo and remote_required:
        ok, detail = github_probe(repo)
        checks.append(
            Check(
                "github write",
                ok,
                detail if ok else f"{detail} — items cannot reach a pull request",
            )
        )
    elif remote_required:
        checks.append(
            Check("github write", False, "no repo is configured, so no pull request can be opened")
        )
    else:
        checks.append(
            Check(
                "github write",
                True,
                "remote publication is disabled; local promotion is the configured destination",
                blocking=False,
            )
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
