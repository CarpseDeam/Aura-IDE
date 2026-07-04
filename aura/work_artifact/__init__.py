"""Work Artifact — visible, reviewable, bounded work items.

A WorkArtifact replaces Aura's hidden campaign orchestration with a visible
artifact that the Planner creates, the GUI renders, and the user reviews
item-by-item through SpecCard before each Worker dispatch.
"""
from aura.work_artifact.model import (
    WorkArtifact,
    WorkArtifactItem,
    WorkArtifactReceipt,
    WorkItemStatus,
)
from aura.work_artifact.controller import WorkArtifactController
from aura.work_artifact.projection import WorkArtifactProjection

__all__ = [
    "WorkArtifact",
    "WorkArtifactItem",
    "WorkArtifactReceipt",
    "WorkItemStatus",
    "WorkArtifactController",
    "WorkArtifactProjection",
]
