from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModeEntered:
    kind: str = "mode_entered"
    workspace_id: str | None = None
    display_name: str | None = None


@dataclass(frozen=True)
class WorkspaceLoaded:
    kind: str = "workspace_loaded"
    workspace_id: str = ""
    display_name: str = ""
    phase: str = ""


@dataclass(frozen=True)
class WorkshopRequested:
    kind: str = "workshop_requested"
    messages: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class WorkshopQuestion:
    kind: str = "workshop_question"
    message: str = ""


@dataclass(frozen=True)
class WorkshopClarifying:
    kind: str = "workshop_clarifying"
    message: str = ""


@dataclass(frozen=True)
class BuildStarted:
    kind: str = "build_started"
    build_brief: str = ""
    dispatch_spec: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BuildCompleted:
    kind: str = "build_completed"
    candidate_path: str = ""
    drone_id: str = ""


@dataclass(frozen=True)
class BuildFailed:
    kind: str = "build_failed"
    error: str = ""


@dataclass(frozen=True)
class Discarded:
    kind: str = "discarded"
    workspace_id: str = ""


@dataclass(frozen=True)
class ThreadCreated:
    kind: str = "thread_created"
    thread_id: str = ""
    title: str = ""


@dataclass(frozen=True)
class ThreadSwitched:
    kind: str = "thread_switched"
    thread_id: str = ""
    title: str = ""


@dataclass(frozen=True)
class ThreadRenamed:
    kind: str = "thread_renamed"
    thread_id: str = ""
    title: str = ""


@dataclass(frozen=True)
class ErrorResult:
    kind: str = "error"
    message: str = ""
