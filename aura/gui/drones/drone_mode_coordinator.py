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
from aura.drones.workspaces.paths import candidate_dir, edit_candidate_dir, workspace_folder
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
        self._workspace_pane.edit_ready.connect(self.edit_ready_drone)
        self._workspace_pane.thread_selected.connect(self._on_thread_selected)
        self._workspace_pane.back_to_project_requested.connect(self.exit_drone_mode)

        # Active build tool state for cancellation after drone mode exit.
        self._active_drone_build_tool_id: str | None = None
        self._active_thread_id: str | None = None
        self._workspace_root: Path | None = None
        self._drone_mode: bool = False
        self._suspended_project_messages: list[dict] | None = None

    def set_workspace_root(self, root: Path | None) -> None:
        self._workspace_root = root
        self._controller.set_workspace_root(root)
        self._workspace_pane.set_project_root(root)
        if root is not None:
            DroneWorkspaceStore.migrate_stale_folders(root)

    def edit_ready_drone(self, drone_id: str) -> None:
        """Open a Drone in the architect for editing."""
        result = self._controller.load_drone_workspace(drone_id)
        self._workspace_pane.refresh()
        if isinstance(result, ErrorResult):
            self._chat.add_error("Drone Builder", f"Could not load Drone: {result.message}")
            return
        self._activate_workspace()

    def edit_ready_drone_by_folder(self, drone_id: str, folder: Path) -> None:
        """Open a Drone in the architect using the canonical folder directly, bypassing global rediscovery."""
        result = self._controller.load_drone_workspace_by_folder(drone_id, folder)
        self._workspace_pane.refresh()
        if isinstance(result, ErrorResult):
            self._chat.add_error("Drone Builder", f"Could not load Drone: {result.message}")
            return
        self._activate_workspace()

    def edit_builder_drone(self, workspace_id: str) -> None:
        """Open a builder workspace for the user to continue working."""
        result = self._controller.load_workspace(workspace_id)
        self._workspace_pane.refresh()
        if isinstance(result, ErrorResult):
            self._chat.add_error("Drone Builder", f"Could not load workspace: {result.message}")
            return
        self._activate_workspace()

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
        cand = edit_candidate_dir(ws)
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
            f"The Drone folder itself is the runnable Drone — no separate install step.\n"
            f"The Worker must NOT write files outside the candidate folder, unless the user\n"
            f"explicitly asks for a project code change (not a Drone change)."
        )

    def _is_drone_ui_attached(self) -> bool:
        """Return True when the workspace pane is visible and the left pane is hidden."""
        return (
            self._main_splitter.indexOf(self._workspace_pane) >= 0
            and self._workspace_pane.isVisible()
            and not self._left_pane.isVisible()
        )

    def _attach_drone_ui(self) -> bool:
        """Idempotent and recoverable — always returns True."""
        if self._main_splitter.indexOf(self._workspace_pane) >= 0:
            self._workspace_pane.show()
            self._left_pane.hide()
            return True
        if self._main_splitter.indexOf(self._left_pane) >= 0:
            idx = self._main_splitter.indexOf(self._left_pane)
            self._left_pane.hide()
            self._main_splitter.replaceWidget(idx, self._workspace_pane)
            self._workspace_pane.show()
            return True
        logger.warning(
            "Neither left pane nor workspace pane found in splitter "
            "— inserting workspace pane at index 0"
        )
        self._main_splitter.insertWidget(0, self._workspace_pane)
        self._workspace_pane.show()
        self._left_pane.hide()
        return True

    def _attach_project_ui(self) -> bool:
        """Idempotent and recoverable — always returns True."""
        if self._main_splitter.indexOf(self._left_pane) >= 0:
            self._left_pane.show()
            self._workspace_pane.hide()
            return True
        if self._main_splitter.indexOf(self._workspace_pane) >= 0:
            idx = self._main_splitter.indexOf(self._workspace_pane)
            self._workspace_pane.hide()
            self._main_splitter.replaceWidget(idx, self._left_pane)
            self._left_pane.show()
            return True
        logger.warning(
            "Neither left pane nor workspace pane found in splitter "
            "— inserting left pane at index 0"
        )
        self._main_splitter.insertWidget(0, self._left_pane)
        self._left_pane.show()
        self._workspace_pane.hide()
        return True

    def _restore_project_chat(self) -> None:
        """Restore suspended project messages into bridge and chat, then clear the saved copy."""
        if self._suspended_project_messages is not None:
            self._bridge.reset_history()
            self._bridge.history.messages = copy.deepcopy(self._suspended_project_messages)
            self._chat.reset()
            self._chat.replay_messages(self._suspended_project_messages)
            self._suspended_project_messages = None

    def enter_drone_mode(self, *, load_active: bool = True) -> None:
        # Reconcile stale state vs fresh entry
        if self._drone_mode and self._is_drone_ui_attached():
            return  # already properly in drone mode

        entering_fresh = False
        if self._drone_mode and not self._is_drone_ui_attached():
            # Stale state — repair without re-saving project messages
            logger.warning(
                "Drone mode stale state detected: _drone_mode=%s but drone UI not visible — repairing",
                self._drone_mode,
            )
        else:
            entering_fresh = True
            # Save/suspend the current project conversation before swapping to Drone mode
            self._suspended_project_messages = copy.deepcopy(self._bridge.history.messages)
            self._bridge.reset_history()
            self._chat.reset()

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
        if not self._attach_drone_ui():
            logger.error("Failed to attach drone UI")
            self._chat.add_error("Drone Builder", "Could not switch to Drone mode: left pane not found.")
            if entering_fresh:
                self._restore_project_chat()  # undo the bridge reset
            return

        self._drone_mode = True
        self._input.set_drone_architect_mode(True)
        self._status_bar.set_drone_architect_mode(True)
        self.drone_mode_changed.emit(True)

    def exit_drone_mode(self, *, restore_project_chat: bool = True) -> None:
        if not self._drone_mode and not self._is_drone_ui_attached():
            return
        self._save_current_thread()
        self._active_thread_id = None
        self._drone_mode = False

        # Restore the suspended project chat if requested
        if self._suspended_project_messages is not None:
            if restore_project_chat:
                self._restore_project_chat()
            else:
                self._suspended_project_messages = None

        # Swap workspace pane back to left pane
        self._attach_project_ui()

        self._input.set_drone_architect_mode(False)
        self._status_bar.set_drone_architect_mode(False)
        self.drone_mode_changed.emit(False)

        self._workspace_pane.set_active_workspace_id(None)
        self._controller.exit_mode()

    def handle_drone_toggle(self) -> None:
        """Toggle drone mode on/off based on current state."""
        if self._drone_mode and self._is_drone_ui_attached():
            self.exit_drone_mode()
        else:
            self.enter_drone_mode()

    def _activate_workspace(self, thread_id: str | None = None) -> None:
        """Centralized workspace activation.

        Must be called after the controller has loaded the target workspace
        (via load_workspace, load_drone_workspace, or load_drone_workspace_by_folder).
        Enters drone mode if not already in it, ensures a thread exists,
        loads the thread into the UI, and updates the pane.
        """
        ws = self._controller.active_workspace
        if ws is None:
            return
        if thread_id is None:
            thread = self._ensure_thread_for_workspace(ws.workspace_id)
            thread_id = thread.id if thread else None
        self._active_thread_id = thread_id

        # Enter drone mode first (saves project chat, resets bridge/chat, swaps pane)
        self.enter_drone_mode(load_active=False)

        # Then load the thread into the (now empty) UI
        if thread_id:
            self._load_thread_into_ui(ws.workspace_id, thread_id)

        self._workspace_pane.set_active(ws.workspace_id, thread_id)
        self._input.focus_editor()

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
            self._chat.add_info("Drone Builder", "Build complete — Drone ready.")
        elif isinstance(result, BuildFailed):
            self._chat.add_error("Build Failed", summary)

        self._active_drone_build_tool_id = None

    def _worker_result_metadata(self, tool_id: str) -> dict:
        getter = getattr(self._bridge, "worker_result_metadata", None)
        if not callable(getter):
            return {}
        metadata = getter(tool_id)
        return metadata if isinstance(metadata, dict) else {}

    @staticmethod
    def _status_for_phase(phase: str) -> str:
        if phase == WorkspacePhase.WORKSHOP.value:
            return "Draft"
        if phase in (WorkspacePhase.BUILDING.value, WorkspacePhase.ITERATING.value):
            return "Building"
        if phase == WorkspacePhase.READY.value:
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
            saved_id = ws.active_thread_id
            if saved_id:
                for t in threads:
                    if t.id == saved_id:
                        return t
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
        messages = copy.deepcopy(self._bridge.history.messages)
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
        self._bridge.history.messages = copy.deepcopy(thread.messages)
        self._chat.reset()
        self._chat.replay_messages(thread.messages)

    def _on_thread_selected(self, workspace_id: str, thread_id: str) -> None:
        """Switch to a different thread in the current or a different workspace."""
        self._save_current_thread()
        result = self._controller.load_workspace(workspace_id)
        if isinstance(result, ErrorResult):
            self._chat.add_error("Drone Builder", f"Could not load workspace: {result.message}")
            return
        self._activate_workspace(thread_id=thread_id)

    def _resolve_drone_name(self, ws: DroneWorkspace) -> str:
        """Resolve the best display name for a workspace context.

        Priority: candidate drone.json name (from edit_source_folder or candidate dir)
                  -> installed Drone name -> display_name -> workspace_id.
        """
        wid = ws.workspace_id
        project_root = Path(ws.project_root)
        # 1. Candidate drone.json — use edit_source_folder if set
        try:
            if ws.edit_source_folder:
                cand_folder = Path(ws.edit_source_folder)
            else:
                cand_folder = candidate_dir(project_root, wid)
            drone_json = cand_folder / "drone.json"
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
        self._activate_workspace()

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
