"""Client for a session host: something that owns PTY sessions over HTTP.

A session host creates PTY sessions, keeps their scrollback, streams them to
every attached client, and tracks whether one is idle, running, waiting for
input, or errored. AIDevEnv (and its private ancestor MyDevEnv2) is the
reference implementation, and the wire format here matches it — but nothing
in the harness depends on that particular server, only on this shape.

So the harness does not run agents itself. It asks MyDevEnv2 to, and gets
back a session id. That id is the whole point: it is a **deep link**. An item
the harness is working can be opened as a terminal tab in the same UI, on any
device, with full scrollback — and an agent stuck on an approval prompt shows
up in the tab strip as `waiting-for-input` without anyone hunting for it.

The alternative — the harness spawning its own subprocesses — would produce
work you cannot see, cannot attach to, and cannot answer a prompt in.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

#: Activity states MyDevEnv2 reports. `waiting-for-input` is the one that
#: matters most: the agent has stopped to ask something, and treating it as
#: "finished" or "hung" are both wrong.
IDLE = "idle"
RUNNING = "running"
WAITING = "waiting-for-input"
ERRORED = "errored"


class SessionHostError(RuntimeError):
    pass


@dataclass
class Session:
    id: str
    name: str
    activity: str
    exit_code: int | None = None
    cwd: str = ""
    created_at: str = ""
    scrollback: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def finished(self) -> bool:
        """A session is finished when the process exited.

        Deliberately not "activity is idle": a CLI agent sitting at a prompt
        having printed its answer is idle and very much not finished, and an
        agent waiting for approval is idle-looking too. Only the exit code
        settles it.
        """
        return self.exit_code is not None

    @property
    def waiting_for_input(self) -> bool:
        return self.activity == WAITING

    def tab_url(self, base: str) -> str:
        """Where a human opens this session in the host's UI."""
        return f"{base.rstrip('/')}/t/{self.id}"


class SessionHost(Protocol):
    """What the executor actually needs from a session host.

    Narrower than the client on purpose: the executor does not care whether
    sessions come from MyDevEnv2, and depending on the concrete class would
    make it untestable without a server and unusable with anything else.
    """

    def create_session(
        self,
        name: str,
        command: Sequence[str],
        cwd: str,
        env: Mapping[str, str] | None = ...,
        scrollback_bytes: int | None = ...,
    ) -> Session: ...

    def wait_for_exit(
        self,
        session_id: str,
        *,
        timeout: float = ...,
        poll_seconds: float = ...,
        on_waiting: Callable[[Session], None] | None = ...,
    ) -> Session: ...


class HttpSessionHost:
    """Minimal client. Only the session endpoints the harness needs."""

    def __init__(
        self,
        base_url: str,
        token: str,
        opener: Callable[..., Any] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
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
            raise SessionHostError(f"{method} {path} -> {exc.code}: {detail}") from exc
        except OSError as exc:
            raise SessionHostError(f"{method} {path}: {exc}") from exc
        if not body:
            return None
        try:
            return json.loads(body)
        except ValueError as exc:
            raise SessionHostError(f"{method} {path}: response was not JSON") from exc

    # ------------------------------------------------------------ sessions

    def create_session(
        self,
        name: str,
        command: Sequence[str],
        cwd: str,
        env: Mapping[str, str] | None = None,
        scrollback_bytes: int | None = None,
    ) -> Session:
        """Start a PTY session. Returns as soon as it exists, not when it
        finishes — the caller decides how long to wait and why."""
        spec: dict[str, Any] = {"name": name, "command": list(command), "cwd": cwd}
        if env:
            # The wire format is a list of pairs, not an object.
            spec["env"] = [[k, v] for k, v in env.items()]
        if scrollback_bytes:
            spec["scrollback_bytes"] = scrollback_bytes
        return _session_from(self._request("POST", "/api/sessions", spec))

    def get_session(self, session_id: str, with_scrollback: bool = False) -> Session:
        payload = self._request("GET", f"/api/sessions/{session_id}")
        summary = payload.get("summary", payload)
        session = _session_from(summary)
        if with_scrollback and payload.get("scrollback_base64"):
            session.scrollback = base64.b64decode(payload["scrollback_base64"]).decode(
                errors="replace"
            )
        return session

    def list_sessions(self) -> list[Session]:
        payload = self._request("GET", "/api/sessions") or []
        return [_session_from(item) for item in payload]

    def kill_session(self, session_id: str) -> None:
        self._request("POST", f"/api/sessions/{session_id}/kill")

    def delete_session(self, session_id: str) -> None:
        self._request("DELETE", f"/api/sessions/{session_id}")

    # ------------------------------------------------------------- waiting

    def wait_for_exit(
        self,
        session_id: str,
        *,
        timeout: float = 3600.0,
        poll_seconds: float = 5.0,
        on_waiting: Callable[[Session], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> Session:
        """Block until the session's process exits, or the timeout expires.

        `on_waiting` fires when the agent is asking for input. A prompt is
        NOT a failure and NOT completion — it is a human's turn. The caller
        gets told so it can notify, extend a lease, or give up deliberately
        rather than silently timing out on a question nobody saw.

        A timeout returns the session as it stands rather than raising: the
        caller has a claim to release and a partial result to record, and an
        exception here would discard both.
        """
        deadline = now() + timeout
        warned = False
        while True:
            session = self.get_session(session_id)
            if session.finished:
                return session
            if session.waiting_for_input and not warned and on_waiting:
                on_waiting(session)
                warned = True
            elif not session.waiting_for_input:
                warned = False
            if now() >= deadline:
                return session
            sleep(poll_seconds)


def _session_from(payload: Mapping[str, Any]) -> Session:
    return Session(
        id=str(payload.get("id", "")),
        name=str(payload.get("name", "")),
        activity=str(payload.get("activity", "")),
        exit_code=payload.get("exit_code"),
        cwd=str(payload.get("cwd", "")),
        created_at=str(payload.get("created_at", "")),
        raw=payload,
    )
