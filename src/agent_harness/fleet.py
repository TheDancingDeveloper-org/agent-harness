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

from .work import CLAIMED, FAILED, RUNNING, STOPPED, WorkQueue

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


@dataclass
class Worker:
    """One worker thread and the switch that stops **it**.

    Per worker rather than per pool, because that is what makes a pool
    shrinkable: telling three of five workers to finish and leave is not
    expressible with a single shared event, and stopping everything and
    restarting the survivors would interrupt in-flight items to change a
    number.
    """

    thread: threading.Thread
    stop: threading.Event

    @property
    def alive(self) -> bool:
        return self.thread.is_alive()


@dataclass
class ProjectPool:
    """The workers running one project."""

    project_id: str
    workers: list[Worker] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(1 for w in self.workers if w.alive)

    def live(self) -> list[Worker]:
        return [w for w in self.workers if w.alive]

    def stop_all(self) -> None:
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
            if existing is not None and existing.size:
                return existing.size

            pool = ProjectPool(project_id=project_id)
            # Set control BEFORE the threads exist. A worker that starts while
            # the project still reads `stopped` claims nothing and sleeps a
            # full poll for no reason.
            self.queue.set_control(RUNNING, project_id=project_id)
            self._pools[project_id] = pool
            started = self._add_workers(pool, max(1, project.max_workers))
            log.info("started %d worker(s) for project %s", started, project_id)
            return started

    def resize(self, project_id: str, size: int | None = None) -> int:
        """Change a running project's worker count in place. Returns the target.

        The target, not the live count, because during a shrink those differ
        and the live one is momentarily a lie: workers told to leave are still
        alive until their current item finishes. `running()` is the live
        answer; this is what the pool is converging to. Zero means the project
        was not running and nothing was started.

        Raising the limit starts only the additional workers. Lowering it
        tells the excess to stop claiming and leave once their current item
        finishes -- they are never joined here and never interrupted, because
        killing an agent mid-item destroys the context that makes its work
        resumable, and a capacity change is nowhere near a good enough reason.

        Serialized by the pool lock, so two concurrent resizes cannot both
        read the same count and both act on it. Idempotent: resizing to the
        number already running does nothing at all.

        A project that is not running is left alone -- the new limit applies
        the next time it starts, which is what `max_workers` already meant.
        """
        with self._lock:
            pool = self._pools.get(project_id)
            if pool is None:
                return 0
            if size is None:
                project = self.queue.get_project(project_id)
                if project is None:
                    raise KeyError(f"no project {project_id!r}")
                size = project.max_workers
            target = max(1, size)
            # Threads that have already exited do not count towards capacity,
            # and leaving them in the list would make a pool that lost workers
            # look full.
            pool.workers = pool.live()
            current = len(pool.workers)
            if target > current:
                self._add_workers(pool, target - current)
            elif target < current:
                # Newest first: the ones least likely to be deep into an item.
                for worker in pool.workers[target:]:
                    worker.stop.set()
                log.info(
                    "project %s shrinking from %d to %d worker(s); the excess will "
                    "finish their current item first",
                    project_id,
                    current,
                    target,
                )
            return target

    def _add_workers(self, pool: ProjectPool, count: int) -> int:
        """Start `count` more workers. A failed start does not disturb its siblings."""
        started = 0
        for n in range(count):
            stop = threading.Event()
            thread = threading.Thread(
                target=self._worker,
                args=(pool.project_id, stop),
                name=f"harness-{pool.project_id}-{len(pool.workers) + n}",
                daemon=True,
            )
            worker = Worker(thread=thread, stop=stop)
            pool.workers.append(worker)
            try:
                thread.start()
            except RuntimeError as exc:  # pragma: no cover - OS thread limits
                # Recorded rather than raised: one thread the OS would not
                # give us must not take down the workers that are running.
                pool.workers.remove(worker)
                self._died(pool.project_id, None, f"could not start a worker: {exc}")
                continue
            started += 1
        return started

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
        pool.stop_all()
        for worker in pool.workers:
            worker.thread.join(timeout=timeout)
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
            if any(w.alive and w.thread is not current for w in pool.workers):
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
