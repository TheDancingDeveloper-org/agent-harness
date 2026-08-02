"""Keeping the audit store bounded, on a timer.

`rollup()` and `thin()` are correct and, on their own, useless: a method
nobody calls is the same defect as the session reaper that existed on the
client and was never invoked. This is the thing that calls them.

Deliberately a background thread inside `serve` rather than a cron entry.
Retention that depends on an external scheduler is retention that silently
stops when someone forgets to install it, and the symptom -- a database that
grows -- takes months to notice and cannot be repaired retroactively once the
disk fills and writes start failing.

Maintenance failing must never take the API down with it. Everything here is
best-effort and logged; a run that throws is a warning and a retry next tick.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .audit import AuditStore

log = logging.getLogger(__name__)

#: How often maintenance runs. Rollups only close whole days, so anything
#: under a day is about promptness after a restart rather than throughput --
#: an hour means a fresh deployment has its first rollup within an hour
#: instead of at some arbitrary point tomorrow.
DEFAULT_INTERVAL_SECONDS = 3600.0

#: How long raw events are kept once a rollup covers them. Long enough to
#: debug a specific bad week from the raw record; short enough that a year of
#: running does not carry a year of high-cardinality rows.
DEFAULT_RETENTION_DAYS = 90


#: How often merge and revert outcomes are pulled from GitHub. Far less often
#: than rollups: a merged pull request stays merged, and a revert next week is
#: still a revert when found tomorrow. Hammering the API to learn nothing is
#: how a token gets rate limited for no benefit.
DEFAULT_RECONCILE_EVERY = 6


@dataclass
class MaintenanceReport:
    rolled_up: int = 0
    thinned: int = 0
    reconciled: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        parts = [f"rolled up {self.rolled_up}", f"thinned {self.thinned}"]
        if self.reconciled:
            parts.append("reconciled " + ", ".join(f"{k}={v}" for k, v in self.reconciled.items()))
        if self.errors:
            parts.append(f"errors {len(self.errors)}")
        return ", ".join(parts)


def reconcile_projects(audit: AuditStore, queue: Any) -> tuple[dict[str, int], list[str]]:
    """Pull GitHub outcomes for every project that names a repository.

    Per project, because a repo is a project's property -- reconciling one
    repo for a fleet running three would attribute other projects' pull
    requests to nothing, or worse, to the wrong item.
    """
    from .reconcile import GitHubReconciler, items_by_pr

    counts: dict[str, int] = {}
    errors: list[str] = []
    try:
        projects = queue.projects()
    except Exception as exc:  # noqa: BLE001
        return counts, [f"projects: {exc}"]

    mapping = items_by_pr(queue)
    for project in projects:
        if not project.repo:
            continue
        scoped = {
            pr: attribution
            for pr, attribution in mapping.items()
            if attribution.get("project_id") == project.project_id
        }
        try:
            report = GitHubReconciler(project.repo, audit).reconcile(scoped)
        except Exception as exc:  # noqa: BLE001 - one repo must not stop the others
            errors.append(f"reconcile {project.repo}: {exc}")
            continue
        errors.extend(report.errors)
        recorded = report.merged + report.closed_unmerged + report.reverted
        if recorded:
            counts[project.project_id] = recorded
    return counts, errors


def run_maintenance(
    audit: AuditStore,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    queue: Any = None,
) -> MaintenanceReport:
    """One maintenance pass: aggregate, then thin what is now covered.

    The order is load-bearing and is the whole discipline of the retention
    design -- thinning first would remove raw rows that no aggregate has
    replaced, leaving a hole in the series that nothing reports.
    """
    report = MaintenanceReport()
    if audit.degraded:
        return report
    try:
        report.rolled_up = audit.rollup()
    except Exception as exc:  # noqa: BLE001 - maintenance must not take the API down
        report.errors.append(f"rollup: {exc}")
        log.warning("audit maintenance: rollup failed: %s", exc)
        # Deliberately no thin: without a fresh rollup, thinning could remove
        # events whose day was never aggregated.
        return report
    try:
        report.thinned = audit.thin(older_than_days=retention_days)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"thin: {exc}")
        log.warning("audit maintenance: thin failed: %s", exc)
    if queue is not None:
        counts, errors = reconcile_projects(audit, queue)
        report.reconciled = counts
        report.errors.extend(errors)
    if report.rolled_up or report.thinned or report.reconciled:
        log.info("audit maintenance: %s", report)
    return report


class MaintenanceLoop:
    """Runs `run_maintenance` on an interval until stopped.

    A daemon thread, so it can never keep the process alive after the server
    has gone: a harness that will not exit because a housekeeping thread is
    mid-sleep is worse than one that skips a rollup.
    """

    def __init__(
        self,
        audit: AuditStore,
        *,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        queue: Any = None,
        reconcile_every: int = DEFAULT_RECONCILE_EVERY,
        on_pass: Callable[[MaintenanceReport], Any] | None = None,
    ) -> None:
        self.audit = audit
        self.interval = interval
        self.retention_days = retention_days
        self.queue = queue
        self.reconcile_every = max(1, reconcile_every)
        self.on_pass = on_pass
        self._passes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None or self.audit.degraded:
            # A degraded store has nothing to maintain, and starting a thread
            # to discover that once an hour is noise.
            return
        self._thread = threading.Thread(target=self._run, name="audit-maintenance", daemon=True)
        self._thread.start()
        log.info(
            "audit maintenance every %.0fs, retaining raw events %d days",
            self.interval,
            self.retention_days,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        # Runs once immediately: after a restart the first useful thing to
        # know is whether yesterday closed, not to wait an hour to find out.
        while True:
            # Reconciliation runs on a slower cadence than rollups: it costs a
            # GitHub API call per project and the answers rarely change.
            due = self._passes % self.reconcile_every == 0
            self._passes += 1
            report = run_maintenance(
                self.audit,
                retention_days=self.retention_days,
                queue=self.queue if due else None,
            )
            if self.on_pass is not None:
                with_suppressed_errors(self.on_pass, report)
            if self._stop.wait(self.interval):
                return


def with_suppressed_errors(fn: Callable[..., Any], *args: Any) -> None:
    """A callback must not be able to kill the loop that invokes it."""
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001
        log.warning("audit maintenance: callback failed: %s", exc)
