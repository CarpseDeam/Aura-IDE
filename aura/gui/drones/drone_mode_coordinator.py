from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QFrame, QSplitter

from aura.drones.architect.controller import DroneArchitectController
from aura.drones.architect.results import (
    BuildCompleted,
    BuildFailed,
    ErrorResult,
)
from aura.drones.store import DroneStore
from aura.drones.workspaces.model import DroneThread, DroneWorkspace, WorkspacePhase
from aura.drones.workspaces.paths import candidate_dir, workspace_folder
from aura.drones.workspaces.store import DroneWorkspaceStore
from aura.gui.drones.drone_workspace_pane import DroneWorkspacePane

logger = logging.getLogger(__name__)


class DroneModeCoordinator(QObject):
    """Thin UI adapter that delegates lifecycle to DroneArchitectController."""

    drone_mode_changed = Signal(bool)
    drone_list_changed = Signal()
    fresh_session_requested = Signal()

    def __init__(
        self,
        main_splitter: QSplitter,
        left_pane: QFrame,
        bridge,
        chat,
        input_panel,
        status_bar,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._main_splitter = main_splitter
        self._left_pane = left_pane
        self._bridge = bridge
        self._chat = chat
        self._input = input_panel
        self._status_bar = status_bar

        # Controller owns lifecycle state.
        self._controller = DroneArchitectController()

        # Background thread/worker references — retained to prevent Qt GC.
        self._background_threads: list[QThread] = []
        self._background_workers: list[QObject] = []

        # Workspace pane placed into the splitter during drone mode.
        self._workspace_pane = DroneWorkspacePane(parent=None)
        self._workspace_pane.hide()
        self._workspace_pane.workspace_selected.connect(self._on_workspace_selected)
        self._workspace_pane.new_workspace_requested.connect(self._on_new_workspace)
        self._workspace_pane.discard_workspace_requested.connect(self._on_discard_workspace)
        self._workspace_pane.edit_installed.connect(self.edit_installed_drone)
        self._workspace_pane.thread_selected.connect(self._on_thread_selected)

        # Active build tool state for cancellation after drone mode exit.
        self._active_drone_build_tool_id: str | None = None
        self._active_thread_id: str | None = None
        self._workspace_root: Path | None = None
        self._drone_mode: bool = False

    def set_workspace_root(self, root: Path | None) -> None:
        self._workspace_root = root
        self._controller.set_workspace_root(root)
        self._workspace_pane.set_project_root(root)
        if root is not None:
            DroneWorkspaceStore.migrate_stale_folders(root)

    def edit_installed_drone(self, drone_id: str) -> None:
        """Open a Drone in the architect for editing."""
        result = self._controller.load_drone_workspace(drone_id)
        self._workspace_pane.refresh()
        if isinstance(result, ErrorResult):
            self._chat.add_error("Drone Builder", f"Could not load Drone: {result.message}")
            return
        ws = self._controller.active_workspace
        if ws is not None:
            thread = self._ensure_thread_for_workspace(ws.workspace_id)
            if thread is not None:
                self._active_thread_id = thread.id
                self._load_thread_into_ui(ws.workspace_id, thread.id)
        self.enter_drone_mode(load_active=False)

    def edit_builder_drone(self, workspace_id: str) -> None:
        """Open a builder workspace for the user to continue working."""
        result = self._controller.load_workspace(workspace_id)
        self._workspace_pane.refresh()
        if isinstance(result, ErrorResult):
            self._chat.add_error("Drone Builder", f"Could not load workspace: {result.message}")
            return
        thread = self._ensure_thread_for_workspace(workspace_id)
        if thread is not None:
            self._active_thread_id = thread.id
            self._load_thread_into_ui(workspace_id, thread.id)
        self.enter_drone_mode(load_active=False)

    def discard_builder_drone(self, workspace_id: str) -> None:
        """Discard a builder workspace."""
        self._controller.load_workspace(workspace_id)
        self._controller.discard_workspace()
        self._workspace_pane.refresh()
        self.drone_list_changed.emit()

    def _is_edit_workspace(self) -> bool:
        ws = self._controller.active_workspace
        return ws is not None and ws.mode == "edit"

    def is_drone_mode(self) -> bool:
        return self._drone_mode

    def active_drone_context(self) -> str:
        """Return a context prompt for the active Drone workspace."""
        ws = self._controller.active_workspace
        if ws is None:
            return ""
        cand = candidate_dir(Path(ws.project_root), ws.workspace_id)
        ws_dir = workspace_folder(Path(ws.project_root), ws.workspace_id)
        drone_name = self._resolve_drone_name(ws)
        return (
            f"[Drone Mode Active]\n"
            f"You are building or editing a folder-backed Drone: "
            f'"{drone_name}" (workspace: {ws.workspace_id}).\n'
            f"The Drone's candidate source folder is: {cand}\n"
            f"The Drone workspace is: {ws_dir}/\n"
            f"\n"
            f"--- Folder-backed Drone Build Contract ---\n"
            f"The user's current message is the Drone brief — treat it as the build or edit specification.\n"
            f"Build or edit the Drone entirely inside the candidate source folder ({cand}).\n"
            f"A valid Drone folder needs at minimum: drone.json (the manifest) and an entrypoint\n"
            f"file, usually main.py. Add a requirements.txt only if the Drone has Python\n"
            f"dependencies beyond the standard library.\n"
            f"\n"
            f"The Worker must NOT register or install the Drone — DroneModeCoordinator handles\n"
            f"installation after Worker success.\n"
            f"The Worker must NOT write files outside the candidate folder, unless the user\n"
            f"explicitly asks for a project code change (not a Drone change).\n"
            f"After the Worker finishes, the system automatically installs the Drone."
        )

    def enter_drone_mode(self, *, load_active: bool = True) -> None:
        if self._drone_mode:
            return
        self._drone_mode = True

        if load_active:
            self._controller.enter_mode()
        ws = self._controller.active_workspace

        # Load thread for active workspace if not already loaded
        if ws is not None and self._active_thread_id is None:
            thread = self._ensure_thread_for_workspace(ws.workspace_id)
            if thread is not None:
                self._active_thread_id = thread.id
                self._load_thread_into_ui(ws.workspace_id, thread.id)

        self._workspace_pane.set_active(
            ws.workspace_id if ws else None,
            self._active_thread_id,
        )

        # Swap left pane for workspace pane in the splitter
        idx = self._main_splitter.indexOf(self._left_pane)
        if idx >= 0:
            self._left_pane.hide()
            self._main_splitter.replaceWidget(idx, self._workspace_pane)
            self._workspace_pane.show()

        self._input.set_drone_architect_mode(True)
        self._status_bar.set_drone_architect_mode(True)
        self.drone_mode_changed.emit(True)

    def exit_drone_mode(self) -> None:
        if not self._drone_mode:
            return
        self._save_current_thread()
        self._active_thread_id = None
        self._drone_mode = False

        # Swap workspace pane back to left pane
        idx = self._main_splitter.indexOf(self._workspace_pane)
        if idx >= 0:
            self._workspace_pane.hide()
            self._main_splitter.replaceWidget(idx, self._left_pane)
            self._left_pane.show()

        self._input.set_drone_architect_mode(False)
        self._status_bar.set_drone_architect_mode(False)

        pass  # workshop runner removed

        self._workspace_pane.set_active_workspace_id(None)
        self._controller.exit_mode()

    # ------------------------------------------------------------------
    # Build dispatch + Worker orchestration
    # ------------------------------------------------------------------

    def _on_drone_dispatch_requested(
        self, tool_call_id: str, text: str, submit_func
    ) -> None:
        """Route a user message through the Drone architect controller."""
        self._active_drone_build_tool_id = tool_call_id

        result = self._controller.handle_user_message(text)
        if result is None:
            return

        if result.kind == "workshop_requested":
            submit_func(result.messages)
            return

        if result.kind == "build_started":
            from aura.conversation.backend import AssistantRequest

            spec = result.dispatch_spec
            model = spec.get("model", "")
            messages = spec.get("messages", [])
            tools = spec.get("tools", [])

            req = AssistantRequest(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                max_tokens=spec.get("max_tokens", 16384),
                stop=spec.get("stop"),
                temperature=spec.get("temperature", 0.7),
                thinking=spec.get("thinking", "off"),
            )
            submit_func(req)

            self._workspace_pane.refresh()
            return

        if result.kind == "error":
            self._chat.add_error("Drone Builder", result.message)
            return

    def _on_drone_worker_cancelled(self, tool_call_id: str) -> None:
        if self._active_drone_build_tool_id == tool_call_id:
            self._active_drone_build_tool_id = None

    def _on_worker_finished(
        self,
        tool_call_id: str,
        ok: bool,
        summary: str,
        *,
        failure_detail: Any = None,
    ) -> None:
        if self._active_drone_build_tool_id != tool_call_id:
            return

        result = self._controller.on_build_completed(
            ok,
            error=None if ok else summary,
            failure_detail=failure_detail,
        )
        self._workspace_pane.refresh()

        if isinstance(result, BuildCompleted):
            self._chat.add_info("Drone Builder", "Build complete. Installing Drone...")
            QTimer.singleShot(0, self._start_ready_step)
        elif isinstance(result, BuildFailed):
            self._chat.add_error("Build Failed", summary)

        self._active_drone_build_tool_id = None

    def _worker_result_metadata(self, tool_id: str) -> dict:
        getter = getattr(self._bridge, "worker_result_metadata", None)
        if not callable(getter):
            return {}
        metadata = getter(tool_id)
        return metadata if isinstance(metadata, dict) else {}

    # ------------------------------------------------------------------
    # Ready step (install)
    # ------------------------------------------------------------------

    def _start_ready_step(self) -> None:
        ws = self._controller.active_workspace
        workspace_root = self._workspace_root
        if ws is None or workspace_root is None:
            return

        self._controller.mark_ready_step_started()
        self._workspace_pane.refresh()
        self.drone_list_changed.emit()
        workspace_id = ws.workspace_id

        def _do_ready():
            try:
                from aura.drones.architect.installer import install_or_reinstall

                return install_or_reinstall(ws, workspace_root)
            except Exception as exc:
                logger.exception("Failed to install Drone")
                return {"ok": False, "error": str(exc)}

        self._run_in_thread(
            _do_ready,
            lambda result, wid=workspace_id: self._on_ready_step_done(wid, result),
        )

    def _on_ready_step_done(self, workspace_id: str, result: dict) -> None:
        ws = self._controller.active_workspace
        if ws is None or ws.workspace_id != workspace_id:
            self.drone_list_changed.emit()
            return

        if result.get("ok"):
            drone_name = result.get("drone_name", "Drone")
            self._chat.add_info("Drone Ready", f"{drone_name} is Ready in the Drone list.")
            self.drone_list_changed.emit()
            self._workspace_pane.refresh()
            self.exit_drone_mode()
            return

        ws.phase = WorkspacePhase.BUILD_FAILED.value
        ws.last_error = result.get("error", "Unknown install error")
        DroneWorkspaceStore.save_workspace(ws)
        self._chat.add_error("Drone Builder", f"{ws.display_name} needs a fix: {ws.last_error}")
        self.drone_list_changed.emit()
        self._workspace_pane.refresh()

    @staticmethod
    def _status_for_phase(phase: str) -> str:
        if phase == WorkspacePhase.WORKSHOP.value:
            return "Draft"
        if phase in (WorkspacePhase.BUILDING.value, WorkspacePhase.ITERATING.value):
            return "Building"
        if phase == WorkspacePhase.INSTALLING.value:
            return "Installing"
        if phase == WorkspacePhase.BUILD_FAILED.value:
            return "Needs Fix"
        if phase == WorkspacePhase.INSTALLED.value:
            return "Ready"
        return "Draft"

    # ------------------------------------------------------------------
    # Background thread helpers
    # ------------------------------------------------------------------

    def _run_in_thread(self, fn, callback) -> None:
        """Run *fn* in a background thread and call *callback* on the main thread."""

        class _Worker(QObject):
            done = Signal(object)

            def run(self) -> None:
                try:
                    result = fn()
                except Exception as exc:
                    result = exc
                self.done.emit(result)

        worker = _Worker()
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(thread.quit)
        worker.done.connect(callback)
        worker.done.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()

        self._background_threads.append(thread)
        self._background_workers.append(worker)

        # Clean up references on completion.
        def _cleanup():
            self._background_threads.remove(thread)
            self._background_workers.remove(worker)

        thread.finished.connect(_cleanup)

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def _ensure_thread_for_workspace(self, workspace_id: str) -> DroneThread | None:
        """Get the active or most recent thread for a workspace, creating one if needed."""
        ws = self._controller.active_workspace
        if ws is None:
            return None
        project_root = Path(ws.project_root)
        threads = DroneWorkspaceStore.list_threads(project_root, workspace_id)
        if threads:
            return threads[0]
        return DroneWorkspaceStore.create_thread(project_root, workspace_id)

    def _save_current_thread(self) -> None:
        """Persist the current conversation into the active thread."""
        if self._active_thread_id is None:
            return
        ws = self._controller.active_workspace
        if ws is None:
            return
        project_root = Path(ws.project_root)
        messages = copy.deepcopy(self._bridge.history().messages)
        thread = DroneWorkspaceStore.load_thread(
            project_root, ws.workspace_id, self._active_thread_id
        )
        if thread is not None:
            thread.messages = messages
            DroneWorkspaceStore.save_thread(project_root, ws.workspace_id, thread)

    def _load_thread_into_ui(self, workspace_id: str, thread_id: str) -> None:
        """Clear the UI and replay messages from the given thread."""
        ws = self._controller.active_workspace
        if ws is None:
            return
        project_root = Path(ws.project_root)
        thread = DroneWorkspaceStore.load_thread(project_root, workspace_id, thread_id)
        if thread is None:
            return
        self._bridge.reset_history()
        self._bridge.history().messages = copy.deepcopy(thread.messages)
        self._chat.reset()
        self._chat.replay_messages(thread.messages)

    def _on_thread_selected(self, workspace_id: str, thread_id: str) -> None:
        """Switch to a different thread in the current or a different workspace."""
        self._save_current_thread()
        result = self._controller.load_workspace(workspace_id)
        if isinstance(result, ErrorResult):
            self._chat.add_error("Drone Builder", f"Could not load workspace: {result.message}")
            return
        self._load_thread_into_ui(workspace_id, thread_id)
        self._active_thread_id = thread_id
        self._workspace_pane.set_active(workspace_id, thread_id)

    def _resolve_drone_name(self, ws: DroneWorkspace) -> str:
        """Resolve the best display name for a workspace context.

        Priority: candidate drone.json name -> installed Drone name -> display_name -> workspace_id.
        """
        wid = ws.workspace_id
        project_root = Path(ws.project_root)
        # 1. Candidate drone.json
        try:
            cand = candidate_dir(project_root, wid)
            drone_json = cand / "drone.json"
            if drone_json.exists():
                data = json.loads(drone_json.read_text(encoding="utf-8"))
                name = data.get("name")
                if name:
                    return str(name)
        except Exception as exc:
            logger.debug("Failed to read candidate drone.json for %s: %s", wid, exc)
        # 2. Installed Drone name
        if ws.installed_drone_id:
            try:
                drone = DroneStore.load_drone(project_root, ws.installed_drone_id)
                if drone and drone.name:
                    return drone.name
            except Exception as exc:
                logger.debug("Failed to load installed Drone %s: %s", ws.installed_drone_id, exc)
        # 3. Fallback
        return ws.display_name or wid

    # ------------------------------------------------------------------
    # Workspace pane callbacks
    # ------------------------------------------------------------------

    def _on_workspace_selected(self, workspace_id: str) -> None:
        self._save_current_thread()
        result = self._controller.load_workspace(workspace_id)
        if isinstance(result, ErrorResult):
            self._chat.add_error("Drone Builder", f"Could not load workspace: {result.message}")
            return
        thread = self._ensure_thread_for_workspace(workspace_id)
        thread_id = thread.id if thread else None
        self._active_thread_id = thread_id
        if thread_id:
            self._load_thread_into_ui(workspace_id, thread_id)
        if self._drone_mode:
            self._workspace_pane.set_active(workspace_id, thread_id)
            self._input.focus_editor()
        else:
            self.enter_drone_mode(load_active=False)

    def start_fresh_drone_session(self) -> None:
        """Create a new workspace and reset the Builder session without exiting Drone mode."""
        self._controller.create_workspace("New Drone")
        ws = self._controller.active_workspace
        if ws is not None:
            thread = DroneWorkspaceStore.create_thread(
                Path(ws.project_root), ws.workspace_id
            )
            self._active_thread_id = thread.id
            self._load_thread_into_ui(ws.workspace_id, thread.id)
        self._workspace_pane.set_active(
            ws.workspace_id if ws else None,
            self._active_thread_id,
        )
        self.fresh_session_requested.emit()
        self._input.focus_editor()

    def _on_new_workspace(self) -> None:
        self.start_fresh_drone_session()

    def _on_discard_workspace(self, workspace_id: str) -> None:
        self.discard_builder_drone(workspace_id)
