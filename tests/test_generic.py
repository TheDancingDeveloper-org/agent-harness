"""Nothing workload-specific may sit on the execution path.

The harness was built for one consumer and then made generic. "Generic" is
easy to claim and easy to lose: one convenient import from the adapter into
the executor, and the next workload inherits assumptions nobody remembers
making.

So it is asserted rather than believed. `T42` asks for this to be verified by
grep; a test is a grep that runs every time, on a machine that does not forget.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "agent_harness"

#: The modules a work item passes through, from claim to pull request.
EXECUTION_PATH = [
    "work.py",
    "graph.py",
    "fleet.py",
    "role_runners.py",
    "session_executor.py",
    "executor.py",
    # The refusal list is on the path an item passes through, and a refusal
    # list is exactly where one workload's commands would get hardcoded.
    "guard.py",
    "model_client.py",
    "providers.py",
    "protocols.py",
    "pricing.py",
    "plan.py",
    "github.py",
    "preflight.py",
    "audit.py",
    "reconcile.py",
    "maintenance.py",
    "reaper.py",
    "inception.py",
]


def imports_of(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def test_no_execution_module_imports_an_adapter() -> None:
    """Adapters read one workload's logs. Importing one from the path that
    runs work is how a generic harness quietly stops being one."""
    offenders = {}
    for name in EXECUTION_PATH:
        path = SRC / name
        if not path.exists():
            continue
        bad = {i for i in imports_of(path) if "adapter" in i}
        if bad:
            offenders[name] = sorted(bad)
    assert not offenders, f"the execution path imports adapters: {offenders}"


def test_no_execution_module_names_a_specific_workload() -> None:
    """A workload's name appearing on the path is the tell.

    Checked as a word, not a substring, so a docstring mentioning it in prose
    is fine -- the point is that no code branches on it.
    """
    import re

    offenders = {}
    for name in EXECUTION_PATH:
        path = SRC / name
        if not path.exists():
            continue
        source = path.read_text()
        # Strip docstrings and comments: explaining a past mistake is not the
        # same as depending on it.
        code = "\n".join(
            line.split("#", 1)[0]
            for line in source.splitlines()
            if not line.strip().startswith("#")
        )
        hits = {w for w in ("oxidex", "ngms") if re.search(rf"\b{w}\b", code, re.IGNORECASE)}
        if hits:
            offenders[name] = sorted(hits)
    assert not offenders, f"workload names appear in execution code: {offenders}"


def test_the_adapter_is_reachable_only_through_an_explicit_choice() -> None:
    """Opt-in means the CLI names it, not that the core imports it."""
    main = (SRC / "__main__.py").read_text()
    assert "from .adapters import oxidex" in main, "the adapter should still be usable"
    # ...and only inside the branch that asks for it.
    assert 'args.adapter == "oxidex"' in main


def test_the_core_package_does_not_import_adapters_at_all() -> None:
    """Importing agent_harness must not drag a workload's log format in."""
    init = SRC / "__init__.py"
    assert "adapters" not in imports_of(init), "the package root imports an adapter"


def test_no_module_on_the_path_names_a_vendor_preset() -> None:
    """A route preset is resolved by name at runtime, from metadata. An
    importable module path to an adapter, written into core as a string, would
    be an import wearing a string's clothes: lazy, but still core knowing what
    a particular vendor is called.

    Prose is fine — pointing a reader at the worked example is the opposite of
    depending on it — so only dotted module paths count.
    """
    import re

    offenders = {}
    for name in EXECUTION_PATH:
        path = SRC / name
        if not path.exists():
            continue
        hits = re.findall(r"[\w.]*adapters\.\w+", path.read_text())
        if hits:
            offenders[name] = sorted(set(hits))
    assert not offenders, f"core names an adapter module: {offenders}"


def test_the_shipped_presets_are_declared_rather_than_imported() -> None:
    """They reach the harness through the distribution's entry points, which is
    the same door a third party's package uses. If it were not, "add a vendor
    without editing core" would be true only for vendors we ship."""
    import tomllib

    manifest = tomllib.loads((SRC.parents[1] / "pyproject.toml").read_text())
    declared = manifest["project"]["entry-points"]["agent_harness.route_presets"]
    assert set(declared) == {"chat-completions", "claw-bay"}
    assert all(value.startswith("agent_harness.adapters.") for value in declared.values())


def test_the_shipped_dependency_resolvers_are_declared_the_same_way() -> None:
    """Stage G's resolver lookup and Stage B's preset lookup were written apart
    and reached the same place: a name in core, a module in metadata. Merging
    them made the difference visible, so they now use one door rather than two
    conventions that happen to agree today."""
    import tomllib

    manifest = tomllib.loads((SRC.parents[1] / "pyproject.toml").read_text())
    declared = manifest["project"]["entry-points"]["agent_harness.dependency_resolvers"]
    assert set(declared) == {"github-issue"}
    assert all(value.startswith("agent_harness.adapters.") for value in declared.values())


def test_the_shipped_role_runners_are_declared_the_same_way() -> None:
    """A runner is an adapter even when it is the primary execution path."""
    import tomllib

    manifest = tomllib.loads((SRC.parents[1] / "pyproject.toml").read_text())
    declared = manifest["project"]["entry-points"]["agent_harness.role_runners"]
    assert set(declared) == {"agent-loop"}
    assert all(value.startswith("agent_harness.adapters.") for value in declared.values())
