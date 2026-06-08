from __future__ import annotations

from typing import Any
from aura.conversation.tools._types import ToolExecResult


class PlannerHandlersMixin:
    """Mixin for ToolRegistry implementing planner-specific tool handlers."""

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
            "max_tool_rounds": drone.budget.max_tool_rounds,
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

    def _handle_run_read_only_drone(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        drone_id = str(args.get("drone_id") or "").strip()
        goal = str(args.get("goal") or "").strip()
        wait_seconds = int(args.get("wait_seconds", 120) or 0) or 120
        include_receipt = bool(args.get("include_receipt", False))

        if not drone_id:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "missing required arg: drone_id", "failure_class": "invalid_args"},
            )
        if not goal:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "missing required arg: goal", "failure_class": "invalid_args"},
            )

        from aura.drones.store import DroneStore

        drone = DroneStore.load_drone(self._root, drone_id)
        if drone is None:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": f"Unknown Drone: {drone_id}", "failure_class": "drone_not_found"},
            )

        if drone.write_policy != "read_only":
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": f"Drone '{drone_id}' has write_policy='{drone.write_policy}'. Only read_only Drones can be run with this tool.",
                    "failure_class": "drone_not_read_only",
                },
            )

        count = getattr(self, "_run_read_only_drone_count", 0)
        if count >= 2:
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": "Per-turn limit reached: at most 2 run_read_only_drone calls per conversation turn.",
                    "failure_class": "per_turn_limit_exceeded",
                },
            )
        self._run_read_only_drone_count = count + 1

        budget = drone.budget
        timeout = min(wait_seconds, budget.timeout_seconds if budget else 120)
        max_rounds = budget.max_tool_rounds if budget else 8

        from aura.drones.sync_runner import run_read_only_drone_sync

        result = run_read_only_drone_sync(
            workspace_root=self._root,
            drone_id=drone_id,
            drone=drone,
            goal=goal,
            timeout_seconds=timeout,
            max_tool_rounds=max_rounds,
        )

        payload: dict[str, Any] = {
            "ok": result["ok"],
            "drone_id": result["drone_id"],
            "drone_name": result["drone_name"],
            "run_id": result["run_id"],
            "status": result["status"],
            "summary": result["summary"],
            "tool_calls_made": result["tool_calls_made"],
            "tool_errors": result["tool_errors"],
            "elapsed_seconds": result["elapsed_seconds"],
        }

        if include_receipt:
            payload["receipt"] = result.get("receipt")

        return ToolExecResult(ok=result["ok"], payload=payload)
