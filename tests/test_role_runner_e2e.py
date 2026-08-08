"""Stage 1: a multi-turn implementer through the real executor and gates."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_harness import __main__ as cli
from agent_harness.adapters.minisweagent import RUNNER
from agent_harness.audit import AuditStore
from agent_harness.events import KINDS, MODEL_CALL, Event
from agent_harness.execution_environment import LocalExecutionEnvironment
from agent_harness.executor import Checks, Executor
from agent_harness.model_client import ModelClient, Response, Route
from agent_harness.pricing import Price, PriceTable
from agent_harness.work import BLOCKED, DONE, FAILED, Project, WorkRecord
from conftest import make_queue

DONE_COMMAND = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
APPROVED = "APPROVED\nVerified the requested change.\n\n4. Follow-ups\n- none"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def tool_reply(command: str, call: int) -> str:
    return json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "working",
                        "tool_calls": [
                            {
                                "id": f"call_{call}",
                                "type": "function",
                                "function": {
                                    "name": "bash",
                                    "arguments": json.dumps({"command": command}),
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )


class ScriptedLoop:
    def __init__(
        self,
        commands: Sequence[str],
        *,
        tokens: int = 0,
        usage_roles: Sequence[str] | None = None,
    ) -> None:
        self.commands = iter(commands)
        self.tokens = tokens
        self.usage_roles = set(usage_roles) if usage_roles is not None else None
        self.calls = 0
        self.implementer_messages: list[list[Mapping[str, Any]]] = []

    def __call__(
        self,
        route: Route,
        messages: Sequence[Mapping[str, Any]],
        options: Mapping[str, Any],
    ) -> Response:
        del options
        role = str(route.options.get("role") or route.model)
        if role == "planner":
            body = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "plan": "change the greeting",
                                        "targets": [
                                            {"path": "greeting.txt", "reason": "requested file"}
                                        ],
                                        "cannot_identify_target": None,
                                    }
                                )
                            }
                        }
                    ]
                }
            )
        elif role == "reviewer":
            body = json.dumps({"choices": [{"message": {"content": APPROVED}}]})
        else:
            self.calls += 1
            self.implementer_messages.append(list(messages))
            body = tool_reply(next(self.commands), self.calls)
        if self.tokens and (self.usage_roles is None or role in self.usage_roles):
            payload = json.loads(body)
            payload["usage"] = {
                "prompt_tokens": self.tokens,
                "completion_tokens": self.tokens,
            }
            body = json.dumps(payload)
        return Response(200, {}, body)


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "greeting.txt").write_text("hello world\n")
    check = repo / "check.sh"
    check.write_text(
        "\n".join(
            (
                "#!/bin/sh",
                "echo called >> check-runs.txt",
                "grep -q '^hello harness$' greeting.txt",
                "",
            )
        )
    )
    check.chmod(0o755)
    (repo / ".gitignore").write_text("check-runs.txt\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "initial")
    return repo


class RecordingEnvironmentFactory:
    """A host-backed test seam for executor/worktree wiring only."""

    name = "recording"
    api_version = 1
    version = "test"

    def __init__(self) -> None:
        self.worktree: Path | None = None
        self.closed = False
        self.git_is_self_contained = False

    def check(self) -> tuple[bool, str]:
        return True, "test backend available"

    def create(self, worktree: Path, **_: Any) -> LocalExecutionEnvironment:
        self.worktree = worktree
        self.git_is_self_contained = (worktree / ".git").is_dir()
        environment = LocalExecutionEnvironment(worktree)
        original_close = environment.close

        def close() -> None:
            self.closed = True
            original_close()

        environment.close = close  # type: ignore[method-assign]
        return environment


def test_loop_changes_feed_the_existing_checks_review_and_attempt_pipeline(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    queue = make_queue(str(tmp_path / "queue.sqlite"))
    queue.add(
        [
            WorkRecord(
                item_id="T1",
                title="Change the greeting",
                brief="Make greeting.txt say hello harness.",
            )
        ]
    )
    transport = ScriptedLoop(
        (
            "cat greeting.txt",
            "./check.sh || true",
            "printf 'hello harness\\n' > greeting.txt",
            "./check.sh",
            DONE_COMMAND,
        )
    )
    events: list[dict[str, Any]] = []
    client = ModelClient(
        roles={
            role: Route("scripted", "https://example.invalid", options={"role": role})
            for role in ("planner", "implementer", "reviewer")
        },
        transport=transport,
        on_event=events.append,
    )
    executor = Executor(
        queue,
        client,
        repo,
        checks=Checks(commands=[["./check.sh"]]),
        role_runner=RUNNER,
        push=False,
        on_event=events.append,
    )

    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == DONE, outcome.reason
    assert git(repo, "show", "harness/t1:greeting.txt") == "hello harness\n"
    # Once before the edit and once after it were feedback; the third run was
    # the harness's authoritative gate over the exact candidate it committed.
    assert (repo / "check-runs.txt").read_text().splitlines() == ["called"] * 3
    assert transport.calls == 5, "the implementer was collapsed back to one call"

    outcomes = [event.get("outcome") for event in events]
    assert "runner_started" in outcomes and "runner_finished" in outcomes
    assert "checks_passed" in outcomes and "review_approved" in outcomes
    model_events = [event for event in events if event.get("role") == "implementer"]
    assert len([event for event in model_events if event.get("outcome") == "ok"]) == 5
    assert all(event.get("project_id") == "default" for event in model_events)
    assert all(event.get("item_id") == "T1" for event in model_events)
    assert all(event.get("work_attempt") == 1 for event in model_events)

    history = queue.attempts_log.history("default", "T1")
    implementation = next(row for _, row in history if row.stage == "implemented")
    assert implementation.artefact["runner"] == "agent-loop"
    assert implementation.artefact["calls"] == 5
    item = queue.get("T1")
    assert item is not None and item.unpriced_calls == 7

    final_messages = "\n".join(
        str(message.get("content") or "") for message in transport.implementer_messages[-1]
    )
    assert "hello world" in final_messages, "the first observation did not reach later turns"


def test_selected_environment_gets_a_disposable_item_worktree(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    queue = make_queue(str(tmp_path / "queue.sqlite"))
    queue.add([WorkRecord(item_id="T1", title="Change the greeting", brief="Change greeting.txt.")])
    transport = ScriptedLoop(("printf 'hello harness\\n' > greeting.txt", DONE_COMMAND))
    client = ModelClient(
        roles={
            role: Route("scripted", "https://example.invalid", options={"role": role})
            for role in ("planner", "implementer", "reviewer")
        },
        transport=transport,
    )
    factory = RecordingEnvironmentFactory()

    outcome = Executor(
        queue,
        client,
        repo,
        checks=Checks(),
        role_runner=RUNNER,
        push=False,
        environment_factory=factory,
        environment_image="test-image",
    ).run_once()

    assert outcome is not None and outcome.state == DONE, outcome.reason if outcome else "missing"
    assert factory.worktree is not None and factory.worktree != repo
    assert factory.closed
    assert factory.git_is_self_contained
    assert not factory.worktree.exists()
    assert git(repo, "show", "harness/t1:greeting.txt") == "hello harness\n"
    assert str(factory.worktree) not in git(repo, "worktree", "list")


def test_selected_environment_reuses_and_reaps_a_stale_item_worktree(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    queue = make_queue(str(tmp_path / "queue.sqlite"))
    queue.add([WorkRecord(item_id="T1", title="Change the greeting", brief="Change greeting.txt.")])
    transport = ScriptedLoop(("printf 'hello harness\\n' > greeting.txt", DONE_COMMAND))
    client = ModelClient(
        roles={
            role: Route("scripted", "https://example.invalid", options={"role": role})
            for role in ("planner", "implementer", "reviewer")
        },
        transport=transport,
    )
    factory = RecordingEnvironmentFactory()
    executor = Executor(
        queue,
        client,
        repo,
        checks=Checks(),
        role_runner=RUNNER,
        push=False,
        environment_factory=factory,
        environment_image="test-image",
    )
    record = queue.get("T1")
    assert record is not None
    stale = executor._execution_tree_path(record)
    stale.mkdir(parents=True)
    (stale / "stale.txt").write_text("orphaned\n")

    outcome = executor.run_once()

    assert outcome is not None and outcome.state == DONE, outcome.reason if outcome else "missing"
    assert not stale.exists()
    assert not (stale / "stale.txt").exists()


def test_loop_events_can_be_written_to_the_append_only_audit_sink(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    queue = make_queue(str(tmp_path / "queue.sqlite"))
    queue.add([WorkRecord(item_id="T1", title="Change it", brief="Change greeting.txt.")])
    transport = ScriptedLoop(("printf 'changed\\n' > greeting.txt", DONE_COMMAND))
    raw: list[dict[str, Any]] = []
    audit = AuditStore(tmp_path / "audit.sqlite")

    def sink(event: dict[str, Any]) -> None:
        raw.append(event)
        known = {
            "ts",
            "kind",
            "worker",
            "role",
            "model",
            "endpoint",
            "outcome",
            "error_class",
            "latency_s",
        }
        data = {key: value for key, value in event.items() if key not in known}
        audit.append(
            [
                Event(
                    ts=float(event.get("ts", 0.0)),
                    kind=(str(event.get("kind")) if event.get("kind") in KINDS else MODEL_CALL),
                    source="role-runner-test",
                    worker=event.get("worker"),
                    role=event.get("role"),
                    model=event.get("model"),
                    endpoint=event.get("endpoint"),
                    outcome=event.get("outcome"),
                    error_class=event.get("error_class"),
                    latency_s=event.get("latency_s"),
                    data=data,
                )
            ]
        )

    client = ModelClient(
        roles={
            role: Route("scripted", "https://example.invalid", options={"role": role})
            for role in ("planner", "implementer", "reviewer")
        },
        transport=transport,
        on_event=sink,
    )
    outcome = Executor(
        queue,
        client,
        repo,
        role_runner=RUNNER,
        push=False,
        on_event=sink,
    ).run_once()

    assert outcome is not None and outcome.state == DONE
    assert audit.count() == len(raw)
    model_rows = [row for row in audit.recent(limit=100) if row["kind"] == MODEL_CALL]
    assert model_rows and all(json.loads(row["data"]).get("item_id") == "T1" for row in model_rows)


def test_a_new_file_created_by_the_loop_reaches_the_authoritative_pipeline(
    tmp_path: Path,
) -> None:
    """Untracked files are candidate work, not an empty implementation."""
    repo = repository(tmp_path)
    queue = make_queue(str(tmp_path / "queue.sqlite"))
    queue.add(
        [
            WorkRecord(
                item_id="T1",
                title="Add a farewell",
                brief="Create farewell.txt containing goodbye.",
            )
        ]
    )
    transport = ScriptedLoop(("printf 'goodbye\\n' > farewell.txt", DONE_COMMAND))
    client = ModelClient(
        roles={
            role: Route("scripted", "https://example.invalid", options={"role": role})
            for role in ("planner", "implementer", "reviewer")
        },
        transport=transport,
    )
    executor = Executor(
        queue,
        client,
        repo,
        checks=Checks(commands=[["test", "-f", "farewell.txt"]]),
        role_runner=RUNNER,
        push=False,
    )

    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == DONE, outcome.reason
    assert git(repo, "show", "harness/t1:farewell.txt") == "goodbye\n"


def test_a_policy_refusal_is_terminal_in_the_harness_path(tmp_path: Path) -> None:
    from agent_harness.guard import CommandGuard

    repo = repository(tmp_path)
    queue = make_queue(str(tmp_path / "queue.sqlite"))
    queue.add([WorkRecord(item_id="T1", title="Try it", brief="Make a change.")])
    transport = ScriptedLoop(("rm -rf .", DONE_COMMAND))
    client = ModelClient(
        roles={
            role: Route("scripted", "https://example.invalid", options={"role": role})
            for role in ("planner", "implementer", "reviewer")
        },
        transport=transport,
    )
    executor = Executor(
        queue,
        client,
        repo,
        checks=Checks(guard=CommandGuard(refusals=("rm",))),
        role_runner=RUNNER,
        push=False,
    )

    outcome = executor.run_once()

    assert outcome is not None
    assert outcome.state == "blocked"
    assert outcome.reason_kind == "command_blocked"
    assert transport.calls == 1, "a terminal refusal was returned as another loop turn"
    assert git(repo, "branch", "--list", "harness/t1").strip() == ""


def test_a_spend_ceiling_stops_the_loop_inside_the_harness_path(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    queue = make_queue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="default", name="Default", max_item_spend_usd=0.003))
    queue.add([WorkRecord(item_id="T1", title="Never finish", brief="Keep working.")])
    transport = ScriptedLoop(["echo working"] * 20, tokens=1_000)
    client = ModelClient(
        roles={
            role: Route("scripted", "https://example.invalid", options={"role": role})
            for role in ("planner", "implementer", "reviewer")
        },
        transport=transport,
        prices=PriceTable(version="test", prices={"scripted": Price(1.0, 1.0)}),
    )
    executor = Executor(queue, client, repo, role_runner=RUNNER, push=False)

    outcome = executor.run_once()

    assert outcome is not None and outcome.state == BLOCKED
    assert outcome.reason_kind == "item_spend"
    assert transport.calls == 1, "the loop crossed its remaining item budget and kept calling"
    item = queue.get("T1")
    assert item is not None
    assert item.spend_usd == pytest.approx(0.004), "planner and loop calls were not combined"


def test_a_loop_that_never_terminates_exhausts_its_steps_in_the_harness_path(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    queue = make_queue(str(tmp_path / "queue.sqlite"))
    queue.add([WorkRecord(item_id="T1", title="Never finish", brief="Keep working.")])
    transport = ScriptedLoop(["echo working"] * 20)
    client = ModelClient(
        roles={
            role: Route("scripted", "https://example.invalid", options={"role": role})
            for role in ("planner", "implementer", "reviewer")
        },
        transport=transport,
    )
    executor = Executor(
        queue,
        client,
        repo,
        role_runner=RUNNER,
        runner_step_limit=3,
        push=False,
    )

    outcome = executor.run_once()

    assert outcome is not None and outcome.state == FAILED
    assert "step_limit" in outcome.reason
    assert transport.calls == 3
    assert git(repo, "branch", "--list", "harness/t1").strip() == ""


def test_an_unpriced_prior_attempt_does_not_reenable_the_loop_spend_limit(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    queue = make_queue(str(tmp_path / "queue.sqlite"))
    queue.add_project(Project(project_id="default", name="Default", max_item_spend_usd=0.005))
    queue.add([WorkRecord(item_id="T1", title="Keep working", brief="Keep working.")])
    transport = ScriptedLoop(
        (
            "printf 'first\\n' > result.txt",
            DONE_COMMAND,
            "printf 'second\\n' > result.txt",
            DONE_COMMAND,
        ),
        tokens=500,
        usage_roles=("implementer",),
    )
    client = ModelClient(
        roles={
            role: Route("scripted", "https://example.invalid", options={"role": role})
            for role in ("planner", "implementer", "reviewer")
        },
        transport=transport,
        prices=PriceTable(version="test", prices={"scripted": Price(1.0, 1.0)}),
    )
    executor = Executor(queue, client, repo, role_runner=RUNNER, push=False)

    first = executor.run_once()
    assert first is not None and first.state == DONE
    queue.requeue("T1")
    # The scripted client reports no usage for the first attempt in this
    # fixture's planner/reviewer responses, so the item carries an unpriced
    # history even though the loop's own reply was priced.
    record = queue.get("T1")
    assert record is not None and record.unpriced_calls > 0

    second = executor.run_once()

    assert second is not None and second.state == DONE
    assert transport.calls == 4


def test_run_selects_the_installed_loop_and_delivers_through_the_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository(tmp_path)
    database = tmp_path / "queue.sqlite"
    queue = make_queue(str(database))
    queue.add([WorkRecord(item_id="T1", title="Change it", brief="Change the greeting.")])
    transport = ScriptedLoop(("printf 'hello harness\\n' > greeting.txt", DONE_COMMAND))
    monkeypatch.setenv("HARNESS_API_KEY", "test-key")
    monkeypatch.setattr(cli, "_http_transport", lambda _key: transport)

    code = cli.main(
        [
            "--db",
            str(database),
            "run",
            "--role-runner",
            "agent-loop",
            "--work",
            str(repo),
            "--no-push",
            "--planner",
            "planner",
            "--implementer",
            "implementer",
            "--reviewer",
            "reviewer",
            "--endpoint",
            "https://example.invalid",
            "--check",
            "./check.sh",
            "--events",
            str(tmp_path / "events.jsonl"),
            "--limit",
            "1",
        ]
    )

    assert code == 0
    assert git(repo, "show", "harness/t1:greeting.txt") == "hello harness\n"
    assert queue.get_setting("role_runner") == "agent-loop"
