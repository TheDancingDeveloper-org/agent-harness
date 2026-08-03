"""The deployment contract, checked against the thing it describes.

A deployment document is read once, by someone who cannot yet run the system,
and is trusted completely. That makes it the worst place for a stale flag or a
blocker name that no longer exists: the reader has no way to tell it is wrong,
and the failure lands at deploy time.

So the parts of `docs/DEPLOYMENT.md` that name something real -- the `serve`
flags, the readiness fields, the blocker names an operator is told to fix --
are asserted against the CLI and the preflight that produce them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from agent_harness.__main__ import main
from agent_harness.preflight import RoleReachability, preflight_project
from agent_harness.schemas import ExecutionReadiness, ProjectReadiness, ReadinessProbe
from agent_harness.work import Project

DOC = (Path(__file__).resolve().parent.parent / "docs" / "DEPLOYMENT.md").read_text()


def serve_help(capsys: Any) -> str:
    with pytest.raises(SystemExit):
        main(["serve", "--help"])
    return str(capsys.readouterr().out)


def test_every_serve_flag_the_document_uses_exists(capsys: Any) -> None:
    """The document's whole purpose is to be copied into a unit file."""
    # `--flag` at the start of a continued shell line. The `-` in a markdown
    # rule is not a flag, so the pattern insists on letters.
    documented = set(re.findall(r"^\s*(--[a-z]+(?:-[a-z]+)*)\b", DOC, re.MULTILINE))
    assert documented, "the flags stopped being extractable; fix this test, not the doc"
    help_text = serve_help(capsys)
    missing = sorted(flag for flag in documented if flag not in help_text)
    assert not missing, f"documented but not accepted by `serve`: {missing}"


def test_the_supervised_mode_is_reached_the_way_the_document_says(capsys: Any) -> None:
    help_text = serve_help(capsys)
    assert "--session-host" in help_text
    # And the help itself says what its absence means, so an operator reading
    # `--help` instead of the document reaches the same conclusion.
    assert "monitoring-only" in help_text


def test_every_readiness_field_the_document_shows_is_in_the_schema() -> None:
    """The sample response is what a reader will build their `jq` against."""
    top = set(ExecutionReadiness.model_fields) | {"projects"}
    for name in ("mode", "ready_to_start", "workers", "session_host", "reviewer", "projects"):
        assert name in top, f"the document shows `{name}`, which the schema does not have"
    assert {"configured", "ok", "detail"} <= set(ReadinessProbe.model_fields)
    assert {"project_id", "ready_to_start", "summary", "blockers", "warnings"} <= set(
        ProjectReadiness.model_fields
    )


def test_every_blocker_the_document_tells_you_to_fix_can_actually_occur() -> None:
    """A remedy table naming a check that no longer exists sends an operator
    looking for something they will never see."""
    table = DOC.split("## When readiness says no", 1)[1]
    documented = set(re.findall(r"^\| `([a-z ]+)` \|", table, re.MULTILINE))
    assert documented, "the remedy table stopped being extractable"

    everything_missing = preflight_project(
        Project(project_id="p", name="P", repo=None, work_dir="/w", checks=[]),
        has_fleet=False,
        reviewer_route=None,
        reviewer_independent=(False, "same vendor"),
        role_probe=lambda: RoleReachability(silent={"reviewer": "m returned HTTP 504"}),
        session_host=lambda: (False, "refused"),
        checks_probe=lambda: (False, "base check failed"),
        disk_probe=lambda path, floor: (False, "disk is full"),
        git_probe=lambda path: (False, "missing checkout"),
    )
    real = {c.name for c in everything_missing.checks}
    assert documented <= real, f"documented blockers that cannot occur: {sorted(documented - real)}"


def test_the_warnings_named_as_non_blocking_really_are() -> None:
    """The document tells an operator these will not stop a start. If one
    became blocking, the advice would be actively wrong."""
    report = preflight_project(
        Project(project_id="p", name="P", repo="o/r", work_dir="/w", checks=[]),
        has_fleet=True,
        reviewer_route={"model": "m"},
        reviewer_independent=(False, "same vendor"),
        role_probe=lambda: RoleReachability(slow={"reviewer": "m answered after 14s"}),
        git_probe=lambda path: (True, path),
        github_probe=lambda repo: (True, repo),
        disk_probe=lambda path, floor: (True, "100 GiB free"),
    )
    assert report.ready, "a start would be refused for reasons the document calls warnings"
    assert {"checks", "reviewer independence", "model latency"} <= {c.name for c in report.warnings}
