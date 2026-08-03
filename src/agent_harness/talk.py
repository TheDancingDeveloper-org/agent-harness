"""The agent-side protocol: how a worker speaks to the coordination plane.

An agent running in a terminal session needs to be able to say "this item
depends on something that does not exist" and get an answer. Before this it
could only stop and hope somebody read the transcript.

The client is deliberately thin and deliberately HTTP. It holds a scoped
credential and nothing else -- no database handle, no GitHub token, no
knowledge of where the ledger lives. An agent that could open the ledger
directly could write another project's rooms, and an agent that could reach
GitHub could publish unreviewed work; both are exactly what the plane exists
to prevent.

Two conventions the executor relies on, and which the CLI reads from the
environment so a prompt never has to carry a credential:

    HARNESS_URL         base URL of the harness API
    HARNESS_TALK_TOKEN  the scoped credential issued for this attempt
    HARNESS_PROJECT     project the agent is working in
    HARNESS_ROOM        room to use by default, usually `item:<id>`

Messages are retrieved, never injected. Nothing here writes to a terminal:
text delivered into a live PTY can land at a shell or an approval prompt,
where an answer becomes a command.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any


class TalkError(RuntimeError):
    """The plane refused, or could not be reached. Never a silent failure."""


class TalkClient:
    """Minimal client for the `talk` routes."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        project_id: str,
        opener: Callable[..., Any] | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not base_url:
            raise TalkError("no harness URL: set HARNESS_URL or pass --url")
        if not token:
            raise TalkError("no credential: set HARNESS_TALK_TOKEN or pass --token")
        if not project_id:
            raise TalkError("no project: set HARNESS_PROJECT or pass --project")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.project_id = project_id
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "content-type": "application/json",
            },
        )
        try:
            with self._open(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise TalkError(f"{method} {path} -> {exc.code}: {detail}") from exc
        except OSError as exc:
            raise TalkError(f"{method} {path}: {exc}") from exc
        return json.loads(body) if body else None

    def _room(self, room_id: str | None) -> str:
        room = room_id or os.environ.get("HARNESS_ROOM") or "general"
        return urllib.parse.quote(room, safe="")

    def send(
        self,
        body: str,
        *,
        message_type: str = "observation",
        room_id: str | None = None,
        idempotency_key: str | None = None,
        recipients: Sequence[str] = (),
        reply_to: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Say something. Returns only once the record is durable.

        The key defaults to a fresh one, so an agent that does not think
        about idempotency still gets a distinct message rather than a
        collision. An agent retrying a specific send passes its own.
        """
        project = urllib.parse.quote(self.project_id, safe="")
        result = self._request(
            "POST",
            f"/api/talk/{project}/{self._room(room_id)}",
            {
                "message_type": message_type,
                "body": body,
                "idempotency_key": idempotency_key or uuid.uuid4().hex,
                "recipients": list(recipients),
                "payload": dict(payload or {}),
                "reply_to": reply_to,
            },
        )
        return dict(result)

    def read(
        self,
        *,
        room_id: str | None = None,
        after: int = 0,
        limit: int = 200,
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        project = urllib.parse.quote(self.project_id, safe="")
        query = urllib.parse.urlencode(
            {"after": after, "limit": limit, "wait_seconds": wait_seconds}
        )
        return dict(self._request("GET", f"/api/talk/{project}/{self._room(room_id)}?{query}"))

    def ask(
        self,
        question: str,
        *,
        room_id: str | None = None,
        wait_seconds: float = 0.0,
        recipients: Sequence[str] = ("oversight",),
    ) -> dict[str, Any] | None:
        """Ask, and optionally wait for the first reply to this question.

        Returns the answer, or None if nothing replied in time. None is a
        real outcome and says so: an agent that cannot get an answer should
        stop and report that, not invent one.
        """
        asked = self.send(question, message_type="question", room_id=room_id, recipients=recipients)
        if wait_seconds <= 0:
            return None
        cursor = int(asked["sequence"])
        remaining = wait_seconds
        while remaining > 0:
            page = self.read(room_id=room_id, after=cursor, wait_seconds=min(remaining, 30.0))
            for message in page["messages"]:
                if message.get("reply_to") == asked["message_id"]:
                    return dict(message)
            if page["cursor"] == cursor:
                # Nothing arrived within the poll; the deadline is what
                # elapsed, not what the server happened to return.
                remaining -= min(remaining, 30.0)
            cursor = page["cursor"]
        return None

    def acknowledge(self, message_id: str, *, room_id: str | None = None) -> dict[str, Any]:
        """Record that this message was received and read.

        A receipt is its own append. Reading a message never mutates it --
        there is no "seen" flag to set, because there is nothing to set it on.
        """
        return self.send(
            f"acknowledged {message_id}",
            message_type="delivery_receipt",
            room_id=room_id,
            reply_to=message_id,
            idempotency_key=f"ack:{message_id}",
        )


def client_from_environment(
    *,
    url: str | None = None,
    token: str | None = None,
    project_id: str | None = None,
    opener: Callable[..., Any] | None = None,
) -> TalkClient:
    """Build a client from flags, falling back to the agent's environment."""

    return TalkClient(
        url or os.environ.get("HARNESS_URL", ""),
        token or os.environ.get("HARNESS_TALK_TOKEN", ""),
        project_id=project_id or os.environ.get("HARNESS_PROJECT", ""),
        opener=opener,
    )
