from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from aura.humanizer.features import CodeFeatureReport
from aura.humanizer.slop_model import SlopReport


@dataclass
class HumanizerResult:
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
    slop_report: SlopReport | None = None
    slop_score: float = 0.0
    slop_issue_count: int = 0
