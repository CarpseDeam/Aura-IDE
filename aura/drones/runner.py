"""DroneRunner — executes a registered folder-backed Drone on a QThread."""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot
from aura.drones.store import DroneStore, RunHistoryStore

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
        bridge: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._workspace_root = workspace_root
        self._drone = drone
        self._run = DroneRun(drone=drone)
        self._provider = provider_id
        self._model = model
        self._auto_approve = auto_approve
        self._bridge = bridge

    def cancel(self) -> None:
        self._run.cancel()

    def set_bridge(self, bridge: Any) -> None:
        self._bridge = bridge

    def _is_harness_lap_drone(self) -> bool:
        if self._drone.kind == "harness-lap":
            return True
        # Fallback: check the manifest on disk directly
        try:
            folder = DroneStore.drone_folder(self._workspace_root, self._drone.id)
            manifest_raw = (folder / "drone.json").read_text(encoding="utf-8")
            manifest = json.loads(manifest_raw)
            return manifest.get("kind") == "harness-lap"
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("Not a harness-lap drone (%s): %s", self._drone.id, exc)
            return False

    def _build_harness_lap_want(self) -> str:
        instructions = self._drone.instructions or self._drone.description or ""
        runs = RunHistoryStore.list_runs(self._workspace_root, limit=50)
        prior = [r for r in runs if r.get("drone_id") == self._drone.id][:10]
        if not prior:
            return instructions
        lines = ["## Recent laps (most recent first) — do not redo or revert", ""]
        for i, r in enumerate(prior, 1):
            summary = r.get("summary", "")
            artifact = r.get("produced_artifact") or {}
            changed = artifact.get("changed_files", [])
            suffix = f" [changed: {', '.join(changed)}]" if changed else " [no changes]"
            lines.append(f"{i}. {summary}{suffix}")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(instructions)
        return "\n".join(lines)

    def _run_harness_lap(self) -> None:
        if self._bridge is None:
            raise RuntimeError("Harness-lap drone requires a bridge but none was provided")
        want = self._build_harness_lap_want()
        lap_result = self._bridge.run_one_lap(want)
        receipt = DroneReceipt(
            run_id=self._run.run_id,
            drone_id=self._drone.id,
            drone_name=self._drone.name,
            status="completed",
            started_at=dt.datetime.fromtimestamp(self._run.started_at, tz=dt.timezone.utc).isoformat(),
            ended_at=dt.datetime.now(tz=dt.timezone.utc).isoformat(),
            summary=lap_result.summary,
            produced_artifact={
                "has_work": lap_result.has_work,
                "changed_files": list(lap_result.changed_files),
            },
            elapsed_seconds=self._run.elapsed_seconds,
        )
        RunHistoryStore.save_run(self._workspace_root, receipt)
        self.contentDelta.emit(lap_result.summary)
        self._run.mark("completed")
        self.statusChanged.emit("completed")
        self.receiptReady.emit(receipt)

    @property
    def run_state(self) -> DroneRun:
        return self._run

    @Slot()
    def run(self) -> None:
        logger.info("Drone run started: %s (%s)", self._drone.name, self._run.run_id)
        self._run.mark("running")
        self.statusChanged.emit("running")
        try:
            if self._is_harness_lap_drone():
                self._run_harness_lap()
                return
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
