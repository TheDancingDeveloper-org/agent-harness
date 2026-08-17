from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from agent_harness.plan_contract import PlanContractError, parse_plan_contract

MANIFEST = """version = 1

[project]
key = "demo"
name = "Demo"

[repository]
base_ref = "main"
integration_ref = "harness/demo"

[agents]
role_runner = "runner"
required_roles = ["implementer", "reviewer"]
max_workers = 1
max_attempts = 2
max_item_seconds = 60
max_item_spend_usd = 0.0
max_hold_seconds = 120

[execution]
backend = "fake"
network = "none"
toolchains = ["Python 3.12"]
system_packages = []

[execution.image]
strategy = "existing"
reference = "example/runner@sha256:abc"

[execution.limits]
command_timeout_seconds = 30
memory = "1g"
cpus = "1"
pids = 64
user = "1000:1000"
rootfs_read_only = true
tmpfs_size = "64m"

[[execution.probes]]
name = "python"
command = ["python", "--version"]

[checks]
item = [["python", "-m", "pytest"]]
integration = [["python", "-m", "pytest", "-q"]]

[local_delivery]
backend = "local"
build = ["python", "-m", "build"]
build_context = "runner"
readiness = ["python", "ready.py"]
readiness_context = "host"
acceptance = ["python", "-m", "pytest", "acceptance"]
acceptance_context = "host"
required_host_tools = ["python"]
command_timeout_seconds = 60
readiness_timeout_seconds = 30
readiness_interval_seconds = 1
"""


def plan(*, manifest: str = MANIFEST, item: str = "depends on: none") -> str:
    sections = "\n\n".join(
        f"## {name}\ncontent"
        for name in (
            "Project identity and brief",
            "Scope and non-goals",
            "Rough architecture",
            "Data model",
            "GUI and interfaces",
            "Execution and local delivery",
            "Cross-cutting requirements",
            "Work items",
            "Local definition of done",
            "Assumptions, risks, and open questions",
        )
    )
    return (
        f"{sections}\n\n### W1: Build it\n\n**Deliverable:** a result\n\n"
        f"**Acceptance:**\n\n- it works\n\n{item}\n\n```harness\n{manifest}\n```\n"
    )


def test_complete_contract_keeps_argv_and_has_stable_canonical_form() -> None:
    contract = parse_plan_contract(plan())

    assert contract.project.key == "demo"
    assert contract.parsed.items[0].depends_on == []
    assert contract.checks.item == (("python", "-m", "pytest"),)
    assert contract.canonical_json() == contract.canonical_json()
    assert "parsed" not in contract.canonical()


def test_contract_domain_is_frozen() -> None:
    contract = parse_plan_contract(plan())
    with pytest.raises(AttributeError):
        contract.version = 2  # type: ignore[misc]
    assert replace(contract.project, key="other").key == "other"


@pytest.mark.parametrize(
    "change, expected",
    [
        (
            lambda text: text.replace("```harness", "```harness\n", 1).replace(
                "```\n", "```\n```harness\n", 1
            ),
            "exactly one",
        ),
        (
            lambda text: text.replace("## Data model", "## Data model\n## Data model", 1),
            "duplicates",
        ),
        (lambda text: text.replace("depends on: none", "", 1), "must declare dependencies"),
    ],
)
def test_contract_rejects_document_shape_errors(
    change: Callable[[str], str], expected: str
) -> None:
    with pytest.raises(PlanContractError, match=expected):
        parse_plan_contract(change(plan()))


def test_unknown_manifest_field_is_rejected() -> None:
    with pytest.raises(PlanContractError, match="unknown field"):
        parse_plan_contract(plan(manifest=MANIFEST + "\n[unexpected]\nvalue = true\n"))


def test_unsafe_mount_and_secret_scope_are_rejected() -> None:
    unsafe = MANIFEST + '\n[[execution.mounts]]\nsource = "cache"\ntarget = "/proc/x"\n'
    with pytest.raises(PlanContractError, match="unsafe mount"):
        parse_plan_contract(plan(manifest=unsafe))

    secret = (
        MANIFEST
        + '\n[[execution.secrets]]\nname = "TOKEN"\nsource = "environment"\nscope = "everywhere"\n'
    )
    with pytest.raises(PlanContractError, match="secret scope"):
        parse_plan_contract(plan(manifest=secret))


def test_bad_toml_reports_location() -> None:
    with pytest.raises(PlanContractError, match=r"line .*column"):
        parse_plan_contract(plan(manifest=MANIFEST + "\ninvalid = [\n"))
