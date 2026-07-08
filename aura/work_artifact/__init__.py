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
    add_retry_context,
    classify_item_attempt,
    declared_validation_commands,
    derive_scoped_validation_commands,
    ensure_item_verification_source,
    evidence_records,
    is_infrastructure_failure,
    validation_satisfied,
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
    "add_retry_context",
    "classify_item_attempt",
    "declared_validation_commands",
    "derive_scoped_validation_commands",
    "ensure_item_verification_source",
    "evidence_records",
    "is_infrastructure_failure",
    "validation_satisfied",
]
