"""DroneRunner — executes a registered folder-backed Drone on a QThread."""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from aura.conversation.tools._types import ApprovalDecision
from aura.drones.definition import DroneDefinition
from aura.drones.folder_runner import is_folder_backed_drone, run_folder_drone_sync
from aura.drones.receipt import DroneReceipt
from aura.drones.run import DroneRun

logger = logging.getLogger(__name__)


class DroneRunner(QObject):
    """Executes a single registered folder-backed Drone on a background thread."""

    statusChanged = Signal(str)
    contentDelta = Signal(str)
    toolCallStart = Signal(int, str, str)
    toolCallArgsDelta = Signal(int, str)
    toolCallEnd = Signal(int)
    toolResult = Signal(str, str, bool, str)
    usageEmitted = Signal(int, int, int, int)
    apiError = Signal(int, str)
    receiptReady = Signal(object)
    approval_requested = Signal(object)
    finished = Signal()

    def __init__(
        self,
        workspace_root: Path,
        drone: DroneDefinition,
        provider_id: str | None = None,
        model: str | None = None,
        auto_approve: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._workspace_root = workspace_root
        self._drone = drone
        self._run = DroneRun(drone=drone)
        self._provider = provider_id
        self._model = model
        self._auto_approve = auto_approve

    def cancel(self) -> None:
        self._run.cancel()

    @property
    def run_state(self) -> DroneRun:
        return self._run

    @Slot()
    def run(self) -> None:
        logger.info("Drone run started: %s (%s)", self._drone.name, self._run.run_id)
        self._run.mark("running")
        self.statusChanged.emit("running")
        try:
            if not is_folder_backed_drone(self._drone):
                raise ValueError("Only folder-backed command Drones with json-stdio protocol can be executed")

            goal = self._drone.description or self._drone.instructions
            result = run_folder_drone_sync(
                self._workspace_root,
                self._drone.id,
                self._drone,
                goal,
                run=self._run,
            )
            summary = str(result.get("summary") or "")
            if summary:
                self.contentDelta.emit(summary)
            receipt_data = result.get("receipt")
            receipt = (
                DroneReceipt.from_dict(receipt_data)
                if isinstance(receipt_data, dict)
                else None
            )
            if receipt is None:
                raise RuntimeError("Folder Drone did not return a receipt")
            self._run.mark(str(result.get("status") or receipt.status))
            self.statusChanged.emit(self._run.status)
            self.receiptReady.emit(receipt)
        except Exception as exc:
            logger.exception("Drone runner error")
            self._run.mark("failed")
            self.statusChanged.emit("failed")
            self.apiError.emit(-1, str(exc))
            self.receiptReady.emit(self._failed_receipt(str(exc)))
        finally:
            self.finished.emit()

    def set_approval_result(
        self,
        decision: ApprovalDecision,
        approval_id: str | None = None,
    ) -> None:
        _ = (decision, approval_id)

    def _failed_receipt(self, error: str) -> DroneReceipt:
        ended = dt.datetime.now(dt.timezone.utc).isoformat()
        return DroneReceipt(
            run_id=self._run.run_id,
            drone_id=self._drone.id,
            drone_name=self._drone.name,
            status="failed",
            started_at=dt.datetime.fromtimestamp(
                self._run.started_at,
                tz=dt.timezone.utc,
            ).isoformat(),
            ended_at=ended,
            tool_calls_made=0,
            tool_errors=0,
            summary="",
            output_contract=self._drone.output_contract,
            tool_calls=[],
            errors=[error],
            elapsed_seconds=self._run.elapsed_seconds,
            met=False,
            evidence="Folder-backed Drone execution failed.",
        )
