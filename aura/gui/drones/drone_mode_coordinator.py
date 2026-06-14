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
from aura.drones.workshop_runner import DroneWorkshopRunner
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

        # Workshop runner references retained until their QThreads finish.
        self._workshop_runner: DroneWorkshopRunner | None = None
        self._workshop_thread: QThread | None = None
        self._retiring_workshop_runs: list[tuple[QThread, DroneWorkshopRunner | None]] = []
        self._pending_workshop_model: str = ""
        self._pending_workshop_thinking: str = "off"

        # Build dispatch guard — track whether we are awaiting build completion.
        self._awaiting_build_result: bool = False
        self._pending_build_dispatch_spec: dict | None = None

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
        self._workspace_pane.planner_model_changed.connect(
            lambda model: self._bridge.set_planner_model(model)
            if hasattr(self._bridge, 'set_planner_model') else None
        )
        self._workspace_pane.worker_model_changed.connect(
            self._bridge.set_worker_model
        )

        self._drone_mode: bool = False
        self._workspace_root: Path | None = None

        # Bridge auto-chain signals.
        self._bridge.finished.connect(self._on_bridge_finished)
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
        self._render_result(result)

        self._workspace_pane.refresh()

    def edit_builder_drone(self, workspace_id: str) -> None:
        """Enter drone mode and load a Drone that is still in the Builder."""
        if not self._drone_mode:
            self.enter_drone_mode(load_active=False)

        result = self._controller.load_workspace(workspace_id)
        self._render_result(result)
        self._workspace_pane.refresh()

    def discard_builder_drone(self, workspace_id: str) -> None:
        """Discard a Drone that is still in the Builder."""
        if self._workspace_root is None:
            return
        result = self._controller.load_workspace(workspace_id)
        if getattr(result, "kind", "") != "workspace_loaded":
            self._render_result(result)
            return
        result = self._controller.discard_workspace()
        self._workspace_pane.refresh()
        self._render_result(result)

    def _is_edit_workspace(self) -> bool:
        ws = self._controller.active_workspace
        return ws is not None and ws.mode == "edit"

    def is_drone_mode(self) -> bool:
        return self._drone_mode

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

        # Populate model combos from bridge providers and sync selections
        planner_provider = getattr(self._bridge, '_planner_provider', None)
        worker_provider = getattr(self._bridge, '_worker_provider', None)
        if planner_provider is not None and worker_provider is not None:
            self._workspace_pane.populate_models(planner_provider, worker_provider)
            planner_model = self._left_pane.current_planner_model()
            worker_model = self._left_pane.current_worker_model()
            self._workspace_pane.set_planner_model(planner_model)
            self._workspace_pane.set_worker_model(worker_model)

        if load_active:
            # Delegate to controller and render result.
            result = self._controller.enter_mode()
            self._render_result(result)

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

        # Tear down any active workshop runner.
        self._cancel_workshop()
        self._awaiting_build_result = False
        self._pending_build_dispatch_spec = None

        self._controller.exit_mode()
        self.drone_mode_changed.emit(False)

    def handle_message(self, payload, model: str, thinking) -> None:
        if self._bridge.is_running():
            self._chat.add_info("Busy", "Wait for the current response to finish.")
            return

        text = payload.text.strip() if hasattr(payload, "text") else str(payload)

        result = self._controller.handle_user_message(text)
        self._render_result(result, model, thinking)
        self._workspace_pane.refresh()

    # ------------------------------------------------------------------
    # Result rendering
    # ------------------------------------------------------------------

    def _render_result(self, result, model=None, thinking=None):
        kind = result.kind if hasattr(result, "kind") else "unknown"

        if kind == "mode_entered":
            if result.workspace_id is None:
                self._chat.add_info(
                    "Drone Builder",
                    "Describe the Drone you want to build.",
                )
            else:
                self._chat.add_info(
                    "Drone Builder",
                    "Drone mode active. Describe what you want to build.",
                )
        elif kind == "workspace_loaded":
            if self._is_edit_workspace():
                self._chat.add_info(
                    "Drone Builder",
                    f"Editing {result.display_name}. Tell me what should change.",
                )
            else:
                status = self._status_for_phase(result.phase)
                self._chat.add_info(
                    "Drone Builder",
                    f"{result.display_name} is {status}.",
                )
            self.drone_list_changed.emit()
        elif kind == "workshop_requested":
            self._run_workshop_llm(result.messages, model, thinking)
        elif kind == "workshop_question":
            self._chat.add_info("Drone Workshop", result.message)
        elif kind == "workshop_clarifying":
            self._chat.add_info("Drone Workshop", result.message)
        elif kind == "build_started":
            self._chat.add_info(
                "Drone Builder",
                f"Building Drone: {result.build_brief[:200]}",
            )
            self.drone_list_changed.emit()
            self._dispatch_build(result.dispatch_spec, model, thinking)
        elif kind == "build_completed":
            self._chat.add_info(
                "Drone Builder",
                f"Build complete: {result.drone_id or 'Drone'}",
            )
            self.drone_list_changed.emit()
        elif kind == "build_failed":
            self._chat.add_error("Build Failed", result.error)
            self.drone_list_changed.emit()
        elif kind == "readiness_running":
            self._chat.add_info("Drone Builder", "Checking the Drone...")
            self.drone_list_changed.emit()
        elif kind == "readiness_passed":
            self._chat.add_info("Drone Builder", "Check passed.")
            self.drone_list_changed.emit()
        elif kind == "readiness_failed":
            self._chat.add_error("Needs Fix", result.error)
            self.drone_list_changed.emit()
        elif kind == "awaiting_decision":
            self._chat.add_info(
                "Drone Ready",
                result.ready_message
                + "\n\nTell me any changes you want, or say new to build another Drone.",
            )
        elif kind == "installed":
            self._chat.add_info(
                "Drone Ready", f"{result.drone_name} is Ready in the Drone list."
            )
            self.drone_list_changed.emit()
            self.exit_drone_mode()
        elif kind == "discarded":
            self._chat.add_info("Drone Builder", "Drone discarded.")
            self.drone_list_changed.emit()
            self.exit_drone_mode()
        elif kind == "error":
            self._chat.add_error("Drone Builder", result.message)
            self.drone_list_changed.emit()

    # ------------------------------------------------------------------
    # Workshop LLM runner
    # ------------------------------------------------------------------

    def _run_workshop_llm(self, messages, model, thinking):
        """Run a workshop LLM call through DroneWorkshopRunner."""
        # Show the user's text in chat.
        self._chat.add_user(messages[-1]["content"] if messages else "")

        # Cancel any previous workshop run.
        self._cancel_workshop()

        provider_id = getattr(self._bridge, "_provider", None) or "deepseek"
        runner = DroneWorkshopRunner()
        thread = QThread()
        self._workshop_runner = runner
        self._workshop_thread = thread
        runner.contentDelta.connect(self._on_workshop_delta)
        runner.responseReady.connect(self._on_workshop_response)
        runner.apiError.connect(self._on_workshop_error)
        self._pending_workshop_model = model
        self._pending_workshop_thinking = thinking

        runner.configure(
            conversation=messages,
            provider_id=provider_id,
            model=model,
            thinking=thinking,
            temperature=0.4,
        )

        runner.moveToThread(thread)
        thread.started.connect(runner.do_run)
        runner.finished.connect(thread.quit)
        runner.finished.connect(runner.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda t=thread, r=runner: self._on_workshop_thread_finished(t, r))

        self._chat.begin_assistant()
        thread.start()

    @Slot(str)
    def _on_workshop_delta(self, text: str) -> None:
        self._chat.append_to_current(text)

    @Slot(object)
    def _on_workshop_response(self, response) -> None:
        result = self._controller.handle_workshop_response(
            response, response.raw_text
        )
        self._render_result(
            result,
            self._pending_workshop_model,
            self._pending_workshop_thinking,
        )
        self._workspace_pane.refresh()

    @Slot(int, str)
    def _on_workshop_error(self, status: int, message: str) -> None:
        self._chat.add_error("Workshop Error", message)

    @Slot()
    def _on_workshop_thread_finished(
        self,
        thread: QThread | None = None,
        runner: DroneWorkshopRunner | None = None,
    ) -> None:
        if thread is None and runner is None:
            self._workshop_thread = None
            self._workshop_runner = None
            return
        if thread is self._workshop_thread:
            self._workshop_thread = None
        if runner is not None and runner is self._workshop_runner:
            self._workshop_runner = None
        try:
            self._retiring_workshop_runs.remove((thread, runner))
        except ValueError:
            pass

    def _cancel_workshop(self) -> None:
        runner = self._workshop_runner
        thread = self._workshop_thread
        self._workshop_runner = None
        self._workshop_thread = None
        if runner is not None:
            runner.cancel()
        if thread is None:
            return
        self._retiring_workshop_runs.append((thread, runner))
        try:
            if thread.isRunning():
                thread.quit()
            else:
                self._on_workshop_thread_finished(thread, runner)
        except RuntimeError:
            self._on_workshop_thread_finished(thread, runner)

    # ------------------------------------------------------------------
    # Build dispatch
    # ------------------------------------------------------------------

    def _dispatch_build(self, dispatch_spec, model, thinking):
        """Dispatch the build spec directly to the Worker via an isolated lane.

        Uses the bridge's dispatch_drone_build() which creates a fresh
        History and ConversationManager — the parent project conversation
        history is never touched.
        """
        if not model or not thinking:
            self._chat.add_error(
                "Drone Builder",
                "Cannot start build: model or thinking mode is not configured.",
            )
            self._awaiting_build_result = False
            self._pending_build_dispatch_spec = None
            return

        self._awaiting_build_result = True
        self._pending_build_dispatch_spec = dispatch_spec

        from aura.conversation.dispatch import WorkerDispatchRequest

        req = WorkerDispatchRequest(
            goal=dispatch_spec.get("goal", "Build Drone"),
            files=list(dispatch_spec.get("files", [])),
            spec=dispatch_spec.get("spec", ""),
            acceptance=dispatch_spec.get("acceptance", ""),
            summary=dispatch_spec.get("summary", ""),
        )

        self._bridge.dispatch_drone_build(req)

    @Slot()
    def _on_bridge_finished(self) -> None:
        if self._awaiting_build_result:
            # The build completed after bridge finished.  The actual build
            # result is handled in _on_worker_finished, which fires before
            # bridge.finished.  We use this only as a fallback detach.
            pass

    @Slot(str, bool, str, bool, str)
    def _on_worker_finished(
        self, tool_id: str, ok: bool, summary: str, needs_followup: bool, status: str
    ) -> None:
        if not self._awaiting_build_result:
            return
        self._awaiting_build_result = False
        self._pending_build_dispatch_spec = None

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
        self._render_result(result)
        self._workspace_pane.refresh()

        if isinstance(result, BuildCompleted):
            # Auto-chain: build → readiness → ready
            self._run_readiness()
        elif isinstance(result, BuildFailed):
            # Stay in failed Builder state so user can revise.
            pass

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

        self._render_result(ReadinessRunning())

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
        self._render_result(ctrl_result)
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
            self._render_result(
                Installed(
                    drone_id=result.get("drone_id", ""),
                    drone_name=result.get("drone_name", "Drone"),
                )
            )
            self._workspace_pane.refresh()
            return

        ws.phase = WorkspacePhase.READINESS_FAILED.value
        ws.last_error = result.get("error", "Unknown readiness error")
        DroneWorkspaceStore.save_workspace(ws)
        self._render_result(
            ErrorResult(message=f"{ws.display_name} needs a fix: {ws.last_error}")
        )
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
        self._render_result(result)

    def _on_new_workspace(self) -> None:
        if self._workspace_root is None:
            return
        result = self._controller.create_workspace()
        self._workspace_pane.refresh()
        self._render_result(result)

    def _on_discard_workspace(self, workspace_id: str) -> None:
        if self._workspace_root is None:
            return
        result = self._controller.load_workspace(workspace_id)
        if getattr(result, "kind", "") != "workspace_loaded":
            self._render_result(result)
            return
        self._controller.discard_workspace()
        self._workspace_pane.refresh()
        self._render_result(Discarded(workspace_id=workspace_id))
