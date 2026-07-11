"""Conversation-scoped execution mode names used at the GUI/bridge boundary."""
from __future__ import annotations

from typing import Literal

ExecutionMode = Literal["planner_worker", "interactive"]

PLANNER_WORKER_MODE: ExecutionMode = "planner_worker"
INTERACTIVE_MODE: ExecutionMode = "interactive"


def normalize_execution_mode(value: object) -> ExecutionMode:
    """Return a supported execution mode, defaulting safely to Planner/Worker."""
    if value == INTERACTIVE_MODE:
        return INTERACTIVE_MODE
    return PLANNER_WORKER_MODE
