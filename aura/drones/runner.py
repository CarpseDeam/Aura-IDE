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

        import datetime as dt
        from aura.git_ops import working_tree_status, snapshot, changes_since, restore_to_snapshot, commit_all
        from aura.conversation.dispatch import WorkerOutcomeStatus

        # 1. Check clean working tree before starting
        workspace_root = self._workspace_root
        git_ok, git_output, git_err = working_tree_status(workspace_root)
        if git_ok and git_output.strip():
            # Working tree is dirty -- skip the lap
            ended = dt.datetime.now(tz=dt.timezone.utc).isoformat()
            receipt = DroneReceipt(
                run_id=self._run.run_id,
                drone_id=self._drone.id,
                drone_name=self._drone.name,
                status="failed",
                started_at=dt.datetime.fromtimestamp(
                    self._run.started_at, tz=dt.timezone.utc
                ).isoformat(),
                ended_at=ended,
                summary="Harness-lap skipped: working tree is dirty. Commit or stash changes first.",
                produced_artifact={
                    "has_work": False,
                    "changed_files": [],
                    "worker_ok": False,
                    "worker_status": "skipped_dirty_tree",
                    "worker_errors": [],
                    "policy_violations": ["working tree is dirty"],
                    "rollback_status": None,
                },
                errors=["Working tree is not clean. Harness-lap requires a clean working tree."],
                elapsed_seconds=self._run.elapsed_seconds,
            )
            RunHistoryStore.save_run(workspace_root, receipt)
            self.contentDelta.emit(receipt.summary)
            self._run.mark("failed")
            self.statusChanged.emit("failed")
            self.receiptReady.emit(receipt)
            return

        # 2. Capture pre-lap snapshot
        pre_sha = snapshot(workspace_root)

        # 3. Read permissions from drone definition (with safe defaults)
        permissions = self._drone.permissions or {}
        require_clean_worktree = permissions.get("require_clean_worktree", True)
        revert_on_failure = permissions.get("revert_on_failure", True)
        max_changed_files = permissions.get("max_changed_files", 0)  # 0 = unlimited
        protected_paths = permissions.get("protected_paths", []) or []

        import fnmatch

        def _path_matches_protected(path: str, patterns: list[str]) -> bool:
            for pat in patterns:
                if fnmatch.fnmatch(path, pat):
                    return True
            return False

        def _revert(sha: str | None) -> tuple[bool, str]:
            if sha:
                ok, msg = restore_to_snapshot(workspace_root, sha)
                return ok, msg
            return False, "No pre-lap snapshot to restore"

        # 4. Run the lap
        want = self._build_harness_lap_want()
        lap_result = self._bridge.run_one_lap(want)

        # 5. Collect outcomes
        changed_files = list(lap_result.changed_files)
        worker_ok = lap_result.worker_ok
        worker_status = lap_result.worker_status
        worker_errors = list(lap_result.worker_errors)

        # 6. Determine if lap failed based on worker outcome
        hard_failure_statuses = {
            WorkerOutcomeStatus.validation_failed.value,
            WorkerOutcomeStatus.edit_mechanics_blocked.value,
            WorkerOutcomeStatus.harness_error.value,
            WorkerOutcomeStatus.needs_followup.value,
            WorkerOutcomeStatus.needs_planner_resolution.value,
            WorkerOutcomeStatus.craft_blocked.value,
            WorkerOutcomeStatus.craft_rejected.value,
            WorkerOutcomeStatus.scope_mismatch.value,
            WorkerOutcomeStatus.approval_rejected.value,
        }
        lap_failed = not worker_ok or worker_status in hard_failure_statuses or bool(worker_errors)

        if not lap_failed and not changed_files:
            # Empty lap -- not a failure, but no work done
            pass

        # 7. Check policy constraints
        policy_violations: list[str] = []

        if max_changed_files > 0 and len(changed_files) > max_changed_files:
            policy_violations.append(
                f"Changed {len(changed_files)} files, exceeds max_changed_files={max_changed_files}"
            )
            lap_failed = True

        for f in changed_files:
            if _path_matches_protected(f, protected_paths):
                policy_violations.append(
                    f"Changed protected path '{f}'"
                )
                lap_failed = True

        # 8. Revert on failure if policy says so
        rollback_status: str | None = None
        if lap_failed and revert_on_failure:
            if changed_files:
                revert_ok, revert_msg = _revert(pre_sha)
                rollback_status = "reverted" if revert_ok else f"rollback_failed: {revert_msg}"
                if not revert_ok:
                    rollback_status = f"rollback_failed: {revert_msg}"
            else:
                rollback_status = "no_changes_to_revert"

        # 8.5. Commit successful lap changes
        commit_sha: str | None = None
        if not lap_failed and changed_files:
            commit_ok, sha, commit_msg = commit_all(workspace_root, lap_result.summary)
            commit_sha = sha
            if not commit_ok:
                # Commit failure is not a lap failure — the edit is valid, just uncommitted
                summary_extra = f" [commit failed: {commit_msg}]"
            else:
                summary_extra = ""
        else:
            summary_extra = ""

        final_status: str
        if not lap_failed:
            final_status = "completed"
        elif rollback_status == "reverted" or rollback_status == "no_changes_to_revert":
            final_status = "failed"
        elif rollback_status and rollback_status.startswith("rollback_failed"):
            final_status = "failed"
        else:
            final_status = "failed"

        # 9. Build summary
        if lap_failed:
            violations_str = "; ".join(policy_violations) if policy_violations else ""
            errors_str = "; ".join(worker_errors) if worker_errors else ""
            parts = [s for s in [violations_str, errors_str] if s]
            combined = " | ".join(parts)
            summary = f"Harness-lap failed: {combined}" if combined else "Harness-lap failed."
            if rollback_status and "rollback_failed" in rollback_status:
                summary += f" Rollback failed -- tree may be dirty."
        else:
            summary = lap_result.summary + summary_extra

        artifact: dict[str, Any] = {
            "has_work": bool(changed_files),
            "changed_files": changed_files,
            "worker_ok": worker_ok,
            "worker_status": worker_status,
            "worker_errors": worker_errors,
            "policy_violations": policy_violations,
            "rollback_status": rollback_status,
            "commit_sha": commit_sha,
        }

        receipt = DroneReceipt(
            run_id=self._run.run_id,
            drone_id=self._drone.id,
            drone_name=self._drone.name,
            status=final_status,
            started_at=dt.datetime.fromtimestamp(
                self._run.started_at, tz=dt.timezone.utc
            ).isoformat(),
            ended_at=dt.datetime.now(tz=dt.timezone.utc).isoformat(),
            summary=summary,
            produced_artifact=artifact,
            errors=worker_errors if lap_failed else [],
            elapsed_seconds=self._run.elapsed_seconds,
        )
        RunHistoryStore.save_run(workspace_root, receipt)
        self.contentDelta.emit(summary)
        self._run.mark(final_status)
        self.statusChanged.emit(final_status)
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
