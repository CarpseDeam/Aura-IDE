"""Aura event bus — pure, Qt-free publish / subscribe foundation.

This package provides the base layer for event-driven communication
across Aura subsystems:

* **events** — immutable ``AuraEvent`` dataclass acting as a single fact.
* **bus** — synchronous ``EventBus`` with subscriber isolation and wildcards.
* **topics** — stable string constants for all known event topics.

No Qt, no GUI, no async.  Pure Python with ``dataclasses`` and ``typing``.
"""

from aura.events.bus import EventBus
from aura.events.event import AuraEvent
from aura.events.topics import (
    ALL,
    ALL_TOPICS,
    WORK_ARTIFACT_CREATED,
    WORK_ARTIFACT_ITEM_COMPLETED,
    WORK_ARTIFACT_ITEM_READY,
    WORK_ARTIFACT_UPDATED,
    WORK_ARTIFACT_TOPICS,
    WORKER_COMMAND_FINISHED,
    WORKER_COMMAND_STARTED,
    WORKER_FAILED,
    WORKER_FILE_CHANGED,
    WORKER_FINAL_REPORT_FINISHED,
    WORKER_FINAL_REPORT_STARTED,
    WORKER_PRE_TOOL_GATE_DECIDED,
    WORKER_TODO_UPDATED,
    WORKER_TOOL_FINISHED,
    WORKER_TOOL_STARTED,
    WORKER_TOPICS,
    WORKER_VALIDATION_FINISHED,
    WORKER_VALIDATION_STARTED,
)

__all__ = [
    "AuraEvent",
    "EventBus",
    # ── individual topic constants ──────────────────────────────
    "WORK_ARTIFACT_CREATED",
    "WORK_ARTIFACT_UPDATED",
    "WORK_ARTIFACT_ITEM_READY",
    "WORK_ARTIFACT_ITEM_COMPLETED",
    "WORKER_TOOL_STARTED",
    "WORKER_TOOL_FINISHED",
    "WORKER_FILE_CHANGED",
    "WORKER_COMMAND_STARTED",
    "WORKER_COMMAND_FINISHED",
    "WORKER_VALIDATION_STARTED",
    "WORKER_VALIDATION_FINISHED",
    "WORKER_FINAL_REPORT_STARTED",
    "WORKER_FINAL_REPORT_FINISHED",
    "WORKER_FAILED",
    "WORKER_PRE_TOOL_GATE_DECIDED",
    "WORKER_TODO_UPDATED",
    # ── sentinel ────────────────────────────────────────────────
    "ALL",
    # ── convenience groupings ───────────────────────────────────
    "WORK_ARTIFACT_TOPICS",
    "WORKER_TOPICS",
    "ALL_TOPICS",
]
