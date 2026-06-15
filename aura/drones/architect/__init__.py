from __future__ import annotations

from aura.drones.architect.build_prompts import (
    build_candidate_dispatch_prompt,
    build_repair_dispatch_prompt,
)
from aura.drones.architect.commands import DroneCommand, parse_drone_command
from aura.drones.architect.controller import DroneArchitectController
from aura.drones.architect.results import (
    BuildCompleted,
    BuildFailed,
    BuildStarted,
    Discarded,
    ErrorResult,
    ModeEntered,
    ThreadCreated,
    ThreadSwitched,
    WorkshopClarifying,
    WorkshopQuestion,
    WorkshopRequested,
    WorkspaceLoaded,
)
from aura.drones.architect.workshop_prompt import (
    WORKSHOP_SYSTEM_PROMPT,
    build_workshop_messages,
)

__all__ = [
    "DroneArchitectController",
    # Results
    "ModeEntered",
    "ThreadCreated",
    "ThreadSwitched",
    "WorkspaceLoaded",
    "WorkshopRequested",
    "WorkshopQuestion",
    "WorkshopClarifying",
    "BuildStarted",
    "BuildCompleted",
    "BuildFailed",
    "Discarded",
    "ErrorResult",
    # Commands
    "DroneCommand",
    "parse_drone_command",
    # Workshop
    "WORKSHOP_SYSTEM_PROMPT",
    "build_workshop_messages",
    # Build
    "build_candidate_dispatch_prompt",
    "build_repair_dispatch_prompt",
]
