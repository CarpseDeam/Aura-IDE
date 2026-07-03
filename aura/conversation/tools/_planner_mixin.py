from __future__ import annotations

import logging
from typing import Any

from aura.conversation.tools._types import ToolExecResult
from aura.drones.store import DroneStore
from aura.research.adapter import WEB_RESEARCH_DRONE_ID
from aura.research.result import format_research_answer
from aura.research.ui_contract import (
    RESEARCH_UI_MODE_SILENT,
    with_research_ui_contract,
)

DEFAULT_READ_ONLY_DRONE_LIMIT = 3
WEB_RESEARCH_DRONE_LIMIT = 6
_log = logging.getLogger(__name__)


class PlannerHandlersMixin:
    """Mixin for ToolRegistry implementing planner-specific tool handlers."""

    def reset_drone_budget(self) -> None:
        self._drone_budget.clear()

    def _handle_summon_drone(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        """Queue a Drone summon request for GUI confirmation.

        The planner cannot launch GUI work directly from the model thread. This
        handler validates the Drone and returns metadata that MainWindow uses to
        render the confirmation card in the right-side execution surface.
        """
        drone_id = str(args.get("drone_id") or "").strip()
        goal = str(args.get("goal") or "").strip()
        reason = str(args.get("reason") or "").strip()
        if not drone_id:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "drone_id is required"},
            )
        if not goal:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "goal is required"},
            )

        from aura.drones.store import DroneStore

        drone = DroneStore.load_drone(self._root, drone_id)
        if drone is None:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": f"unknown drone: {drone_id}"},
            )

        payload = {
            "ok": True,
            "status": "pending_user_confirmation",
            "message": "Drone summon request is waiting for user confirmation.",
            "drone_id": drone.id,
            "drone_name": drone.name,
            "goal": goal,
            "reason": reason,
            "write_policy": drone.write_policy,
            "timeout_seconds": drone.budget.timeout_seconds,
        }
        return ToolExecResult(
            ok=True,
            payload=payload,
            extras={"summon_drone": True, **payload},
        )

    def _handle_get_workspace_snapshot(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        from aura.conversation.tools.workspace_snapshot_handler import gather_workspace_snapshot

        try:
            snapshot = gather_workspace_snapshot(self._root)
            return ToolExecResult(ok=True, payload=snapshot)
        except Exception:
            import sys

            exc = sys.exc_info()[1]
            return ToolExecResult(
                ok=False,
                payload={"error": str(exc), "workspace_root": str(self._root)},
            )

    def _handle_launch_read_only_drone(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        """Launch a read-only Drone in background, return immediately."""
        drone_id = str(args.get("drone_id") or "").strip()
        goal = str(args.get("goal") or "").strip()
        ui_mode = str(args.get("ui_mode") or RESEARCH_UI_MODE_SILENT).strip().lower()

        if not drone_id:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "drone_id is required"},
            )
        if not goal:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "goal is required"},
            )

        from aura.drones.store import DroneStore

        drone = DroneStore.load_drone(self._root, drone_id)
        if drone is None:
            drones = DroneStore.list_drones(self._root)
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": f"Unknown drone_id: '{drone_id}'. Available: {[d.id for d in drones]}",
                },
            )

        if drone.write_policy != "read_only":
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": (
                        f"Drone '{drone_id}' has write_policy='{drone.write_policy}'. "
                        "Only read_only Drones are allowed for this tool."
                    ),
                },
            )

        from aura.drones.background_runner import get_background_runner

        upstream = None
        if drone_id == WEB_RESEARCH_DRONE_ID:
            upstream = with_research_ui_contract(
                {
                    "research_request": {
                        "question": goal,
                        "original_text": goal,
                        "drone_id": WEB_RESEARCH_DRONE_ID,
                        "route": "answer_only",
                        "ui_mode": ui_mode,
                    }
                },
                route="answer_only",
                ui_mode=ui_mode,
            )
            folder = DroneStore.drone_folder(self._root, drone_id)
            _log.info(
                "answer_only_research_launch drone_id=%s folder=%s "
                "silent_requested=%s",
                drone_id,
                folder,
                bool(upstream.get("headless")),
            )
        runner = get_background_runner(self._root)
        job = runner.launch(drone, goal, upstream=upstream)

        return ToolExecResult(
            ok=True,
            payload={
                "ok": True,
                "run_id": job.run_id,
                "drone_id": drone.id,
                "drone_name": drone.name,
                "status": job.status,
            },
        )

    def _handle_run_read_only_drone(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        """Run a saved read-only Drone directly in the background."""
        drone_id = str(args.get("drone_id") or "").strip()
        goal = str(args.get("goal") or "").strip()
        ui_mode = str(args.get("ui_mode") or RESEARCH_UI_MODE_SILENT).strip().lower()

        if not drone_id:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "Missing required parameter: drone_id"},
            )
        if not goal:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "Missing required parameter: goal"},
            )

        from aura.drones.store import DroneStore

        drone = DroneStore.load_drone(self._root, drone_id)
        if drone is None:
            if drone_id == WEB_RESEARCH_DRONE_ID:
                folder = DroneStore.drone_folder(self._root, drone_id)
                _log.warning(
                    "web_research_drone_unregistered drone_id=%s folder=%s "
                    "silent_requested=%s",
                    drone_id,
                    folder,
                    ui_mode == RESEARCH_UI_MODE_SILENT,
                )
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": f"No drone found with id: {drone_id}"},
            )

        if drone.write_policy != "read_only":
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": (
                        f"Drone '{drone_id}' is not read-only; "
                        "only read-only Drones can be run directly."
                    ),
                },
            )

        # Per-turn limit check (tracked per drone_id)
        budget = self._drone_budget.get(drone_id, 0) + 1
        self._drone_budget[drone_id] = budget
        limit = WEB_RESEARCH_DRONE_LIMIT if drone_id == WEB_RESEARCH_DRONE_ID else DEFAULT_READ_ONLY_DRONE_LIMIT
        if budget > limit:
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": f"Per-turn limit of {limit} runs reached for drone '{drone_id}'",
                    "code": "drone_budget_exhausted",
                    "limit": limit,
                    "used": budget - 1,
                    "drone_id": drone_id,
                },
            )

        from aura.drones.sync_runner import run_read_only_drone_sync

        try:
            upstream = None
            if drone_id == WEB_RESEARCH_DRONE_ID:
                upstream = with_research_ui_contract(
                    {
                        "research_request": {
                            "question": goal,
                            "original_text": goal,
                            "drone_id": WEB_RESEARCH_DRONE_ID,
                            "route": "answer_only",
                            "ui_mode": ui_mode,
                        }
                    },
                    route="answer_only",
                    ui_mode=ui_mode,
                )
                folder = DroneStore.drone_folder(self._root, drone_id)
                _log.info(
                    "answer_only_research_start drone_id=%s folder=%s "
                    "silent_requested=%s",
                    drone_id,
                    folder,
                    bool(upstream.get("headless")),
                )
            result = run_read_only_drone_sync(
                drone_id=drone_id,
                goal=goal,
                workspace_root=self._root,
                drone=drone,
                upstream=upstream,
            )
            if drone_id == WEB_RESEARCH_DRONE_ID:
                result = dict(result)
                result["answer_for_chat"] = format_research_answer(result)
            return ToolExecResult(ok=True, payload=result)
        except Exception as exc:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": f"Drone execution failed: {exc}"},
            )

    def _handle_check_drone_run(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        """Check status of a background Drone run."""
        run_id = str(args.get("run_id") or "").strip()
        if not run_id:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "run_id is required"},
            )

        try:
            wait_seconds = float(args.get("wait_seconds", 0) or 0)
        except (TypeError, ValueError):
            wait_seconds = 0.0
        include_receipt = bool(args.get("include_receipt", False))

        from aura.drones.background_runner import get_background_runner

        runner = get_background_runner(self._root)
        job = runner.get(run_id, wait_seconds=wait_seconds)

        if job is None:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": f"Unknown run_id: '{run_id}'"},
            )

        result: dict[str, Any] = {
            "ok": True,
            "run_id": job.run_id,
            "drone_id": job.drone_id,
            "drone_name": job.drone_name,
            "status": job.status,
            "goal": job.goal,
        }

        if job.status == "completed":
            result["summary"] = job.summary
            result["tool_calls_made"] = job.tool_calls_made
            result["tool_errors"] = job.tool_errors
            result["elapsed_seconds"] = job.elapsed_seconds
            if include_receipt and job.receipt:
                result["receipt"] = job.receipt
        elif job.status == "failed":
            result["error"] = job.error or "Unknown error"

        return ToolExecResult(ok=True, payload=result)

    def _handle_register_drone_folder(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        """Validate and register a completed folder-backed Drone."""
        folder_raw = str(args.get("folder_path") or "").strip()
        if not folder_raw:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "folder_path is required"},
            )
        try:
            folder = self._resolve_in_root(folder_raw)
            if not folder.is_dir():
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": f"Drone folder does not exist: {folder_raw}"},
                )

            drone = DroneStore.register_drone_folder(self._root, folder)
            return ToolExecResult(
                ok=True,
                payload={
                    "ok": True,
                    "drone_saved": True,
                    "folder_drone": True,
                    "drone_id": drone.id,
                    "id": drone.id,
                    "name": drone.name,
                    "runtime": drone.runtime,
                    "entrypoint": drone.entrypoint,
                    "permissions": drone.permissions,
                },
                extras={
                    "drone_saved": True,
                    "drone_id": drone.id,
                    "folder_drone": True,
                },
            )
        except Exception as e:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": str(e)},
            )

    def _handle_declare_ui_contract(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        """Write a ui_contract.json sidecar into a drone folder."""
        import json

        folder_raw = str(args.get("folder_path") or "").strip()
        if not folder_raw:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "folder_path is required"},
            )

        try:
            folder = self._resolve_in_root(folder_raw)
            if not folder.is_dir():
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": f"Drone folder does not exist: {folder_raw}"},
                )

            assertions = args.get("assertions")
            if not isinstance(assertions, list) or not assertions:
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": "assertions must be a non-empty list"},
                )

            for i, assertion in enumerate(assertions):
                if not isinstance(assertion, dict):
                    return ToolExecResult(
                        ok=False,
                        payload={"ok": False, "error": f"assertions[{i}] must be an object"},
                    )
                a_type = assertion.get("type")
                if a_type not in ("node_exists", "node_absent"):
                    return ToolExecResult(
                        ok=False,
                        payload={
                            "ok": False,
                            "error": (
                                f"assertions[{i}].type must be 'node_exists' or 'node_absent', "
                                f"got '{a_type}'"
                            ),
                        },
                    )
                if not any(k in assertion for k in ("role", "name", "object_name")):
                    return ToolExecResult(
                        ok=False,
                        payload={
                            "ok": False,
                            "error": (
                                f"assertions[{i}] must have at least one of "
                                "role, name, object_name"
                            ),
                        },
                    )

            contract = {"schema_version": 1, "assertions": assertions}
            contract_path = folder / "ui_contract.json"
            contract_text = json.dumps(contract, indent=2) + "\n"
            contract_path.write_text(contract_text, encoding="utf-8")

            return ToolExecResult(
                ok=True,
                payload={
                    "ok": True,
                    "contract_written": True,
                    "path": str(contract_path),
                    "assertion_count": len(assertions),
                },
            )
        except Exception as e:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": str(e)},
            )


def format_web_research_answer(result: dict[str, Any]) -> str:
    """Compatibility wrapper for compact Web Research Drone chat prose."""
    return format_research_answer(result)


__all__ = [
    "PlannerHandlersMixin",
    "WEB_RESEARCH_DRONE_ID",
    "format_web_research_answer",
]
