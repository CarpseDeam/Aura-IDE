from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SlopSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SlopAxis(str, Enum):
    NOISE = "noise"
    QUALITY = "quality"
    STYLE = "style"
    STRUCTURE = "structure"


@dataclass(slots=True)
class SlopIssue:
    code: str
    message: str
    severity: SlopSeverity
    axis: SlopAxis
    line: int
    column: int = 0
    snippet: str | None = None
    suggestion: str | None = None


@dataclass(slots=True)
class SlopReport:
    path: Path | None = None
    issues: list[SlopIssue] = field(default_factory=list)
    score: float = 0.0
    status: str = "clean"
    syntax_error: str | None = None

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    @property
    def critical_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == SlopSeverity.CRITICAL)

    @property
    def high_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == SlopSeverity.HIGH)

    @property
    def has_critical_issues(self) -> bool:
        return self.critical_count > 0