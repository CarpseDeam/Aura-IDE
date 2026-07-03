from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

QualitySeverity = Literal["info", "warning", "error"]


@dataclass
class QualityFinding:
    kind: str
    severity: QualitySeverity
    file: str
    line: int | None
    message: str
    suggested_action: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkerQualityDecision:
    ok: bool
    hard_block: bool
    needs_cleanup: bool
    findings: list[QualityFinding]
    instruction: str = ""
