"""Packaging checks for the first-party browser application."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path


def test_ui_resources_are_in_the_installed_package() -> None:
    """The distribution must carry the UI; runtime must not reach a CDN."""
    import importlib.resources

    package = importlib.resources.files("agent_harness")
    assert package.joinpath("templates", "base.html").is_file()
    assert package.joinpath("templates", "graph.html").is_file()
    assert package.joinpath("static", "app.css").is_file()
    assert package.joinpath("static", "app.js").is_file()
    assert package.joinpath("static", "htmx.min.js").is_file()


def test_wheel_contains_templates_and_static_assets(tmp_path: Path) -> None:
    """A built wheel remains self-contained after installation."""
    root = Path(__file__).resolve().parents[1]
    dist = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
    assert "agent_harness/templates/base.html" in names
    assert "agent_harness/templates/graph.html" in names
    assert "agent_harness/static/app.css" in names
    assert "agent_harness/static/app.js" in names
    assert "agent_harness/static/htmx.min.js" in names
