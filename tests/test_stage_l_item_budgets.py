"""Stage L: how long one item may take, and how much it may spend.

Two properties are hard to test and are the whole point, so they are tested
adversarially rather than incidentally.

**A budget stop must not park the endpoint.** A spend ceiling is *our*
statement about *one item*; `window_cap` and `terminal_cap` are a *provider's*
statement about our account, and those are in the never-retry set. Conflating
them would take a shared endpoint out of service because one item was
expensive. So the parks are inspected directly after a stop.

**Unknown cost must not read as zero cost.** An item whose spend cannot be
measured is reported as unmeasurable and its ceiling as unenforceable — never
as satisfied. The unpriced case therefore has its own tests, and the assertion
is on what the harness *says*, not only on what it does.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_harness import budgets as B
from agent_harness import outcomes as O
from agent_harness import providers as P
from agent_harness.executor import Checks, Executor
from agent_harness.model_client import ModelClient, Response, RetryPolicy, Route
from agent_harness.pricing import Price, PriceTable
from agent_harness.work import BLOCKED, DONE, Project, WorkQueue, WorkRecord

DIFF = """\
diff --git a/hello.txt b/hello.txt
index 3b18e51..8c7e5a6 100644
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello world
+hello harness
"""

REPLIES = {"planner": "plan", "implementer": DIFF, "reviewer": "APPROVED\nfine"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    (path / "hello.txt").write_text("hello world\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start
        #: Seconds added by every read, so wall clock advances *during* a run
        #: without any test having to reach into the executor.
        self.per_read = 0.0

    def __call__(self) -> float:
        self.t += self.per_read
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class SpendingModel:
    """Replies with a stated token usage, so a cost is real rather than stubbed."""

    def __init__(self, tokens_out: int = 1000, report_usage: bool = True) -> None:
        self.tokens_out = tokens_out
        self.report_usage = report_usage
        self.calls: list[str] = []

    def __call__(
        self, route: Route, messages: Sequence[Mapping[str, Any]], options: Mapping[str, Any]
    ) -> Response:
        role = str(route.options.get("role", route.model))
        self.calls.append(role)
        body: dict[str, Any] = {"choices": [{"message": {"content": REPLIES.get(role, "ok")}}]}
        if self.report_usage:
            body["usage"] = {"prompt_tokens": 1000, "completion_tokens": self.tokens_out}
        return Response(200, {}, json.dumps(body))


#: One dollar per million tokens in and out, so the arithmetic in these tests
#: is legible: 1000 in + 1000 out is 0.002.
PRICES = PriceTable(
    version="test",
    prices={"model-": Price(in_per_mtok=1.0, out_per_mtok=1.0)},
)


def build(
    repo: Path,
    tmp_path: Path,
    clock: Clock,
    *,
    max_seconds: float = 0.0,
    max_spend: float = 0.0,
    report_usage: bool = True,
    events: list[dict[str, Any]] | None = None,
) -> tuple[Executor, WorkQueue, SpendingModel]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    queue = WorkQueue(str(tmp_path / "w.sqlite"), lease_seconds=10_000.0, now=clock)
    queue.add_project(
        Project(
            project_id="default",
            name="Default",
            max_item_seconds=max_seconds,
            max_item_spend_usd=max_spend,
        )
    )
    queue.set_control("running")
    transport = SpendingModel(report_usage=report_usage)
    client = ModelClient(
        roles={
            role: Route(f"model-{role}", "https://api.example", P.GENERIC, options={"role": role})
            for role in ("planner", "implementer", "reviewer")
        },
        transport=transport,
        policy=RetryPolicy(max_attempts=1, backoff_seconds=0.001),
        sleep=lambda _s: None,
        now=clock,
        prices=PRICES,
    )
    executor = Executor(
        queue,
        client,
        repo,
        checks=Checks(),
        push=False,
        now=clock,
        on_event=(events.append if events is not None else None),
    )
    return executor, queue, transport


def add_item(queue: WorkQueue, item_id: str = "T1") -> None:
    queue.add(
        [
            WorkRecord(
                item_id=item_id,
                title="Change the greeting",
                brief="Change hello.txt to say 'hello harness'.",
            )
        ]
    )


def api_item(queue: WorkQueue, item_id: str = "T1") -> dict[str, Any]:
    import tempfile

    from fastapi.testclient import TestClient

    from agent_harness.api import create_api
    from agent_harness.store import EventStore

    store = EventStore(Path(tempfile.mkdtemp()) / "e.sqlite")
    with TestClient(create_api(store, queue=queue, token="t")) as client:  # noqa: S106
        response = client.get(f"/api/work/{item_id}", headers={"Authorization": "Bearer t"})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# ---------------------------------------------------------- the defaults


def test_by_default_nothing_is_bounded_and_an_item_completes(repo: Path, tmp_path: Path) -> None:
    """§8.4: defaults are unlimited; an existing database upgrades with no
    behaviour change."""
    clock = Clock()
    clock.per_read = 3600.0  # an hour per boundary, and still no ceiling
    executor, queue, _ = build(repo, tmp_path / "a", clock)
    add_item(queue)
    outcome = executor.run_once()
    assert outcome is not None and outcome.state == DONE


def test_an_upgraded_database_reads_as_unlimited_and_nothing_spent(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE work (
            project_id TEXT NOT NULL DEFAULT 'default',
            item_id TEXT NOT NULL,
            issue INTEGER,
            title TEXT NOT NULL,
            brief TEXT NOT NULL DEFAULT '',
            depends_on TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL DEFAULT 'pending',
            owner TEXT,
            lease_until REAL NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            branch TEXT,
            pr_url TEXT,
            updated_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (project_id, item_id)
        );
        INSERT INTO work (item_id, title, state) VALUES ('OLD1', 'From before', 'pending');
        """
    )
    connection.commit()
    connection.close()

    queue = WorkQueue(str(path))
    record = queue.get("OLD1")
    assert record is not None
    assert record.budget_seconds == 0.0
    assert record.budget_spend_usd == 0.0
    assert record.spend_usd == 0.0
    assert record.unpriced_calls == 0
    assert record.first_started_at == 0.0
    assert not B.budget_for(queue.get_project("default"), record).bounded


# ------------------------------------------------------ the wall clock


def test_an_item_over_its_wall_clock_ceiling_stops_with_the_ceiling_named(
    repo: Path, tmp_path: Path
) -> None:
    """§8.4's first criterion."""
    clock = Clock()
    events: list[dict[str, Any]] = []
    executor, queue, model = build(repo, tmp_path / "a", clock, max_seconds=60.0, events=events)
    add_item(queue)
    clock.per_read = 100.0  # every boundary is past the ceiling
    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == BLOCKED, "not failed, and not exhausted"
    assert "60s wall-clock ceiling" in outcome.reason

    item = api_item(queue)
    assert item["state"] == BLOCKED
    assert item["disposition"] == O.ESCALATED
    assert item["reason_kind"] == O.ITEM_WALL_CLOCK
    assert any(e.get("outcome") == "budget_exceeded" for e in events)


def test_a_budget_stop_happens_at_a_boundary_and_not_mid_stage(repo: Path, tmp_path: Path) -> None:
    """A stop that killed work in flight would destroy the context and leave a
    half-finished worktree, which is the reasoning `work.py` already gives
    about pause semantics."""
    clock = Clock()
    events: list[dict[str, Any]] = []
    executor, queue, model = build(repo, tmp_path / "a", clock, max_seconds=1.0, events=events)
    add_item(queue)
    clock.per_read = 100.0
    executor.run_once()

    outcomes = [e.get("outcome") for e in events]
    # It stopped *before* asking anything, at the first boundary. Nothing was
    # begun and abandoned half way.
    assert "budget_exceeded" in outcomes
    assert model.calls == [], "a call was made after the ceiling was already passed"


def test_the_clock_runs_from_the_item_not_the_attempt(repo: Path, tmp_path: Path) -> None:
    """An item that crashes in a loop must not reset its own clock.

    This is the hole D11's ruling opened — `max_attempts` no longer counts
    crashes — and the reason this stage is not optional.
    """
    clock = Clock()
    executor, queue, _ = build(repo, tmp_path / "a", clock, max_seconds=100_000.0)
    add_item(queue)
    executor.run_once()
    first = queue.get("T1")
    assert first is not None and first.first_started_at > 0

    stamped = first.first_started_at
    queue.requeue("T1")
    clock.advance(5000.0)
    executor.run_once()
    again = queue.get("T1")
    assert again is not None
    assert again.first_started_at == stamped, "the item's clock restarted on re-claim"


# ------------------------------------------------------------ the money


def test_an_item_over_its_spend_ceiling_stops_and_does_not_park_the_endpoint(
    repo: Path, tmp_path: Path
) -> None:
    """§8.4's second criterion, both halves.

    The parks are inspected directly. A spend ceiling that parked a shared
    endpoint would be a local policy decision entering the never-retry set.
    """
    clock = Clock()
    # Each call is 1000 in + 1000 out at 1.0/Mtok = 0.002. A ceiling of 0.003
    # is passed after the second call.
    executor, queue, model = build(repo, tmp_path / "a", clock, max_spend=0.003)
    add_item(queue)
    outcome = executor.run_once()

    assert outcome is not None and outcome.state == BLOCKED
    item = api_item(queue)
    assert item["disposition"] == O.ESCALATED
    assert item["reason_kind"] == O.ITEM_SPEND
    assert item["spend_usd"] > 0.003

    parks = executor.client.parks
    assert not parks.remaining("https://api.example", clock(), "planner")
    assert not parks.remaining("https://api.example", clock(), "implementer")
    assert not parks.remaining("https://api.example", clock(), "reviewer")


def test_a_spend_ceiling_is_not_a_provider_cost_cap() -> None:
    """Two vocabularies, and this stage must not merge them.

    `window_cap` and `terminal_cap` are a provider's statement about our
    account. `item_spend` is our statement about one item.
    """
    assert O.ITEM_SPEND not in (P.WINDOW_CAP, P.TERMINAL_CAP, P.RPM, P.NON_RETRYABLE)
    assert O.ITEM_SPEND != O.BUDGET_EXHAUSTED
    assert set(B.CEILINGS).isdisjoint({P.WINDOW_CAP, P.TERMINAL_CAP})


def test_spend_accumulates_across_attempts(repo: Path, tmp_path: Path) -> None:
    """The ceiling bounds the item, so a total that reset on re-claim would
    bound one attempt and never catch a loop."""
    clock = Clock()
    executor, queue, _ = build(repo, tmp_path / "a", clock)
    add_item(queue)
    executor.run_once()
    after_one = queue.get("T1")
    assert after_one is not None and after_one.spend_usd > 0

    queue.requeue("T1")
    executor.run_once()
    after_two = queue.get("T1")
    assert after_two is not None
    assert after_two.spend_usd > after_one.spend_usd


# ------------------------------------------- unknown cost is not zero cost


def test_an_unmeasurable_spend_is_reported_as_unmeasurable(repo: Path, tmp_path: Path) -> None:
    """§8.4's third criterion: reported as unmeasurable, and the report says
    which ceiling could therefore not be enforced."""
    clock = Clock()
    events: list[dict[str, Any]] = []
    executor, queue, _ = build(
        repo, tmp_path / "a", clock, max_spend=0.0001, report_usage=False, events=events
    )
    add_item(queue)
    outcome = executor.run_once()

    # It completed: an unenforceable ceiling does not stop an item, because
    # stopping on a number nobody can defend would be worse than not stopping.
    assert outcome is not None and outcome.state == DONE

    said = [e for e in events if e.get("outcome") == "budget_unenforceable"]
    assert said, "a ceiling that could not be enforced was silently treated as met"
    assert "LOWER BOUND" in said[0]["detail"]
    assert "cannot be enforced" in said[0]["detail"]
    assert "not zero cost" in said[0]["detail"]


def test_an_unpriced_call_is_counted_rather_than_costed() -> None:
    spend = B.Spend()
    spend.add_call({"tokens_in": 100, "tokens_out": 100})  # no price keys
    assert spend.usd == 0.0
    assert spend.unpriced == 1
    assert not spend.measurable


def test_a_priced_call_is_costed() -> None:
    spend = B.Spend()
    spend.add_call(
        {
            "tokens_in": 1_000_000,
            "tokens_out": 0,
            "price_in_per_mtok": 3.0,
            "price_out_per_mtok": 15.0,
        }
    )
    assert spend.usd == pytest.approx(3.0)
    assert spend.measurable


def test_a_call_that_reported_no_usage_at_all_is_unpriced_and_not_free() -> None:
    """The #128 shape, and the sharpest edge in this stage.

    A provider that reports nothing looks identical to a free call unless the
    harness insists otherwise. Treating it as zero would let an item run past
    a spend ceiling while the report said it was comfortably inside one.
    """
    spend = B.Spend()
    spend.add_call({})
    assert spend.priced == 0
    assert spend.unpriced == 1
    assert not spend.measurable


def test_an_unmeasurable_spend_never_satisfies_a_ceiling() -> None:
    """The rule, at the level it is decided."""
    budget = B.Budget(spend_usd=0.001)
    unmeasured = B.Spend(usd=99.0, unpriced=1)
    verdict = B.check(budget, elapsed=0.0, spend=unmeasured)
    assert verdict.ok, "it must not stop on a number nobody can defend"
    assert verdict.unenforceable
    assert verdict.unenforceable[0][0] == B.SPEND


def test_the_wall_clock_is_checked_even_when_the_spend_is_not_measurable() -> None:
    """An item that has run for a week and whose spend is unknown should stop
    for the reason that is knowable."""
    budget = B.Budget(seconds=10.0, spend_usd=1.0)
    verdict = B.check(budget, elapsed=1000.0, spend=B.Spend(unpriced=5))
    assert not verdict.ok
    assert verdict.exceeded is not None
    assert verdict.exceeded.ceiling == B.WALL_CLOCK


# -------------------------------------------------------- configuration


def test_a_per_item_ceiling_overrides_the_project_and_zero_inherits() -> None:
    project = Project(project_id="p", name="P", max_item_seconds=100.0, max_item_spend_usd=5.0)
    inherits = WorkRecord(item_id="A", title="A")
    overrides = WorkRecord(item_id="B", title="B", budget_seconds=7.0)

    assert B.budget_for(project, inherits) == B.Budget(seconds=100.0, spend_usd=5.0)
    assert B.budget_for(project, overrides) == B.Budget(seconds=7.0, spend_usd=5.0)


def test_a_per_item_ceiling_round_trips_through_the_queue(tmp_path: Path) -> None:
    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    add_item(queue)
    assert queue.set_item_budget("T1", seconds=42.0, spend_usd=0.5)
    record = queue.get("T1")
    assert record is not None
    assert record.budget_seconds == 42.0
    assert record.budget_spend_usd == 0.5


def test_the_ceilings_are_readable_through_the_api(tmp_path: Path) -> None:
    """§8.2: an operator can see what a project is permitted to spend before it
    spends it."""
    import tempfile

    from fastapi.testclient import TestClient

    from agent_harness.api import create_api
    from agent_harness.store import EventStore

    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    store = EventStore(Path(tempfile.mkdtemp()) / "e.sqlite")
    with TestClient(create_api(store, queue=queue, token="t")) as client:  # noqa: S106
        auth = {"Authorization": "Bearer t"}
        created = client.post(
            "/api/projects",
            json={
                "project_id": "widgets",
                "name": "Widgets",
                "max_item_seconds": 3600,
                "max_item_spend_usd": 2.5,
            },
            headers=auth,
        )
        assert created.status_code in (200, 201), created.text
        project = client.get("/api/projects/widgets", headers=auth).json()["project"]
    assert project["max_item_seconds"] == 3600
    assert project["max_item_spend_usd"] == 2.5


def test_a_negative_ceiling_is_refused() -> None:
    import pydantic

    from agent_harness.schemas import ProjectSpec

    with pytest.raises(pydantic.ValidationError):
        ProjectSpec(project_id="p", name="P", max_item_seconds=-1)


def test_doctor_says_what_a_project_may_spend_before_it_spends_it(tmp_path: Path) -> None:
    from agent_harness.doctor import OK, WARN, diagnose

    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.add_project(Project(project_id="unbounded", name="Unbounded"))
    queue.add_project(
        Project(project_id="bounded", name="Bounded", max_item_seconds=3600, max_item_spend_usd=2.0)
    )
    report = diagnose(queue, queue.projects())

    by_project = {p.project_id: {f.name: f for f in p.findings} for p in report.projects}
    assert by_project["unbounded"]["item budgets"].state == WARN
    assert "unsafe unattended" in by_project["unbounded"]["item budgets"].detail
    assert by_project["bounded"]["item budgets"].state == OK
    assert "3600s wall clock" in by_project["bounded"]["item budgets"].detail
    # A declared spend ceiling brings its own caveat, because it can only be
    # enforced over calls whose price is known.
    assert "spend ceiling enforceability" in by_project["bounded"]
    assert "item budgets" in by_project["unbounded"]
    assert "spend ceiling enforceability" not in by_project["unbounded"]


def test_a_budget_stop_does_not_consume_an_attempt(repo: Path, tmp_path: Path) -> None:
    """The item did not fail and did not exhaust its ladder — a policy stopped
    it. Spending an attempt on that would retire sound work."""
    clock = Clock()
    executor, queue, _ = build(repo, tmp_path / "a", clock, max_seconds=1.0)
    add_item(queue)
    clock.per_read = 100.0
    executor.run_once()

    record = queue.get("T1")
    assert record is not None
    assert record.state == BLOCKED
    assert record.attempts == 0
