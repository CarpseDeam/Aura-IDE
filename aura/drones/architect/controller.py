from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aura.drones.architect.build_prompts import (
    build_candidate_dispatch_prompt,
    build_repair_dispatch_prompt,
)
from aura.drones.architect.commands import DroneCommand, parse_drone_command
from aura.drones.architect.failure_text import build_failure_error_text
from aura.drones.architect.results import (
    BuildCompleted,
    BuildFailed,
    BuildStarted,
    Discarded,
    ErrorResult,
    ModeEntered,
    ThreadCreated,
    ThreadRenamed,
    ThreadSwitched,
    WorkshopClarifying,
    WorkshopQuestion,
    WorkshopRequested,
    WorkspaceLoaded,
)
from aura.drones.architect.workshop_prompt import build_workshop_messages
from aura.drones.store import DroneStore
from aura.drones.workspaces.model import DroneThread, DroneWorkspace, WorkspacePhase
from aura.drones.workspaces.paths import candidate_dir, edit_candidate_dir
from aura.drones.workspaces.store import DEFAULT_THREAD_TITLE, DroneWorkspaceStore
from aura.projects.store import _clean_thread_title

if TYPE_CHECKING:
    from aura.drones.workshop_runner import DroneWorkshopResponse

logger = logging.getLogger(__name__)

_AUTO_RESUME_BLOCKED_PHASES = {
    WorkspacePhase.BUILD_FAILED.value,
    WorkspacePhase.DISCARDED.value,
}
class DroneArchitectController:
    """Owns the Drone authoring lifecycle state machine. Pure Python, no Qt."""

    def __init__(self):
        self._workspace_root: Path | None = None
        self._active_workspace: DroneWorkspace | None = None
        self._active_thread: DroneThread | None = None
        self._pending_dispatch_spec: dict | None = None
        self._last_candidate_path: str = ""
        self._last_drone_id: str = ""

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def active_workspace(self) -> DroneWorkspace | None:
        return self._active_workspace

    @property
    def phase(self) -> str:
        if self._active_workspace is None:
            return "inactive"
        return self._active_workspace.phase

    @property
    def is_active(self) -> bool:
        return self._active_workspace is not None

    @property
    def workshop_messages(self) -> list[dict[str, str]]:
        if self._active_thread is not None:
            return list(self._active_thread.messages)
        return []

    @property
    def pending_dispatch_spec(self) -> dict | None:
        return self._pending_dispatch_spec

    @property
    def last_candidate_path(self) -> str:
        return self._last_candidate_path

    @property
    def last_drone_id(self) -> str:
        return self._last_drone_id

    @property
    def has_installed_drone(self) -> bool:
        ws = self._active_workspace
        return ws is not None and bool(ws.installed_drone_id)

    @property
    def resolved_candidate_path(self) -> Path | None:
        """Return the canonical candidate folder for the active workspace."""
        if self._active_workspace is None:
            return None
        return edit_candidate_dir(self._active_workspace)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_workspace_root(self, root: Path | None) -> None:
        """Set workspace root. Clears active workspace if root changes."""
        if root != self._workspace_root:
            self._workspace_root = root
            self._active_workspace = None
            self._active_thread = None
            self._pending_dispatch_spec = None

    def enter_mode(self):
        """Enter Drone mode. Load active non-terminal Builder state if present."""
        if self._workspace_root is None:
            return ModeEntered(workspace_id=None, display_name=None)
        ws = DroneWorkspaceStore.load_active_workspace(self._workspace_root)
        if ws is None:
            self._active_workspace = None
            self._active_thread = None
            self._pending_dispatch_spec = None
            return ModeEntered(workspace_id=None, display_name=None)
        self._active_workspace = ws
        DroneWorkspaceStore.sync_display_name_from_candidate(
            self._workspace_root, ws
        )
        self._load_or_create_active_thread()
        return WorkspaceLoaded(
            workspace_id=ws.workspace_id,
            display_name=ws.display_name,
            phase=ws.phase,
        )

    def _maybe_auto_title_thread(self, text: str) -> None:
        """Auto-title a thread from the first user message if still the default."""
        if self._active_thread is not None and self._active_thread.title == DEFAULT_THREAD_TITLE:
            cleaned = _clean_thread_title(text)
            if cleaned:
                self._active_thread.title = cleaned

    def _start_workshop_from_text(self, text: str):
        self._maybe_auto_title_thread(text)
        if self._active_thread is not None:
            self._active_thread.messages.append({"role": "user", "content": text})
            if self._workspace_root is not None and self._active_workspace is not None:
                DroneWorkspaceStore.save_thread(
                    self._workspace_root,
                    self._active_workspace.workspace_id,
                    self._active_thread,
                )
        history = self._active_thread.messages[:-1] if self._active_thread else []
        messages = build_workshop_messages(text, history)
        return WorkshopRequested(messages=messages)

    def _create_workspace_from_first_message(self, text: str):
        """Create the first persisted workspace once the user starts work."""
        cmd, arg = parse_drone_command(text, WorkspacePhase.WORKSHOP.value)
        if cmd == DroneCommand.NEW:
            return self.create_workspace(arg or "New Drone")
        if cmd == DroneCommand.LOAD:
            if arg:
                return self.load_workspace(arg)
            return ErrorResult(message="Load requires a Drone name")
        if cmd == DroneCommand.HELP:
            return WorkshopQuestion(
                message=(
                    "I'm helping you design a Drone. Tell me what you want "
                    "it to do — for example: \"remind me when a CI build "
                    'fails" or "fetch the latest PRs and summarize them". '
                    "You can also say 'new' to start over, or 'load <name>' "
                    "to switch to another Drone."
                )
            )

        self.create_workspace("New Drone")
        return self._start_workshop_from_text(text)

    def exit_mode(self) -> None:
        """Reset internal state."""
        self._save_current_thread()
        self._active_thread = None
        self._active_workspace = None
        self._pending_dispatch_spec = None
        self._last_candidate_path = ""
        self._last_drone_id = ""

    def _load_or_create_active_thread(self) -> None:
        """Load the saved active thread, or the most recent, or create one."""
        if self._workspace_root is None or self._active_workspace is None:
            self._active_thread = None
            return
        ws = self._active_workspace
        threads = DroneWorkspaceStore.list_threads(
            self._workspace_root, ws.workspace_id
        )
        if threads:
            # Prefer the saved active_thread_id if it still exists.
            saved_id = ws.active_thread_id
            if saved_id:
                for t in threads:
                    if t.id == saved_id:
                        self._active_thread = t
                        return
            self._active_thread = threads[0]
        else:
            self._active_thread = DroneWorkspaceStore.create_thread(
                self._workspace_root, ws.workspace_id
            )
            ws.active_thread_id = self._active_thread.id
            DroneWorkspaceStore.save_workspace(ws)

    def _save_current_thread(self) -> None:
        """Persist the current active thread if one exists."""
        if (
            self._workspace_root is not None
            and self._active_workspace is not None
            and self._active_thread is not None
        ):
            DroneWorkspaceStore.save_thread(
                self._workspace_root,
                self._active_workspace.workspace_id,
                self._active_thread,
            )
            if self._active_workspace.active_thread_id != self._active_thread.id:
                self._active_workspace.active_thread_id = self._active_thread.id
                DroneWorkspaceStore.save_workspace(self._active_workspace)

    def create_new_thread(self):
        """Save current thread and create a new one for the active workspace."""
        if self._workspace_root is None or self._active_workspace is None:
            return ErrorResult(message="No active workspace")
        self._save_current_thread()
        thread = DroneWorkspaceStore.create_thread(
            self._workspace_root, self._active_workspace.workspace_id
        )
        self._active_thread = thread
        if self._active_workspace is not None:
            self._active_workspace.active_thread_id = thread.id
            DroneWorkspaceStore.save_workspace(self._active_workspace)
        return ThreadCreated(thread_id=thread.id, title=thread.title)

    def switch_thread(self, thread_id: str):
        """Save current thread and switch to another by ID."""
        if self._workspace_root is None or self._active_workspace is None:
            return ErrorResult(message="No active workspace")
        self._save_current_thread()
        thread = DroneWorkspaceStore.load_thread(
            self._workspace_root,
            self._active_workspace.workspace_id,
            thread_id,
        )
        if thread is None:
            return ErrorResult(message=f"Thread not found: {thread_id}")
        self._active_thread = thread
        if self._active_workspace is not None:
            self._active_workspace.active_thread_id = thread.id
            DroneWorkspaceStore.save_workspace(self._active_workspace)
        return ThreadSwitched(thread_id=thread.id, title=thread.title)

    def rename_thread(self, thread_id: str, new_title: str):
        """Rename a thread.  Returns ThreadRenamed or ErrorResult."""
        if self._workspace_root is None or self._active_workspace is None:
            return ErrorResult(message="No active workspace")
        stripped = new_title.strip()
        if not stripped:
            return ErrorResult(message="Title cannot be empty")
        if self._active_thread is not None and thread_id == self._active_thread.id:
            self._active_thread.title = stripped
            DroneWorkspaceStore.save_thread(
                self._workspace_root,
                self._active_workspace.workspace_id,
                self._active_thread,
            )
            return ThreadRenamed(thread_id=thread_id, title=stripped)
        thread = DroneWorkspaceStore.load_thread(
            self._workspace_root,
            self._active_workspace.workspace_id,
            thread_id,
        )
        if thread is None:
            return ErrorResult(message=f"Thread not found: {thread_id}")
        thread.title = stripped
        DroneWorkspaceStore.save_thread(
            self._workspace_root,
            self._active_workspace.workspace_id,
            thread,
        )
        return ThreadRenamed(thread_id=thread.id, title=thread.title)

    def load_workspace(self, workspace_id: str):
        """Load a workspace by ID."""
        if self._workspace_root is None:
            return ErrorResult(message="No project root set")
        ws = DroneWorkspaceStore.load_workspace(self._workspace_root, workspace_id)
        if ws is None:
            return ErrorResult(message=f"Drone not found: {workspace_id}")
        self._save_current_thread()
        self._active_workspace = ws
        self._load_or_create_active_thread()
        DroneWorkspaceStore.set_active_workspace(self._workspace_root, ws)
        DroneWorkspaceStore.sync_display_name_from_candidate(
            self._workspace_root, ws
        )
        return WorkspaceLoaded(
            workspace_id=ws.workspace_id,
            display_name=ws.display_name,
            phase=ws.phase,
        )

    def create_workspace(self, display_name: str = "New Drone"):
        """Create a new workspace in workshop phase."""
        if self._workspace_root is None:
            return ErrorResult(message="No project root set")
        ws = DroneWorkspaceStore.create_workspace(
            self._workspace_root, display_name
        )
        self._active_workspace = ws
        self._active_thread = DroneWorkspaceStore.create_thread(
            self._workspace_root, ws.workspace_id
        )
        self._pending_dispatch_spec = None
        DroneWorkspaceStore.set_active_workspace(self._workspace_root, ws)
        return WorkspaceLoaded(
            workspace_id=ws.workspace_id,
            display_name=ws.display_name,
            phase=ws.phase,
        )

    def load_drone_workspace(self, drone_id: str):
        """Load or create an edit workspace for an installed Drone."""
        if self._workspace_root is None:
            return ErrorResult(message="No project root set")

        ws = DroneWorkspaceStore.load_or_create_workspace_for_drone(
            self._workspace_root, drone_id
        )
        if ws is None:
            return ErrorResult(message=f"Drone not found: {drone_id}")

        self._save_current_thread()
        self._active_workspace = ws
        self._load_or_create_active_thread()
        DroneWorkspaceStore.set_active_workspace(self._workspace_root, ws)
        return WorkspaceLoaded(
            workspace_id=ws.workspace_id,
            display_name=ws.display_name,
            phase=ws.phase,
        )

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    def handle_user_message(self, text: str):
        """Route user message based on current workspace phase."""
        if self._active_workspace is None:
            return self._ensure_active_workspace(text)

        phase = self._active_workspace.phase

        if phase == WorkspacePhase.WORKSHOP.value:
            return self._handle_workshop_message(text)

        if phase == WorkspacePhase.BUILDING.value:
            return ErrorResult(message="Build already in progress")

        if phase == WorkspacePhase.BUILD_FAILED.value:
            return self._handle_failed_phase_message(text)

        if phase == WorkspacePhase.ITERATING.value:
            return self._handle_iterating_message(text)

        if phase in (
            WorkspacePhase.READY.value,
            WorkspacePhase.DISCARDED.value,
        ):
            self.create_workspace("New Drone")
            return self._handle_workshop_message(text)

        return ErrorResult(message=f"Unhandled phase: {phase}")

    def handle_workshop_response(
        self, ws_response: DroneWorkshopResponse, raw_text: str
    ):
        """Process parsed workshop response. Return next result."""
        if self._active_workspace is None:
            return ErrorResult(message="No active Drone")

        if self._active_thread is not None:
            self._active_thread.messages.append(
                {"role": "assistant", "content": raw_text}
            )
            if self._workspace_root is not None and self._active_workspace is not None:
                DroneWorkspaceStore.save_thread(
                    self._workspace_root,
                    self._active_workspace.workspace_id,
                    self._active_thread,
                )

        if ws_response.kind == "question":
            return WorkshopQuestion(message=ws_response.message)

        if ws_response.kind == "brief":
            brief = ws_response.brief
            if brief is None:
                return WorkshopClarifying(message=ws_response.message)

            if brief.ready_to_build and brief.build_brief.strip():
                self._active_workspace.build_brief = brief.build_brief
                self._active_workspace.phase = WorkspacePhase.BUILDING.value
                DroneWorkspaceStore.save_workspace(self._active_workspace)

                dispatch_spec = build_candidate_dispatch_prompt(
                    self._active_workspace, brief
                )
                self._pending_dispatch_spec = dispatch_spec

                return BuildStarted(
                    build_brief=brief.build_brief,
                    dispatch_spec=dispatch_spec,
                )

            return WorkshopClarifying(message=ws_response.message)

        if ws_response.kind == "error":
            return ErrorResult(message=ws_response.message)

        return ErrorResult(
            message=f"Unknown workshop response kind: {ws_response.kind}"
        )

    def on_build_completed(
        self,
        success: bool,
        error: str | None = None,
        *,
        failure_detail: Any = None,
    ):
        """Called after Worker finishes building. Marks the Drone ready."""
        if self._active_workspace is None:
            return ErrorResult(message="No active Drone")

        if success:
            self._active_workspace.phase = WorkspacePhase.READY.value
            DroneWorkspaceStore.save_workspace(self._active_workspace)

            # Extract drone_id from candidate drone.json.
            drone_id = ""
            candidate_path = ""
            if self._workspace_root is not None:
                cand = self.resolved_candidate_path
                if cand is None:
                    project_root = Path(self._active_workspace.project_root)
                    cand = candidate_dir(
                        project_root, self._active_workspace.workspace_id
                    )
                candidate_path = str(cand)
                try:
                    drone = DroneStore.load_drone_from_folder(cand)
                    drone_id = drone.id
                    self._active_workspace.candidate_drone_id = drone.id
                    if self._active_workspace.mode != "edit":
                        self._active_workspace.display_name = drone.name
                except Exception:
                    logger.warning(
                        "Could not load drone.json from candidate %s", cand
                    )

            self._last_candidate_path = candidate_path
            self._last_drone_id = drone_id
            DroneWorkspaceStore.save_workspace(self._active_workspace)

            # Rename workspace to match the built drone's ID.
            if drone_id and drone_id != self._active_workspace.workspace_id and self._active_workspace.mode != "edit":
                self._active_workspace = DroneWorkspaceStore.rename_workspace(
                    Path(self._active_workspace.project_root),
                    self._active_workspace,
                    drone_id,
                )

            return BuildCompleted(
                candidate_path=candidate_path, drone_id=drone_id
            )
        else:
            failure_error = build_failure_error_text(
                summary=error,
                metadata=failure_detail,
            )
            self._active_workspace.phase = WorkspacePhase.BUILD_FAILED.value
            self._active_workspace.last_error = failure_error
            DroneWorkspaceStore.save_workspace(self._active_workspace)
            return BuildFailed(error=failure_error)



    def discard_workspace(self):
        """Mark workspace as discarded."""
        if self._active_workspace is None:
            return ErrorResult(message="No active Drone")

        workspace_id = self._active_workspace.workspace_id
        DroneWorkspaceStore.discard_workspace(self._active_workspace)
        self._active_workspace = None
        return Discarded(workspace_id=workspace_id)

    # ------------------------------------------------------------------
    # Internal phase handlers
    # ------------------------------------------------------------------

    def _ensure_active_workspace(self, text: str | None = None):
        """Auto-create or load a workspace if none is active."""
        if self._workspace_root is None:
            return ErrorResult(message="No project root set")
        ws = DroneWorkspaceStore.load_active_workspace(self._workspace_root)
        if ws is not None:
            if ws.phase in _AUTO_RESUME_BLOCKED_PHASES:
                ws = None
            else:
                self._active_workspace = ws
                self._load_or_create_active_thread()
                if not text:
                    return WorkspaceLoaded(
                        workspace_id=ws.workspace_id,
                        display_name=ws.display_name,
                        phase=ws.phase,
                    )
                # Text provided — route to phase handler.
                phase = ws.phase
                if phase in {WorkspacePhase.WORKSHOP.value, WorkspacePhase.BUILDING.value, WorkspacePhase.ITERATING.value, WorkspacePhase.READY.value}:
                    return self._handle_workshop_message(text)
                # Terminal/unusable phases (discarded, build_failed) — start fresh.
                result = self.create_workspace("New Drone")
                if result.kind == "error":
                    return result
                return self._start_workshop_from_text(text)
        if not text:
            return ModeEntered(workspace_id=None, display_name=None)
        return self._create_workspace_from_first_message(text)

    def _handle_workshop_message(self, text: str):
        cmd, arg = parse_drone_command(text, self._active_workspace.phase)

        if cmd == DroneCommand.NEW:
            return self.create_workspace(arg or "New Drone")

        if cmd == DroneCommand.LOAD:
            if arg:
                return self.load_workspace(arg)
            return ErrorResult(message="Load requires a Drone name")

        if cmd == DroneCommand.HELP:
            return WorkshopQuestion(
                message=(
                    "I'm helping you design a Drone. Tell me what you want "
                    "it to do — for example: \"remind me when a CI build "
                    'fails" or "fetch the latest PRs and summarize them". '
                    "You can also say 'new' to start over, or 'load <name>' "
                    "to switch to another Drone."
                )
            )

        # UNKNOWN or any other text — treat as workshop conversation.
        self._maybe_auto_title_thread(text)
        if self._active_thread is not None:
            self._active_thread.messages.append({"role": "user", "content": text})
            if self._workspace_root is not None and self._active_workspace is not None:
                DroneWorkspaceStore.save_thread(
                    self._workspace_root,
                    self._active_workspace.workspace_id,
                    self._active_thread,
                )
        history = self._active_thread.messages[:-1] if self._active_thread else []
        messages = build_workshop_messages(text, history)
        return WorkshopRequested(messages=messages)

    def _handle_failed_phase_message(self, text: str):
        """Handle messages in build_failed phase."""
        cmd, arg = parse_drone_command(text, self._active_workspace.phase)

        if cmd == DroneCommand.NEW:
            return self.create_workspace(arg or "New Drone")

        if cmd == DroneCommand.LOAD:
            if arg:
                return self.load_workspace(arg)
            return ErrorResult(message="Load requires a Drone name")

        if cmd == DroneCommand.DISCARD:
            return self.discard_workspace()

        # Any other text — treat as revision feedback.
        self._active_workspace.phase = WorkspacePhase.ITERATING.value
        DroneWorkspaceStore.save_workspace(self._active_workspace)

        repair_spec = build_repair_dispatch_prompt(self._active_workspace, text)
        self._pending_dispatch_spec = repair_spec

        return BuildStarted(
            build_brief=self._active_workspace.build_brief,
            dispatch_spec=repair_spec,
        )

    def _handle_iterating_message(self, text: str):
        """Handle messages in iterating phase — same as decision but always revises."""
        cmd, arg = parse_drone_command(text, self._active_workspace.phase)

        if cmd == DroneCommand.NEW:
            return self.create_workspace(arg or "New Drone")

        if cmd == DroneCommand.LOAD:
            if arg:
                return self.load_workspace(arg)
            return ErrorResult(message="Load requires a Drone name")

        if cmd == DroneCommand.DISCARD:
            return self.discard_workspace()

        # Any other text — revision feedback.
        revision_text = arg if cmd == DroneCommand.REVISE else text
        repair_spec = build_repair_dispatch_prompt(
            self._active_workspace, revision_text
        )
        self._pending_dispatch_spec = repair_spec

        return BuildStarted(
            build_brief=self._active_workspace.build_brief,
            dispatch_spec=repair_spec,
        )
