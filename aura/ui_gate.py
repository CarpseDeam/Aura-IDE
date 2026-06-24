"""Gate evaluation for UI contract assertions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from aura.ui_contract import check_contract


@dataclass(frozen=True)
class UiGateVerdict:
    severity: str
    summary: str
    report: object | None


def evaluate_ui_contract(contract_path: Path, artifact_path: Path) -> UiGateVerdict:
    """Evaluate a UI contract against a UI-tree snapshot.

    Returns a UiGateVerdict with severity one of "block", "warn", "none".
    """
    if not contract_path.exists():
        return UiGateVerdict("none", "no UI contract declared", None)

    try:
        if not artifact_path.exists():
            return UiGateVerdict(
                "block",
                "UI contract declared but no readable snapshot produced"
                " — an artifact is required.",
                None,
            )
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return UiGateVerdict(
            "block",
            "UI contract declared but no readable snapshot produced"
            " — an artifact is required.",
            None,
        )

    report = check_contract(artifact, contract)

    if report.failed > 0:
        details = []
        for f in report.findings:
            if f.status == "fail" and len(details) < 5:
                details.append(f.detail)
        detail_str = "; ".join(details)
        summary = (
            f"UI contract failed: {report.failed} assertion(s) failed."
        )
        if detail_str:
            summary += f" {detail_str}"
        return UiGateVerdict("block", summary, report)

    if report.inconclusive > 0:
        summary = (
            f"UI contract: {report.inconclusive} assertion(s) inconclusive"
        )
        return UiGateVerdict("warn", summary, report)

    return UiGateVerdict(
        "none",
        f"UI contract passed ({report.passed} assertion(s))",
        report,
    )
