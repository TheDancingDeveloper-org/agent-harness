"""Worker pools, one per project.

A single pool serving every project is a shared queue with extra steps: the
project with the most items takes the most workers, and a small urgent project
waits behind a large slow one. That is precisely the co-mingling the project
scope exists to prevent, so the concurrency budget is a property of the
project rather than of the fleet.

Nothing here starts on its own. Boot sets every project to `stopped`, and a
pool is created only when someone asks for one — an auto-resuming fleet turns
a routine restart into unattended spend against a stack nobody has looked at
yet, and a crash-looping deploy would restart it on every loop.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .work import RUNNING, STOPPED, WorkQueue

log = logging.getLogger(__name__)

#: How long a worker waits before asking for work again when the queue is dry.
#: Short enough that a newly-synced plan starts within a few seconds; long
#: enough that an idle fleet is not a busy-wait against SQLite.
DEFAULT_POLL_SECONDS = 15.0

#: An executor factory: given a project id, build something with `.serve()`.
#: Injected so a pool can be tested without a git repository, a session host
#: or a provider.
ExecutorFactory = Callable[[str], Any]


@dataclass
class ProjectPool:
    """The workers running one project, and the switch that stops them."""

    project_id: str
    threads: list[threading.Thread] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)

    @property
    def size(self) -> int:
        return sum(1 for t in self.threads if t.is_alive())


class Fleet:
    """Starts and stops per-project worker pools.

    Deliberately not a scheduler over one shared pool. Sharing threads across
    projects reintroduces starvation through the back door: the fair thing to
    share is nothing.
    """

    def __init__(
        self,
        queue: WorkQueue,
        executor_factory: ExecutorFactory,
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.queue = queue
        self.executor_factory = executor_factory
        self.poll_seconds = poll_seconds
        self._pools: dict[str, ProjectPool] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ starting

    def start(self, project_id: str) -> int:
        """Start this project's workers. Returns how many are now running.

        Idempotent: starting a project that is already running is a no-op
        rather than a second pool. Two pools on one project would double its
        budget silently, which is worse than an error.
        """
        project = self.queue.get_project(project_id)
        if project is None:
            raise KeyError(f"no project {project_id!r}")

        with self._lock:
            existing = self._pools.get(project_id)
            if existing is not None and existing.size:
                return existing.size

            pool = ProjectPool(project_id=project_id)
            # Set control BEFORE the threads exist. A worker that starts while
            # the project still reads `stopped` claims nothing and sleeps a
            # full poll for no reason.
            self.queue.set_control(RUNNING, project_id=project_id)
            for n in range(max(1, project.max_workers)):
                thread = threading.Thread(
                    target=self._worker,
                    args=(project_id, pool.stop),
                    name=f"harness-{project_id}-{n}",
                    daemon=True,
                )
                pool.threads.append(thread)
                thread.start()
            self._pools[project_id] = pool
            log.info("started %d worker(s) for project %s", len(pool.threads), project_id)
            return len(pool.threads)

    def stop(self, project_id: str, *, reason: str | None = None, timeout: float = 30.0) -> None:
        """Stop claiming and wait for in-flight work to finish.

        **Nothing in flight is interrupted.** Killing an agent mid-item
        destroys the context that makes its work resumable and leaves a
        half-finished worktree; waiting for the current item is strictly
        better, which is why this joins rather than kills.
        """
        with self._lock:
            pool = self._pools.pop(project_id, None)
        self.queue.set_control(STOPPED, reason=reason, project_id=project_id)
        if pool is None:
            return
        pool.stop.set()
        for thread in pool.threads:
            thread.join(timeout=timeout)
        log.info("stopped project %s", project_id)

    def stop_all(self, *, reason: str | None = None) -> None:
        for project_id in list(self._pools):
            self.stop(project_id, reason=reason)

    # ------------------------------------------------------------- state

    def running(self) -> dict[str, int]:
        """Live worker count per project. Counts threads that are actually
        alive, not threads that were started -- a pool whose workers died is
        the thing worth seeing."""
        with self._lock:
            return {pid: pool.size for pid, pool in self._pools.items() if pool.size}

    def is_running(self, project_id: str) -> bool:
        return bool(self.running().get(project_id))

    # ------------------------------------------------------------ internals

    def _worker(self, project_id: str, stop: threading.Event) -> None:
        try:
            executor = self.executor_factory(project_id)
        except Exception as exc:  # noqa: BLE001 - one project must not kill the fleet
            log.warning("could not build an executor for %s: %s", project_id, exc)
            return
        try:
            executor.serve(poll_seconds=self.poll_seconds, stop=stop)
        except Exception as exc:  # noqa: BLE001
            # A worker dying must not take its siblings or other projects with
            # it. Its claim is a lease, so whatever it held returns on its own.
            log.warning("worker for %s exited: %s", project_id, exc)
