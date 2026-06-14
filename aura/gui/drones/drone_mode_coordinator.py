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
    ProofResult,
    ProofRunning,
    ReadinessPassed,
    ReadinessRunning,
)
from aura.drones.workshop_runner import DroneWorkshopRunner
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

        # Workshop runner — created once, reused per workshop turn.
        self._workshop_runner: DroneWorkshopRunner | None = None
        self._workshop_thread: QThread | None = None
        self._pending_workshop_model: str = ""
        self._pending_workshop_thinking: str = "off"

        # Build dispatch guard — track whether we are awaiting build completion.
        self._awaiting_build_result: bool = False
        self._pending_build_dispatch_spec: dict | None = None

        # Saved bridge history for build dispatch isolation
        self._saved_bridge_messages: list | None = None
        self._saved_bridge_system: str | None = None

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
            self.enter_drone_mode()

        result = self._controller.load_drone_workspace(drone_id)
        self._render_result(result)

        self._workspace_pane.refresh()

    def _is_edit_workspace(self) -> bool:
        ws = self._controller.active_workspace
        return ws is not None and ws.mode == "edit"

    def is_drone_mode(self) -> bool:
        return self._drone_mode

    def enter_drone_mode(self) -> None:
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
                    "Drone Workspaces",
                    "No Drone workspaces yet. Describe the Drone you want to build.",
                )
            else:
                self._chat.add_info(
                    "Drone Workspaces",
                    "Drone mode active. Describe what you want to build.",
                )
        elif kind == "workspace_loaded":
            if self._is_edit_workspace():
                self._chat.add_info(
                    "Drone Workspace",
                    f"Editing installed Drone: {result.display_name}. The candidate is ready for your changes.",
                )
            else:
                self._chat.add_info(
                    "Drone Workspace",
                    f"Loaded '{result.display_name}' (phase: {result.phase}).",
                )
        elif kind == "workshop_requested":
            self._run_workshop_llm(result.messages, model, thinking)
        elif kind == "workshop_question":
            self._chat.add_info("Drone Workshop", result.message)
        elif kind == "workshop_clarifying":
            self._chat.add_info("Drone Workshop", result.message)
        elif kind == "build_started":
            self._chat.add_info(
                "Drone Architect",
                f"Building candidate: {result.build_brief[:200]}",
            )
            self._dispatch_build(result.dispatch_spec, model, thinking)
        elif kind == "build_completed":
            self._chat.add_info(
                "Drone Architect",
                f"Candidate built: {result.drone_id}",
            )
        elif kind == "build_failed":
            self._chat.add_error("Build Failed", result.error)
        elif kind == "readiness_running":
            self._chat.add_info("Drone Architect", "Running readiness check...")
        elif kind == "readiness_passed":
            self._chat.add_info("Drone Architect", "Readiness passed.")
        elif kind == "readiness_failed":
            self._chat.add_error("Readiness Failed", result.error)
        elif kind == "proof_running":
            self._chat.add_info("Drone Architect", "Running proof...")
        elif kind == "proof_completed":
            self._render_proof_card(result.proof_result)
        elif kind == "awaiting_decision":
            if self._is_edit_workspace():
                commands = "You can say: revise <feedback>, install, or discard."
            else:
                commands = "You can say: revise <feedback>, install, discard, or new."
            self._chat.add_info(
                "Drone Ready",
                result.proof_summary + "\n\n" + commands,
            )
        elif kind == "installed":
            if self._is_edit_workspace():
                self._chat.add_info(
                    "Drone Reinstalled",
                    f"{result.drone_name} — the updated Drone is ready to use.",
                )
            else:
                self._chat.add_info(
                    "Drone Installed", f"{result.drone_name} is ready to use."
                )
            self.drone_list_changed.emit()
            self.exit_drone_mode()
        elif kind == "discarded":
            self._chat.add_info("Drone Workspace", "Workspace discarded.")
            self.drone_list_changed.emit()
            self.exit_drone_mode()
        elif kind == "error":
            self._chat.add_error("Drone Architect", result.message)

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
        self._workshop_runner = DroneWorkshopRunner()
        self._workshop_runner.contentDelta.connect(self._on_workshop_delta)
        self._workshop_runner.responseReady.connect(self._on_workshop_response)
        self._workshop_runner.apiError.connect(self._on_workshop_error)
        self._pending_workshop_model = model
        self._pending_workshop_thinking = thinking

        self._workshop_runner.configure(
            conversation=messages,
            provider_id=provider_id,
            model=model,
            thinking=thinking,
            temperature=0.4,
        )

        self._workshop_thread = QThread()
        self._workshop_runner.moveToThread(self._workshop_thread)
        self._workshop_thread.started.connect(self._workshop_runner.do_run)
        self._workshop_runner.finished.connect(self._workshop_thread.quit)
        self._workshop_runner.finished.connect(self._workshop_runner.deleteLater)
        self._workshop_thread.finished.connect(self._workshop_thread.deleteLater)
        self._workshop_thread.finished.connect(self._on_workshop_thread_finished)

        self._chat.begin_assistant()
        self._workshop_thread.start()

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
    def _on_workshop_thread_finished(self) -> None:
        self._workshop_runner = None
        self._workshop_thread = None

    def _cancel_workshop(self) -> None:
        if self._workshop_runner is not None:
            self._workshop_runner.cancel()
            self._workshop_runner = None
        if self._workshop_thread is not None:
            self._workshop_thread.quit()
            self._workshop_thread.wait(1000)
            self._workshop_thread = None

    # ------------------------------------------------------------------
    # Build dispatch
    # ------------------------------------------------------------------

    def _dispatch_build(self, dispatch_spec, model, thinking):
        """Send the build dispatch spec through the bridge for Worker execution."""
        if not model or not thinking:
            self._chat.add_error(
                "Drone Architect",
                "Cannot start build: model or thinking mode is not configured.",
            )
            self._awaiting_build_result = False
            self._pending_build_dispatch_spec = None
            return

        self._awaiting_build_result = True
        self._pending_build_dispatch_spec = dispatch_spec

        # Save project conversation history before the internal build dispatch
        # overwrites it, so we can restore it afterward and prevent the build
        # dispatch from leaking into the project chat history.
        self._saved_bridge_messages = list(self._bridge.history.messages)
        self._saved_bridge_system = self._bridge.history.system_prompt
        self._bridge.suppress_auto_save = True

        # Format the dispatch spec as a user message for the Planner.
        # The Planner, in drone_architect mode, will dispatch to Worker.
        user_message = (
            "Dispatch the following Drone candidate build to the Worker.\n\n"
            f"Goal: {dispatch_spec.get('goal', 'Build Drone candidate')}\n\n"
            f"Files: {dispatch_spec.get('files', [])}\n\n"
            "Spec:\n"
            f"{dispatch_spec.get('spec', '')}\n\n"
            "Acceptance:\n"
            f"{dispatch_spec.get('acceptance', '')}"
        )

        from aura.drones.build_prompt import build_drone_architect_prompt
        self._bridge.set_system_prompt(build_drone_architect_prompt(for_candidate=True))

        # Clear residual project messages so the Planner only sees the build dispatch.
        self._bridge.history.messages.clear()

        self._bridge.history.append_user_text(user_message)
        self._chat.begin_assistant()
        self._bridge.send(model=model, thinking=thinking)

    @Slot()
    def _on_bridge_finished(self) -> None:
        if self._awaiting_build_result:
            # The build completed after bridge finished.  The actual build
            # result is handled in _on_worker_finished, which fires before
            # bridge.finished.  We use this only as a fallback detach.
            pass

        # Restore project conversation history after internal build dispatch.
        if self._saved_bridge_messages is not None:
            self._bridge.history.messages = self._saved_bridge_messages
            if self._saved_bridge_system is not None:
                self._bridge.history.system_prompt = self._saved_bridge_system
            self._saved_bridge_messages = None
            self._saved_bridge_system = None
        self._bridge.suppress_auto_save = False

    @Slot(str, bool, str, bool, str)
    def _on_worker_finished(
        self, tool_id: str, ok: bool, summary: str, needs_followup: bool, status: str
    ) -> None:
        if not self._awaiting_build_result:
            return
        self._awaiting_build_result = False
        self._pending_build_dispatch_spec = None

        result = self._controller.on_build_completed(ok)
        self._render_result(result)
        self._workspace_pane.refresh()

        if isinstance(result, BuildCompleted):
            # Auto-chain: build → readiness → proof
            self._run_readiness()
        elif isinstance(result, BuildFailed):
            # Stay in building phase so user can revise.
            pass

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
            self._run_proof()

    # ------------------------------------------------------------------
    # Proof
    # ------------------------------------------------------------------

    def _run_proof(self) -> None:
        if self._controller.active_workspace is None:
            return
        if self._workspace_root is None:
            return

        self._render_result(ProofRunning())

        ws = self._controller.active_workspace
        workspace_root = self._workspace_root

        def _do_proof():
            from aura.drones.architect.proof import run_candidate_proof
            return run_candidate_proof(workspace_root, ws)

        self._run_in_thread(_do_proof, self._on_proof_done)

    def _on_proof_done(self, proof_result) -> None:
        # Handle thread error dict shape from _run_in_thread.
        if isinstance(proof_result, dict) and not proof_result.get("ok", True):
            error = proof_result.get("error", "Unknown proof thread error")
            ws = self._controller.active_workspace
            proof_result = ProofResult(
                drone_name=ws.display_name if ws else "unknown",
                proof_status="failed",
                what_tried="Running proof",
                route_used="",
                output_sample="",
                errors=[error],
            )
        ctrl_result = self._controller.on_proof_completed(proof_result)
        self._render_result(ctrl_result)
        self._workspace_pane.refresh()
        next_result = self._controller.finalize_proof()
        self._render_result(next_result)
        self._workspace_pane.refresh()

    # ------------------------------------------------------------------
    # Proof result card
    # ------------------------------------------------------------------

    def _render_proof_card(self, proof_result) -> None:
        """Render a rich proof result card in chat."""
        pr = proof_result

        if pr is None:
            self._chat.add_info("Drone Proof", "No proof result available.")
            return

        status_icon = "\u2713" if pr.proof_status == "passed" else "\u26a0" if pr.proof_status == "warnings" else "\u2717"

        lines = [
            f"{status_icon} Proof: {pr.proof_status.upper()}",
            f"Drone: {pr.drone_name}",
            f"Tried: {pr.what_tried}",
        ]
        if pr.route_used:
            lines.append(f"Route: {pr.route_used}")
        if pr.output_sample:
            sample = pr.output_sample[:300]
            lines.append(f"Output sample: {sample}")
        if pr.warnings:
            lines.append(f"Warnings: {'; '.join(pr.warnings[:5])}")
        if pr.errors:
            lines.append(f"Errors: {'; '.join(pr.errors[:5])}")

        self._chat.add_info("Drone Proof", "\n".join(lines))

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
        self._controller.discard_workspace()
        self._workspace_pane.refresh()
        self._render_result(Discarded(workspace_id=workspace_id))
