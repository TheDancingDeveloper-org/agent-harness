"""Pure, deterministic validation of the generic plan contract.

Validation is deliberately separate from admission and execution.  It reads
text and returns findings; it never writes queue state, calls a model, or
loads an adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .plan import parse_plan
from .plan_contract import REQUIRED_SECTIONS, PlanContractError, parse_plan_contract


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    remediation: str
    path: str
    line: int | None = None
    column: int | None = None
    pointer: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "remediation": self.remediation,
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "pointer": self.pointer,
        }


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    findings: tuple[Finding, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "findings": [finding.as_dict() for finding in self.findings]}


def _finding(
    code: str,
    message: str,
    remediation: str,
    *,
    path: str = "plan",
    line: int | None = None,
    column: int | None = None,
    pointer: str | None = None,
    severity: str = "error",
) -> Finding:
    return Finding(code, severity, message, remediation, path, line, column, pointer)


def _contract_finding(exc: PlanContractError) -> Finding:
    message = str(exc)
    line = column = None
    words = message.split()
    if "line" in words:
        try:
            line = int(words[words.index("line") + 1].rstrip(","))
            column = int(words[words.index("column") + 1].rstrip(":"))
        except (ValueError, IndexError):
            pass
    if "fence" in message:
        code = "PLAN-D001"
        remediation = "Keep exactly one closed fenced `harness` block in the plan."
    elif "required sections" in message:
        code = "PLAN-D002"
        remediation = "Add each required level-two section exactly once."
    elif "unknown field" in message or "manifest" in message:
        code = "PLAN-M001"
        remediation = "Use only fields defined by the version-one manifest contract."
    elif "TOML" in message:
        code = "PLAN-M002"
        remediation = "Fix the TOML syntax at the reported location."
    elif "mount" in message or "secret" in message or "ref" in message:
        code = "PLAN-P001"
        remediation = "Correct the path, reference, or secret declaration."
    elif "item" in message:
        code = "PLAN-W001"
        remediation = (
            "Give every work item a deliverable, acceptance list, and dependency declaration."
        )
    else:
        code = "PLAN-M003"
        remediation = "Correct the manifest value named by the diagnostic."
    return _finding(code, message, remediation, line=line, column=column)


def validate_plan(text: str, *, source: str = "plan") -> ValidationReport:
    """Return every independently discoverable finding in stable order."""
    findings: list[Finding] = []
    try:
        contract = parse_plan_contract(text)
    except PlanContractError as exc:
        finding = _contract_finding(exc)
        findings.append(
            Finding(
                finding.code,
                finding.severity,
                finding.message,
                finding.remediation,
                source,
                finding.line,
                finding.column,
                finding.pointer,
            )
        )
        contract = None

    parsed = parse_plan(text)
    lines = text.splitlines()
    heading_names = [
        " ".join(line[3:].split()).casefold()
        for line in lines
        if line.startswith("## ") and not line.startswith("### ")
    ]
    required = {name.casefold() for name in REQUIRED_SECTIONS}
    for name in sorted(required - set(heading_names)):
        findings.append(
            _finding(
                "PLAN-D002",
                f"required section is missing: {name}",
                f"Add a level-two `{name}` section, or state Not applicable with a reason.",
                path=source,
            )
        )
    for name in sorted(name for name in required if heading_names.count(name) > 1):
        findings.append(
            _finding(
                "PLAN-D003",
                f"required section is duplicated: {name}",
                "Keep exactly one copy of each required level-two section.",
                path=source,
            )
        )
    if text.count("```harness") != 1 or text.count("```harness") != text.count("```harness\n"):
        findings.append(
            _finding(
                "PLAN-D001",
                "plan must contain exactly one harness manifest fence",
                "Keep exactly one closed fenced `harness` block in the plan.",
                path=source,
            )
        )
    if not parsed.items:
        findings.append(
            _finding(
                "PLAN-W002",
                "the plan contains no recognized work items",
                "Add work items using a supported heading, checkbox, or table form.",
                path=source,
            )
        )
    for item_id, duplicate_lines in sorted(parsed.duplicate_ids().items()):
        findings.append(
            _finding(
                "PLAN-W003",
                f"work item {item_id} is declared more than once",
                "Keep one authoritative declaration for each work-item id.",
                path=source,
                line=duplicate_lines[0],
                pointer=f"items.{item_id}",
            )
        )
    for item in parsed.items:
        if not item.body:
            findings.append(
                _finding(
                    "PLAN-W001",
                    f"work item {item.id} has no brief",
                    "Add a non-empty Deliverable and Acceptance section.",
                    path=source,
                    line=item.line,
                    pointer=f"items.{item.id}",
                )
            )
        if "**Deliverable:**" not in item.body:
            findings.append(
                _finding(
                    "PLAN-W004",
                    f"work item {item.id} has no Deliverable field",
                    "Add an observable `**Deliverable:**` outcome.",
                    path=source,
                    line=item.line,
                    pointer=f"items.{item.id}.deliverable",
                )
            )
        if "**Acceptance:**" not in item.body:
            findings.append(
                _finding(
                    "PLAN-W005",
                    f"work item {item.id} has no Acceptance list",
                    "Add a non-empty `**Acceptance:**` list.",
                    path=source,
                    line=item.line,
                    pointer=f"items.{item.id}.acceptance",
                )
            )
    if "[[execution.mounts]]" in text:
        for line_number, line in enumerate(lines, 1):
            if line.strip().startswith("target = ") and '"/proc' in line:
                findings.append(
                    _finding(
                        "PLAN-P001",
                        "execution mount target enters a protected system path",
                        "Use a safe absolute runner path outside protected system paths.",
                        path=source,
                        line=line_number,
                        pointer="execution.mounts.target",
                    )
                )
    if 'scope = "everywhere"' in text:
        findings.append(
            _finding(
                "PLAN-P002",
                "secret scope is not one of provisioning, checks, or delivery",
                "Choose one explicit secret scope.",
                path=source,
                pointer="execution.secrets.scope",
            )
        )
    report = parsed.dependency_report()
    for item_id, tokens in sorted(report.unresolved.items()):
        findings.append(
            _finding(
                "PLAN-G001",
                f"{item_id} names unresolved local dependency: {', '.join(tokens)}",
                "Add the missing local item or declare the dependency with its proper target kind.",
                path=source,
                pointer=f"items.{item_id}.depends_on",
            )
        )
    for item_id, tokens in sorted(report.malformed.items()):
        findings.append(
            _finding(
                "PLAN-G002",
                f"{item_id} has malformed dependency: {', '.join(tokens)}",
                "Use a supported dependency token grammar.",
                path=source,
                pointer=f"items.{item_id}.depends_on",
            )
        )
    for cycle in report.cycles:
        findings.append(
            _finding(
                "PLAN-G003",
                "dependency cycle: " + " -> ".join([*cycle, cycle[0]]),
                "Remove at least one required edge from the cycle.",
                path=source,
            )
        )
    for arrow_line, text_line in report.unattached_arrows:
        findings.append(
            _finding(
                "PLAN-G004",
                f"dependency declaration names no work item: {text_line}",
                "Attach the edge to an item declared in this plan.",
                path=source,
                line=arrow_line,
            )
        )
    if contract is not None:
        # The variable is intentionally retained as a proof that successful
        # contract parsing occurred; no adapter or execution is invoked here.
        del contract
    findings.sort(key=lambda item: (item.line is None, item.line or 0, item.column or 0, item.code))
    return ValidationReport(not any(item.severity == "error" for item in findings), tuple(findings))


def validate_plan_file(path: str | Path) -> ValidationReport:
    target = Path(path)
    if not target.is_file():
        return ValidationReport(
            False,
            (
                _finding(
                    "PLAN-D000",
                    f"plan file does not exist: {target}",
                    "Provide a readable local plan path.",
                    path=str(target),
                ),
            ),
        )
    return validate_plan(target.read_text(encoding="utf-8"), source=str(target))
