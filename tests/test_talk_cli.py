"""The agent-side protocol, end to end.

Driven through a real `TestClient`-backed opener rather than a mock: the
things most likely to be wrong are the URL shapes, the credential header and
what an agent does when nobody answers — none of which a stub can tell you.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_harness.__main__ import _talk, build_parser
from agent_harness.api import create_api
from agent_harness.coordination import GENERAL_ROOM, MessageLedger, item_room
from agent_harness.identity import Scope, ScopedTokens
from agent_harness.store import EventStore
from agent_harness.talk import TalkClient, TalkError

TOKEN = "test-token"  # noqa: S105 - a fixture, not a credential


class _Response(io.BytesIO):
    """Just enough of an HTTP response for `urlopen`'s contract."""

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def opener_for(client: TestClient):  # type: ignore[no-untyped-def]
    """Route urllib requests into the in-process app. No ports, no server."""

    def opener(request: urllib.request.Request, timeout: float = 0) -> _Response:
        response = client.request(
            request.get_method(),
            request.full_url.replace("http://harness", ""),
            content=request.data,
            headers=dict(request.header_items()),
        )
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                response.status_code,
                response.text,
                {},
                io.BytesIO(response.content),  # type: ignore[arg-type]
            )
        return _Response(response.content)

    return opener


@pytest.fixture
def ledger(tmp_path: Path) -> MessageLedger:
    return MessageLedger(tmp_path / "coordination.sqlite")


@pytest.fixture
def client(tmp_path: Path, ledger: MessageLedger) -> Iterator[TestClient]:
    app = create_api(EventStore(tmp_path / "e.sqlite"), token=TOKEN, ledger=ledger)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def agent(client: TestClient) -> TalkClient:
    token = ScopedTokens(TOKEN).issue(
        Scope(project_id="alpha", agent_id="worker-1", item_id="T7", attempt=1)
    )
    return TalkClient("http://harness", token, project_id="alpha", opener=opener_for(client))


# --------------------------------------------------------------- the client


def test_an_agent_can_report_a_missing_dependency_and_be_heard(
    agent: TalkClient, ledger: MessageLedger
) -> None:
    """The scenario the whole plane exists for. Before this an agent could
    only stop and hope somebody read the transcript."""
    sent = agent.send(
        "T7 needs the schema task, which is not in the queue",
        message_type="dependency_found",
        room_id=item_room("T7"),
    )
    assert sent["message_type"] == "dependency_found"
    assert sent["item_id"] == "T7", "the credential stamps the item, so this is attributable"

    stored = ledger.read("alpha", item_room("T7"))
    assert [m.body for m in stored] == ["T7 needs the schema task, which is not in the queue"]
    assert stored[0].sender_id == "worker-1"


def test_a_send_without_a_key_still_gets_a_distinct_message(agent: TalkClient) -> None:
    """An agent that never thinks about idempotency must not collide with
    itself and silently lose its second observation."""
    first = agent.send("one", room_id=GENERAL_ROOM)
    second = agent.send("two", room_id=GENERAL_ROOM)
    assert first["message_id"] != second["message_id"]
    assert (first["sequence"], second["sequence"]) == (1, 2)


def test_reading_resumes_from_the_cursor(agent: TalkClient) -> None:
    agent.send("one", room_id=GENERAL_ROOM)
    page = agent.read(room_id=GENERAL_ROOM)
    agent.send("two", room_id=GENERAL_ROOM)
    later = agent.read(room_id=GENERAL_ROOM, after=page["cursor"])
    assert [m["body"] for m in later["messages"]] == ["two"]


def test_ask_returns_the_reply_that_answers_it(
    agent: TalkClient, client: TestClient, ledger: MessageLedger
) -> None:
    asked = agent.send("Which component owns this format?", message_type="question")
    client.post(
        f"/api/talk/alpha/{GENERAL_ROOM}",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "message_type": "answer",
            "body": "the importer does",
            "idempotency_key": "answer-1",
            "reply_to": asked["message_id"],
        },
    )
    page = agent.read(after=asked["sequence"])
    answers = [m for m in page["messages"] if m["reply_to"] == asked["message_id"]]
    assert [m["body"] for m in answers] == ["the importer does"]


def test_ask_returns_none_when_nobody_answers(agent: TalkClient) -> None:
    """None is a real outcome. An agent that cannot get an answer should
    report that, not invent one."""
    assert agent.ask("anyone there?", wait_seconds=0) is None


def test_acknowledging_appends_a_receipt_rather_than_mutating(
    agent: TalkClient, ledger: MessageLedger
) -> None:
    sent = agent.send("read this", room_id=GENERAL_ROOM)
    receipt = agent.acknowledge(sent["message_id"], room_id=GENERAL_ROOM)

    assert receipt["message_type"] == "delivery_receipt"
    assert receipt["reply_to"] == sent["message_id"]
    # The original is untouched, and the room still verifies.
    stored = ledger.read("alpha", GENERAL_ROOM)
    assert stored[0].digest == sent["digest"]
    assert ledger.verify("alpha", GENERAL_ROOM) is True
    # Acknowledging twice is the same receipt, not two.
    assert (
        agent.acknowledge(sent["message_id"], room_id=GENERAL_ROOM)["message_id"]
        == (receipt["message_id"])
    )


def test_a_refusal_is_raised_not_swallowed(agent: TalkClient) -> None:
    with pytest.raises(TalkError, match="422"):
        agent.send("anything", message_type="vibes")


def test_a_client_without_a_credential_refuses_to_be_built() -> None:
    """Failing here beats failing on the first request with a 401 an agent
    will read as "the plane is down"."""
    with pytest.raises(TalkError, match="credential"):
        TalkClient("http://harness", "", project_id="alpha")
    with pytest.raises(TalkError, match="URL"):
        TalkClient("", "t", project_id="alpha")
    with pytest.raises(TalkError, match="project"):
        TalkClient("http://harness", "t", project_id="")


# ------------------------------------------------------------------- CLI


def run_cli(client: TestClient, monkeypatch: pytest.MonkeyPatch, *argv: str) -> tuple[int, Any]:
    token = ScopedTokens(TOKEN).issue(Scope(project_id="alpha", agent_id="worker-1"))
    monkeypatch.setenv("HARNESS_URL", "http://harness")
    monkeypatch.setenv("HARNESS_TALK_TOKEN", token)
    monkeypatch.setenv("HARNESS_PROJECT", "alpha")
    parsed = _parse(["talk", *argv])
    return _talk(parsed, opener=opener_for(client)), parsed


def _parse(argv: list[str]) -> Any:
    return build_parser().parse_args(argv)


def test_the_cli_sends_and_prints_json(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON on stdout, because the caller is usually an agent and a format it
    has to parse loosely is one it will parse wrongly."""
    code, _ = run_cli(
        client,
        monkeypatch,
        "send",
        "--message",
        "the schema task is absent",
        "--type",
        "dependency_found",
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["body"] == "the schema task is absent"
    assert printed["message_type"] == "dependency_found"
    assert printed["sender_id"] == "worker-1"


def test_the_cli_reads_a_room(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_cli(client, monkeypatch, "send", "--message", "one")
    capsys.readouterr()
    code, _ = run_cli(client, monkeypatch, "read")
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert [m["body"] for m in printed["messages"]] == ["one"]


def test_the_cli_exits_two_when_nobody_answers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "Nobody answered" and "here is the answer" must not be the same exit
    code to whatever is driving the agent."""
    code, _ = run_cli(client, monkeypatch, "ask", "--message", "anyone?", "--wait", "0")
    assert code == 2
    assert "no answer" in capsys.readouterr().err


def test_the_cli_reports_a_refusal_on_stderr(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run_cli(client, monkeypatch, "send", "--message", "x", "--type", "vibes")
    assert code == 1
    assert "422" in capsys.readouterr().err


def test_the_cli_says_what_is_missing_rather_than_failing_obscurely(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("HARNESS_TALK_TOKEN", raising=False)
    monkeypatch.setenv("HARNESS_URL", "http://harness")
    monkeypatch.setenv("HARNESS_PROJECT", "alpha")
    parsed = _parse(["talk", "send", "--message", "x"])
    assert _talk(parsed, opener=opener_for(client)) == 1
    assert "HARNESS_TALK_TOKEN" in capsys.readouterr().err
