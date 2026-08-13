from __future__ import annotations

import logging
from typing import Any

from aura.conversation.tools._types import ToolExecResult
from aura.drones.store import DroneStore
from aura.research.adapter import WEB_RESEARCH_DRONE_ID

_log = logging.getLogger(__name__)


class DroneHandlersMixin:
    """Drone-management handlers owned by the workspace tool registry."""

    def _handle_summon_drone(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        """Queue a Drone summon request for GUI confirmation.

        This handler validates the Drone and returns metadata that MainWindow uses to
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

    def _handle_launch_read_only_drone(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        """Launch a read-only Drone in background, return immediately."""
        drone_id = str(args.get("drone_id") or "").strip()
        goal = str(args.get("goal") or "").strip()

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

        if drone_id == WEB_RESEARCH_DRONE_ID:
            return _web_research_drone_retired_result()

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

        runner = get_background_runner(self._root)
        job = runner.launch(drone, goal, upstream=None)

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

        if drone_id == WEB_RESEARCH_DRONE_ID:
            return _web_research_drone_retired_result()

        from aura.drones.store import DroneStore

        drone = DroneStore.load_drone(self._root, drone_id)
        if drone is None:
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

        from aura.drones.sync_runner import run_read_only_drone_sync

        try:
            result = run_read_only_drone_sync(
                drone_id=drone_id,
                goal=goal,
                workspace_root=self._root,
                drone=drone,
                upstream=None,
            )
            # The drone's own result carries the authoritative ok: a failed
            # drone run must not be reported as a successful tool call.
            return ToolExecResult(ok=bool(result.get("ok", False)), payload=result)
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

        # A failed drone is a failed result. Reporting ok=True beside the
        # job's own error made the payload contradict itself, and the model
        # reads `ok` first.
        job_failed = job.status == "failed"
        result: dict[str, Any] = {
            "ok": not job_failed,
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
        elif job_failed:
            result["error"] = job.error or "Unknown error"
            result["failure_class"] = "drone_run_failed"

        return ToolExecResult(ok=not job_failed, payload=result)

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
        """Write a ui_contract.json sidecar into a drone folder.

        The write itself goes through Aura's normal approved atomic write
        path (``write_file``): the folder's ``ui_contract.json`` is written
        only with approval, via temp-file replace, with a backup, and after a
        stale-approval check.  This handler never performs a direct
        filesystem mutation.
        """
        import json

        from aura.paths import safe_relative_to

        folder_raw = str(args.get("folder_path") or "").strip()
        if not folder_raw:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "folder_path is required"},
            )

        try:
            folder = self._resolve_in_root(folder_raw)
        except ValueError as exc:
            return ToolExecResult(ok=False, payload={"ok": False, "error": str(exc)})
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
        contract_text = json.dumps(contract, indent=2) + "\n"
        rel_folder = safe_relative_to(folder, self._root).as_posix()
        rel_path = f"{rel_folder}/ui_contract.json"

        result = self._handle_write_file(
            {"path": rel_path, "content": contract_text},
            approval_cb,
            reject_all,
        )
        if result.payload.get("applied") is True:
            result.payload["contract_written"] = True
            result.payload["assertion_count"] = len(assertions)
        return result


def _web_research_drone_retired_result() -> ToolExecResult:
    """Reject the retired browser-based research Drone in favor of web_search."""
    return ToolExecResult(
        ok=False,
        payload={
            "ok": False,
            "status": "unsupported",
            "failure_class": "drone_retired_unsupported",
            "error": "The 'web-research' Drone is retired. Use web_search instead.",
        },
    )

__all__ = [
    "DroneHandlersMixin",
]
