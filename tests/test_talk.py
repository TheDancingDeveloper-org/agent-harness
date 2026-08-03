"""The coordination plane over HTTP.

Two things are being tested here that the ledger's own tests cannot: that a
credential decides what a caller may say and see, and that the wire format
tells "recorded" apart from "lost".
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.api import create_api
from agent_harness.coordination import GENERAL_ROOM, MessageLedger, item_room
from agent_harness.identity import AGENT, OPERATOR, Scope, ScopedTokens
from agent_harness.store import EventStore

TOKEN = "test-token"  # noqa: S105 - a fixture, not a credential


@pytest.fixture
def ledger(tmp_path: Path) -> MessageLedger:
    return MessageLedger(tmp_path / "coordination.sqlite")


@pytest.fixture
def slept() -> list[float]:
    return []


@pytest.fixture
def client(tmp_path: Path, ledger: MessageLedger, slept: list[float]) -> Iterator[TestClient]:
    # The fake sleep advances the fake clock, because a deadline read from
    # the real clock while sleeping is faked is a busy loop that passes.
    clock = [1000.0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    app = create_api(
        EventStore(tmp_path / "e.sqlite"),
        token=TOKEN,
        ledger=ledger,
        sleep=sleep,
        now=lambda: clock[0],
    )
    with TestClient(app) as c:
        yield c


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def agent_token(project_id: str = "alpha", **kwargs: object) -> str:
    return ScopedTokens(TOKEN).issue(
        Scope(project_id=project_id, agent_id=str(kwargs.pop("agent_id", "worker-1")), **kwargs)  # type: ignore[arg-type]
    )


def body(**kwargs: object) -> dict[str, object]:
    return {"body": "hello", "idempotency_key": "k1", **kwargs}


# ------------------------------------------------------------------ saying


def test_a_sent_message_is_readable_and_permanent(client: TestClient) -> None:
    sent = client.post(f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth(), json=body())
    assert sent.status_code == 200, sent.text
    assert sent.json()["sequence"] == 1
    assert sent.json()["digest"]

    page = client.get(f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth()).json()
    assert [m["body"] for m in page["messages"]] == ["hello"]
    assert page["cursor"] == 1


def test_reading_resumes_from_the_cursor_it_returned(client: TestClient) -> None:
    for index in range(3):
        client.post(
            f"/api/talk/alpha/{GENERAL_ROOM}",
            headers=auth(),
            json=body(body=f"m{index}", idempotency_key=f"k{index}"),
        )
    first = client.get(f"/api/talk/alpha/{GENERAL_ROOM}?limit=2", headers=auth()).json()
    assert [m["body"] for m in first["messages"]] == ["m0", "m1"]

    rest = client.get(
        f"/api/talk/alpha/{GENERAL_ROOM}?after={first['cursor']}", headers=auth()
    ).json()
    assert [m["body"] for m in rest["messages"]] == ["m2"]


def test_an_empty_room_returns_the_cursor_it_was_given(client: TestClient) -> None:
    """A caller that polls and gets nothing must not rewind to the start."""
    page = client.get(f"/api/talk/alpha/{GENERAL_ROOM}?after=7", headers=auth()).json()
    assert page["messages"] == []
    assert page["cursor"] == 7


def test_a_long_poll_waits_rather_than_busy_looping(client: TestClient, slept: list[float]) -> None:
    page = client.get(f"/api/talk/alpha/{GENERAL_ROOM}?wait_seconds=1", headers=auth()).json()
    assert page["messages"] == []
    assert slept, "a waiting read must sleep between checks, not spin"
    assert sum(slept) <= 1.0 + 1e-6, "it must not wait longer than it was asked to"


def test_a_long_poll_returns_immediately_when_there_is_something(
    client: TestClient, slept: list[float]
) -> None:
    client.post(f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth(), json=body())
    page = client.get(f"/api/talk/alpha/{GENERAL_ROOM}?wait_seconds=30", headers=auth()).json()
    assert [m["body"] for m in page["messages"]] == ["hello"]
    assert slept == []


# ------------------------------------------------------------- credentials


def test_the_sender_comes_from_the_credential_not_the_request(client: TestClient) -> None:
    """An agent cannot post as somebody else, however it is prompted."""
    sent = client.post(
        f"/api/talk/alpha/{GENERAL_ROOM}",
        headers=auth(agent_token(agent_id="worker-7")),
        json=body(sender_id="oversight", sender_role="operator"),
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["sender_id"] == "worker-7"
    assert sent.json()["sender_role"] == AGENT


def test_a_scoped_credential_cannot_reach_another_project(client: TestClient) -> None:
    scoped = auth(agent_token("alpha"))
    assert client.get(f"/api/talk/alpha/{GENERAL_ROOM}", headers=scoped).status_code == 200
    # 404 rather than 403: whether `beta` exists is itself something this
    # credential has no business learning.
    assert client.get(f"/api/talk/beta/{GENERAL_ROOM}", headers=scoped).status_code == 404
    assert (
        client.post(f"/api/talk/beta/{GENERAL_ROOM}", headers=scoped, json=body()).status_code
        == 404
    )


def test_a_scoped_credential_stamps_its_own_item_and_attempt(client: TestClient) -> None:
    """The scope is not decorative: a body cannot claim to be about other work."""
    scoped = auth(agent_token("alpha", item_id="T7", attempt=2))
    sent = client.post(
        f"/api/talk/alpha/{item_room('T7')}",
        headers=scoped,
        json=body(item_id="SOMETHING-ELSE", attempt=99),
    ).json()
    assert sent["item_id"] == "T7"
    assert sent["attempt"] == 2


def test_an_expired_credential_is_refused(client: TestClient) -> None:
    expired = ScopedTokens(TOKEN, now=lambda: 1000.0).issue(
        Scope(project_id="alpha", agent_id="worker-1"), ttl_seconds=1
    )
    response = client.get(f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth(expired))
    assert response.status_code == 401
    assert "expired" in response.json()["detail"]


def test_a_forged_credential_is_refused(client: TestClient) -> None:
    forged = ScopedTokens("not-the-service-secret").issue(
        Scope(project_id="alpha", agent_id="worker-1", role=OPERATOR)
    )
    assert client.get(f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth(forged)).status_code == 401


def test_only_an_operator_may_issue_credentials(client: TestClient) -> None:
    """An agent that could mint its own scope could mint another project's."""
    request = {"project_id": "alpha", "agent_id": "worker-1", "item_id": "T1", "attempt": 1}
    refused = client.post("/api/talk/tokens", headers=auth(agent_token()), json=request)
    assert refused.status_code == 403

    issued = client.post("/api/talk/tokens", headers=auth(), json=request)
    assert issued.status_code == 200, issued.text
    assert issued.json()["expires_at"] > 0
    # The issued credential works, and only where it was issued for.
    granted = auth(issued.json()["token"])
    assert client.get(f"/api/talk/alpha/{GENERAL_ROOM}", headers=granted).status_code == 200
    assert client.get(f"/api/talk/beta/{GENERAL_ROOM}", headers=granted).status_code == 404


def test_an_anonymous_caller_is_refused_everywhere(client: TestClient) -> None:
    assert client.get(f"/api/talk/alpha/{GENERAL_ROOM}").status_code == 401
    assert client.post(f"/api/talk/alpha/{GENERAL_ROOM}", json=body()).status_code == 401
    assert client.post("/api/talk/tokens", json={}).status_code == 401


# ---------------------------------------------------------------- refusals


def test_a_reused_key_with_different_content_is_a_conflict(client: TestClient) -> None:
    client.post(f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth(), json=body())
    replay = client.post(f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth(), json=body())
    assert replay.status_code == 200, "an exact replay is harmless"

    conflict = client.post(
        f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth(), json=body(body="different")
    )
    assert conflict.status_code == 409


def test_an_unknown_message_type_is_refused(client: TestClient) -> None:
    response = client.post(
        f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth(), json=body(message_type="vibes")
    )
    assert response.status_code == 422


def test_something_that_looks_like_a_credential_is_refused(client: TestClient) -> None:
    """The ledger is permanent, so a posted secret cannot be unposted."""
    response = client.post(
        f"/api/talk/alpha/{GENERAL_ROOM}",
        headers=auth(),
        json=body(body="key is AKIAIOSFODNN7EXAMPLE"),
    )
    assert response.status_code == 422
    assert "AKIAIOSFODNN7EXAMPLE" not in response.text


def test_an_unavailable_ledger_is_never_reported_as_accepted(
    tmp_path: Path, ledger: MessageLedger
) -> None:
    class Broken:
        def append(self, _submission: object) -> None:
            from agent_harness.coordination import LedgerUnavailable

            raise LedgerUnavailable("disk is gone")

    app = create_api(EventStore(tmp_path / "e.sqlite"), token=TOKEN, ledger=Broken())  # type: ignore[arg-type]
    with TestClient(app) as c:
        response = c.post(f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth(), json=body())
    assert response.status_code == 503, "a lost message must not answer 200"


def test_no_ledger_attached_says_so_rather_than_looking_quiet(tmp_path: Path) -> None:
    """ "The room is empty" and "there is no ledger" must not be the same answer
    to an agent that is waiting for one."""
    with TestClient(create_api(EventStore(tmp_path / "e.sqlite"), token=TOKEN)) as c:
        assert c.get(f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth()).status_code == 409


# ------------------------------------------------------------ restrictions


def test_only_an_operator_may_restrict_and_the_record_survives(
    client: TestClient, ledger: MessageLedger
) -> None:
    sent = client.post(
        f"/api/talk/alpha/{GENERAL_ROOM}",
        headers=auth(),
        json=body(body="an internal hostname"),
    ).json()

    refused = client.post(
        f"/api/talk/alpha/{sent['message_id']}/restrict",
        headers=auth(agent_token()),
        json={"audience": ["operator"], "reason": "leaks a hostname"},
    )
    assert refused.status_code == 403, "an agent must not be able to hide its own messages"

    restricted = client.post(
        f"/api/talk/alpha/{sent['message_id']}/restrict",
        headers=auth(),
        json={"audience": ["operator"], "reason": "leaks a hostname"},
    )
    assert restricted.status_code == 200, restricted.text

    hidden = client.get(f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth(agent_token())).json()
    assert "hostname" not in hidden["messages"][0]["body"]
    assert hidden["messages"][0]["restricted"] is True
    # Hiding is not deleting: the record is untouched and still verifies.
    assert hidden["messages"][0]["digest"] == sent["digest"]
    assert ledger.verify("alpha", GENERAL_ROOM) is True

    visible = client.get(f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth()).json()
    assert visible["messages"][0]["body"] == "an internal hostname"


def test_restricting_a_message_from_another_project_is_a_404(client: TestClient) -> None:
    sent = client.post(f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth(), json=body()).json()
    response = client.post(
        f"/api/talk/beta/{sent['message_id']}/restrict",
        headers=auth(),
        json={"audience": ["operator"], "reason": "x"},
    )
    assert response.status_code == 404


def test_an_empty_audience_is_refused(client: TestClient) -> None:
    """A restriction nobody can see through is a deletion by another name."""
    sent = client.post(f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth(), json=body()).json()
    response = client.post(
        f"/api/talk/alpha/{sent['message_id']}/restrict",
        headers=auth(),
        json={"audience": [], "reason": "x"},
    )
    assert response.status_code == 422


# ----------------------------------------------------------------- rooms


def test_rooms_are_listed_per_project(client: TestClient) -> None:
    client.post(f"/api/talk/alpha/{GENERAL_ROOM}", headers=auth(), json=body(idempotency_key="a"))
    client.post(
        f"/api/talk/alpha/{item_room('T1')}", headers=auth(), json=body(idempotency_key="b")
    )
    client.post(f"/api/talk/beta/{item_room('T2')}", headers=auth(), json=body(idempotency_key="c"))

    assert client.get("/api/talk/alpha/rooms", headers=auth()).json()["rooms"] == [
        GENERAL_ROOM,
        item_room("T1"),
    ]
    assert client.get("/api/talk/beta/rooms", headers=auth()).json()["rooms"] == [item_room("T2")]


# ---------------------------------------------------------------- schemas


def test_every_talk_route_names_a_described_response_model(client: TestClient) -> None:
    """AGENTS.md: the schema IS the documentation. A route returning a bare
    dict documents `{}`, which is useless to anyone generating a client."""
    schema = client.get("/openapi.json").json()
    talk = {p: v for p, v in schema["paths"].items() if p.startswith("/api/talk")}
    assert talk, "no talk routes are documented"
    for path, methods in talk.items():
        for method, operation in methods.items():
            content = operation["responses"]["200"].get("content", {})
            ref = content.get("application/json", {}).get("schema", {})
            assert ref, f"{method.upper()} {path} documents no response schema"

    message = schema["components"]["schemas"]["Message"]
    undocumented = [
        name
        for name, spec in message["properties"].items()
        if not spec.get("description") and not spec.get("allOf") and name not in {"payload"}
    ]
    assert undocumented == [], f"Message fields with no description: {undocumented}"
