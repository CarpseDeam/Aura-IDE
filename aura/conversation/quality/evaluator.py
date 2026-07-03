from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from aura.code_intel.audit import audit_changed_files
from aura.conversation.quality.diff_parser import parse_unified_diff
from aura.conversation.quality.models import (
    QualityFinding,
    QualitySeverity,
    WorkerQualityDecision,
)
from aura.conversation.quality.path_policy import normalize_changed_files
from aura.conversation.quality.production_checks import (
    placeholder_production_code_findings,
    swallowed_exception_findings,
    temporary_production_code_findings,
    unexpected_production_file_findings,
)
from aura.conversation.quality.structural_checks import (
    duplicate_changed_string_findings,
    large_diff_findings,
    protected_control_flow_findings,
)


def evaluate_worker_quality(
    workspace_root: Path,
    changed_files: list[str],
    diff_text: str,
    validation_passed: bool,
    *,
    expected_files: list[str] | None = None,
) -> WorkerQualityDecision:
    normalized_files = normalize_changed_files(changed_files)
    findings: list[QualityFinding] = []

    for audit_finding in audit_changed_files(Path(workspace_root), normalized_files):
        severity = _coerce_severity(getattr(audit_finding, "severity", "warning"))
        if severity not in {"warning", "error"}:
            continue
        kind = str(getattr(audit_finding, "kind", "audit"))
        file = str(getattr(audit_finding, "file", ""))
        line = getattr(audit_finding, "line", None)
        findings.append(
            QualityFinding(
                kind=kind,
                severity=severity,
                file=file,
                line=line if isinstance(line, int) else None,
                message=str(getattr(audit_finding, "message", "")),
                suggested_action=_audit_suggested_action(kind, severity),
                evidence={
                    "source": "audit_changed_files",
                    "validation_passed": validation_passed,
                },
            )
        )

    diff_files = parse_unified_diff(diff_text)
    findings.extend(unexpected_production_file_findings(normalized_files, expected_files))
    findings.extend(temporary_production_code_findings(diff_files))
    findings.extend(placeholder_production_code_findings(diff_files))
    findings.extend(swallowed_exception_findings(diff_files))
    findings.extend(duplicate_changed_string_findings(diff_files))
    findings.extend(large_diff_findings(diff_files))
    findings.extend(protected_control_flow_findings(diff_files))

    hard_block = any(f.severity == "error" for f in findings)
    needs_cleanup = any(f.severity == "warning" for f in findings)
    instruction = ""
    if needs_cleanup and not hard_block:
        instruction = _cleanup_instruction(findings)
    return WorkerQualityDecision(
        ok=not hard_block and not needs_cleanup,
        hard_block=hard_block,
        needs_cleanup=needs_cleanup,
        findings=findings,
        instruction=instruction,
    )


def findings_to_receipt(findings: list[QualityFinding]) -> list[dict[str, Any]]:
    return [asdict(finding) for finding in findings]


def _coerce_severity(value: str) -> QualitySeverity:
    if value == "error":
        return "error"
    if value == "info":
        return "info"
    return "warning"


def _audit_suggested_action(kind: str, severity: QualitySeverity) -> str:
    if kind == "removed_export" and severity == "error":
        return "Restore the removed public symbol or update all importers before final release."
    if kind == "removed_export":
        return "Confirm the removal is intentional and update any same-file references."
    if kind == "stale_reference":
        return "Update the stale reference or restore the referenced symbol."
    if kind == "parse_failure":
        return "Fix the parse failure and rerun the focused validation command."
    if kind == "unresolved_dependency":
        return "Fix the import path or add the missing workspace dependency."
    return "Patch the reported audit finding and rerun the focused validation command."


def _cleanup_instruction(findings: list[QualityFinding]) -> str:
    lines = [
        "Do not redesign.",
        "Do not broaden scope.",
        "Patch only the listed findings.",
        "Preserve behavior.",
        "Rerun the smallest relevant validation.",
        "Finish only after it passes.",
        "",
        "Findings:",
    ]
    for finding in findings:
        if finding.severity != "warning":
            continue
        location = finding.file or "<workspace>"
        if finding.line is not None:
            location = f"{location}:{finding.line}"
        lines.append(
            f"- {location} - {finding.message} - {finding.suggested_action}"
        )
    return "\n".join(lines)
