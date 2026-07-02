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
    DISPATCH_CAMPAIGN_FINISHED,
    DISPATCH_CAMPAIGN_STARTED,
    DISPATCH_STEP_COMPLETED,
    DISPATCH_STEP_STARTED,
    ALL,
    ALL_TOPICS,
    DISPATCH_TOPICS,
    WORKER_FAILED,
    WORKER_FILE_CHANGED,
    WORKER_COMMAND_STARTED,
    WORKER_COMMAND_FINISHED,
    WORKER_FINAL_REPORT_STARTED,
    WORKER_FINAL_REPORT_FINISHED,
    WORKER_PRE_TOOL_GATE_DECIDED,
    WORKER_TOOL_STARTED,
    WORKER_TOOL_FINISHED,
    WORKER_TOPICS,
    WORKER_VALIDATION_STARTED,
    WORKER_VALIDATION_FINISHED,
)

__all__ = [
    "AuraEvent",
    "EventBus",
    # ── individual topic constants ──────────────────────────────
    "DISPATCH_CAMPAIGN_STARTED",
    "DISPATCH_STEP_STARTED",
    "DISPATCH_STEP_COMPLETED",
    "DISPATCH_CAMPAIGN_FINISHED",
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
    # ── sentinel ────────────────────────────────────────────────
    "ALL",
    # ── convenience groupings ───────────────────────────────────
    "DISPATCH_TOPICS",
    "WORKER_TOPICS",
    "ALL_TOPICS",
]
