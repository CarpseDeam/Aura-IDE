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
    EXECUTION_COMMAND_FINISHED,
    EXECUTION_COMMAND_STARTED,
    EXECUTION_PRE_TOOL_GATE_DECIDED,
    EXECUTION_TOPICS,
    EXECUTION_VALIDATION_FINISHED,
    TASK_CHECKLIST_UPDATED,
)

__all__ = [
    "AuraEvent",
    "EventBus",
    # ── individual topic constants ──────────────────────────────
    "EXECUTION_COMMAND_STARTED",
    "EXECUTION_COMMAND_FINISHED",
    "EXECUTION_VALIDATION_FINISHED",
    "EXECUTION_PRE_TOOL_GATE_DECIDED",
    "TASK_CHECKLIST_UPDATED",
    # ── sentinel ────────────────────────────────────────────────
    "ALL",
    # ── convenience groupings ───────────────────────────────────
    "EXECUTION_TOPICS",
    "ALL_TOPICS",
]
