"""A lease on a file a long model call is going to write.

`run` cannot have two workers on one item: a claim is a lease, it expires on
its own, and a worker killed mid-item releases it by doing nothing. The
single-call commands that *produce* the plan `run` then executes had no
equivalent, so two of them could be pointed at one `--out` and neither would
notice. Whichever finished second overwrote the other, and the operator had no
way to tell which proposal they were reading.

Observed: a surveyor appeared to be killed by its caller's timeout, was
relaunched, and both processes ran for ten minutes against the same output —
each billed in full, one destined to be discarded silently.

The same rule as the queue's, for the same reason: **the marker expires.** A
lock held by a process that died is a lock nobody can release, and needing a
human to delete a stale file is precisely the unattended-operation failure the
lease design exists to reject. Staleness is decided by time, not by asking the
operating system whether a pid is alive — a pid is reused, and a claim written
on another host says nothing about this one.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

#: How long a claim is honoured without being refreshed. Long enough for a
#: slow model call on a degraded gateway, short enough that a crash does not
#: block the next attempt for an appreciable part of a working day.
DEFAULT_TTL_SECONDS = 30 * 60


class OutputBusy(Exception):
    """Another live claim holds this output."""


@dataclass
class Claim:
    """Who holds an output, and until when."""

    pid: int
    host: str
    started: float
    expires: float

    @classmethod
    def read(cls, path: Path) -> Claim | None:
        """The claim beside `path`, or None if there is none we can read.

        An unreadable or malformed claim is treated as absent rather than as a
        blocker: a corrupt marker must not be able to wedge an output forever,
        which would be the stale-lock failure wearing a different hat.
        """
        try:
            raw = json.loads(path.read_text())
            return cls(
                pid=int(raw["pid"]),
                host=str(raw.get("host", "")),
                started=float(raw["started"]),
                expires=float(raw["expires"]),
            )
        except Exception:
            return None

    def live(self, now: float) -> bool:
        return self.expires > now

    def describe(self, now: float) -> str:
        age = max(0.0, now - self.started)
        return (
            f"pid {self.pid} on {self.host or 'an unnamed host'} "
            f"claimed it {age / 60:.0f} min ago; "
            f"the claim expires in {max(0.0, self.expires - now) / 60:.0f} min"
        )


def claim_path(out: Path) -> Path:
    return out.with_name(out.name + ".claim")


@contextlib.contextmanager
def claiming(
    out: Path | None,
    *,
    ttl: float = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> Iterator[None]:
    """Hold a lease on `out` for the duration of the block.

    A `None` output means nothing durable is being written, so there is
    nothing to claim and nothing to collide over.

    Raises `OutputBusy` if a live claim already exists. Releases on the way
    out, including on failure — and if it cannot, the expiry does it.
    """
    if out is None:
        yield
        return

    stamp = time.time() if now is None else now
    marker = claim_path(out)
    held = Claim.read(marker)
    if held is not None and held.live(stamp):
        raise OutputBusy(
            f"{out} is already being written: {held.describe(stamp)}. "
            f"Wait for it, point --out somewhere else, or delete {marker} "
            f"if you know that process is gone."
        )

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": os.uname().nodename,
                "started": stamp,
                "expires": stamp + ttl,
            }
        )
    )
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            marker.unlink()
