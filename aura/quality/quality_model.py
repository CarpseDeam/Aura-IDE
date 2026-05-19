"""Data models for code quality analysis."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class QualitySeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class QualityAxis(str, Enum):
    PLACEHOLDER = "placeholder"
    COMPLEXITY = "complexity"
    DEAD_CODE = "dead_code"
    NAMING = "naming"
    COMMENT = "comment"
    STRUCTURE = "structure"
    CROSS_LANGUAGE = "cross_language"


@dataclass
class QualityIssue:
    axis: QualityAxis
    severity: QualitySeverity
    code: str
    message: str
    line: int | None
    col: int | None = None

    def to_dict(self) -> dict:
        return {
            "axis": self.axis.value,
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "line": self.line,
            "col": self.col,
        }


@dataclass
class QualityReport:
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def score(self) -> float:
        if not self.issues:
            return 1.0
        base = 100.0
        for issue in self.issues:
            if issue.severity == QualitySeverity.CRITICAL:
                base -= 15
            elif issue.severity == QualitySeverity.HIGH:
                base -= 8
            elif issue.severity == QualitySeverity.MEDIUM:
                base -= 3
            elif issue.severity == QualitySeverity.LOW:
                base -= 1
        cutoff = 0  # scores cap at 0
        return max(cutoff, base) / 100.0

    @property
    def critical_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == QualitySeverity.CRITICAL)

    @property
    def high_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == QualitySeverity.HIGH)

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    @property
    def status(self) -> str:
        if not self.issues:
            return "clean"
        if self.score >= 0.8:
            return "pass"
        if self.score >= 0.5:
            return "warn"
        return "fail"
