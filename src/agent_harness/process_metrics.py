"""Portable metrics for the process serving the control plane.

This is intentionally a sampler, not a process registry. It reports the
current harness process without consulting a session host, walking a process
tree, or assuming a platform-specific metrics filesystem exists.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProcessSample:
    """One observation of the process that owns the API application."""

    sampled_at: float
    started_at: float
    uptime_seconds: float
    pid: int
    thread_count: int
    cpu_seconds: float


class ProcessMetricsSource(Protocol):
    """Injected source contract used by API and browser read services."""

    def sample(self) -> ProcessSample:
        """Observe the current service process without mutating it."""


class ProcessMetricsSampler:
    """A stdlib-only, per-application process sampler.

    The start time is captured when the application is built, which is the
    boundary the API can state honestly. It is not a guess at an ancestor
    process's start time.
    """

    def __init__(
        self,
        *,
        wall_time: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        cpu_time: Callable[[], float] = time.process_time,
        pid: Callable[[], int] = os.getpid,
        thread_count: Callable[[], int] = threading.active_count,
    ) -> None:
        self._wall_time = wall_time
        self._monotonic = monotonic
        self._cpu_time = cpu_time
        self._pid = pid
        self._thread_count = thread_count
        self._started_at = wall_time()
        self._started_monotonic = monotonic()

    def sample(self) -> ProcessSample:
        """Return a portable snapshot; no session or filesystem access."""
        sampled_at = self._wall_time()
        return ProcessSample(
            sampled_at=sampled_at,
            started_at=self._started_at,
            uptime_seconds=max(0.0, self._monotonic() - self._started_monotonic),
            pid=self._pid(),
            thread_count=self._thread_count(),
            cpu_seconds=max(0.0, self._cpu_time()),
        )
