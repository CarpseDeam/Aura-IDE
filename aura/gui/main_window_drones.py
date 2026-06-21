"""Drone responsibility cluster for MainWindow — extracted controller."""

from __future__ import annotations

import difflib
import json as json_module
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, QTimer, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from aura.conversation.tools._types import ApprovalDecision, ApprovalRequest
from aura.drones.construction_context import enter_drone_construction
from aura.drones.definition import DroneBudget, DroneDefinition
from aura.drones.receipt import DroneReceipt
from aura.drones.runner import DroneRunner
from aura.drones.store import (
    DroneStore,
    RunHistoryStore,
    _global_drones_root,
    _project_root_for_drone_storage,
)
from aura.gui.drones.drone_run_card import DroneRunCard
from aura.gui.drones.drone_summon_card import DroneSummonCard
from aura.gui.drones.drone_workbay_window import DroneWorkbayWindow

if TYPE_CHECKING:
    from aura.gui.main_window import MainWindow

logger = logging.getLogger(__name__)

# Parallel read-only Drone limit — preserve existing project behaviour.
MAX_PARALLEL_READ_ONLY_DRONES = 3


class MainWindowDroneController(QObject):
    """Owns the Drone lifecycle responsibility cluster for MainWindow.

    Stores a reference to the parent MainWindow and delegates to its
    attributes/methods for workspace, UI, and signal access.
    """

    def __init__(self, window: MainWindow, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._window = window

        # Drone runner state. Read-only Drones can run in parallel; write-capable
        # Drones remain exclusive because they share the write approval lane.
        self._drone_workbay_window: DroneWorkbayWindow | None = None
        self._drone_runner: DroneRunner | None = None
        self._drone_runner_thread: QThread | None = None
        self._active_run_card: QWidget | None = None
        self._drone_runs: dict[str, dict] = {}
        self._write_drone_run_id: str | None = None
        self._drone_receipt: DroneReceipt | None = None
        self._pending_drone_summons: dict[str, dict[str, str]] = {}
        self._looping_drones: dict[str, int] = {}  # drone_id -> interval_seconds

    # -- properties for external read access --

    @property
    def drone_workbay_window(self) -> DroneWorkbayWindow | None:
        return self._drone_workbay_window

    @property
    def drone_runner(self) -> DroneRunner | None:
        return self._drone_runner

    @property
    def active_run_card(self) -> QWidget | None:
        return self._active_run_card

    @property
    def drone_runs(self) -> dict[str, dict]:
        return self._drone_runs

    # -- helpers --

    def is_workbay_open(self) -> bool:
        return bool(self._drone_workbay_window and self._drone_workbay_window.is_open())

    def hide_workbay(self) -> None:
        if self._drone_workbay_window is not None:
            self._drone_workbay_window.hide()

    # -- drone folder / create --

    def on_drone_folder_selected(self, drone_id: str) -> None:
        w = self._window
        if drone_id.startswith("project:"):
            folder = Path(drone_id.replace("project:", "", 1))
            if not folder.is_dir():
                return
        else:
            base = _global_drones_root(w._workspace_root)
            folder = base / drone_id
            if not folder.is_dir():
                logger.warning("Drone folder not found: %s", folder)
                return

        # Ensure workspace root is always the project root, never a drone folder.
        project_root = _project_root_for_drone_storage(w._workspace_root)
        if (
            w._workspace_root is None
            or w._workspace_root.resolve() != project_root.resolve()
        ):
            w._workspace_controller._retarget_workspace(project_root)

        # Only the tree view focuses on the drone folder.
        w._tree.set_root(folder)

        # Refresh drone sidebar (pass the drone folder for highlight)
        w._left_pane.refresh_drones(folder)

        # Mark construction context
        enter_drone_construction("existing", folder.name)

    def on_create_drone(self) -> None:
        # Ensure workspace is rooted at the project root
        project_root = _project_root_for_drone_storage(self._window._workspace_root)
        project_resolved = project_root.resolve()
        current = (
            self._window._workspace_root.resolve()
            if self._window._workspace_root
            else None
        )
        if current is None or current != project_resolved:
            self._window._workspace_controller._retarget_workspace(
                project_root, restore_last=False
            )

        drone_id = f"drone-{uuid4().hex[:8]}"
        drone_dir = _global_drones_root(self._window._workspace_root) / drone_id
        drone_dir.mkdir(parents=True, exist_ok=True)

        # ── Write valid drone.json ──
        now = datetime.now(timezone.utc).isoformat()
        drone = DroneDefinition(
            id=drone_id,
            name="New Drone",
            description="A new Drone scaffold",
            instructions="You are a helpful Drone. Follow the user's instructions to complete the task.",
            write_policy="read_only",
            runtime="python",
            entrypoint={
                "kind": "command",
                "command": ["python", "main.py"],
                "protocol": "json-stdio",
            },
            budget=DroneBudget(timeout_seconds=60),
            scope="global",
            manifest_version="1",
            input_contract={
                "type": "object",
                "description": "Standard drone input: goal plus workspace context",
                "schema": {"goal": "string", "workspace_root": "string"},
            },
            cargo_contract={
                "type": "object",
                "description": "Standard drone cargo: structured result data",
                "schema": {"drone_id": "string", "ready": "bool"},
            },
            output_contract={
                "description": "Standard drone output",
                "properties": {
                    "ok": {"type": "boolean"},
                    "summary": {"type": "string"},
                },
                "required": ["ok", "summary"],
            },
            created_at=now,
            updated_at=now,
            created_by="user",
        )
        DroneStore._write_manifest(drone_dir, drone)

        # ── Write scaffold main.py ──
        main_py = (
            '"""Scaffold Drone: '
            + drone_id
            + '"""\n'
            "import json\n"
            "import sys\n\n"
            "def main() -> None:\n"
            "    raw = sys.stdin.read()\n"
            "    payload = json.loads(raw) if raw.strip() else {}\n"
            "    goal = payload.get(\"goal\", \"\")\n"
            "    cargo = payload.get(\"cargo\", {})\n"
            "    result = {\n"
            '        "ok": True,\n'
            '        "summary": "New Drone scaffold is ready.",\n'
            '        "cargo": {\n'
            '            "drone_id": "'
            + drone_id
            + '",\n'
            '            "ready": True,\n'
            "        },\n"
            "    }\n"
            "    sys.stdout.write(json.dumps(result))\n\n"
            'if __name__ == "__main__":\n'
            "    main()\n"
        )
        (drone_dir / "main.py").write_text(main_py, encoding="utf-8")

        # Focus the tree view on the new drone folder without moving the workspace root.
        self._window._tree.set_root(drone_dir)
        self._window._left_pane.refresh_drones(drone_dir)

        # Enter Drone construction mode
        enter_drone_construction("new", drone_id)

        # Refresh Workbay roster when Workbay is open
        if (
            self._drone_workbay_window is not None
            and self._drone_workbay_window.is_open()
        ):
            self._drone_workbay_window.chain_editor.refresh_roster()

    # -- Drone Bay --

    def on_drone_bay_requested(self) -> None:
        self.open_or_toggle_drone_workbay()
        self.sync_drone_tab_checked()
        self._window._position_edge_tabs()

    def open_or_toggle_drone_workbay(self) -> None:
        """Open the Drone Workbay as a standalone window or focus it."""
        if self._window._workspace_root is None:
            return
        if self._drone_workbay_window is not None:
            if self._drone_workbay_window.isVisible():
                self._drone_workbay_window.raise_()
                self._drone_workbay_window.activateWindow()
            else:
                self._drone_workbay_window.show_and_raise()
            return

        self._drone_workbay_window = DroneWorkbayWindow(
            workspace_root=self._window._workspace_root,
            initial_geometry=self._window._settings.drone_workbay_window_geometry,
            parent=None,
        )
        workbay = self._drone_workbay_window
        workbay.runDroneRequested.connect(self.on_launch_drone)
        workbay.deleteDroneRequested.connect(self.on_delete_drone)
        workbay.loopDroneRequested.connect(self.on_loop_drone_toggled)
        workbay.loopIntervalChanged.connect(self.on_loop_interval_changed)
        workbay.geometry_saved.connect(self._window._on_drone_workbay_geometry_saved)
        workbay.show_and_raise()

    def sync_drone_tab_checked(self) -> None:
        if self._window._edge_rail.drone_tab is not None:
            workbay_open = (
                self._drone_workbay_window.isVisible()
                if self._drone_workbay_window
                else False
            )
            is_open = workbay_open or self._window._drone_reports_window.is_open()
            self._window._edge_rail.drone_tab.setChecked(is_open)

    # -- delete / refresh --

    def on_delete_drone(self, drone_id: str) -> None:
        reply = QMessageBox.question(
            self._window,
            "Delete Drone",
            "Are you sure you want to delete this drone?\n\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            DroneStore.delete_drone(self._window._workspace_root, drone_id)
            self.refresh_drone_context()

    def refresh_drone_context(self) -> None:
        refresher = getattr(self._window._bridge, "refresh_tier1_context", None)
        if callable(refresher):
            refresher()

    # -- launch / run lifecycle --

    def on_launch_drone(self, drone_id: str, folder: str = "") -> None:
        """Launch a Drone (read-only or write-capable)."""
        if self._window._workspace_root is None:
            return

        if folder and Path(folder).is_dir():
            drone = DroneStore.load_drone_from_folder(Path(folder))
        else:
            drone = DroneStore.load_drone(self._window._workspace_root, drone_id)
        if drone is None:
            return
        self.start_drone_run(drone)

    def update_workbay_card_run_state(
        self, drone_id: str, state: str, detail: str = ""
    ) -> None:
        if (
            self._drone_workbay_window is not None
            and self._drone_workbay_window.isVisible()
        ):
            self._drone_workbay_window.set_card_run_state(drone_id, state, detail)

    def start_drone_run(
        self,
        drone: DroneDefinition,
        summon_goal: str = "",
        loop_drone_id: str = "",
    ) -> None:
        """Start a Drone run from a saved Drone or an Aura-summoned goal."""
        if self._window._workspace_root is None:
            return
        run_drone = (
            self.drone_for_summoned_goal(drone, summon_goal) if summon_goal else drone
        )
        if not self.can_start_drone(run_drone):
            return

        run_card = DroneRunCard(
            run_drone, parent=self._window._drone_reports_window
        )
        thread = QThread(self._window)
        runner = DroneRunner(
            workspace_root=self._window._workspace_root,
            drone=run_drone,
            provider_id=self._window._settings.worker_provider,
            model=self._window.current_worker_model(),
            auto_approve=self._window._settings.auto_approve,
            bridge=self._window._bridge,
            parent=None,
        )
        runner.moveToThread(thread)
        run_id = runner.run_state.run_id
        self._drone_runs[run_id] = {
            "runner": runner,
            "thread": thread,
            "card": run_card,
            "drone": run_drone,
            "loop_drone_id": loop_drone_id,
        }
        if run_drone.write_policy != "read_only":
            self._write_drone_run_id = run_id
        self._drone_runner = runner
        self._window._companion.set_drone_runner(self._drone_runner)
        self._drone_runner_thread = thread
        self._active_run_card = run_card
        self._window._drone_reports_window.add_run_card(run_id, run_card)

        # Connect signals.
        runner.statusChanged.connect(run_card.on_status_changed)
        runner.statusChanged.connect(
            lambda status, rid=run_id, name=run_drone.name: self._window.droneStatusChangedOnUiThread.emit(
                rid, name, status
            )
        )
        runner.contentDelta.connect(run_card.on_content_delta)
        runner.toolCallStart.connect(run_card.on_tool_call_start)
        runner.toolCallArgsDelta.connect(run_card.on_tool_call_args)
        runner.toolResult.connect(run_card.on_tool_result)
        runner.apiError.connect(run_card.on_api_error)
        runner.receiptReady.connect(run_card.on_receipt_ready)
        runner.receiptReady.connect(
            lambda receipt, rid=run_id: self._window.droneReceiptReadyOnUiThread.emit(
                receipt, rid
            )
        )
        runner.finished.connect(
            lambda rid=run_id: self._window.droneRunFinishedOnUiThread.emit(rid)
        )

        # Standard Qt worker lifetime cleanup — no blocking wait on GUI thread.
        runner.finished.connect(thread.quit)
        runner.finished.connect(runner.deleteLater)
        thread.finished.connect(thread.deleteLater)

        # Wire approval for write-capable drones.
        if run_drone.write_policy != "read_only":
            runner.approval_requested.connect(
                lambda request, r=runner, rid=run_id, name=run_drone.name: (
                    self.on_drone_approval_requested(request, r, rid, name)
                )
            )

        # Wire cancel button.
        run_card.cancelRequested.connect(
            lambda rid=run_id: self.on_cancel_drone_run(rid)
        )

        self._window._edge_rail.add_drone_run_pip(run_id, run_drone.name)
        # Notify workbay card that this drone is running
        card_id = loop_drone_id if loop_drone_id else run_drone.id
        self.update_workbay_card_run_state(card_id, "running")
        self._window._position_edge_tabs()

        # Start the thread.
        thread.started.connect(runner.run)
        thread.start()

    def can_start_drone(self, drone: DroneDefinition) -> bool:
        active_runs = []
        for record in self._drone_runs.values():
            try:
                if record["runner"].run_state.is_active:
                    active_runs.append(record)
            except RuntimeError:
                # C++ QObject already freed (deleteLater on worker thread fired
                # before on_drone_finished ran on main thread).  Treat as finished.
                logger.debug(
                    "[DroneRun] can_start_drone: runner C++ object already deleted"
                )

        has_write_active = any(
            record["drone"].write_policy != "read_only" for record in active_runs
        )
        if drone.write_policy != "read_only":
            if active_runs:
                QMessageBox.information(
                    self._window,
                    "Drone Bay",
                    "Write-capable Drones use the shared write lane. Wait for active Drone runs to finish first.",
                )
                return False
            return True
        if has_write_active:
            QMessageBox.information(
                self._window,
                "Drone Bay",
                "A write-capable Drone is active. Read-only parallel Drones can start after it finishes.",
            )
            return False
        if len(active_runs) >= MAX_PARALLEL_READ_ONLY_DRONES:
            QMessageBox.information(
                self._window,
                "Drone Bay",
                f"Up to {MAX_PARALLEL_READ_ONLY_DRONES} read-only Drones can run at once.",
            )
            return False
        return True

    def drone_for_summoned_goal(self, drone: DroneDefinition, summon_goal: str) -> DroneDefinition:
        return replace(
            drone,
            instructions=(
                f"[Aura-summoned goal]\n{summon_goal}\n\n"
                f"--- Original instructions ---\n{drone.instructions}"
            ),
        )

    # -- summon / confirm / cancel --

    def handle_summon_drone_result(self, tool_id: str, extras: dict) -> None:
        if self._window._workspace_root is None:
            return
        drone_id = str(extras.get("drone_id") or "").strip()
        goal = str(extras.get("goal") or "").strip()
        reason = str(extras.get("reason") or "").strip()
        if not drone_id:
            return
        drone = DroneStore.load_drone(self._window._workspace_root, drone_id)
        if drone is None:
            return

        if self._window._settings.auto_summon_drones:
            self.start_drone_run(drone, summon_goal=goal)
            return

        request_id = tool_id or drone_id
        self._pending_drone_summons[request_id] = {
            "drone_id": drone_id,
            "goal": goal,
            "reason": reason,
        }

        card = DroneSummonCard(
            request_id=request_id,
            drone=drone,
            goal=goal or drone.description,
            reason=reason,
            parent=self._window._playground,
        )
        card.summonRequested.connect(self.on_confirm_summon_drone)
        card.cancelRequested.connect(self.on_cancel_summon_drone)
        self._active_run_card = card
        self._window._playground.switch_to_workspace()
        self.sync_drone_tab_checked()
        self._window._playground.add_run_card(f"summon:{request_id}", card)

    def on_confirm_summon_drone(self, request_id: str) -> None:
        if self._window._workspace_root is None:
            return
        request = self._pending_drone_summons.pop(request_id, None)
        if request is None:
            return
        self._window._playground.remove_run_card(f"summon:{request_id}")
        drone = DroneStore.load_drone(
            self._window._workspace_root, request["drone_id"]
        )
        if drone is None:
            return
        self.start_drone_run(drone, summon_goal=request.get("goal", ""))

    def on_cancel_summon_drone(self, request_id: str) -> None:
        self._pending_drone_summons.pop(request_id, None)
        self._window._playground.remove_run_card(f"summon:{request_id}")
        self._active_run_card = None

    def on_cancel_drone(self) -> None:
        """Request cancellation of the active drone run."""
        if self._drone_runner is not None:
            self._drone_runner.cancel()

    def on_cancel_drone_run(self, run_id: str) -> None:
        """Cancel a running Drone — idempotent, does not delete any state.

        Only sets the cancel event and updates the card UI.
        Cleanup happens later when runner emits finished.
        """
        record = self._drone_runs.get(run_id)
        if record is None:
            return
        # If this was a looped run, disable the loop
        loop_id = record.get("loop_drone_id", "")
        if loop_id:
            self._looping_drones.pop(loop_id, None)
            # Update the workbay card loop state if the window is open
            if (
                self._drone_workbay_window is not None
                and self._drone_workbay_window.isVisible()
            ):
                self._drone_workbay_window.set_card_loop_state(loop_id, False)
        record["runner"].cancel()
        self._window._drone_reports_window.mark_cancelling(run_id)

    # -- status / pip --

    def on_drone_status_changed(
        self, run_id: str, drone_name: str, status: str
    ) -> None:
        self._window._edge_rail.set_drone_run_pip_state(run_id, drone_name, status)
        # Also update the workbay card run state
        record = self._drone_runs.get(run_id)
        if record is not None:
            record["last_status"] = status  # store for on_drone_finished
            card_id = record.get("loop_drone_id", "") or record["drone"].id
            if status == "running":
                self.update_workbay_card_run_state(card_id, "running")
            elif status in ("completed", "failed", "cancelled", "timed_out"):
                # Receipt may already be stored; summary update happens in on_drone_finished
                pass

    def remove_drone_run_pip(self, run_id: str) -> None:
        logger.debug("[DroneRun] remove_drone_run_pip run_id=%s", run_id)
        self._window._edge_rail.remove_drone_run_pip(run_id)
        self._window._position_edge_tabs()

    def drone_is_running(self, drone_id: str) -> bool:
        for run in self._drone_runs.values():
            if run["drone"].id == drone_id or run.get("loop_drone_id") == drone_id:
                thread = run.get("thread")
                if thread is not None and thread.isRunning():
                    return True
        return False

    # -- looping --

    def on_loop_drone_toggled(
        self, drone_id: str, enabled: bool, interval_seconds: int = 60
    ) -> None:
        """Enable or disable looping for a single drone."""
        if enabled:
            self._looping_drones[drone_id] = interval_seconds
            logger.info(
                "[DroneLoop] loop enabled for drone=%s interval=%ds",
                drone_id,
                interval_seconds,
            )
            if self.drone_is_running(drone_id):
                # Tag active run records so on_drone_finished sees loop_drone_id
                for r in self._drone_runs.values():
                    if (
                        r.get("loop_drone_id") == drone_id
                        or r["drone"].id == drone_id
                    ):
                        r["loop_drone_id"] = drone_id
                self.update_workbay_card_run_state(drone_id, "running")
            else:
                drone = DroneStore.load_drone(
                    self._window._workspace_root, drone_id
                )
                if drone is not None:
                    self.start_drone_run(drone, loop_drone_id=drone_id)
        else:
            self._looping_drones.pop(drone_id, None)
            logger.info("[DroneLoop] loop disabled for drone=%s", drone_id)

    def on_loop_interval_changed(self, drone_id: str, interval_seconds: int) -> None:
        if drone_id in self._looping_drones:
            self._looping_drones[drone_id] = interval_seconds
            logger.debug(
                "[DroneLoop] interval updated for drone=%s to %ds",
                drone_id,
                interval_seconds,
            )

    def start_next_loop_lap(self, drone_id: str) -> None:
        """Start the next loop lap for a Drone, with full guards.

        Must be called from the main thread.  Checks all preconditions
        before starting so that stale QTimer callbacks never launch
        a run after Loop has been disabled, the Drone deleted, etc.
        """
        # Loop still enabled?
        if drone_id not in self._looping_drones:
            logger.debug(
                "[DroneLoop] start_next_loop_lap: loop disabled for drone=%s, skipping",
                drone_id,
            )
            return
        # Workspace available?
        if self._window._workspace_root is None:
            logger.debug(
                "[DroneLoop] start_next_loop_lap: no workspace root, skipping"
            )
            return
        # Already running?
        if self.drone_is_running(drone_id):
            logger.debug(
                "[DroneLoop] start_next_loop_lap: drone=%s already running, skipping",
                drone_id,
            )
            return
        # Load fresh definition — handles delete/edit between laps
        drone = DroneStore.load_drone(self._window._workspace_root, drone_id)
        if drone is None:
            logger.debug(
                "[DroneLoop] start_next_loop_lap: drone=%s no longer exists, removing from loop state",
                drone_id,
            )
            self._looping_drones.pop(drone_id, None)
            return
        # Notify workbay card that a new lap is starting
        self.update_workbay_card_run_state(drone_id, "running")
        self.start_drone_run(drone, loop_drone_id=drone_id)

    # -- finished / receipt / focus --

    def on_drone_finished(self, run_id: str) -> None:
        """UI/bookkeeping cleanup after a drone run.

        Thread and object lifetime are managed entirely by the signal connections
        wired in start_drone_run (runner.finished->thread.quit, runner.finished->
        runner.deleteLater, thread.finished->thread.deleteLater).  Do NOT touch
        thread or runner here — the runner C++ object may already have been freed
        by its direct deleteLater connection on the worker thread before this slot
        runs on the main thread.
        """
        record = self._drone_runs.pop(run_id, None)
        if record is None:
            logger.debug(
                "[DroneRun] on_drone_finished: unknown run_id=%s (already cleaned up?)",
                run_id,
            )
            return
        runner = record["runner"]
        drone = record["drone"]
        logger.debug("[DroneRun] on_drone_finished start run_id=%s", run_id)
        logger.debug(
            "[DroneRun] finished  run_id=%s  drone=%s", run_id, drone.name
        )
        # Pip state already reflects the final status via statusChanged signal;
        # schedule timed removal so the user can see the final badge briefly.
        QTimer.singleShot(
            15000, lambda rid=run_id: self.remove_drone_run_pip(rid)
        )
        if self._write_drone_run_id == run_id:
            self._write_drone_run_id = None
        if self._drone_runner is runner:
            self._drone_runner = None
            self._window._companion.set_drone_runner(None)
            self._drone_runner_thread = None
        logger.debug("[DroneRun] on_drone_finished end run_id=%s", run_id)

        loop_drone_id = record.get("loop_drone_id", "")
        if loop_drone_id and loop_drone_id in self._looping_drones:
            interval = self._looping_drones[loop_drone_id]
            receipt_for_loop = record.get("receipt")
            artifact = None
            if receipt_for_loop is not None:
                artifact = getattr(receipt_for_loop, "produced_artifact", None) or {}
            lap_failed = receipt_for_loop is not None and getattr(receipt_for_loop, "status", None) == "failed"
            no_work = isinstance(artifact, dict) and artifact.get("has_work") is False
            if lap_failed or no_work:
                self._looping_drones.pop(loop_drone_id, None)
                if not lap_failed:
                    record["_stop_loop_due_to_no_work"] = True
                if self._drone_workbay_window is not None and self._drone_workbay_window.isVisible():
                    self._drone_workbay_window.set_card_loop_state(loop_drone_id, False)
            else:
                logger.debug(
                    "[DroneLoop] loop active for drone=%s, scheduling next lap in %ds",
                    loop_drone_id,
                    interval,
                )
                QTimer.singleShot(
                    interval * 1000,
                    lambda lid=loop_drone_id: self.start_next_loop_lap(lid),
                )

        # Update workbay card with final run state
        card_id = loop_drone_id if loop_drone_id else drone.id
        # Check for stored receipt
        receipt = record.get("receipt")
        if record.get("_stop_loop_due_to_no_work"):
            self.update_workbay_card_run_state(card_id, "idle")
        elif receipt is not None:
            final_status = receipt.status
            if final_status == "completed":
                summary = (receipt.summary or "").strip()
                if len(summary) > 80:
                    summary = summary[:77] + "..."
                self.update_workbay_card_run_state(card_id, "completed", summary)
            elif final_status == "failed":
                error = (receipt.errors or ["Unknown error"])[0]
                if len(error) > 80:
                    error = error[:77] + "..."
                self.update_workbay_card_run_state(card_id, "failed", error)
            else:
                self.update_workbay_card_run_state(card_id, "idle")
        else:
            # No receipt - infer from last_status saved in on_drone_status_changed.
            # Do NOT touch runner.run_state — the runner C++ object may already be deleted.
            runner_status = record.get("last_status", "")
            if runner_status in ("failed", "timed_out"):
                self.update_workbay_card_run_state(
                    card_id, "failed", "Unknown error"
                )
            elif runner_status == "cancelled":
                self.update_workbay_card_run_state(card_id, "idle")
            else:
                self.update_workbay_card_run_state(card_id, "idle")

        # If loop is active, switch to waiting_for_loop after showing completion briefly
        if loop_drone_id and loop_drone_id in self._looping_drones:
            interval = self._looping_drones[loop_drone_id]
            # Show the completion/failure summary for 3 seconds, then switch to waiting countdown
            QTimer.singleShot(
                3000,
                lambda lid=loop_drone_id, iv=interval: (
                    self.update_workbay_card_run_state(lid, "waiting_for_loop", str(iv))
                    if lid in self._looping_drones
                    else None
                ),
            )

    def on_drone_receipt(self, receipt: object, run_id: str = "") -> None:
        """Handle completed drone receipt."""
        self._drone_receipt = receipt
        if not run_id:
            run_id = getattr(receipt, "run_id", "")
        record = self._drone_runs.get(run_id)
        if record is not None:
            record["receipt"] = receipt
        self._window._drone_reports_window.on_receipt_ready(run_id, receipt)

    def on_focus_drone_run(self, run_id: str = "") -> None:
        """Open Drone Reports and focus the requested run card."""
        if run_id:
            self._window._drone_reports_window.show_and_focus(run_id)
            return
        if self._drone_runs:
            self._window._drone_reports_window.show_and_raise()

    def on_view_drone_receipt(self, run_id: str) -> None:
        """Open a read-only run card for a saved receipt."""
        workspace_root = self._window._workspace_root
        if workspace_root is None:
            return

        receipt = RunHistoryStore.load_run(workspace_root, run_id)
        if not receipt:
            return

        # Build a minimal DroneDefinition from the receipt
        minimal_drone = DroneDefinition(
            id="history:" + run_id,
            name=receipt.drone_name,
            description="",
            instructions="",
            write_policy="read_only",
            allowed_tools=(),
            output_contract={},
        )

        run_card = DroneRunCard(
            minimal_drone,
            parent=self._window._drone_reports_window,
            readonly=True,
        )
        run_card.populate_from_receipt(receipt)

        self._active_run_card = run_card
        card_id = f"receipt:{run_id}"
        self._window._drone_reports_window.add_run_card(card_id, run_card)

        self._window._drone_reports_window.show_and_focus(card_id)

    # -- approval --

    def on_drone_approval_requested(
        self,
        request: ApprovalRequest,
        runner: DroneRunner | None = None,
        run_id: str = "",
        drone_name: str = "",
    ) -> None:
        """Show approval dialog for a write operation requested by a Drone."""
        runner = runner or self._drone_runner
        if runner is None:
            return
        record = self._drone_runs.get(run_id) if run_id else None
        run_card = record.get("card") if record else None
        if isinstance(run_card, DroneRunCard):
            run_card.on_status_changed("waiting for approval")
        if run_id and drone_name:
            self._window._edge_rail.set_drone_run_pip_state(
                run_id, drone_name, "waiting for approval"
            )
            self._window._drone_reports_window.show_and_focus(run_id)
        approval_id = request.approval_id or None

        # Build the diff text.
        if request.is_new_file:
            diff_text = f"[New file] {request.rel_path}\n\n{request.new_content}"
        else:
            diff_lines = list(
                difflib.unified_diff(
                    request.old_content.splitlines(keepends=True),
                    request.new_content.splitlines(keepends=True),
                    fromfile=request.rel_path,
                    tofile=request.rel_path,
                )
            )
            diff_text = "".join(diff_lines) if diff_lines else "(no changes)"

        dialog = QDialog(self._window._playground)
        dialog.setWindowTitle(f"Drone: {request.tool_name}")
        dialog.resize(600, 400)

        layout = QVBoxLayout(dialog)

        info = QLabel(
            f"<b>Tool:</b> {request.tool_name} | <b>File:</b> {request.rel_path}"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        diff_view = QPlainTextEdit()
        diff_view.setPlainText(diff_text)
        diff_view.setReadOnly(True)
        layout.addWidget(diff_view, stretch=1)

        button_box = QDialogButtonBox(dialog)
        approve_btn = button_box.addButton(
            "Approve", QDialogButtonBox.ButtonRole.AcceptRole
        )
        reject_btn = button_box.addButton(
            "Reject", QDialogButtonBox.ButtonRole.RejectRole
        )
        approve_all_btn = button_box.addButton(
            "Approve All", QDialogButtonBox.ButtonRole.AcceptRole
        )
        reject_all_btn = button_box.addButton(
            "Reject All", QDialogButtonBox.ButtonRole.RejectRole
        )

        button_box.clicked.connect(
            lambda btn: self.on_drone_approval_button_clicked(
                dialog,
                runner,
                btn,
                approval_id,
                approve_btn,
                reject_btn,
                approve_all_btn,
                reject_all_btn,
            )
        )

        layout.addWidget(button_box)

        # Ensure worker thread unblocks even if dialog is closed via X.
        dialog.rejected.connect(
            lambda: runner.set_approval_result(
                ApprovalDecision(action="reject"),
                approval_id=approval_id,
            )
        )

        dialog.exec()
        if (
            run_id
            and drone_name
            and runner.run_state.is_active
            and not runner.run_state.cancel_event.is_set()
        ):
            if isinstance(run_card, DroneRunCard):
                run_card.on_status_changed("running")
            self._window._edge_rail.set_drone_run_pip_state(
                run_id, drone_name, "running"
            )

    def on_drone_approval_button_clicked(
        self,
        dialog: QDialog,
        runner,
        btn,
        approval_id,
        approve_btn,
        reject_btn,
        approve_all_btn,
        reject_all_btn,
    ) -> None:
        if btn == approve_btn:
            decision = ApprovalDecision(action="approve")
        elif btn == reject_btn:
            decision = ApprovalDecision(action="reject")
        elif btn == approve_all_btn:
            decision = ApprovalDecision(action="approve_all")
        elif btn == reject_all_btn:
            decision = ApprovalDecision(action="reject_all")
        else:
            decision = ApprovalDecision(action="reject")

        runner.set_approval_result(decision, approval_id=approval_id)
        dialog.accept()
