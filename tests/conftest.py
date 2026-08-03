"""Shared test helpers."""

from __future__ import annotations

from typing import Any

import pytest

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


def pytest_configure(config: pytest.Config) -> None:
    """Fail legibly when the TestClient's HTTP dependency is wrong.

    Starlette's TestClient needs a specific HTTP client, and which one changed:
    older releases wanted `httpx`, current ones want `httpx2`. When the
    installed pair does not match, every API test explodes during *fixture
    setup* -- before a single endpoint is exercised -- with an error that
    names httpx rather than the mismatch.

    A suite that fails on its plumbing proves nothing about the code, and the
    error points at the wrong thing. Better to say so once, plainly, than to
    let sixty tests fail for a reason none of them are about.
    """
    try:
        from fastapi.testclient import TestClient  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise pytest.UsageError(
            "The Starlette TestClient could not be imported, which means the "
            f"HTTP client dependency does not match this Starlette: {exc}\n"
            "This project pins `httpx2` in its dev extra. Run `uv sync --all-extras`."
        ) from exc
