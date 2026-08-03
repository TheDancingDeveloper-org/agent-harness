"""One app's wiring must not be another's.

`create_api` used to copy its fleet, model client, session host and probes
into a module-level dictionary, so a second app in the same process silently
took over the first's readiness gate. These tests build two apps deliberately
and assert each answers for itself.

Also here: the two projections that were keyed on an item id alone, and the
routes that answered with an untyped dictionary.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.api import create_api
from agent_harness.audit import AuditStore
from agent_harness.events import WORK, Event
from agent_harness.store import EventStore
from agent_harness.work import PENDING, Project, WorkQueue, WorkRecord


def hdr() -> dict[str, str]:
    return {"Authorization": "Bearer tok"}


def queue_with(path: Path, project_id: str, *item_ids: str) -> WorkQueue:
    queue = WorkQueue(str(path))
    queue.add_project(Project(project_id=project_id, name=project_id, work_dir="/tmp", repo="o/r"))
    queue.add(
        [WorkRecord(item_id=i, title=f"do {i}", brief="b") for i in item_ids],
        project_id=project_id,
    )
    return queue


class Fleet:
    def running(self) -> dict[str, int]:
        return {}


def test_one_apps_readiness_is_not_answered_by_another(tmp_path: Path) -> None:
    """The regression for #106.

    App A has no fleet and healthy probes. App B has a fleet and a failing
    checkout probe. A must not report B's.
    """
    store = EventStore(str(tmp_path / "e.sqlite"))
    queue_a = queue_with(tmp_path / "a.sqlite", "p", "T1")
    queue_b = queue_with(tmp_path / "b.sqlite", "p", "T1")

    app_a = create_api(
        store,
        queue=queue_a,
        token="tok",  # noqa: S106
        fleet=None,
        probes={
            "git_probe": lambda _p: (True, "fine"),
            "github_probe": lambda _r: (True, "fine"),
        },
    )
    # Built second, deliberately: this is the one that used to win.
    create_api(
        store,
        queue=queue_b,
        token="tok",  # noqa: S106
        fleet=Fleet(),
        probes={
            "git_probe": lambda _p: (False, "bad checkout from the other app"),
            "github_probe": lambda _r: (False, "bad github from the other app"),
        },
    )

    with TestClient(app_a) as c:
        body = c.get("/api/projects/p/preflight", headers=hdr()).json()

    details = " ".join(check["detail"] for check in body["checks"])
    assert "the other app" not in details, "app A answered with app B's probes"
    # And A's own truth: no fleet means it cannot start anything.
    workers = next(c for c in body["checks"] if c["name"] == "workers")
    assert workers["ok"] is False


def test_two_projects_with_the_same_item_id_do_not_share_a_latest_event(
    tmp_path: Path,
) -> None:
    """The regression for #109. `(project_id, item_id)` is the identity."""
    store = EventStore(str(tmp_path / "e.sqlite"))
    audit = AuditStore(tmp_path / "audit.sqlite")
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    for project_id in ("a", "b"):
        queue.add_project(Project(project_id=project_id, name=project_id))
        queue.add([WorkRecord(item_id="T1", title="do T1", brief="b")], project_id=project_id)

    audit.append(
        [
            Event(
                ts=1000.0,
                kind=WORK,
                source="serve",
                outcome="checks_failed",
                data={"item_id": "T1", "project_id": "a", "session_id": "sess-a"},
            ),
            Event(
                ts=2000.0,
                kind=WORK,
                source="serve",
                outcome="done",
                data={"item_id": "T1", "project_id": "b", "session_id": "sess-b"},
            ),
        ]
    )

    app = create_api(store, queue=queue, token="tok", audit=audit)  # noqa: S106
    with TestClient(app) as c:
        a = c.get("/api/work/T1?project_id=a", headers=hdr()).json()
        b = c.get("/api/work/T1?project_id=b", headers=hdr()).json()

    assert a["latest"]["outcome"] == "checks_failed"
    assert b["latest"]["outcome"] == "done"
    # The deep link is the dangerous half: it would put a human in the wrong
    # project's terminal.
    assert a["latest"]["session_id"] == "sess-a"
    assert b["latest"]["session_id"] == "sess-b"


def test_an_exhausted_item_can_still_be_listed_and_inspected(tmp_path: Path) -> None:
    """The regression for #108.

    `exhausted` is the state that most needs an operator to look at it, and
    response validation used to reject the value, so both routes 500'd.
    """
    store = EventStore(str(tmp_path / "e.sqlite"))
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.add_project(Project(project_id="p", name="P", max_attempts=1))
    queue.set_control("running", project_id="p")
    queue.add([WorkRecord(item_id="T1", title="do T1", brief="b")], project_id="p")
    assert queue.claim("w", project_id="p") is not None
    queue.release("T1", PENDING, error="checks failed", project_id="p")
    assert queue.claim("w", project_id="p") is None  # retires it
    record = queue.get("T1", project_id="p")
    assert record is not None and record.state == "exhausted"

    app = create_api(store, queue=queue, token="tok")  # noqa: S106
    with TestClient(app) as c:
        one = c.get("/api/work/T1?project_id=p", headers=hdr())
        listing = c.get("/api/work?project_id=p", headers=hdr())

    assert one.status_code == 200, one.text
    assert listing.status_code == 200, listing.text
    assert one.json()["state"] == "exhausted"
    assert [i["state"] for i in listing.json()["items"]] == ["exhausted"]


@pytest.mark.parametrize(
    ("route", "field"),
    [("/api/inception", "state"), ("/api/inception/{p}/plan", "markdown")],
)
def test_inception_routes_publish_a_real_schema(tmp_path: Path, route: str, field: str) -> None:
    """The regression for #111: `additionalProperties: true` documents nothing."""
    store = EventStore(str(tmp_path / "e.sqlite"))
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    app = create_api(store, queue=queue, token="tok")  # noqa: S106

    with TestClient(app) as c:
        schema = c.get("/openapi.json").json()

    path = route.replace("{p}", "{project_id}")
    method = "post" if path == "/api/inception" else "get"
    content = schema["paths"][path][method]["responses"]["200"]["content"]
    ref = content["application/json"]["schema"].get("$ref")
    assert ref, f"{path} still answers with an unnamed schema"
    model = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    assert field in model["properties"]
    assert model["properties"][field].get("description"), f"{field} has no description"
