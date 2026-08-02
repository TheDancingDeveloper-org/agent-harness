"""Shared test helpers."""

from __future__ import annotations

from typing import Any

from agent_harness.work import RUNNING, WorkQueue


def make_queue(path: str, **kwargs: Any) -> WorkQueue:
    """A queue whose default project is running.

    A project starts `stopped` on purpose -- registering one must not begin
    spending money, and boot never resumes anything. Tests that exercise
    claiming therefore have to say so, and saying it here rather than in
    twenty places keeps the reason in one place.

    Tests *about* the stopped default use `WorkQueue` directly.
    """
    queue = WorkQueue(path, **kwargs)
    queue.set_control(RUNNING)
    return queue
