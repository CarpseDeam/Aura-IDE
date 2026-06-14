from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class WorkspacePhase(Enum):
    WORKSHOP = "workshop"
    BUILDING = "building"
    READINESS_RUNNING = "readiness_running"
    READINESS_FAILED = "readiness_failed"
    AWAITING_DECISION = "awaiting_decision"
    ITERATING = "iterating"
    INSTALLING = "installing"
    INSTALLED = "installed"
    DISCARDED = "discarded"


@dataclass
class DroneWorkspace:
    workspace_id: str
    display_name: str
    project_root: str
    workspace_root: str
    mode: str = "new"
    phase: str = "workshop"
    candidate_drone_id: str | None = None
    installed_drone_id: str | None = None
    build_brief: str = ""
    last_build_run: str | None = None
    last_readiness_result: dict | None = None
    last_error: str | None = None
    created_at: str = ""
    updated_at: str = ""
