from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from aura.quality.features import CodeFeatureReport
from aura.quality.quality_model import QualityReport


@dataclass
class QualityResult:
    path: Path | None = None
    language: str = ""
    original: str = ""
    text: str = ""
    changed: bool = False
    markdown_stripped: bool = False
    comments_removed: int = 0
    docstrings_removed: int = 0
    syntax_fallback: bool = False
    error: str | None = None
    elapsed_ms: float = 0.0
    feature_report: CodeFeatureReport | None = None
    structural_smell_count: int = 0
    quality_report: QualityReport | None = None
    quality_score: float = 0.0
    quality_issue_count: int = 0
