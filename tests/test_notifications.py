from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import cast
from urllib.request import Request

import pytest

from agent_harness.notifications import Notification, NotificationOutbox, WebhookChannel


class RecordingChannel:
    def __init__(self) -> None:
        self.fail = True
        self.received: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.received.append(notification)
        if self.fail:
            raise OSError("receiver unavailable")


def test_outbox_deduplicates_retries_and_survives_reopen(tmp_path: Path) -> None:
    now = [100.0]
    channel = RecordingChannel()
    path = tmp_path / "notifications.sqlite"
    outbox = NotificationOutbox(path, channel, retry_seconds=2, clock=lambda: now[0])

    assert outbox.enqueue_event({"kind": "work", "outcome": "hold_opened", "item_id": "T1"})
    assert not outbox.enqueue_event({"kind": "work", "outcome": "hold_opened", "item_id": "T1"})
    assert outbox.deliver_due(now=now[0]) == 0
    first = outbox.rows()[0]
    assert first.state == "pending"
    assert first.attempts == 1
    assert first.last_error == "receiver unavailable"

    outbox.close()
    channel.fail = False
    reopened = NotificationOutbox(path, channel, retry_seconds=2, clock=lambda: now[0])
    assert reopened.deliver_due(now=now[0]) == 0
    now[0] = 102.0
    assert reopened.deliver_due(now=now[0]) == 1
    assert reopened.rows()[0].state == "delivered"
    assert len(channel.received) == 2
    reopened.close()


def test_outbox_only_alerts_on_explicit_outcomes(tmp_path: Path) -> None:
    outbox = NotificationOutbox(tmp_path / "notifications.sqlite")

    assert not outbox.enqueue_event({"kind": "work", "outcome": "tool_step"})
    assert outbox.enqueue_event({"kind": "work", "outcome": "done", "item_id": "T1"})
    assert outbox.rows()[0].payload["item_id"] == "T1"
    outbox.close()


def test_authenticated_webhook_supports_bearer_and_hmac(tmp_path: Path) -> None:
    calls: list[tuple[Request, bytes]] = []

    def send(request: Request, timeout: float) -> None:
        del timeout
        body = cast(bytes, request.data or b"")
        calls.append((request, body))

    channel = WebhookChannel(
        "https://notifications.invalid/inbox",
        bearer_token="bearer-secret",
        hmac_secret="hmac-secret",
        send=send,
    )
    notification = Notification(
        notification_id=4,
        dedupe_key="dedupe",
        kind="work",
        payload={"outcome": "done", "detail": "safe"},
        created_at=1.0,
        attempts=1,
        state="inflight",
        next_attempt_at=1.0,
        lease_until=2.0,
        last_error=None,
    )

    channel.send(notification)

    assert len(calls) == 1
    request, body = calls[0]
    assert request.get_header("Authorization") == "Bearer bearer-secret"
    expected = hmac.new(b"hmac-secret", body, hashlib.sha256).hexdigest()
    assert request.get_header("X-harness-signature") == f"sha256={expected}"
    assert b"bearer-secret" not in body
    assert json.loads(body)["payload"]["detail"] == "safe"


def test_webhook_requires_authentication() -> None:
    with pytest.raises(ValueError, match="requires a bearer token"):
        WebhookChannel("https://notifications.invalid/inbox")
