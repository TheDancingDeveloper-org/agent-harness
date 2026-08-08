"""What is configured, what is missing, and what nobody can see.

`preflight.py` answers one question — *may this project start?* — and answers
it at the moment of starting. This answers a wider one, earlier, and for the
whole deployment: *if I started this, what would go wrong, and what would I
not find out about?*

They deliberately overlap. Where they do, the same probe is used, so doctor
cannot disagree with the gate that actually refuses a start. What doctor adds
is the set of questions that do not block a start but change what you can
believe about a run: which wire protocol each route resolved to, whether the
failure classifier can tell a spend cap from a burst limit, whether the
configured checks are runnable argv at all, and whether the implementer's
traffic is observable.

**Three rules this module keeps.**

*It spends nothing by default.* Every finding below is answered from the
database, the filesystem and installed metadata. The one check that needs a
network — asking a model whether it answers — is opt-in and is otherwise
reported as *not asked*, never as passing.

*It never reports an unknown as a pass.* An unmeasured thing is `unknown`,
which is its own state and is neither ok nor a failure. A diagnostic that
rounds "I could not tell" down to "fine" is worse than no diagnostic, because
it is believed.

*It is read-only.* No row is written, no worktree is made, no session is
created, nothing is registered. Running doctor twice changes nothing.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: A finding's state. `unknown` is a first-class answer, not a soft failure:
#: it is what a check that was not run reports, and it must never be read as
#: either good or bad news.
OK = "ok"
FAIL = "fail"
WARN = "warn"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Finding:
    """One question, its answer, and whether the answer stops work.

    `blocking` means the definition of done is unreachable — the same meaning
    `preflight.Check` gives it, deliberately, so the two cannot drift into
    describing different severities with the same word.
    """

    name: str
    state: str
    detail: str
    blocking: bool = False

    @property
    def ok(self) -> bool:
        return self.state == OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "detail": self.detail,
            "blocking": self.blocking,
        }


@dataclass
class ProjectReport:
    project_id: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking and f.state == FAIL]

    @property
    def ok(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "ok": self.ok,
            "findings": [f.as_dict() for f in self.findings],
        }


@dataclass
class Report:
    """The whole deployment: what is true everywhere, then per project."""

    environment: list[Finding] = field(default_factory=list)
    projects: list[ProjectReport] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        blocked_env = any(f.blocking and f.state == FAIL for f in self.environment)
        return not blocked_env and all(p.ok for p in self.projects)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "environment": [f.as_dict() for f in self.environment],
            "projects": [p.as_dict() for p in self.projects],
        }


# ------------------------------------------------------------ environment


def _git_finding() -> Finding:
    path = shutil.which("git")
    if path is None:
        return Finding(
            "git",
            FAIL,
            "git is not on PATH — every item works in a git worktree, so none can start",
            blocking=True,
        )
    return Finding("git", OK, f"git at {path}")


def _gh_finding() -> Finding:
    """Whether `gh` exists. Deliberately **not** whether it can write.

    Asking GitHub for push permission is a network call against a real
    account, which is exactly what this command promises not to do. Preflight
    asks it at the point a project starts, where the cost is justified because
    the alternative is failing every item at the pull request.
    """
    path = shutil.which("gh")
    if path is None:
        return Finding(
            "gh cli",
            WARN,
            "gh is not on PATH — local work is fine; nothing can reach a pull request",
        )
    return Finding(
        "gh cli",
        UNKNOWN,
        f"gh at {path}; whether it can WRITE is not checked here because that is a "
        "network call. Preflight asks it when a project starts.",
    )


def _presets_finding() -> Finding:
    from . import protocols

    names = protocols.names()
    return Finding("route presets", OK, f"resolvable by name: {', '.join(names)}")


def _resolvers_finding() -> Finding:
    from .graph import _declared_resolver_targets

    names = sorted(_declared_resolver_targets())
    if not names:
        return Finding(
            "dependency resolvers",
            WARN,
            "none declared — an `external:` dependency can be written but never resolved, "
            "so it stays a blocker",
        )
    return Finding("dependency resolvers", OK, f"declared: {', '.join(names)}")


def _runner_finding(selected: str) -> Finding:
    """The selected execution loop and whether its contract can load."""
    from .role_runners import names, probe

    if not selected:
        available = ", ".join(names()) or "none"
        return Finding(
            "role runner",
            WARN,
            "none selected — implementation uses the historical single-shot path; "
            f"installed runner names: {available}",
        )
    ok, detail = probe(selected)
    return Finding("role runner", OK if ok else FAIL, detail, blocking=not ok)


def _execution_environment_finding(queue: Any) -> Finding:
    """Report the item command boundary before a worker can claim anything."""
    selected = str(queue.get_setting("execution_backend") or "").strip()
    image = str(queue.get_setting("execution_image") or "").strip()
    if not selected:
        return Finding(
            "execution environment",
            WARN,
            "host compatibility execution is selected; it is not an OS security boundary. "
            "Configure a metadata-selected backend and image before a real workload.",
            blocking=False,
        )
    from .execution_environments import probe

    ok, detail = probe(selected)
    if not image:
        return Finding(
            "execution environment",
            FAIL,
            f"backend {selected!r} is selected but no execution image is configured; {detail}",
            blocking=True,
        )
    return Finding(
        "execution environment",
        OK if ok else FAIL,
        f"{detail}; image {image}",
        blocking=not ok,
    )


def _redaction_finding() -> Finding:
    """What the write-boundary filter can and cannot promise.

    Deliberately `unknown` rather than `ok`. Pattern redaction cannot catch a
    credential whose shape it does not know and whose value it was not given,
    so reporting it as a passed check would be the one thing this must not
    claim. What it can state is the part that has no false negatives: how
    many exact values this process was handed.
    """
    from .redaction import from_environment

    known = len(from_environment().secrets)
    return Finding(
        "credential redaction",
        UNKNOWN,
        f"events are redacted before they are written; {known} exact value(s) known to "
        "this process, plus credential shapes. It reduces exposure, it does not remove "
        "it: a credential of an unknown shape whose value was not supplied still lands, "
        "and the stores are append-only, so the remedy then is rotation, not deletion.",
    )


def _guard_finding(stored: Any = None) -> Finding:
    """Whether any command policy is in force, and whether anybody chose it.

    Said in the same voice as reviewer independence and GitHub mutation: it
    reports what would happen, in both directions, rather than only complaining.

    **An unconfigured guard is reported as not configured, never as a pass.**
    The built-in default is in force and it does refuse things — but nobody in
    this deployment decided what must never run here, and reading `ok` against
    a policy nobody wrote is exactly the false comfort this module's second rule
    forbids. A guard nobody enabled is not a guard.
    """
    from .guard import DEFAULT_REFUSALS, CommandGuard

    guard = CommandGuard.from_settings(stored)
    if not guard.active:
        return Finding(
            "command guard",
            FAIL,
            "every refusal is switched off: the harness will run any command a plan, a "
            "check or $HARNESS_AGENT_COMMAND names, anywhere on this filesystem",
        )
    if not guard.configured:
        return Finding(
            "command guard",
            WARN,
            "not configured — the built-in default is in force "
            f"({guard.describe()}: {', '.join(DEFAULT_REFUSALS)}), and it knows nothing "
            "about what THIS deployment must never run. `agent-harness guard --refuse "
            "PATTERN` writes a policy; until then nobody has chosen one.",
        )
    return Finding(
        "command guard",
        OK,
        f"{guard.describe()}"
        + (f"; configured: {', '.join(guard.refusals)}" if guard.refusals else ""),
    )


def environment_findings() -> list[Finding]:
    return [
        _git_finding(),
        _gh_finding(),
        _presets_finding(),
        _resolvers_finding(),
        _redaction_finding(),
    ]


# --------------------------------------------------------------- per project


def _checkout_finding(project: Any) -> Finding:
    work_dir = getattr(project, "work_dir", None)
    if not work_dir:
        return Finding(
            "checkout",
            FAIL,
            "no work_dir is configured, so no worktree can be made",
            blocking=True,
        )
    path = Path(work_dir)
    if not path.exists():
        return Finding("checkout", FAIL, f"{work_dir} does not exist", blocking=True)
    if not (path / ".git").exists():
        return Finding("checkout", FAIL, f"{work_dir} is not a git repository", blocking=True)
    return Finding("checkout", OK, str(path))


def _clean_checkout_finding(project: Any) -> Finding | None:
    """What a run would destroy in the checkout, said before it runs.

    `doctor` reports it as blocking for the same reason preflight refuses on
    it: a headless run discards the working tree, and uncommitted work is not
    recoverable once it has. Shares preflight's probe so the two cannot
    disagree about the same directory.
    """
    work_dir = getattr(project, "work_dir", None)
    if not work_dir or not (Path(work_dir) / ".git").exists():
        return None
    from .preflight import _is_clean_tree

    ok, detail = _is_clean_tree(str(work_dir))
    return Finding("clean checkout", OK if ok else FAIL, detail, blocking=not ok)


def _disk_finding(project: Any) -> Finding | None:
    work_dir = getattr(project, "work_dir", None)
    if not work_dir or not Path(work_dir).exists():
        return None
    from .preflight import disk_space_probe

    floor = float(getattr(project, "min_free_disk_gb", 0.0) or 0.0)
    ok, detail = disk_space_probe(str(work_dir), floor)
    return Finding("disk space", OK if ok else FAIL, detail, blocking=not ok)


def _checks_finding(project: Any) -> Finding:
    """Are the project's checks *runnable*, not merely configured?

    A check command is stored as text and split into argv. A command whose
    program is not installed passes every configuration test there is and then
    fails at the one moment that matters: after the implementer has been paid
    for and before the reviewer. So the program is looked up on PATH here,
    which costs nothing and is the whole difference between "configured" and
    "will run".

    The command is **not executed.** Running a project's test suite is not a
    diagnostic, it is the check itself, and it can take minutes.
    """
    import shlex

    commands = list(getattr(project, "checks", None) or [])
    if not commands:
        return Finding(
            "checks",
            WARN,
            "none configured — nothing verifies a diff before the reviewer sees it",
        )
    problems: list[str] = []
    for command in commands:
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            problems.append(f"{command!r} does not parse as argv: {exc}")
            continue
        if not argv:
            problems.append(f"{command!r} is empty")
            continue
        if shutil.which(argv[0]) is None:
            problems.append(f"{argv[0]!r} is not on PATH (from {command!r})")
    if problems:
        return Finding("checks", FAIL, "; ".join(problems), blocking=True)
    return Finding("checks", OK, f"{len(commands)} check(s) run before review")


def _route_findings(routes: dict[str, Any], *, needed: tuple[str, ...]) -> list[Finding]:
    """Completeness, then what each route actually resolved to.

    Two findings rather than one because they answer different questions. The
    first is whether a role can be called at all. The second is what will be
    on the wire and what will happen when the far end says no — which nobody
    configures explicitly and everybody assumes.
    """
    findings: list[Finding] = []
    missing = [
        name
        for name in needed
        if name not in routes
        or not getattr(routes[name], "model", "")
        or not getattr(routes[name], "endpoint", "")
    ]
    if missing:
        findings.append(
            Finding(
                "routes",
                FAIL,
                f"no usable route for: {', '.join(missing)} — an item claimed now would "
                "fail at that role, after paying for the ones before it",
                blocking=True,
            )
        )
    else:
        findings.append(Finding("routes", OK, ", ".join(f"{n}={routes[n].model}" for n in needed)))

    described: list[str] = []
    generic: list[str] = []
    unnamed: list[str] = []
    for name in needed:
        route = routes.get(name)
        if route is None:
            continue
        if not getattr(route, "preset", ""):
            unnamed.append(name)
        try:
            preset = route.resolve()
        except Exception as exc:  # noqa: BLE001 - an unresolvable preset is a finding
            described.append(f"{name}: preset will not resolve — {str(exc)[:120]}")
            continue
        described.append(f"{name}: {preset.name} / {route.classifier.name}")
        if getattr(route.classifier, "name", "") == "generic":
            generic.append(name)
    findings.append(Finding("protocol and classifier", OK, "; ".join(described)))
    if unnamed:
        # Said out loud because the line above is about the *stored* route and
        # a run can override it. Reporting `generic` for a role that will
        # actually speak chat-completions would be a true statement about the
        # database and a false one about the run.
        findings.append(
            Finding(
                "preset not stored",
                UNKNOWN,
                f"{', '.join(unnamed)} name no preset, so the line above is what they "
                "resolve to from the database alone. `run --preset NAME` (default "
                "chat-completions) fills it in for that run and is not recorded here — "
                "store the preset per role if you want the two to agree.",
            )
        )
    if generic:
        findings.append(
            Finding(
                "cost-cap classification",
                WARN,
                f"{', '.join(generic)} use the generic HTTP classifier, which cannot tell "
                "a spend cap from a burst limit — so a 429 that will not clear for days "
                "is retried as if it would clear in seconds",
            )
        )
    return findings


def _reviewer_findings(routes: dict[str, Any], *, implemented_by: str) -> list[Finding]:
    from .model_client import reviewer_independence

    findings: list[Finding] = []
    if routes.get("reviewer") is None:
        findings.append(
            Finding(
                "reviewer",
                FAIL,
                "no reviewer is routed; review fails closed, so every item would fail "
                "after paying for the implementation",
                blocking=True,
            )
        )
        return findings
    independent, why = reviewer_independence(routes, implemented_by=implemented_by)
    findings.append(Finding("reviewer independence", OK if independent else WARN, why))
    return findings


def _observability_findings(project: Any, routes: dict[str, Any]) -> list[Finding]:
    """Whether the harness can see what it spent, and say so when it cannot.

    This is issue #128 stated as a finding rather than left as a surprise. In
    session mode the implementing agent runs inside a hosted CLI session and
    its traffic never passes through `ModelClient`, so the cost rollup for
    those items is not low — it is *incomplete by an unknown amount*, which is
    a different and worse thing to read off a dashboard.
    """
    del project
    findings: list[Finding] = []
    unpriced = [
        name
        for name, route in routes.items()
        if route is not None and str(getattr(route, "model", "")) and not _is_priced(route)
    ]
    if unpriced:
        findings.append(
            Finding(
                "cost visibility",
                UNKNOWN,
                f"no price is known for: {', '.join(sorted(unpriced))} — their calls land "
                "as unpriced rather than free, so any total that includes them is a "
                "lower bound",
            )
        )
    else:
        findings.append(Finding("cost visibility", OK, "every routed model has a price"))
    return findings


def _is_priced(route: Any) -> bool:
    from .pricing import load_price_table

    table = load_price_table()
    try:
        return table.price_for(getattr(route, "model", "")) is not None
    except Exception:  # noqa: BLE001 - an unreadable table is not a routing failure
        return False


def _budget_findings(project: Any) -> list[Finding]:
    """What this project is permitted to spend, before it spends it.

    §8.2 of the extension proposal asks for exactly this: an operator should
    be able to read the ceilings without waiting to discover them. Unlimited
    is reported as a finding rather than as silence — it is the default, it is
    safe on upgrade, and it is the wrong answer for a seven-day unattended run
    (**D14**, open).
    """
    from .budgets import Budget

    budget = Budget(
        seconds=float(getattr(project, "max_item_seconds", 0.0) or 0.0),
        spend_usd=float(getattr(project, "max_item_spend_usd", 0.0) or 0.0),
    )
    if not budget.bounded:
        return [
            Finding(
                "item budgets",
                WARN,
                "no per-item ceiling: one item may run for any length of time and spend "
                "any amount. Safe on upgrade and unsafe unattended — set "
                "max_item_seconds and/or max_item_spend_usd.",
            )
        ]
    findings = [Finding("item budgets", OK, budget.describe())]
    if budget.spend_usd:
        findings.append(
            Finding(
                "spend ceiling enforceability",
                UNKNOWN,
                "a spend ceiling can only be enforced over calls whose price is known. "
                "Session-mode traffic bypasses ModelClient entirely (#128) and an "
                "unpriced model reports no cost, and in both cases the item's recorded "
                "spend is a LOWER BOUND. Whether this project will hit that is not "
                "knowable before it runs; the item reports it when it does.",
            )
        )
    return findings


def _github_finding(project: Any) -> Finding:
    """Whether this project can mutate anything on GitHub.

    Stated as a finding in both directions. "No repo configured" is not a
    defect — it is the first-run default and it is why the demo is safe — but
    it is also the reason a pull request will never appear, and someone
    waiting for one deserves to be told which of the two they are in.
    """
    repo = getattr(project, "repo", None)
    if not repo:
        return Finding(
            "github mutations",
            OK,
            "no repo configured: nothing here can create an issue, branch or pull "
            "request. Local work only.",
        )
    return Finding(
        "github mutations",
        UNKNOWN,
        f"repo {repo} is configured, so a run may push and open pull requests. Whether "
        "the credential actually has write access is a network call and is not asked "
        "here; preflight asks it at start.",
    )


def _model_findings(routes: dict[str, Any], ask: Any, needed: tuple[str, ...]) -> list[Finding]:
    if ask is None:
        return [
            Finding(
                "model reachability",
                UNKNOWN,
                "not asked — it needs a network and a credential. Pass --probe-models "
                "to ask. Not asking is not the same as answering.",
            )
        ]
    findings: list[Finding] = []
    for name in needed:
        route = routes.get(name)
        if route is None:
            continue
        try:
            ok, detail = ask(route)
        except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
            findings.append(
                Finding(f"model reachability: {name}", FAIL, f"could not ask: {str(exc)[:160]}")
            )
            continue
        findings.append(
            Finding(f"model reachability: {name}", OK if ok else FAIL, detail, blocking=not ok)
        )
    return findings


#: The roles the direct-API executor calls. Session mode calls only the
#: reviewer, but which executor a deployment will use is a `run` flag rather
#: than stored state, so doctor reports against the fuller set and says so.
NEEDED_ROLES = ("planner", "implementer", "reviewer")


def diagnose(queue: Any, projects: list[Any], *, ask: Any = None) -> Report:
    """The whole report. Reads; never writes."""
    from .api import ROLE_MAP_KEY
    from .guard import GUARD_KEY
    from .model_client import routes_from_map

    report = Report(environment=environment_findings())
    # Deployment-wide, like the role map: the worktrees, the host and the
    # credentials on disk are shared by every project in one database.
    report.environment.append(_guard_finding(queue.get_setting(GUARD_KEY)))
    report.environment.append(_runner_finding(str(queue.get_setting("role_runner") or "")))
    report.environment.append(_execution_environment_finding(queue))
    stored = queue.get_setting(ROLE_MAP_KEY) or {}

    for project in projects:
        project_id = getattr(project, "project_id", "?")
        found = ProjectReport(project_id=project_id)
        routes = {
            **routes_from_map(stored),
            **routes_from_map(getattr(project, "roles", None) or {}),
        }
        found.findings.append(_checkout_finding(project))
        clean = _clean_checkout_finding(project)
        if clean is not None:
            found.findings.append(clean)
        disk = _disk_finding(project)
        if disk is not None:
            found.findings.append(disk)
        found.findings.append(_checks_finding(project))
        found.findings.extend(_route_findings(routes, needed=NEEDED_ROLES))
        found.findings.extend(_reviewer_findings(routes, implemented_by=""))
        found.findings.extend(_observability_findings(project, routes))
        found.findings.extend(_budget_findings(project))
        found.findings.append(_github_finding(project))
        found.findings.extend(_model_findings(routes, ask, NEEDED_ROLES))
        report.projects.append(found)

    return report


_MARK = {OK: "ok  ", FAIL: "FAIL", WARN: "warn", UNKNOWN: "?   "}


def render(report: Report) -> str:
    """For a human. The failures are not buried under the passes."""
    lines: list[str] = ["environment"]
    for finding in report.environment:
        lines.append(f"  {_MARK[finding.state]}  {finding.name}: {finding.detail}")
    if not report.projects:
        lines.append("")
        lines.append("no projects are registered in this database.")
        lines.append("`agent-harness init --demo` builds one that needs no credentials.")
        return "\n".join(lines)
    for project in report.projects:
        lines.append("")
        lines.append(f"project {project.project_id}")
        for finding in project.findings:
            lines.append(f"  {_MARK[finding.state]}  {finding.name}: {finding.detail}")
    lines.append("")
    if report.ok:
        lines.append(
            "nothing blocks a start. Warnings and unknowns above are not passes: "
            "an unknown is a thing nobody has checked."
        )
    else:
        lines.append("BLOCKED. These would make the definition of done unreachable:")
        for project in report.projects:
            for finding in project.blockers:
                lines.append(f"  {project.project_id}: {finding.name} — {finding.detail}")
        for finding in report.environment:
            if finding.blocking and finding.state == FAIL:
                lines.append(f"  environment: {finding.name} — {finding.detail}")
    return "\n".join(lines)
