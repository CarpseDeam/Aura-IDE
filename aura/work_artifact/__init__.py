"""Work Artifact — one visible approved WorkArtifact job.

Items are bounded internal Worker execution units. One user approval covers
the job. There is no per-item SpecCard or manual item review path.
"""
from aura.work_artifact.model import (
    ValidationCommandSpec,
    WorkArtifact,
    WorkArtifactItem,
    WorkArtifactReceipt,
    WorkItemStatus,
)
from aura.work_artifact.controller import WorkArtifactController
from aura.work_artifact.projection import WorkArtifactProjection
from aura.work_artifact.runner import WorkArtifactRunner
from aura.work_artifact.verification import (
    WorkArtifactAttemptOutcome,
    classify_item_attempt,
    is_infrastructure_failure,
)

__all__ = [
    "ValidationCommandSpec",
    "WorkArtifact",
    "WorkArtifactItem",
    "WorkArtifactReceipt",
    "WorkItemStatus",
    "WorkArtifactController",
    "WorkArtifactProjection",
    "WorkArtifactRunner",
    "WorkArtifactAttemptOutcome",
    "classify_item_attempt",
    "is_infrastructure_failure",
]
