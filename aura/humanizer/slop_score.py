from __future__ import annotations

from aura.humanizer.slop_model import SlopIssue, SlopSeverity


SEVERITY_ORDER = {
    SlopSeverity.CRITICAL: 0,
    SlopSeverity.HIGH: 1,
    SlopSeverity.MEDIUM: 2,
    SlopSeverity.LOW: 3,
}

SEVERITY_WEIGHTS = {
    SlopSeverity.CRITICAL: 10.0,
    SlopSeverity.HIGH: 5.0,
    SlopSeverity.MEDIUM: 2.0,
    SlopSeverity.LOW: 1.0,
}


def calculate_slop_score(issues: list[SlopIssue]) -> float:
    score = sum(SEVERITY_WEIGHTS.get(issue.severity, 1.0) for issue in issues)
    return min(score, 100.0)


def slop_status(score: float) -> str:
    if score >= 70:
        return "critical_deficit"
    if score >= 50:
        return "inflated_signal"
    if score >= 30:
        return "suspicious"
    return "clean"


def sort_slop_issues(issues: list[SlopIssue]) -> list[SlopIssue]:
    return sorted(
        issues,
        key=lambda issue: (
            SEVERITY_ORDER.get(issue.severity, 99),
            issue.line,
            issue.column,
            issue.code,
        ),
    )