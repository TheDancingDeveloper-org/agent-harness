"""The generic role-runner contract and its installed-metadata boundary."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agent_harness import role_runners
from agent_harness.doctor import diagnose
from agent_harness.work import WorkQueue


def test_the_shipped_runner_is_discoverable_without_an_adapter_import() -> None:
    assert "agent-loop" in role_runners.names()


def test_the_selected_runner_reports_a_compatible_contract_and_version() -> None:
    runner = role_runners.resolve("agent-loop")
    assert runner.api_version == role_runners.API_VERSION
    detail = role_runners.describe(runner)
    assert "compatible" in detail
    assert "unknown" not in detail


def test_an_unknown_runner_is_a_named_preflight_failure() -> None:
    ok, detail = role_runners.probe("missing")
    assert not ok
    assert "missing" in detail
    assert role_runners.ENTRY_POINT_GROUP in detail


def test_core_knows_no_runner_adapter_module_path() -> None:
    source = Path(role_runners.__file__ or "").read_text()
    assert "agent_harness.adapters." not in source


def test_an_incompatible_runner_is_refused_before_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OldRunner:
        name = "old"
        api_version = 0
        version = "1"

        def run(self, request: object) -> None:
            raise AssertionError("an incompatible runner was called")

    monkeypatch.setattr(role_runners, "_declared_targets", lambda: {"old": "fixture:RUNNER"})
    module = type("M", (), {"RUNNER": OldRunner})()
    with (
        patch("importlib.import_module", return_value=module),
        pytest.raises(role_runners.IncompatibleRoleRunner, match="contract version"),
    ):
        role_runners.resolve("old")


def test_doctor_reports_the_selected_runner_from_installed_metadata(tmp_path: Path) -> None:
    queue = WorkQueue(str(tmp_path / "queue.sqlite"))
    queue.set_setting(role_runners.SETTING_KEY, "agent-loop")

    report = diagnose(queue, [])

    finding = next(item for item in report.environment if item.name == "role runner")
    assert finding.state == "ok"
    assert "agent-loop" in finding.detail
