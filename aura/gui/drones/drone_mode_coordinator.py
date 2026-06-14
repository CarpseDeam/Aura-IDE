from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QFrame, QSplitter

from aura.drones.architect.controller import DroneArchitectController
from aura.drones.architect.results import (
    BuildCompleted,
    BuildFailed,
    Discarded,
    ErrorResult,
    Installed,
    ReadinessPassed,
    ReadinessRunning,
)
from aura.drones.workspaces.model import WorkspacePhase
from aura.drones.workspaces.store import DroneWorkspaceStore
from aura.gui.drones.drone_workspace_pane import DroneWorkspacePane

logger = logging.getLogger(__name__)


class DroneModeCoordinator(QObject):
    """Thin UI adapter that delegates lifecycle to DroneArchitectController."""

    drone_mode_changed = Signal(bool)
    drone_list_changed = Signal()

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
        self._workspace_pane.discard_workspace_requested.connect(
            self._on_discard_workspace
        )


        self._drone_mode: bool = False
        self._workspace_root: Path | None = None

        # Bridge auto-chain signal.
        self._bridge.workerFinished.connect(self._on_worker_finished)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_workspace_root(self, root: Path | None) -> None:
        self._workspace_root = root
        self._controller.set_workspace_root(root)
        self._workspace_pane.set_project_root(root)

    def edit_installed_drone(self, drone_id: str) -> None:
        """Enter drone mode and load the edit workspace for an installed Drone."""
        if not self._drone_mode:
            self.enter_drone_mode(load_active=False)

        result = self._controller.load_drone_workspace(drone_id)

        self._workspace_pane.refresh()
        ws = self._controller.active_workspace
        self._workspace_pane.set_active_workspace_id(ws.workspace_id if ws else None)

    def edit_builder_drone(self, workspace_id: str) -> None:
        """Enter drone mode and load a Drone that is still in the Builder."""
        if not self._drone_mode:
            self.enter_drone_mode(load_active=False)

        result = self._controller.load_workspace(workspace_id)
        self._workspace_pane.refresh()
        ws = self._controller.active_workspace
        self._workspace_pane.set_active_workspace_id(ws.workspace_id if ws else None)

    def discard_builder_drone(self, workspace_id: str) -> None:
        """Discard a Drone that is still in the Builder."""
        if self._workspace_root is None:
            return
        result = self._controller.load_workspace(workspace_id)
        if getattr(result, "kind", "") != "workspace_loaded":
            return
        result = self._controller.discard_workspace()
        self._workspace_pane.refresh()
        self._workspace_pane.set_active_workspace_id(None)

    def _is_edit_workspace(self) -> bool:
        ws = self._controller.active_workspace
        return ws is not None and ws.mode == "edit"

    def is_drone_mode(self) -> bool:
        return self._drone_mode

    def active_drone_context(self) -> str:
        """Return drone context for injection into model history text.

        Returns an empty string if drone mode is not active or no workspace
        is loaded.
        """
        if not self._drone_mode:
            return ""
        ws = self._controller.active_workspace
        if ws is None or self._workspace_root is None:
            return ""
        from aura.drones.workspaces.paths import candidate_dir, workspace_folder

        cand = candidate_dir(self._workspace_root, ws.workspace_id)
        ws_dir = workspace_folder(self._workspace_root, ws.workspace_id)
        return (
            f"[Drone Mode Active]\n"
            f"You are building or editing a folder-backed Drone: "
            f'"{ws.display_name}" (workspace: {ws.workspace_id}).\n'
            f"The Drone's candidate source folder is: {cand}\n"
            f"The Drone workspace is: {ws_dir}/\n"
            f"When you dispatch a Worker to build or update this Drone, "
            f"it should output files into the candidate folder above. "
            f"After the Worker finishes, the system will automatically "
            f"run readiness checks and install the Drone."
        )

    def enter_drone_mode(self, *, load_active: bool = True) -> None:
        if self._drone_mode:
            return
        self._drone_mode = True
        self._workspace_pane.refresh()

        # Swap left pane for workspace pane in the splitter
        idx = self._main_splitter.indexOf(self._left_pane)
        if idx >= 0:
            self._left_pane.hide()
            self._main_splitter.replaceWidget(idx, self._workspace_pane)
            self._workspace_pane.show()

        self._input.set_drone_architect_mode(True)
        self._status_bar.set_drone_architect_mode(True)
        self.drone_mode_changed.emit(True)

        if load_active:
            self._controller.enter_mode()
        ws = self._controller.active_workspace
        self._workspace_pane.set_active_workspace_id(ws.workspace_id if ws else None)

    def exit_drone_mode(self) -> None:
        if not self._drone_mode:
            return
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
        self.drone_mode_changed.emit(False)







    @Slot(str, bool, str, bool, str)
    def _on_worker_finished(
        self, tool_id: str, ok: bool, summary: str, needs_followup: bool, status: str
    ) -> None:
        if not self._drone_mode:
            return
        ws = self._controller.active_workspace
        if ws is None or self._workspace_root is None:
            return
        # Only auto-chain when the workspace is in a build phase.
        if ws.phase not in (WorkspacePhase.BUILDING.value, WorkspacePhase.ITERATING.value):
            return

        failure_detail = None
        if not ok:
            failure_detail = {
                "summary": summary,
                "status": status,
                "needs_followup": needs_followup,
                "metadata": self._worker_result_metadata(tool_id),
            }

        result = self._controller.on_build_completed(
            ok,
            error=None if ok else summary,
            failure_detail=failure_detail,
        )
        self._workspace_pane.refresh()

        if isinstance(result, BuildCompleted):
            self._chat.add_info("Drone Builder", "Build complete. Running readiness checks...")
            self._run_readiness()
        elif isinstance(result, BuildFailed):
            self._chat.add_error("Build Failed", summary)

    def _worker_result_metadata(self, tool_id: str) -> dict:
        getter = getattr(self._bridge, "worker_result_metadata", None)
        if not callable(getter):
            return {}
        metadata = getter(tool_id)
        return metadata if isinstance(metadata, dict) else {}

    # ------------------------------------------------------------------
    # Readiness
    # ------------------------------------------------------------------

    def _run_readiness(self) -> None:
        if self._controller.active_workspace is None:
            return
        if self._workspace_root is None:
            return

        self._chat.add_info("Drone Builder", "Checking the Drone...")

        ws = self._controller.active_workspace

        def _do_readiness():
            from aura.drones.folder_runner import run_drone_readiness
            from aura.drones.store import DroneStore
            from aura.drones.workspaces.paths import candidate_dir

            project_root = Path(ws.project_root)
            cand = candidate_dir(project_root, ws.workspace_id)
            try:
                drone = DroneStore.load_drone_from_folder(cand)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

            return run_drone_readiness(cand, drone, self._workspace_root)

        self._run_in_thread(_do_readiness, self._on_readiness_done)

    def _on_readiness_done(self, result: dict) -> None:
        ctrl_result = self._controller.on_readiness_completed(result)
        self._workspace_pane.refresh()

        if isinstance(ctrl_result, ReadinessPassed):
            self._start_ready_step()

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
                logger.exception("Failed to make Drone ready")
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

        ws.phase = WorkspacePhase.READINESS_FAILED.value
        ws.last_error = result.get("error", "Unknown readiness error")
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
        if phase in (
            WorkspacePhase.READINESS_RUNNING.value,
            WorkspacePhase.INSTALLING.value,
            WorkspacePhase.AWAITING_DECISION.value,
        ):
            return "Testing"
        if phase == WorkspacePhase.READINESS_FAILED.value:
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
                    result = {"ok": False, "error": str(exc)}
                self.done.emit(result)

        bg_thread = QThread()
        bg_worker = _Worker()
        bg_worker.moveToThread(bg_thread)
        bg_thread.started.connect(bg_worker.run)
        bg_worker.done.connect(callback)
        bg_worker.done.connect(bg_thread.quit)
        bg_worker.done.connect(bg_worker.deleteLater)
        bg_thread.finished.connect(bg_thread.deleteLater)

        # Hold references so Qt doesn't destroy them mid-run.
        self._background_threads.append(bg_thread)
        self._background_workers.append(bg_worker)

        def _remove_refs(t=bg_thread, w=bg_worker):
            if t in self._background_threads:
                self._background_threads.remove(t)
            if w in self._background_workers:
                self._background_workers.remove(w)

        bg_worker.done.connect(_remove_refs)

        bg_thread.start()



    # ------------------------------------------------------------------
    # Workspace pane callbacks
    # ------------------------------------------------------------------

    def _on_workspace_selected(self, workspace_id: str) -> None:
        if self._workspace_root is None:
            return
        result = self._controller.load_workspace(workspace_id)
        self._workspace_pane.refresh()
        ws = self._controller.active_workspace
        self._workspace_pane.set_active_workspace_id(ws.workspace_id if ws else None)

    def _on_new_workspace(self) -> None:
        if self._workspace_root is None:
            return
        result = self._controller.create_workspace()
        self._workspace_pane.refresh()
        ws = self._controller.active_workspace
        self._workspace_pane.set_active_workspace_id(ws.workspace_id if ws else None)

    def _on_discard_workspace(self, workspace_id: str) -> None:
        if self._workspace_root is None:
            return
        result = self._controller.load_workspace(workspace_id)
        if getattr(result, "kind", "") != "workspace_loaded":
            return
        self._controller.discard_workspace()
        self._workspace_pane.refresh()
