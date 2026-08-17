from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_harness.__main__ import main
from agent_harness.plan_validation import validate_plan, validate_plan_file


def valid_plan() -> str:
    return Path("examples/PLAN.md").read_text(encoding="utf-8")


def test_valid_example_has_no_findings() -> None:
    result = validate_plan(valid_plan(), source="example.md")

    assert result.valid
    assert result.findings == ()
    assert result.as_dict() == {"valid": True, "findings": []}


def test_validator_reports_dependency_errors_in_stable_order() -> None:
    text = valid_plan().replace("W1 -> W3", "W9 -> W3\nW3 -> W3\nnot-an-edge")
    result = validate_plan(text, source="broken.md")

    assert not result.valid
    assert [finding.code for finding in result.findings] == [
        "PLAN-G004",
        "PLAN-G001",
        "PLAN-G003",
    ]
    assert result.as_dict()["findings"][0]["path"] == "broken.md"


def test_validator_reports_manifest_failure_without_calling_external_systems() -> None:
    text = valid_plan().replace("version = 1", "version = 2", 1)

    result = validate_plan(text)

    assert not result.valid
    assert result.findings[0].code == "PLAN-M001"
    assert all(value is not None for value in result.as_dict()["findings"][0])


def test_missing_file_is_a_deterministic_finding(tmp_path: Path) -> None:
    result = validate_plan_file(tmp_path / "missing.md")

    assert not result.valid
    assert result.findings[0].code == "PLAN-D000"
    assert "missing.md" in result.findings[0].message


def test_cli_validate_supports_nested_spelling_and_json(capsys: Any, tmp_path: Path) -> None:
    path = tmp_path / "PLAN.md"
    path.write_text(valid_plan(), encoding="utf-8")

    assert main(["plan", "validate", str(path), "--json"]) == 0
    assert '"valid": true' in capsys.readouterr().out
