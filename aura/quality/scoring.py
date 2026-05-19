"""Scoring functions for code quality analysis."""
from __future__ import annotations

from aura.quality.quality_model import QualityIssue, QualitySeverity


def calculate_quality_score(issues: list[QualityIssue]) -> float:
    if not issues:
        return 1.0
    base = 100.0
    for issue in issues:
        if issue.severity == QualitySeverity.CRITICAL:
            base -= 15
        elif issue.severity == QualitySeverity.HIGH:
            base -= 8
        elif issue.severity == QualitySeverity.MEDIUM:
            base -= 3
        elif issue.severity == QualitySeverity.LOW:
            base -= 1
    cutoff = 0
    return max(cutoff, base) / 100.0


def quality_status(score: float) -> str:
    if score >= 0.8:
        return "pass"
    elif score >= 0.5:
        return "warn"
    else:
        return "fail"


def sort_quality_issues(issues: list[QualityIssue]) -> list[QualityIssue]:
    severity_order = {
        QualitySeverity.CRITICAL: 0,
        QualitySeverity.HIGH: 1,
        QualitySeverity.MEDIUM: 2,
        QualitySeverity.LOW: 3,
    }
    return sorted(issues, key=lambda i: severity_order.get(i.severity, 99))
