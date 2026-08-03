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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .work import CLAIMED, DRAINING, FAILED, RUNNING, STOPPED, WorkQueue

log = logging.getLogger(__name__)

#: How long a worker waits before asking for work again when the queue is dry.
#: Short enough that a newly-synced plan starts within a few seconds; long
#: enough that an idle fleet is not a busy-wait against SQLite.
DEFAULT_POLL_SECONDS = 15.0

#: An executor factory: given a project id, build something with `.serve()`.
#: Injected so a pool can be tested without a git repository, a session host
#: or a provider.
ExecutorFactory = Callable[[str], Any]


@dataclass(frozen=True)
class WorkerFailure:
    """A worker that stopped without being asked to.

    Kept because a fleet whose workers are dying looks, from the outside,
    exactly like a fleet with nothing to do: both report no work in progress.
    """

    project_id: str
    worker: str | None
    error: str
    at: float
    released: tuple[str, ...] = ()


@dataclass(eq=False)
class Worker:
    """One worker thread and the switch that retires it on its own.

    Per worker rather than per pool because shrinking a pool has to stop
    *some* of it: with one shared event the only available answers were "keep
    every worker" and "stop the project", which is why changing `max_workers`
    used to need a stop/start cycle.
    """

    thread: threading.Thread
    stop: threading.Event


@dataclass
class ProjectPool:
    """The workers running one project, and the switch that stops them."""

    project_id: str
    workers: list[Worker] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)
    draining: bool = False
    #: Workers ever started, so a resized pool cannot name two threads alike.
    launched: int = 0

    @property
    def threads(self) -> list[threading.Thread]:
        return [worker.thread for worker in self.workers]

    @property
    def size(self) -> int:
        return sum(1 for worker in self.workers if worker.thread.is_alive())

    @property
    def wanted(self) -> int:
        """Workers not on their way out — the size the pool is aiming for.

        Distinct from `size`, which counts threads that are still alive: a
        worker retired by a resize stays alive until its in-flight item
        reaches a boundary, and reporting it as wanted would make a second
        resize retire a worker that is already leaving.
        """
        return sum(1 for worker in self.workers if not worker.stop.is_set())

    def halt(self) -> None:
        """Stop the whole pool, including anything a resize was retiring."""
        self.stop.set()
        for worker in self.workers:
            worker.stop.set()


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
        on_event: Callable[[dict[str, Any]], None] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.queue = queue
        self.executor_factory = executor_factory
        self.poll_seconds = poll_seconds
        self.on_event = on_event
        self.now = now
        self._pools: dict[str, ProjectPool] = {}
        self._failures: list[WorkerFailure] = []
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
            if existing is not None:
                if existing.draining:
                    # The pool remains registered until its background join
                    # completes. Starting a replacement in that small window
                    # would let the old finalizer set the new pool's control
                    # state to STOPPED underneath it.
                    return existing.size
                if existing.size:
                    return existing.size

            pool = ProjectPool(project_id=project_id)
            # Before anything new is dispatched. A worker killed with its
            # lease still running leaves an item `claimed` by a pid that no
            # longer exists -- unavailable to healthy workers, with no session
            # and nothing saying why, until the lease times out. Reclaiming it
            # here is safe precisely because the process is provably gone.
            for item_id in self.queue.reclaim_dead_workers(project_id=project_id):
                log.info("reclaimed %s before starting project %s", item_id, project_id)
            # Set control BEFORE the threads exist. A worker that starts while
            # the project still reads `stopped` claims nothing and sleeps a
            # full poll for no reason.
            self.queue.set_control(RUNNING, project_id=project_id)
            for _ in range(max(1, project.max_workers)):
                pool.workers.append(self._spawn(pool))
            self._pools[project_id] = pool
            log.info("started %d worker(s) for project %s", len(pool.workers), project_id)
            return len(pool.workers)

    def resize(self, project_id: str, size: int | None = None) -> int:
        """Change a running project's worker count in place.

        Returns how many workers are alive when the call returns, which after
        a shrink is still the old count: retired workers are asked to stop and
        then joined, never interrupted, so they stay alive until their
        in-flight item reaches a boundary. `running()` is the number that
        falls; `max_workers` is what was asked for.

        The alternative was a stop/start cycle for every capacity change, and
        stopping a project to give it *more* workers means tearing down agents
        that are mid-item for no reason — avoidable lifecycle risk in exchange
        for a number that could have been applied live.

        `size` defaults to the project's persisted `max_workers`, so
        reconciling after an update is one call with nothing to pass.
        Serialised with every other pool mutation by the fleet lock, and
        idempotent: the delta is computed from the workers not already
        leaving, so resizing to the size it already is does nothing.
        """
        project = self.queue.get_project(project_id)
        if project is None:
            raise KeyError(f"no project {project_id!r}")
        # Zero workers while `running` is the false-running state the start
        # preflight refuses to create; stopping a project is `stop`'s job.
        target = max(1, project.max_workers if size is None else size)

        failures: list[str] = []
        retired: list[Worker] = []
        with self._lock:
            pool = self._pools.get(project_id)
            if pool is None:
                # Nothing to resize. The new budget is persisted and applies
                # at the next start, which is what a stopped project needs.
                return 0
            if pool.draining or pool.stop.is_set():
                # A pool on its way out must not have workers added underneath
                # it: the finalizer joining it has already been handed the list
                # of threads to wait for, so a new one would outlive the stop.
                return pool.size
            # Threads that have already exited are not capacity.
            pool.workers = [worker for worker in pool.workers if worker.thread.is_alive()]
            shortfall = target - pool.wanted
            for _ in range(max(0, shortfall)):
                try:
                    pool.workers.append(self._spawn(pool))
                except RuntimeError as exc:  # the OS refused another thread
                    # Reported after the lock: a sibling that started fine is
                    # working, and must not be torn down over this.
                    failures.append(f"could not start a worker: {exc}")
            # Newest first, so growing and then shrinking again leaves the
            # long-lived workers in place rather than churning the pool.
            for worker in reversed(pool.workers):
                if len(retired) >= -shortfall:
                    break
                if not worker.stop.is_set():
                    worker.stop.set()
                    retired.append(worker)
            live = pool.size

        if retired:
            threading.Thread(
                target=self._join_retired,
                args=(project_id, pool, retired),
                name=f"harness-resize-{project_id}",
                daemon=True,
            ).start()
        for message in failures:
            self._died(project_id, None, message)
        log.info("resized project %s to %d worker(s)", project_id, target)
        return live

    def _spawn(self, pool: ProjectPool) -> Worker:
        """Start one worker with a stop switch of its own."""
        pool.launched += 1
        stop = threading.Event()
        thread = threading.Thread(
            target=self._worker,
            args=(pool.project_id, stop),
            name=f"harness-{pool.project_id}-{pool.launched}",
            daemon=True,
        )
        thread.start()
        return Worker(thread=thread, stop=stop)

    def _join_retired(self, project_id: str, pool: ProjectPool, retired: list[Worker]) -> None:
        """Wait for shrunk-away workers off the caller's thread.

        The join lasts as long as whatever item they are in the middle of, and
        a resize that blocked for it would look like a hang to an HTTP caller
        — while killing them instead would destroy the context that makes an
        agent's work resumable, which is the same reason `stop` joins.
        """
        for worker in retired:
            worker.thread.join()
        leaving = {id(worker) for worker in retired}
        with self._lock:
            if self._pools.get(project_id) is pool:
                pool.workers = [w for w in pool.workers if id(w) not in leaving]
        log.info("retired %d worker(s) from project %s", len(retired), project_id)

    def stop(
        self, project_id: str, *, reason: str | None = None, timeout: float | None = None
    ) -> None:
        """Stop claiming and wait for in-flight work to finish.

        **Nothing in flight is interrupted.** Killing an agent mid-item
        destroys the context that makes its work resumable and leaves a
        half-finished worktree; waiting for the current item is strictly
        better, which is why this joins rather than kills.
        """
        with self._lock:
            pool = self._pools.get(project_id)
        if pool is None:
            self.queue.set_control(STOPPED, reason=reason, project_id=project_id)
            return
        self.queue.set_control(DRAINING, reason=reason, project_id=project_id)
        # Under the lock, so a resize cannot append a worker between the halt
        # and the join and leave one thread running after a completed stop.
        with self._lock:
            pool.halt()
            threads = pool.threads
        for thread in threads:
            thread.join(timeout=timeout)
        with self._lock:
            if self._pools.get(project_id) is pool:
                self._pools.pop(project_id, None)
        self.queue.set_control(STOPPED, reason=reason, project_id=project_id)
        log.info("stopped project %s", project_id)

    def request_stop(self, project_id: str, *, reason: str | None = None) -> None:
        """Cease claims now and finish the blocking join in the background."""
        with self._lock:
            pool = self._pools.get(project_id)
            if pool is None:
                self.queue.set_control(STOPPED, reason=reason, project_id=project_id)
                return
            if pool.draining:
                return
            pool.draining = True
        # State first: workers consult it before every claim, so this is the
        # point after which the HTTP caller can truthfully be told no new work
        # will start.
        self.queue.set_control(DRAINING, reason=reason, project_id=project_id)
        with self._lock:
            pool.halt()
        threading.Thread(
            target=self._finish_stop,
            args=(project_id, pool, reason),
            name=f"harness-drain-{project_id}",
            daemon=True,
        ).start()

    def _finish_stop(self, project_id: str, pool: ProjectPool, reason: str | None) -> None:
        for thread in pool.threads:
            thread.join()
        with self._lock:
            if self._pools.get(project_id) is pool:
                self._pools.pop(project_id, None)
        self.queue.set_control(STOPPED, reason=reason, project_id=project_id)
        log.info("drained project %s", project_id)

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

    def failures(self, project_id: str | None = None) -> list[WorkerFailure]:
        """Workers that died, oldest first."""
        with self._lock:
            return [f for f in self._failures if project_id is None or f.project_id == project_id]

    # ------------------------------------------------------------ internals

    def _worker(self, project_id: str, stop: threading.Event) -> None:
        try:
            executor = self.executor_factory(project_id)
        except Exception as exc:  # noqa: BLE001 - one project must not kill the fleet
            self._died(project_id, None, f"could not build an executor: {exc}")
            return
        try:
            executor.serve(poll_seconds=self.poll_seconds, stop=stop)
        except Exception as exc:  # noqa: BLE001
            # A worker dying must not take its siblings or other projects with
            # it -- but it must not leave its work behind either. "The claim is
            # a lease, so it returns on its own" was true and insufficient: the
            # item stayed `claimed` by a dead owner for a full lease with no
            # completion or failure recorded, so the fleet looked busy and the
            # item was unavailable to everyone including a human.
            self._died(project_id, executor, f"worker exited: {exc}")

    def _died(self, project_id: str, executor: Any, message: str) -> None:
        """Record a worker's death, release what it was holding, and stop the
        project if that was the last of its workers."""
        log.warning("worker for %s: %s", project_id, message)
        owner = getattr(executor, "owner", None)
        released = self._release_claims(project_id, owner, message)
        failure = WorkerFailure(
            project_id=project_id,
            worker=owner,
            error=message,
            at=self.now(),
            released=tuple(released),
        )
        with self._lock:
            self._failures.append(failure)
        self._emit(
            {
                "ts": failure.at,
                "kind": "work",
                "worker": owner,
                "item_id": released[0] if released else None,
                "outcome": "worker_died",
                "detail": message + (f"; released {', '.join(released)}" if released else ""),
                "project_id": project_id,
            }
        )
        self._stop_if_last(project_id, message)

    def _release_claims(self, project_id: str, owner: str | None, message: str) -> list[str]:
        """Hand back whatever the dead worker was holding.

        Released as FAILED rather than re-queued on purpose: the item that
        killed a worker is the likeliest item to kill the next one, and a
        silent requeue turns that into a crash loop that spends money. Retry
        is one call away, and now it is a decision someone makes.
        """
        if owner is None:
            return []
        released = []
        for record in self.queue.items(project_id=project_id):
            if record.state != CLAIMED or record.owner != owner:
                continue
            if self.queue.release(
                record.item_id,
                FAILED,
                error=f"the worker holding this item died: {message}",
                owner=owner,
                project_id=project_id,
            ):
                released.append(record.item_id)
        return released

    def _stop_if_last(self, project_id: str, message: str) -> None:
        """A project whose workers have all died is not running.

        Leaving it `running` with zero workers is the exact state the start
        preflight exists to refuse -- reached the slow way, after the fact.
        """
        with self._lock:
            pool = self._pools.get(project_id)
            if pool is None:
                return
            current = threading.current_thread()
            if any(t.is_alive() and t is not current for t in pool.threads):
                return
            self._pools.pop(project_id, None)
        self.queue.set_control(
            STOPPED,
            reason=f"every worker for {project_id} died: {message}",
            project_id=project_id,
        )

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        # Telemetry is never load-bearing: a broken sink must not stop the
        # release above from having happened.
        try:
            self.on_event(event)
        except Exception:  # noqa: BLE001
            log.warning("fleet event sink failed", exc_info=True)
