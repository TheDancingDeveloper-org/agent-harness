"""In-process journeys for the self-contained, read-only browser slice."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from agent_harness.api import create_api
from agent_harness.audit import AuditStore
from agent_harness.events import MODEL_CALL, WORK, Event
from agent_harness.github import GitHub, GitHubError
from agent_harness.maintenance import MaintenanceReport
from agent_harness.reconcile import ReconcileReport
from agent_harness.runtime import ExecutorRoles
from agent_harness.store import EventStore
from agent_harness.work import Project, WorkQueue, WorkRecord

TOKEN = "browser-test-token"  # noqa: S105 - fixture value

PROPOSAL = {
    "goal": "A safe browser plan.",
    "assumptions": ["The queue is durable"],
    "non_goals": ["Automatic execution"],
    "risks": ["Scope may change"],
    "phases": [{"id": "P0", "title": "Foundation", "items": []}],
    "open_questions": [
        {
            "id": "Q1",
            "question": "Which branch?",
            "severity": "blocking",
            "why_it_matters": "It changes the checkout.",
        }
    ],
}


class FakeScoper:
    def call(self, _role: str, _messages: list[dict[str, Any]]) -> str:
        return json.dumps(PROPOSAL)


class FakeGitHubRunner:
    """Offline GitHub transport that exposes preview writes in argv."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str:
        self.calls.append([*args, *([stdin] if stdin is not None else [])])
        if args[1:3] == ["issue", "list"]:
            return "[]"
        if args[1:3] == ["label", "list"]:
            return "[]"
        if args[1] == "api" and "milestones" in " ".join(args):
            return "[]"
        return "https://github.com/o/r/issues/1\n"


class AdoptionGitHubRunner(FakeGitHubRunner):
    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str:
        if args[1:3] in (["issue", "list"], ["pr", "list"]):
            self.calls.append([*args])
            return "[]"
        return super().__call__(args, stdin)


class StatefulGitHubRunner(FakeGitHubRunner):
    """A remote backlog that can drift or refuse the confirmed write."""

    def __init__(self) -> None:
        super().__init__()
        self.issues = "[]"
        self.fail_writes = False

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str:
        if args[1:3] == ["issue", "list"]:
            self.calls.append([*args])
            return self.issues
        if args[1:3] == ["pr", "list"]:
            self.calls.append([*args])
            return "[]"
        if self.fail_writes and args[1:3] in (["issue", "create"], ["issue", "edit"]):
            self.calls.append([*args])
            raise GitHubError("the remote rejected the write")
        return super().__call__(args, stdin)


def make_client(tmp_path: Path, *, token: str | None = TOKEN) -> TestClient:
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P"))
    queue.add(
        [WorkRecord(item_id="T1", title="First item", brief="A safe read path")],
        project_id="p",
    )
    return TestClient(create_api(store, queue=queue, token=token))


def make_rooted_client(tmp_path: Path) -> TestClient:
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P"))
    return TestClient(create_api(store, queue=queue, token=TOKEN, root_path="/harness"))


def login(client: TestClient) -> str:
    response = client.post("/login", data={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/projects")
    cookie = response.cookies.get("harness_session")
    assert cookie
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    return cookie


def test_root_and_pages_fail_closed_until_login(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        assert client.get("/projects").status_code == 401
        assert client.get("/work").status_code == 401
        assert client.get("/api/work").status_code == 401


def test_analytics_shows_session_independent_process_and_gateway_evidence(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    audit = AuditStore(tmp_path / "audit.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P"))
    audit.append(
        [
            Event(
                ts=3.0,
                kind=MODEL_CALL,
                source="serve",
                role="reviewer",
                model="model-a",
                endpoint="https://user:password123@gateway.example/v1?token=secret123",
                outcome="ok",
                latency_s=0.5,
                data={"project_id": "p", "detail": "completed"},
            )
        ]
    )

    class PoisonSessionHost:
        def __getattribute__(self, name: str) -> Any:
            raise AssertionError(f"analytics read session-host state: {name}")

    with TestClient(
        create_api(
            store,
            queue=queue,
            audit=audit,
            token=TOKEN,
            session_host=PoisonSessionHost(),
        )
    ) as client:
        login(client)
        response = client.get("/analytics")
    assert response.status_code == 200
    html = response.text
    assert "Service process" in html
    assert "monitoring-only" in html
    assert "Gateway calls" in html
    assert "model-a" in html
    assert "https://gateway.example/v1" in html
    assert "password123" not in html
    assert "secret123" not in html
    assert "/api/process" in html
    assert "/api/gateway-logs" in html


def test_adoption_wizard_keeps_inspection_and_reviews_non_mutating_then_applies_once(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "existing-repo"
    repository.mkdir()
    plan = repository / "PLAN.md"
    plan.write_text(
        "# Existing project\n\n"
        "- [x] T1: Already delivered\n\nbrief\n\n"
        "- [ ] T2: Still needed\n\nanother brief\n"
    )
    store = EventStore(tmp_path / "events.sqlite")
    audit = AuditStore(tmp_path / "audit.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(
        Project(
            project_id="existing",
            name="Existing project",
            repo="owner/repository",
            work_dir=str(repository),
            plan_path=str(plan),
            checks=["pytest -q"],
            max_workers=3,
        )
    )
    github_calls: list[str] = []

    def github_factory(repo: str) -> GitHub:
        github_calls.append(repo)
        return GitHub(repo, AdoptionGitHubRunner())

    with TestClient(
        create_api(
            store,
            queue=queue,
            audit=audit,
            token=TOKEN,
            github_factory=github_factory,
            adoption_branches=lambda _repo: [],
        )
    ) as client:
        login(client)
        page = client.get("/plans?project_id=existing")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        inspected = client.post(
            "/ui/actions/adoption/inspect",
            data={"csrf_token": csrf, "project_id": "existing", "inspect_remote": "yes"},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert inspected.status_code == 303
        assert queue.items(project_id="existing") == []
        assert github_calls == ["owner/repository"]

        proposal = client.get("/plans?project_id=existing")
        assert "Adoption proposal" in proposal.text
        assert "Already delivered" in proposal.text
        assert "Nothing has been dropped" in proposal.text
        decision_review = client.post(
            "/ui/actions/adoption/decision/review",
            data={
                "csrf_token": csrf,
                "project_id": "existing",
                "decision": "approve",
                "reason": "checked the existing delivery",
                "approve_drop:T1": "yes",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert decision_review.status_code == 200
        assert "T1" in decision_review.text
        assert "T2" in decision_review.text
        assert "No queue row or remote resource has changed" in decision_review.text
        decision_review_id = decision_review.text.split('name="review_id" value="', 1)[1].split(
            '"', 1
        )[0]
        assert queue.items(project_id="existing") == []

        decided = client.post(
            "/ui/actions/adoption/decision/apply",
            data={"csrf_token": csrf, "project_id": "existing", "review_id": decision_review_id},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert decided.status_code == 303
        assert queue.items(project_id="existing") == []

        assert (
            "Approval alone still created no queue rows"
            in client.get("/plans?project_id=existing").text
        )
        reconcile_review = client.post(
            "/ui/actions/adoption/reconcile/review",
            data={
                "csrf_token": csrf,
                "project_id": "existing",
                "reason": "bring pending work into the queue",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert reconcile_review.status_code == 200
        assert "First real mutation" in reconcile_review.text
        assert "T1" in reconcile_review.text and "done" in reconcile_review.text
        assert "T2" in reconcile_review.text and "pending" in reconcile_review.text
        reconcile_review_id = reconcile_review.text.split('name="review_id" value="', 1)[1].split(
            '"', 1
        )[0]
        assert queue.items(project_id="existing") == []

        applied = client.post(
            "/ui/actions/adoption/reconcile/apply",
            data={
                "csrf_token": csrf,
                "project_id": "existing",
                "review_id": reconcile_review_id,
            },
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert applied.status_code == 303
        assert {row.item_id: row.state for row in queue.items(project_id="existing")} == {
            "T1": "done",
            "T2": "pending",
        }
        project = queue.get_project("existing")
        assert project is not None
        assert project.repo == "owner/repository"
        assert project.checks == ["pytest -q"]
        assert project.max_workers == 3

        replay = client.post(
            "/ui/actions/adoption/reconcile/apply",
            data={
                "csrf_token": csrf,
                "project_id": "existing",
                "review_id": reconcile_review_id,
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert replay.status_code == 409
        assert len(queue.items(project_id="existing")) == 2
        events = [json.loads(row["data"]) for row in audit.recent(limit=50)]
        assert any(
            row.get("action") == "adoption_reconcile"
            and row.get("operator") == "operator"
            and row.get("reason") == "bring pending work into the queue"
            for row in events
        )
        assert any(
            row.get("action") == "adoption_reconcile"
            and row.get("reason_kind") == "invalid_or_expired_review"
            for row in events
        )


def test_adoption_reconciliation_refuses_plan_drift_before_mutation(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    plan = repository / "PLAN.md"
    plan.write_text("# Existing\n\n- [ ] T1: First\n\nbrief\n")
    store = EventStore(tmp_path / "events.sqlite")
    audit = AuditStore(tmp_path / "audit.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(
        Project(
            project_id="existing",
            name="Existing",
            work_dir=str(repository),
            plan_path=str(plan),
        )
    )
    with TestClient(
        create_api(
            store,
            queue=queue,
            audit=audit,
            token=TOKEN,
            adoption_branches=lambda _repo: [],
        )
    ) as client:
        login(client)
        page = client.get("/plans?project_id=existing")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        client.post(
            "/ui/actions/adoption/inspect",
            data={"csrf_token": csrf, "project_id": "existing"},
            headers={"X-CSRF-Token": csrf},
        )
        report = client.get("/api/adoption/existing", headers={"Authorization": f"Bearer {TOKEN}"})
        digest = report.json()["digest"]
        client.post(
            "/api/adoption/existing/decision",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "decision": "approve",
                "approved_drops": [],
                "reason": "reviewed",
                "expected_digest": digest,
            },
        )
        reviewed = client.post(
            "/ui/actions/adoption/reconcile/review",
            data={"csrf_token": csrf, "project_id": "existing", "reason": "adopt it"},
            headers={"X-CSRF-Token": csrf},
        )
        review_id = reviewed.text.split('name="review_id" value="', 1)[1].split('"', 1)[0]
        plan.write_text("# Existing\n\n- [ ] T1: Changed\n\na different brief\n")
        refused = client.post(
            "/ui/actions/adoption/reconcile/apply",
            data={"csrf_token": csrf, "project_id": "existing", "review_id": review_id},
            headers={"X-CSRF-Token": csrf},
        )
        assert refused.status_code == 409
        assert queue.items(project_id="existing") == []
        events = [json.loads(row["data"]) for row in audit.recent(limit=50)]
        assert any(
            row.get("action") == "adoption_reconcile"
            and row.get("reason_kind") == "adoption_inputs_changed"
            for row in events
        )


def test_adoption_reconciliation_reports_a_partial_external_failure(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    plan = repository / "PLAN.md"
    plan.write_text("# Existing\n\n- [ ] T1: First\n\nbrief\n")
    store = EventStore(tmp_path / "events.sqlite")
    audit = AuditStore(tmp_path / "audit.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(
        Project(
            project_id="existing",
            name="Existing",
            repo="owner/repository",
            work_dir=str(repository),
            plan_path=str(plan),
        )
    )
    runner = StatefulGitHubRunner()
    runner.issues = json.dumps(
        [
            {
                "number": 7,
                "title": "First",
                "body": "This is T1 and deliberately has no harness marker.",
                "state": "CLOSED",
                "url": "https://github.example/owner/repository/issues/7",
            }
        ]
    )
    with TestClient(
        create_api(
            store,
            queue=queue,
            audit=audit,
            token=TOKEN,
            github_factory=lambda repo: GitHub(repo, runner),
            adoption_branches=lambda _repo: [],
        )
    ) as client:
        login(client)
        page = client.get("/plans?project_id=existing")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        inspected = client.post(
            "/ui/actions/adoption/inspect",
            data={"csrf_token": csrf, "project_id": "existing", "inspect_remote": "yes"},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert inspected.status_code == 303
        proposal = client.get(
            "/api/adoption/existing", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        payload = proposal.json()
        assert payload["proposed_drops"] == ["T1"]
        approved = client.post(
            "/api/adoption/existing/decision",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "decision": "approve",
                "approved_drops": ["T1"],
                "reason": "confirmed closed issue",
                "expected_digest": payload["digest"],
            },
        )
        assert approved.status_code == 200
        reviewed = client.post(
            "/ui/actions/adoption/reconcile/review",
            data={"csrf_token": csrf, "project_id": "existing", "reason": "adopt it"},
            headers={"X-CSRF-Token": csrf},
        )
        assert reviewed.status_code == 200
        review_id = reviewed.text.split('name="review_id" value="', 1)[1].split('"', 1)[0]
        runner.fail_writes = True
        failed = client.post(
            "/ui/actions/adoption/reconcile/apply",
            data={"csrf_token": csrf, "project_id": "existing", "review_id": review_id},
            headers={"X-CSRF-Token": csrf},
        )
        assert failed.status_code == 502
        assert "may already have landed" in failed.text
        assert "remote rejected" not in failed.text
        landed = queue.get("T1", project_id="existing")
        assert landed is not None and landed.state == "done"
        current = client.get("/api/adoption/existing", headers={"Authorization": f"Bearer {TOKEN}"})
        assert current.json()["state"] == "approved"
        events = [json.loads(row["data"]) for row in audit.recent(limit=50)]
        assert any(
            row.get("action") == "adoption_reconcile"
            and row.get("reason_kind") == "external_reconcile_failed_may_be_partial"
            for row in events
        )


def test_login_cookie_is_opaque_and_pages_are_read_only(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        session = login(client)
        assert TOKEN not in session
        projects = client.get("/projects")
        assert projects.status_code == 200
        assert "Project P" in projects.text
        assert "monitoring-only" in projects.text.lower()
        assert client.get("/work?project_id=p").status_code == 200
        assert "First item" in client.get("/work?project_id=p").text
        assert client.post("/api/projects/p/start").status_code == 401


def test_browser_controls_require_csrf_and_delegate_queue_rules(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        login(client)
        page = client.get("/work/T1?project_id=p")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        assert (
            client.post(
                "/ui/actions/work/block",
                data={"project_id": "p", "item_id": "T1", "reason": "needs a decision"},
            ).status_code
            == 403
        )
        response = client.post(
            "/ui/actions/work/block",
            data={
                "csrf_token": csrf,
                "project_id": "p",
                "item_id": "T1",
                "reason": "needs a decision",
            },
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert client.get("/work/T1?project_id=p").text.count("blocked") >= 1
        audit = client.app.state.store.recent(limit=10)  # type: ignore[attr-defined]
        assert any(event["data"].get("operator") == "operator" for event in audit)


def test_monitoring_only_disables_project_controls(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        login(client)
        html = client.get("/projects").text
        assert "monitoring-only" in html
        assert "no supervised worker pool" in html
        assert "disabled" in html


def test_preflight_page_uses_start_gate_and_base_check_contract(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        login(client)
        html = client.get("/projects/p/preflight").text
        assert "Project preflight" in html
        assert "no worker pool is attached" in html
        assert "Run base checks" in html
        csrf = html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        response = client.post(
            "/ui/actions/preflight/base",
            data={"csrf_token": csrf, "project_id": "p"},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].endswith("/projects/p/preflight?check_base=true")


def test_hold_answer_form_uses_opaque_resume_token_and_csrf(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        queue = client.app.state.queue  # type: ignore[attr-defined]
        queue.set_control("running", project_id="p")
        claimed = queue.claim(owner="worker", project_id="p")
        assert claimed is not None
        hold = queue.hold(
            "T1", project_id="p", question="Choose a path", owner="worker", max_seconds=60
        )
        login(client)
        html = client.get("/holds?project_id=p").text
        assert hold.resume_token in html
        csrf = html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        response = client.post(
            "/ui/actions/hold/answer",
            data={
                "csrf_token": csrf,
                "project_id": "p",
                "item_id": "T1",
                "resume_token": hold.resume_token,
                "text": "the safe path",
                "data": '{"choice":"safe"}',
            },
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert queue.holds.current("p", "T1") is None


def test_ui_security_headers_and_packaged_assets(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/login")
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert "X-Content-Type-Options" in response.headers
        css = client.get("/assets/app.css")
        js = client.get("/assets/htmx.min.js")
        assert css.status_code == 200 and "--accent" in css.text
        assert js.status_code == 200 and "cdn" not in js.text.lower()
        assert "immutable" in css.headers["cache-control"]


def test_login_refuses_without_configured_token(tmp_path: Path) -> None:
    with make_client(tmp_path, token=None) as client:
        assert client.get("/login").status_code == 503
        assert client.post("/login", data={"token": TOKEN}).status_code == 503
        assert client.get("/api/work").status_code == 503


def test_logout_requires_csrf_and_revokes_session(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        login(client)
        assert client.post("/logout", follow_redirects=False).status_code == 403
        csrf = (
            client.get("/projects").text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        )
        response = client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert client.get("/projects").status_code == 401


def test_token_rotation_revokes_existing_browser_session(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        login(client)
        client.app.state.token = "rotated-token"  # type: ignore[attr-defined]
        assert client.get("/projects").status_code == 401


def test_item_detail_renders_durable_evidence_without_xss(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        store = client.app.state.store  # type: ignore[attr-defined]
        store.append(
            [
                Event(
                    ts=time.time(),
                    kind=WORK,
                    source="fixture",
                    outcome="agent_started",
                    data={
                        "project_id": "p",
                        "item_id": "T1",
                        "detail": "<script>alert(1)</script>",
                    },
                )
            ]
        )
        login(client)
        response = client.get("/work/T1?project_id=p")
        assert response.status_code == 200
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
        assert "<script>alert(1)</script>" not in response.text
        evidence = client.get(
            "/api/work/T1/evidence?project_id=p",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert evidence.status_code == 200
        assert evidence.json()["item_id"] == "T1"


def test_event_stream_accepts_cursor_and_requires_auth(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert client.get("/api/events/stream?since_id=0").status_code == 401
        login(client)
        response = client.get("/api/events/stream?since_id=bad")
        assert response.status_code == 400


def test_events_page_preserves_typed_filters_and_workers_page_explains_monitoring_only(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        store = client.app.state.store  # type: ignore[attr-defined]
        store.append(
            [
                Event(
                    ts=time.time(),
                    kind=WORK,
                    source="fixture",
                    outcome="checks_failed",
                    data={"project_id": "p", "reason_kind": "checks_failed"},
                )
            ]
        )
        login(client)
        events = client.get("/events?project_id=p&reason_kind=checks_failed")
        assert events.status_code == 200
        assert 'name="reason_kind" value="checks_failed"' in events.text
        assert "checks_failed" in events.text
        workers = client.get("/workers")
        assert workers.status_code == 200
        assert "Monitoring-only deployment" in workers.text


def test_analytics_page_shows_denominators_and_unknown_costs(tmp_path: Path) -> None:
    audit = AuditStore(tmp_path / "audit.sqlite")
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P"))
    audit.append(
        [
            Event(
                ts=time.time(),
                kind="model_call",
                source="fixture",
                error_class="rpm",
                data={
                    "run_id": "r",
                    "seq": 1,
                    "project_id": "p",
                    "tokens_in": 1,
                    "price_in_per_mtok": 2.0,
                    "price_table": "fixture",
                },
            ),
            Event(
                ts=time.time(),
                kind="model_call",
                source="fixture",
                error_class="unclassified",
                data={"run_id": "r", "seq": 2, "project_id": "p"},
            ),
        ]
    )
    with TestClient(create_api(store, queue=queue, audit=audit, token=TOKEN)) as client:
        login(client)
        page = client.get("/analytics?window=all&project_id=p")
        assert page.status_code == 200
        assert "Denominator: 2 observed rate-limit rows" in page.text
        assert "unclassified" in page.text
        assert "Known spend excludes 1 unpriced calls" in page.text


def test_ui_named_urls_honor_root_path(tmp_path: Path) -> None:
    with make_rooted_client(tmp_path) as client:
        response = client.get("/login")
        assert response.status_code == 200
        assert "/harness/login" in response.text
        assert "/harness/assets/app.css" in response.text


def test_plans_surface_scoping_gate_and_plan_preview(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P"))
    app = create_api(store, queue=queue, token=TOKEN, model_client=FakeScoper())
    with TestClient(app) as client:
        login(client)
        csrf = (
            client.get("/plans?project_id=p")
            .text.split('name="csrf_token" value="', 1)[1]
            .split('"', 1)[0]
        )
        assert "Describe a project" in client.get("/plans?project_id=p").text
        started = client.post(
            "/ui/actions/inception/start",
            data={"csrf_token": csrf, "project_id": "p", "overview": "A browser plan"},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert started.status_code == 303
        scoped = client.post(
            "/ui/actions/inception/scope",
            data={"csrf_token": csrf, "project_id": "p"},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert scoped.status_code == 303
        html = client.get("/plans?project_id=p").text
        assert "Which branch?" in html and "Approve scope" in html
        assert "disabled" in html
        question = client.post(
            "/ui/actions/inception/question",
            data={
                "csrf_token": csrf,
                "project_id": "p",
                "question_id": "Q1",
                "answer": "main",
            },
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert question.status_code == 303
        approved = client.post(
            "/ui/actions/inception/approve",
            data={"csrf_token": csrf, "project_id": "p"},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert approved.status_code == 303
        html = client.get("/plans?project_id=p").text
        assert "PLAN.md preview" in html
        assert "A safe browser plan." in html


def test_plans_show_loss_report_for_configured_plan(tmp_path: Path) -> None:
    plan_path = tmp_path / "PLAN.md"
    plan_path.write_text(
        "# Plan\n\n## Narrative\n\n### T1 — One\n\nDo one.\n\n"
        "### T1 — Duplicate\n\nDo two.\n\n### T2 — Two\n\ndepends on: T9\n",
        encoding="utf-8",
    )
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P", plan_path=str(plan_path)))
    with TestClient(create_api(store, queue=queue, token=TOKEN)) as client:
        login(client)
        html = client.get("/plans?project_id=p").text
        assert "Parse review" in html
        assert "Duplicate ids" in html
        assert "unresolved" in html.lower()


def test_plan_sync_requires_preview_and_rechecks_exact_plan(tmp_path: Path) -> None:
    plan_path = tmp_path / "PLAN.md"
    plan_path.write_text("# Plan\n\n### T1 — Ship it\n\nA precise brief.\n", encoding="utf-8")
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(
        Project(project_id="p", name="Project P", repo="o/r", plan_path=str(plan_path))
    )
    runner = FakeGitHubRunner()
    app = create_api(
        store,
        queue=queue,
        token=TOKEN,
        github_factory=lambda repo: GitHub(repo, runner),
    )
    with TestClient(app) as client:
        login(client)
        html = client.get("/plans?project_id=p").text
        assert "Preview GitHub sync" in html
        csrf = html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        review = client.post(
            "/ui/actions/plan-sync/review",
            data={"csrf_token": csrf, "project_id": "p"},
            headers={"X-CSRF-Token": csrf},
        )
        assert review.status_code == 200
        assert "No external writes yet" in review.text
        assert not any(call[1:3] == ["issue", "create"] for call in runner.calls)
        review_id = review.text.split('name="review_id" value="', 1)[1].split('"', 1)[0]

        plan_path.write_text(
            "# Plan\n\n### T1 — Changed after review\n\nA different brief.\n",
            encoding="utf-8",
        )
        stale = client.post(
            "/ui/actions/plan-sync/apply",
            data={"csrf_token": csrf, "project_id": "p", "review_id": review_id},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert stale.status_code == 409
        assert not any(call[1:3] == ["issue", "create"] for call in runner.calls)

        plan_path.write_text("# Plan\n\n### T1 — Ship it\n\nA precise brief.\n", encoding="utf-8")
        fresh = client.post(
            "/ui/actions/plan-sync/review",
            data={"csrf_token": csrf, "project_id": "p"},
            headers={"X-CSRF-Token": csrf},
        )
        fresh_id = fresh.text.split('name="review_id" value="', 1)[1].split('"', 1)[0]
        applied = client.post(
            "/ui/actions/plan-sync/apply",
            data={"csrf_token": csrf, "project_id": "p", "review_id": fresh_id},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert applied.status_code == 303
        assert any(call[1:3] == ["issue", "create"] for call in runner.calls)
        assert any(event["data"].get("action") == "plan_sync" for event in store.recent(limit=20))


def test_plan_sync_remote_drift_and_refusal_write_nothing_unreviewed(tmp_path: Path) -> None:
    plan_path = tmp_path / "PLAN.md"
    plan_path.write_text("# Plan\n\n### T1 — Ship it\n\nA precise brief.\n", encoding="utf-8")
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(
        Project(project_id="p", name="Project P", repo="o/r", plan_path=str(plan_path))
    )
    runner = StatefulGitHubRunner()
    app = create_api(
        store,
        queue=queue,
        token=TOKEN,
        github_factory=lambda repo: GitHub(repo, runner),
    )
    with TestClient(app) as client:
        login(client)
        page = client.get("/plans?project_id=p")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        review = client.post(
            "/ui/actions/plan-sync/review",
            data={"csrf_token": csrf, "project_id": "p"},
            headers={"X-CSRF-Token": csrf},
        )
        review_id = review.text.split('name="review_id" value="', 1)[1].split('"', 1)[0]
        runner.issues = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Changed remotely",
                    "body": "<!-- harness:id=T1 -->",
                    "state": "OPEN",
                    "labels": [],
                    "milestone": None,
                    "assignees": [],
                    "url": "https://github.com/o/r/issues/1",
                }
            ]
        )
        drifted = client.post(
            "/ui/actions/plan-sync/apply",
            data={"csrf_token": csrf, "project_id": "p", "review_id": review_id},
            headers={"X-CSRF-Token": csrf},
        )
        assert drifted.status_code == 409
        assert not any(
            call[1:3] in (["issue", "create"], ["issue", "edit"]) for call in runner.calls
        )
        assert any(
            event["data"].get("reason_kind") == "remote_preview_changed"
            for event in store.recent(limit=20)
        )

        runner.issues = "[]"
        fresh = client.post(
            "/ui/actions/plan-sync/review",
            data={"csrf_token": csrf, "project_id": "p"},
            headers={"X-CSRF-Token": csrf},
        )
        fresh_id = fresh.text.split('name="review_id" value="', 1)[1].split('"', 1)[0]
        runner.fail_writes = True
        refused = client.post(
            "/ui/actions/plan-sync/apply",
            data={"csrf_token": csrf, "project_id": "p", "review_id": fresh_id},
            headers={"X-CSRF-Token": csrf},
        )
        assert refused.status_code == 502
        assert any(
            event["data"].get("reason_kind") == "github_refused" for event in store.recent(limit=20)
        )


def test_plan_sync_rejects_project_target_drift_and_audits_it(tmp_path: Path) -> None:
    plan_path = tmp_path / "PLAN.md"
    plan_path.write_text("# Plan\n\n### T1 — Ship it\n\nA precise brief.\n", encoding="utf-8")
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(
        Project(project_id="p", name="Project P", repo="o/r", plan_path=str(plan_path))
    )
    runner = FakeGitHubRunner()
    with TestClient(
        create_api(
            store,
            queue=queue,
            token=TOKEN,
            github_factory=lambda repo: GitHub(repo, runner),
        )
    ) as client:
        login(client)
        page = client.get("/plans?project_id=p")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        review = client.post(
            "/ui/actions/plan-sync/review",
            data={"csrf_token": csrf, "project_id": "p"},
            headers={"X-CSRF-Token": csrf},
        )
        review_id = review.text.split('name="review_id" value="', 1)[1].split('"', 1)[0]
        queue.add_project(
            Project(project_id="p", name="Project P", repo="other/repo", plan_path=str(plan_path))
        )
        response = client.post(
            "/ui/actions/plan-sync/apply",
            data={"csrf_token": csrf, "project_id": "p", "review_id": review_id},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 409
        assert not any(call[1:3] == ["issue", "create"] for call in runner.calls)
        assert any(
            event["data"].get("reason_kind") == "project_configuration_changed"
            for event in store.recent(limit=20)
        )


def test_plan_sync_control_needs_both_plan_and_repository(tmp_path: Path) -> None:
    plan_path = tmp_path / "PLAN.md"
    plan_path.write_text("# Plan\n\n### T1 — Ship it\n\nA precise brief.\n", encoding="utf-8")
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P", plan_path=str(plan_path)))
    with TestClient(create_api(store, queue=queue, token=TOKEN)) as client:
        login(client)
        html = client.get("/plans?project_id=p").text
        assert "Preview GitHub sync" not in html
        assert "Configure a repository" in html


def test_graph_page_shows_typed_edges_and_readiness(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P"))
    queue.add(
        [
            WorkRecord(item_id="T1", title="First", brief="first"),
            WorkRecord(item_id="T2", title="Second", brief="second", depends_on=["T1"]),
        ],
        project_id="p",
    )
    with TestClient(create_api(store, queue=queue, token=TOKEN)) as client:
        login(client)
        html = client.get("/graph?project_id=p").text
        assert "Dependency graph" in html
        assert "T2" in html and "T1" in html
        assert "blocked" in html.lower()


def test_graph_explorer_preserves_every_typed_distinction_and_list_equivalent(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    for project_id in ("p", "other"):
        queue.add_project(Project(project_id=project_id, name=project_id))
    queue.add([WorkRecord(item_id="REMOTE", title="Remote work")], project_id="other")
    queue.add(
        [
            WorkRecord(item_id="T1", title="First", state="done"),
            WorkRecord(item_id="D9", title="Human choice"),
            WorkRecord(
                item_id="T2",
                title="Everything waits here",
                depends_on=[
                    "T1",
                    "?external:tracker:TICKET-9",
                    "decision:D9",
                    "project:other/REMOTE",
                ],
            ),
            WorkRecord(item_id="C1", title="Cycle one", depends_on=["C2"]),
            WorkRecord(item_id="C2", title="Cycle two", depends_on=["C1"]),
        ],
        project_id="p",
    )
    with TestClient(create_api(store, queue=queue, token=TOKEN)) as client:
        login(client)
        response = client.get("/graph?project_id=p")
    assert response.status_code == 200
    html = response.text
    assert 'data-dependency-graph data-graph-revision="' in html
    assert 'data-graph-revision="' in html and " hidden>" in html
    assert 'data-graph-search aria-controls="dependency-diagram graph-edge-table"' in html
    assert 'data-graph-canvas tabindex="0" role="region"' in html
    assert "Search items, targets, kinds, states, or evidence" in html
    assert "Zoom in" in html and "Pan left" in html and "Reset view" in html
    assert "Complete edge list" in html
    assert "accessible, no-script equivalent" in html
    for kind in (
        "local_work",
        "external_reference",
        "human_decision",
        "cross_project_work",
    ):
        assert f'data-kind="{kind}"' in html
    for state in ("satisfied", "blocked", "unresolved"):
        assert f'data-state="{state}"' in html
    assert 'data-required="false"' in html
    assert "advisory" in html.lower()
    assert "data-graph-cycle=" in html
    assert "Cycles block work" in html
    assert "TICKET-9" in html and "tracker" in html
    assert "other/REMOTE" in html


def test_packaged_graph_script_is_keyboard_accessible_and_read_only() -> None:
    script = Path(__file__).parents[1] / "src" / "agent_harness" / "static" / "app.js"
    source = script.read_text(encoding="utf-8")
    for key in ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Enter", "Escape"):
        assert key in source
    assert "focusSummary.textContent" in source
    assert 'aria-live="polite"' in (
        Path(__file__).parents[1] / "src" / "agent_harness" / "templates" / "graph.html"
    ).read_text(encoding="utf-8")
    assert "aria-label" in source
    assert "data-graph-edge" in source and "data-graph-item" in source
    assert "root.hidden = false" in source
    assert (
        "fetch("
        not in source[
            source.index("const initializeDependencyGraph") : source.index(
                "document.addEventListener('DOMContentLoaded'"
            )
        ]
    )


def test_graph_data_attributes_escape_untrusted_evidence(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="p"))
    hostile = '"><img src=x onerror=alert(1)>'
    queue.add(
        [
            WorkRecord(item_id=hostile, title="Hostile target"),
            WorkRecord(item_id="T2", title="Waiting", depends_on=[hostile]),
        ],
        project_id="p",
    )
    with TestClient(create_api(store, queue=queue, token=TOKEN)) as client:
        login(client)
        response = client.get("/graph?project_id=p")
    assert response.status_code == 200
    assert hostile not in response.text
    assert "&gt;&lt;img" in response.text
    assert "<img src=x onerror=alert(1)>" not in response.text


def test_dependency_override_is_explicit_revision_scoped_and_audited(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P"))
    queue.add(
        [
            WorkRecord(item_id="T1", title="First", brief="first"),
            WorkRecord(item_id="T2", title="Second", brief="second", depends_on=["T1"]),
        ],
        project_id="p",
    )
    with TestClient(create_api(store, queue=queue, token=TOKEN)) as client:
        login(client)
        html = client.get("/graph?project_id=p").text
        csrf = html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        missing_reason = client.post(
            "/ui/actions/work/dependency-override",
            data={
                "csrf_token": csrf,
                "project_id": "p",
                "item_id": "T2",
                "reason": "",
            },
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert missing_reason.status_code == 422
        response = client.post(
            "/ui/actions/work/dependency-override",
            data={
                "csrf_token": csrf,
                "project_id": "p",
                "item_id": "T2",
                "reason": "the dependency is tracked in the external release board",
            },
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].endswith("/graph?project_id=p")
        readiness = queue.readiness("T2", project_id="p")
        assert readiness.ready is True
        assert readiness.overridden is True
        assert readiness.override_reason is not None
        assert "external release board" in readiness.override_reason
        html = client.get("/graph?project_id=p").text
        assert "Recorded overrides" in html
        assert "external release board" in html
        assert "operator" in html
        assert any(
            event["data"].get("action") == "dependency_override"
            and event["data"].get("operator") == "operator"
            for event in store.recent(limit=20)
        )


def _configuration_form(csrf: str, *, name: str) -> dict[str, str]:
    return {
        "csrf_token": csrf,
        "project_id": "p",
        "name": name,
        "base_branch": "main",
        "repo": "",
        "work_dir": "",
        "plan_path": "",
        "durability": "",
        "max_workers": "1",
        "max_attempts": "5",
        "max_item_seconds": "0",
        "max_item_spend_usd": "0",
        "max_hold_seconds": "21600",
        "min_free_disk_gb": "0",
    }


def test_project_configuration_is_secret_safe_and_requires_review(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P", checks=["echo SECRET"]))
    with TestClient(create_api(store, queue=queue, token=TOKEN)) as client:
        login(client)
        page = client.get("/projects/p/configuration")
        assert page.status_code == 200
        assert "1 check(s)" in page.text
        assert "SECRET" not in page.text
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        review = client.post(
            "/ui/actions/project-configuration/review",
            data=_configuration_form(csrf, name="Renamed project"),
            headers={"X-CSRF-Token": csrf},
        )
        assert review.status_code == 200
        assert "No changes applied" in review.text
        assert "name" in review.text
        configured = queue.get_project("p")
        assert configured is not None and configured.name == "Project P"


def test_project_configuration_apply_is_one_time_audited_and_stale_safe(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P"))
    with TestClient(create_api(store, queue=queue, token=TOKEN)) as client:
        login(client)
        page = client.get("/projects/p/configuration")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        review = client.post(
            "/ui/actions/project-configuration/review",
            data=_configuration_form(csrf, name="Renamed project"),
            headers={"X-CSRF-Token": csrf},
        )
        review_id = review.text.split('name="review_id" value="', 1)[1].split('"', 1)[0]
        applied = client.post(
            "/ui/actions/project-configuration/apply",
            data={"csrf_token": csrf, "project_id": "p", "review_id": review_id},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert applied.status_code == 303
        configured = queue.get_project("p")
        assert configured is not None and configured.name == "Renamed project"
        assert any(
            event["data"].get("action") == "project_configuration"
            and event["data"].get("operator") == "operator"
            for event in store.recent(limit=20)
        )
        replay = client.post(
            "/ui/actions/project-configuration/apply",
            data={"csrf_token": csrf, "project_id": "p", "review_id": review_id},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert replay.status_code == 409

        page = client.get("/projects/p/configuration")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        stale = client.post(
            "/ui/actions/project-configuration/review",
            data=_configuration_form(csrf, name="Stale browser value"),
            headers={"X-CSRF-Token": csrf},
        )
        stale_id = stale.text.split('name="review_id" value="', 1)[1].split('"', 1)[0]
        queue.add_project(Project(project_id="p", name="Concurrent API value"))
        refused = client.post(
            "/ui/actions/project-configuration/apply",
            data={"csrf_token": csrf, "project_id": "p", "review_id": stale_id},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert refused.status_code == 409
        configured = queue.get_project("p")
        assert configured is not None and configured.name == "Concurrent API value"


def test_global_role_routing_is_secret_safe_reviewed_complete_and_stale_safe(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P"))
    app = create_api(
        store,
        queue=queue,
        token=TOKEN,
        executor_roles=ExecutorRoles(calls=frozenset({"reviewer"}), implemented_by="agent"),
    )
    route_map: dict[str, dict[str, object]] = {
        "implementer": {
            "models": ["writer", "writer-fallback"],
            "endpoint": "https://user:password@models.example/v1?token=secret",
            "provider": "generic",
            "preset": "chat-completions",
            "price_ref": "writer-price",
        },
        "reviewer": {
            "model": "reviewer",
            "endpoint": "https://review.example/v1",
            "provider": "generic",
            "preset": "chat-completions",
            "price_ref": "review-price",
        },
    }
    with TestClient(app) as client:
        login(client)
        page = client.get("/settings")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        review = client.post(
            "/ui/actions/roles/review",
            data={"csrf_token": csrf, "roles": json.dumps(route_map)},
            headers={"X-CSRF-Token": csrf},
        )
        assert review.status_code == 200
        assert "models.example/v1" in review.text
        assert "password" not in review.text
        assert "token=secret" not in review.text
        assert "Unused:" in review.text
        assert "writer → writer-fallback" in review.text
        review_id = review.text.split('name="review_id" value="', 1)[1].split('"', 1)[0]
        applied = client.post(
            "/ui/actions/roles/apply",
            data={"csrf_token": csrf, "review_id": review_id},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert applied.status_code == 303
        stored = cast(dict[str, dict[str, object]], queue.get_setting("role_map"))
        assert stored["implementer"]["models"] == ["writer", "writer-fallback"]
        assert stored["implementer"]["preset"] == "chat-completions"
        assert stored["implementer"]["price_ref"] == "writer-price"
        assert any(
            event["data"].get("action") == "role_configuration"
            and event["data"].get("operator") == "operator"
            for event in store.recent(limit=20)
        )

        page = client.get("/settings")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        stale = client.post(
            "/ui/actions/roles/review",
            data={"csrf_token": csrf, "roles": json.dumps({"reviewer": route_map["reviewer"]})},
            headers={"X-CSRF-Token": csrf},
        )
        stale_id = stale.text.split('name="review_id" value="', 1)[1].split('"', 1)[0]
        queue.set_setting(
            "role_map", {"reviewer": {**route_map["reviewer"], "model": "concurrent"}}
        )
        refused = client.post(
            "/ui/actions/roles/apply",
            data={"csrf_token": csrf, "review_id": stale_id},
            headers={"X-CSRF-Token": csrf},
        )
        assert refused.status_code == 409
        current = cast(dict[str, dict[str, object]], queue.get_setting("role_map"))
        assert current["reviewer"]["model"] == "concurrent"
        assert any(
            event["data"].get("reason_kind") == "role_map_changed"
            for event in store.recent(limit=20)
        )


def test_github_reconciliation_is_reviewed_resolved_audited_and_one_time(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    audit = AuditStore(tmp_path / "audit.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P", repo="owner/repository"))
    queue.add(
        [WorkRecord(item_id="T1", title="Known PR", brief="")],
        project_id="p",
    )
    assert queue.record_pr_url("T1", "https://x/pull/7", project_id="p")
    queue.add_project(Project(project_id="other", name="Other", repo="elsewhere/repository"))
    queue.add(
        [WorkRecord(item_id="O1", title="Other PR", brief="")],
        project_id="other",
    )
    assert queue.record_pr_url("O1", "https://x/pull/8", project_id="other")
    calls: list[tuple[str, dict[int, dict[str, str]]]] = []

    class FakeReconciler:
        def __init__(self, repo: str, _audit: AuditStore) -> None:
            self.repo = repo

        def reconcile(self, mapping: dict[int, dict[str, str]]) -> ReconcileReport:
            calls.append((self.repo, mapping))
            return ReconcileReport(merged=2, skipped=1, errors=["one remote row was invalid"])

    monkeypatch.setattr("agent_harness.audit_service.GitHubReconciler", FakeReconciler)
    with TestClient(create_api(store, queue=queue, audit=audit, token=TOKEN)) as client:
        login(client)
        page = client.get("/analytics")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        review = client.post(
            "/ui/actions/audit-reconcile/review",
            data={"csrf_token": csrf, "project_id": "p", "reason": "refresh delivery facts"},
            headers={"X-CSRF-Token": csrf},
        )
        assert review.status_code == 200
        assert "owner/repository" in review.text
        assert "No external request has been made" in review.text
        assert "Dry run is not supported" in review.text
        assert calls == []
        review_id = review.text.split('name="review_id" value="', 1)[1].split('"', 1)[0]

        result = client.post(
            "/ui/actions/audit-reconcile/apply",
            data={"csrf_token": csrf, "project_id": "p", "review_id": review_id},
            headers={"X-CSRF-Token": csrf},
        )
        assert result.status_code == 200
        assert calls == [("owner/repository", {7: {"project_id": "p", "item_id": "T1"}})]
        assert "Merged" in result.text and ">2<" in result.text
        assert "one remote row was invalid" in result.text
        events = [json.loads(event["data"]) for event in audit.recent(limit=20)]
        assert any(
            event.get("action") == "audit_reconcile"
            and event.get("operator") == "operator"
            and event.get("reason") == "refresh delivery facts"
            and event.get("repo") == "owner/repository"
            for event in events
        )
        replay = client.post(
            "/ui/actions/audit-reconcile/apply",
            data={"csrf_token": csrf, "project_id": "p", "review_id": review_id},
            headers={"X-CSRF-Token": csrf},
        )
        assert replay.status_code == 409
        assert len(calls) == 1
        replay_events = [json.loads(event["data"]) for event in audit.recent(limit=20)]
        assert any(
            event.get("action") == "audit_reconcile"
            and event.get("reason_kind") == "invalid_or_expired_review"
            for event in replay_events
        )


def test_github_reconciliation_refuses_project_drift_and_missing_reason(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    audit = AuditStore(tmp_path / "audit.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P", repo="owner/first"))
    calls: list[str] = []

    class FakeReconciler:
        def __init__(self, repo: str, _audit: AuditStore) -> None:
            calls.append(repo)

        def reconcile(self, _mapping: dict[int, dict[str, str]]) -> ReconcileReport:
            return ReconcileReport()

    monkeypatch.setattr("agent_harness.audit_service.GitHubReconciler", FakeReconciler)
    with TestClient(create_api(store, queue=queue, audit=audit, token=TOKEN)) as client:
        login(client)
        page = client.get("/analytics")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        missing = client.post(
            "/ui/actions/audit-reconcile/review",
            data={"csrf_token": csrf, "project_id": "p", "reason": ""},
            headers={"X-CSRF-Token": csrf},
        )
        assert missing.status_code == 422
        review = client.post(
            "/ui/actions/audit-reconcile/review",
            data={"csrf_token": csrf, "project_id": "p", "reason": "refresh facts"},
            headers={"X-CSRF-Token": csrf},
        )
        review_id = review.text.split('name="review_id" value="', 1)[1].split('"', 1)[0]
        queue.add_project(Project(project_id="p", name="Project P", repo="owner/changed"))
        refused = client.post(
            "/ui/actions/audit-reconcile/apply",
            data={"csrf_token": csrf, "project_id": "p", "review_id": review_id},
            headers={"X-CSRF-Token": csrf},
        )
        assert refused.status_code == 409
        assert calls == []
        events = [json.loads(event["data"]) for event in audit.recent(limit=20)]
        assert any(
            event.get("action") == "audit_reconcile"
            and event.get("reason_kind") == "project_configuration_changed"
            for event in events
        )


def test_audit_maintenance_is_reviewed_validated_audited_and_reports_errors(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    audit = AuditStore(tmp_path / "audit.sqlite")
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P"))
    calls: list[int] = []

    def fake_maintenance(_audit: AuditStore, *, retention_days: int) -> MaintenanceReport:
        calls.append(retention_days)
        return MaintenanceReport(rolled_up=3, thinned=7, errors=["thin: database busy"])

    monkeypatch.setattr("agent_harness.audit_service.run_maintenance", fake_maintenance)
    with TestClient(create_api(store, queue=queue, audit=audit, token=TOKEN)) as client:
        login(client)
        page = client.get("/analytics")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        invalid = client.post(
            "/ui/actions/audit-maintenance/review",
            data={"csrf_token": csrf, "retention_days": "-1", "reason": "close history"},
            headers={"X-CSRF-Token": csrf},
        )
        assert invalid.status_code == 422
        review = client.post(
            "/ui/actions/audit-maintenance/review",
            data={"csrf_token": csrf, "retention_days": "30", "reason": "close history"},
            headers={"X-CSRF-Token": csrf},
        )
        assert review.status_code == 200
        assert "30 days" in review.text
        assert "No audit rows have been rolled up or thinned" in review.text
        assert "Dry run is not supported" in review.text
        assert calls == []
        review_id = review.text.split('name="review_id" value="', 1)[1].split('"', 1)[0]
        result = client.post(
            "/ui/actions/audit-maintenance/apply",
            data={"csrf_token": csrf, "review_id": review_id},
            headers={"X-CSRF-Token": csrf},
        )
        assert result.status_code == 200
        assert calls == [30]
        assert "thin: database busy" in result.text
        events = [json.loads(event["data"]) for event in audit.recent(limit=20)]
        assert any(
            event.get("action") == "audit_maintenance"
            and event.get("operator") == "operator"
            and event.get("reason") == "close history"
            and event.get("retention_days") == 30
            for event in events
        )
        replay = client.post(
            "/ui/actions/audit-maintenance/apply",
            data={"csrf_token": csrf, "review_id": review_id},
            headers={"X-CSRF-Token": csrf},
        )
        assert replay.status_code == 409
        assert calls == [30]
        replay_events = [json.loads(event["data"]) for event in audit.recent(limit=20)]
        assert any(
            event.get("action") == "audit_maintenance"
            and event.get("reason_kind") == "invalid_or_expired_review"
            for event in replay_events
        )


def test_audit_actions_refuse_when_no_audit_store_is_attached(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        queue = client.app.state.queue  # type: ignore[attr-defined]
        queue.add_project(Project(project_id="p", name="Project P", repo="owner/repository"))
        login(client)
        html = client.get("/analytics").text
        csrf = html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        reconcile = client.post(
            "/ui/actions/audit-reconcile/review",
            data={"csrf_token": csrf, "project_id": "p", "reason": "refresh facts"},
            headers={"X-CSRF-Token": csrf},
        )
        maintenance = client.post(
            "/ui/actions/audit-maintenance/review",
            data={"csrf_token": csrf, "retention_days": "90", "reason": "close history"},
            headers={"X-CSRF-Token": csrf},
        )
        assert reconcile.status_code == 409
        assert maintenance.status_code == 409


def test_audit_actions_refuse_when_operator_events_cannot_be_recorded(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    audit = AuditStore(tmp_path / "audit.sqlite", degraded=True)
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="p", name="Project P", repo="owner/repository"))
    with TestClient(create_api(store, queue=queue, audit=audit, token=TOKEN)) as client:
        login(client)
        html = client.get("/analytics").text
        csrf = html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        response = client.post(
            "/ui/actions/audit-reconcile/review",
            data={"csrf_token": csrf, "project_id": "p", "reason": "refresh facts"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 409
        assert "cannot be recorded" in response.json()["detail"]
