"""Worker production-quality evaluation package."""

from aura.conversation.quality.evaluator import evaluate_worker_quality
from aura.conversation.quality.models import (
    QualityFinding,
    QualitySeverity,
    WorkerQualityDecision,
)
from aura.conversation.quality.structural_checks import (
    DUPLICATE_STRING_MIN_LENGTH,
    LARGE_DIFF_LINE_THRESHOLD,
    PROTECTED_CONTROL_FLOW_FILES,
)

__all__ = [
    "DUPLICATE_STRING_MIN_LENGTH",
    "LARGE_DIFF_LINE_THRESHOLD",
    "PROTECTED_CONTROL_FLOW_FILES",
    "QualityFinding",
    "QualitySeverity",
    "WorkerQualityDecision",
    "evaluate_worker_quality",
]
