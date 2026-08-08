"""Durable, generic delivery of harness notifications.

The event and audit stores are history. A notification has a different job:
it must remember a delivery that has not happened yet, retry it, and recover
after a process dies while a receiver is being called. The payload is
immutable; only delivery bookkeeping is mutable.

Nothing here knows what consumes a notice. ``WebhookChannel`` is one opt-in
authenticated channel, and the protocol makes other channels injectable
without adding their names to core.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .redaction import Redact, from_environment, redact_text

log = logging.getLogger(__name__)

DEFAULT_RETRY_SECONDS = 5.0
DEFAULT_MAX_RETRY_SECONDS = 15 * 60.0
DEFAULT_LEASE_SECONDS = 60.0

# Work events are intentionally a small, stable notification contract. A
# caller can still enqueue any explicit kind/payload; this set prevents a
# progress event for every tool step becoming an accidental alert stream.
NOTIFIABLE_OUTCOMES = frozenset(
    {
        "blocked",
        "blocked_by_policy",
        "checks_failed",
        "done",
        "failed",
        "hold_opened",
        "plan_promotion",
        "remote_review_received",
        "review_rejected",
        "worker_died",
    }
)


@dataclass(frozen=True)
class Notification:
    """One immutable notification and its current delivery metadata."""

    notification_id: int
    dedupe_key: str
    kind: str
    payload: dict[str, Any]
    created_at: float
    attempts: int
    state: str
    next_attempt_at: float
    lease_until: float
    last_error: str | None


class NotificationChannel(Protocol):
    """A destination that either accepts a notification or raises."""

    def send(self, notification: Notification) -> None: ...


Sender = Callable[[urllib.request.Request, float], None]


class WebhookChannel:
    """POST JSON to an authenticated, operator-supplied endpoint.

    At least one of ``bearer_token`` or ``hmac_secret`` is required. Secrets
    stay in process configuration and are never put in the outbox payload.
    ``send`` is injectable so delivery can be tested without a network call.
    """

    def __init__(
        self,
        url: str,
        *,
        bearer_token: str = "",
        hmac_secret: str = "",
        timeout: float = 5.0,
        send: Sender | None = None,
    ) -> None:
        if not url.strip():
            raise ValueError("notification webhook URL cannot be empty")
        if not bearer_token and not hmac_secret:
            raise ValueError("notification webhook requires a bearer token or HMAC secret")
        if timeout <= 0:
            raise ValueError("notification webhook timeout must be positive")
        self.url = url
        self.bearer_token = bearer_token
        self.hmac_secret = hmac_secret.encode()
        self.timeout = timeout
        self._send = send or self._post

    @staticmethod
    def _post(request: urllib.request.Request, timeout: float) -> None:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise OSError(f"notification endpoint returned HTTP {response.status}")

    def send(self, notification: Notification) -> None:
        body = json.dumps(
            {
                "notification_id": notification.notification_id,
                "dedupe_key": notification.dedupe_key,
                "kind": notification.kind,
                "created_at": notification.created_at,
                "payload": notification.payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        if self.bearer_token:
            request.add_header("Authorization", f"Bearer {self.bearer_token}")
        if self.hmac_secret:
            digest = hmac.new(self.hmac_secret, body, hashlib.sha256).hexdigest()
            request.add_header("X-Harness-Signature", f"sha256={digest}")
        self._send(request, self.timeout)


SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key      TEXT NOT NULL UNIQUE,
    created_at      REAL NOT NULL,
    kind           TEXT NOT NULL,
    payload        TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending',
    attempts       INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    lease_until    REAL NOT NULL DEFAULT 0,
    delivered_at   REAL,
    last_error     TEXT
);
CREATE INDEX IF NOT EXISTS notifications_due
    ON notifications (state, next_attempt_at, lease_until);
"""


class NotificationOutbox:
    """A durable queue whose delivery cannot affect the work path."""

    def __init__(
        self,
        path: Path | str,
        channel: NotificationChannel | None = None,
        *,
        redact: Redact | None = None,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
        max_retry_seconds: float = DEFAULT_MAX_RETRY_SECONDS,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if retry_seconds <= 0 or max_retry_seconds < retry_seconds or lease_seconds <= 0:
            raise ValueError("notification retry and lease durations are invalid")
        self.path = Path(path)
        self.channel = channel
        self.redact = redact if redact is not None else from_environment()
        self.retry_seconds = retry_seconds
        self.max_retry_seconds = max_retry_seconds
        self.lease_seconds = lease_seconds
        self.clock = clock
        self._local = threading.local()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connect().executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        self.stop()
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            self._local.conn = None

    def enqueue(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        dedupe_key: str | None = None,
    ) -> bool:
        """Persist one notification. Returns false when it is a duplicate."""
        clean = redact_text(dict(payload), self.redact)
        encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"))
        identity = dedupe_key or hashlib.sha256(f"{kind}\0{encoded}".encode()).hexdigest()
        now = self.clock()
        cursor = self._connect().execute(
            "INSERT OR IGNORE INTO notifications "
            "(dedupe_key, created_at, kind, payload, next_attempt_at) VALUES (?, ?, ?, ?, ?)",
            (identity, now, kind, encoded, now),
        )
        return bool(cursor.rowcount)

    def enqueue_event(self, event: Mapping[str, Any]) -> bool:
        """Queue selected work outcomes using the event's stable content."""
        outcome = str(event.get("outcome") or "")
        if outcome not in NOTIFIABLE_OUTCOMES:
            return False
        source = str(event.get("source") or "")
        remote_id = str(event.get("remote_id") or "")
        identity = f"event:{source}\0{remote_id}" if source and remote_id else None
        return self.enqueue(str(event.get("kind") or "work"), event, dedupe_key=identity)

    def hook(self) -> Callable[[dict[str, Any]], None]:
        """Return a hold/event-compatible durable notification hook."""

        def notify(payload: dict[str, Any]) -> None:
            self.enqueue_event(payload)

        return notify

    def _row(self, row: sqlite3.Row) -> Notification:
        return Notification(
            notification_id=int(row["notification_id"]),
            dedupe_key=str(row["dedupe_key"]),
            kind=str(row["kind"]),
            payload=dict(json.loads(str(row["payload"]))),
            created_at=float(row["created_at"]),
            attempts=int(row["attempts"]),
            state=str(row["state"]),
            next_attempt_at=float(row["next_attempt_at"]),
            lease_until=float(row["lease_until"]),
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        )

    def _claim(self, now: float) -> Notification | None:
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM notifications WHERE "
                "(state = 'pending' AND next_attempt_at <= ?) "
                "OR (state = 'inflight' AND lease_until <= ?) "
                "ORDER BY notification_id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            lease = now + self.lease_seconds
            conn.execute(
                "UPDATE notifications SET state = 'inflight', attempts = attempts + 1, "
                "lease_until = ?, last_error = NULL WHERE notification_id = ?",
                (lease, row["notification_id"]),
            )
            conn.execute("COMMIT")
            row = conn.execute(
                "SELECT * FROM notifications WHERE notification_id = ?",
                (row["notification_id"],),
            ).fetchone()
            assert row is not None
            return self._row(row)
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise

    def _ack(self, notification_id: int) -> None:
        self._connect().execute(
            "UPDATE notifications SET state = 'delivered', delivered_at = ?, lease_until = 0 "
            "WHERE notification_id = ? AND state = 'inflight'",
            (self.clock(), notification_id),
        )

    def _fail(self, notification: Notification, error: str) -> None:
        delay = min(
            self.max_retry_seconds,
            self.retry_seconds * (2 ** max(notification.attempts - 1, 0)),
        )
        self._connect().execute(
            "UPDATE notifications SET state = 'pending', next_attempt_at = ?, "
            "lease_until = 0, last_error = ? WHERE notification_id = ? AND state = 'inflight'",
            (self.clock() + delay, self.redact(error), notification.notification_id),
        )

    def deliver_due(self, *, limit: int = 100, now: float | None = None) -> int:
        """Attempt due rows and return the number accepted by the channel."""
        if self.channel is None:
            return 0
        delivered = 0
        for _ in range(limit):
            notification = self._claim(self.clock() if now is None else now)
            if notification is None:
                break
            try:
                self.channel.send(notification)
            except Exception as exc:  # noqa: BLE001 - retry is the contract
                self._fail(notification, str(exc))
                log.warning("notification %s delivery failed", notification.notification_id)
            else:
                self._ack(notification.notification_id)
                delivered += 1
        return delivered

    def start(self, *, poll_seconds: float = 1.0) -> None:
        """Start a small dispatcher; pending rows are recoverable on restart."""
        if self.channel is None or self._thread is not None:
            return
        self._stop.clear()

        def run() -> None:
            while not self._stop.is_set():
                self.deliver_due()
                self._stop.wait(poll_seconds)

        self._thread = threading.Thread(target=run, name="notification-dispatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join(timeout=max(self.lease_seconds, 1.0))
        self._thread = None

    def rows(self) -> list[Notification]:
        """Read delivery state for diagnostics and tests."""
        rows = (
            self._connect()
            .execute("SELECT * FROM notifications ORDER BY notification_id")
            .fetchall()
        )
        return [self._row(row) for row in rows]
