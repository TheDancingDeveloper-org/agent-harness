"""Session-host client tests. HTTP is faked at the opener; no server, no network."""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from agent_harness.session_host import (
    IDLE,
    RUNNING,
    WAITING,
    HttpSessionHost,
    Session,
    SessionHostError,
)


class FakeHTTP:
    """Replays canned responses and records what was sent."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: Any, timeout: float = 0) -> Any:
        self.requests.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "body": json.loads(request.data) if request.data else None,
            }
        )
        if not self.responses:
            raise AssertionError("more requests than scripted")
        payload = self.responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        body = json.dumps(payload).encode() if payload is not None else b""

        class Ctx:
            def __enter__(self) -> Any:
                return io.BytesIO(body)

            def __exit__(self, *_a: object) -> None:
                return None

        return Ctx()


def client(http: FakeHTTP) -> HttpSessionHost:
    return HttpSessionHost("https://dev.example/", token="tok", opener=http)


SUMMARY = {"id": "abc", "name": "W1", "activity": RUNNING, "cwd": "/w"}


def test_creating_a_session_sends_the_spec_the_server_expects() -> None:
    http = FakeHTTP(SUMMARY)
    session = client(http).create_session("W1", ["claude", "-p", "x.md"], "/w", env={"A": "1"})
    sent = http.requests[0]
    assert sent["method"] == "POST"
    assert sent["url"] == "https://dev.example/api/sessions"
    assert sent["body"]["command"] == ["claude", "-p", "x.md"]
    assert sent["body"]["cwd"] == "/w"
    # env is a list of PAIRS on the wire, not an object -- an easy thing to
    # get wrong and a confusing 422 when you do.
    assert sent["body"]["env"] == [["A", "1"]]
    assert session.id == "abc"


def test_the_bearer_token_is_sent() -> None:
    http = FakeHTTP(SUMMARY)
    client(http).create_session("W1", ["true"], "/w")
    assert http.requests[0]["headers"]["authorization"] == "Bearer tok"


def test_get_session_unwraps_the_detail_envelope() -> None:
    http = FakeHTTP({"summary": {**SUMMARY, "exit_code": 0}, "scrollback_pos": 10})
    session = client(http).get_session("abc")
    assert session.exit_code == 0


def test_scrollback_is_decoded_only_when_asked_for() -> None:
    import base64

    payload = {"summary": SUMMARY, "scrollback_base64": base64.b64encode(b"hello\n").decode()}
    assert client(FakeHTTP(payload)).get_session("abc").scrollback == ""
    assert (
        client(FakeHTTP(payload)).get_session("abc", with_scrollback=True).scrollback == "hello\n"
    )


def test_a_session_is_finished_only_when_the_process_exited() -> None:
    """Not when it looks idle: a CLI agent that printed its answer and is
    sitting at a prompt is idle and very much not finished."""
    assert not Session("a", "n", IDLE).finished
    assert not Session("a", "n", WAITING).finished
    assert Session("a", "n", IDLE, exit_code=0).finished
    assert Session("a", "n", IDLE, exit_code=1).finished


def test_wait_returns_as_soon_as_the_process_exits() -> None:
    http = FakeHTTP(
        {"summary": SUMMARY},
        {"summary": {**SUMMARY, "activity": IDLE, "exit_code": 0}},
    )
    slept: list[float] = []
    session = client(http).wait_for_exit("abc", poll_seconds=7, sleep=slept.append, now=lambda: 0.0)
    assert session.exit_code == 0
    assert slept == [7]


def test_waiting_for_input_notifies_once_not_every_poll() -> None:
    """A prompt should page a human once. Re-notifying every five seconds
    trains people to ignore the notification."""
    http = FakeHTTP(
        {"summary": {**SUMMARY, "activity": WAITING}},
        {"summary": {**SUMMARY, "activity": WAITING}},
        {"summary": {**SUMMARY, "activity": IDLE, "exit_code": 0}},
    )
    seen: list[Session] = []
    client(http).wait_for_exit(
        "abc", on_waiting=seen.append, sleep=lambda _s: None, now=lambda: 0.0
    )
    assert len(seen) == 1


def test_a_new_prompt_after_answering_notifies_again() -> None:
    http = FakeHTTP(
        {"summary": {**SUMMARY, "activity": WAITING}},
        {"summary": {**SUMMARY, "activity": RUNNING}},  # answered
        {"summary": {**SUMMARY, "activity": WAITING}},  # asks again
        {"summary": {**SUMMARY, "activity": IDLE, "exit_code": 0}},
    )
    seen: list[Session] = []
    client(http).wait_for_exit(
        "abc", on_waiting=seen.append, sleep=lambda _s: None, now=lambda: 0.0
    )
    assert len(seen) == 2


def test_a_timeout_returns_the_session_rather_than_raising() -> None:
    """The caller has a claim to release and a partial result to record; an
    exception here would discard both."""
    http = FakeHTTP({"summary": SUMMARY})
    clock = iter([0.0, 999.0, 999.0])
    session = client(http).wait_for_exit(
        "abc", timeout=10, sleep=lambda _s: None, now=lambda: next(clock)
    )
    assert not session.finished
    assert session.activity == RUNNING


def test_an_http_error_carries_the_server_message() -> None:
    error = urllib.error.HTTPError(
        "https://dev.example/api/sessions",
        401,
        "Unauthorized",
        None,  # type: ignore[arg-type]
        io.BytesIO(b'{"error":"bad token"}'),
    )
    with pytest.raises(SessionHostError, match="bad token"):
        client(FakeHTTP(error)).create_session("W1", ["true"], "/w")


def test_a_connection_failure_is_wrapped_not_leaked() -> None:
    with pytest.raises(SessionHostError):
        client(FakeHTTP(OSError("connection refused"))).list_sessions()


def test_the_tab_url_is_where_a_human_attaches() -> None:
    assert Session("s1", "n", IDLE).tab_url("https://dev.example/") == "https://dev.example/t/s1"
