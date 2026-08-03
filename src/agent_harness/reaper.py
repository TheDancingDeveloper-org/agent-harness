"""Cleaning up sessions that were kept alive on purpose.

When an agent times out, the executor leaves its session running. That is
correct: the session holds the agent's context, and killing it destroys the
one thing that makes the item resumable by a human who comes back to it.

The half that was missing is that nothing ever came back. `kill_session` and
`delete_session` existed on the client and were never called, so survivors
accumulated with no cap and no count. Each one may still hold an agent
spending tokens, and after a week "preserved deliberately" and "leaked" look
identical from outside.

So: keep them, but own them. Every survivor is recorded, and one that nobody
has come back to within `max_age` is reaped. The default is deliberately
generous -- a human returning after lunch should still find their terminal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from .work import WorkQueue

log = logging.getLogger(__name__)

#: How long an abandoned session is kept before it is reaped. Long enough to
#: outlast a lunch break and a long meeting, short enough that a week of
#: unattended running does not accumulate hundreds.
DEFAULT_MAX_AGE_SECONDS = 6 * 3600.0


class Reapable(Protocol):
    """Only what reaping needs. The executor's `SessionHost` does not include
    these, because an executor has no business killing sessions."""

    def kill_session(self, session_id: str) -> None: ...

    def delete_session(self, session_id: str) -> None: ...


@dataclass
class ReapReport:
    reaped: list[str] = field(default_factory=list)
    kept: int = 0
    failed: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        parts = [f"reaped {len(self.reaped)}", f"kept {self.kept}"]
        if self.failed:
            parts.append(f"failed {len(self.failed)}")
        return ", ".join(parts)


def reap_abandoned_sessions(
    queue: WorkQueue,
    host: Reapable,
    *,
    max_age: float = DEFAULT_MAX_AGE_SECONDS,
    on_event: Any = None,
) -> ReapReport:
    """Kill and forget sessions nobody returned to.

    Failure to reap one session never stops the others: the host may have
    restarted, or a session may already be gone, and neither is a reason to
    leave the rest running.
    """
    report = ReapReport()
    all_open = queue.abandoned_sessions()
    due = queue.abandoned_sessions(older_than=max_age)
    report.kept = len(all_open) - len(due)

    for row in due:
        session_id = row["session_id"]
        try:
            host.kill_session(session_id)
            host.delete_session(session_id)
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the sweep
            report.failed[session_id] = str(exc)
            log.warning("could not reap session %s: %s", session_id, exc)
            # Deliberately still forgotten below only if it is gone; a session
            # we failed to kill stays on the list so the next sweep retries.
            continue
        queue.forget_abandoned_session(session_id)
        report.reaped.append(session_id)
        if on_event is not None:
            on_event(
                {
                    "kind": "work",
                    "outcome": "session_reaped",
                    "data": {
                        "session_id": session_id,
                        "item_id": row["item_id"],
                        "age_seconds": queue.now() - row["abandoned_at"],
                    },
                }
            )
    return report
