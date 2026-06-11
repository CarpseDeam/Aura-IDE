from __future__ import annotations

from typing import Any

from aura.conversation.tools._types import ToolExecResult
from aura.drones.capabilities import CapabilityBinding, CapabilityRequirement
from aura.drones.definition import DroneBudget, DroneDefinition
from aura.drones.store import DroneStore
from aura.drones.capability_resolver import (
    AppRouteProvider,
    CapabilityContext,
    CapabilityResolver,
    DynamicToolProvider,
    GeneratedCodeFallbackProvider,
    InstalledMCPProvider,
    MCPDiscoveryProvider,
    StaticToolProvider,
)


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
        job = runner.launch(drone, goal)

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

        # Per-turn limit check
        count = getattr(self, "_drone_runs", 0) + 1
        self._drone_runs = count
        if count >= 3:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "Per-turn limit of 3 drone runs reached"},
            )

        from aura.drones.sync_runner import run_read_only_drone_sync

        try:
            result = run_read_only_drone_sync(
                drone_id=drone_id,
                goal=goal,
                workspace_root=self._root,
                drone=drone,
            )
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

    def _handle_save_drone_definition(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        """Create and persist a DroneDefinition via DroneStore."""
        try:
            # Required fields
            name = str(args.get("name") or "").strip()
            description = str(args.get("description") or "").strip()
            instructions = str(args.get("instructions") or "").strip()
            write_policy = str(args.get("write_policy") or "").strip()
            allowed_tools_raw = args.get("allowed_tools", [])
            output_contract = str(args.get("output_contract") or "").strip()

            if not name:
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": "name is required"},
                )
            if not description:
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": "description is required"},
                )
            if not instructions:
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": "instructions is required"},
                )
            if not write_policy:
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": "write_policy is required"},
                )
            if not allowed_tools_raw or not isinstance(allowed_tools_raw, list):
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": "allowed_tools must be a non-empty array of strings"},
                )
            if not output_contract:
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": "output_contract is required"},
                )

            # ID: use provided or generate from name
            drone_id = str(args.get("id") or "").strip()
            if not drone_id:
                drone_id = DroneStore.next_id(self._root, name)

            # Optional fields with defaults
            scope = str(args.get("scope") or "global").strip()
            enabled = bool(args.get("enabled", True))
            created_by = str(args.get("created_by") or "user").strip()

            # Budget
            budget_raw = args.get("budget", {})
            if not isinstance(budget_raw, dict):
                budget_raw = {}
            max_tool_rounds = int(budget_raw.get("max_tool_rounds", 8))
            timeout_seconds = int(budget_raw.get("timeout_seconds", 300))
            budget = DroneBudget(
                max_tool_rounds=max_tool_rounds,
                timeout_seconds=timeout_seconds,
            )

            # Allowed tools
            allowed_tools = tuple(str(t) for t in allowed_tools_raw if isinstance(t, str))

            # Capability requirements
            cap_reqs_raw = args.get("capability_requirements", [])
            if not isinstance(cap_reqs_raw, list):
                cap_reqs_raw = []
            capability_requirements: tuple[CapabilityRequirement, ...] = ()
            if cap_reqs_raw:
                reqs = []
                for item in cap_reqs_raw:
                    if not isinstance(item, dict):
                        continue
                    reqs.append(
                        CapabilityRequirement(
                            capability=str(item.get("capability", "")),
                            purpose=str(item.get("purpose", "")),
                            notes=str(item.get("notes", "")),
                        )
                    )
                capability_requirements = tuple(reqs)

            # Capability bindings
            cap_binds_raw = args.get("capability_bindings", [])
            if not isinstance(cap_binds_raw, list):
                cap_binds_raw = []
            capability_bindings: tuple[CapabilityBinding, ...] = ()
            if cap_binds_raw:
                binds = []
                for item in cap_binds_raw:
                    if not isinstance(item, dict):
                        continue
                    tool_name_val = item.get("tool_name", "")
                    sn = item.get("setup_notes", [])
                    if isinstance(sn, list):
                        setup_notes_val = "; ".join(str(s) for s in sn)
                    else:
                        setup_notes_val = str(sn)
                    binds.append(
                        CapabilityBinding(
                            capability=str(item.get("capability", "")),
                            route_kind=str(item.get("route", "")),
                            source="user",
                            tool_names=tuple([str(tool_name_val)]) if tool_name_val else (),
                            setup_notes=setup_notes_val,
                        )
                    )
                capability_bindings = tuple(binds)

            # Setup steps
            setup_steps_raw = args.get("setup_steps", [])
            if isinstance(setup_steps_raw, list):
                setup_steps = tuple(str(s) for s in setup_steps_raw)
            else:
                setup_steps = ()

            first_run_test = str(args.get("first_run_test", "")).strip()

            # Build DroneDefinition
            drone = DroneDefinition(
                id=drone_id,
                name=name,
                description=description,
                instructions=instructions,
                write_policy=write_policy,
                allowed_tools=allowed_tools,
                output_contract=output_contract,
                budget=budget,
                scope=scope,
                enabled=enabled,
                created_by=created_by,
                created_at="",
                updated_at="",
                capability_requirements=capability_requirements,
                capability_bindings=capability_bindings,
                setup_steps=setup_steps,
                first_run_test=first_run_test,
            )

            # Validate
            try:
                DroneStore.validate_drone(drone)
            except ValueError as ve:
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": str(ve)},
                )

            # Save
            DroneStore.save_drone(self._root, drone)

            return ToolExecResult(
                ok=True,
                payload={
                    "ok": True,
                    "drone_saved": True,
                    "drone_id": drone.id,
                    "id": drone.id,
                    "name": drone.name,
                    "scope": drone.scope,
                    "write_policy": drone.write_policy,
                    "allowed_tools": list(drone.allowed_tools),
                },
                extras={
                    "drone_saved": True,
                    "drone_id": drone.id,
                },
            )
        except Exception as e:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": str(e)},
            )

    def _handle_resolve_capability(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        """Resolve capability requirements and return candidate routes."""
        requirements_raw = args.get("requirements", [])
        if not isinstance(requirements_raw, list) or len(requirements_raw) == 0:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "requirements must be a non-empty list"},
            )

        # 1. Parse requirements into CapabilityRequirement objects
        requirements: list[CapabilityRequirement] = []
        for item in requirements_raw:
            if not isinstance(item, dict):
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": "each requirement must be a dict"},
                )
            cap = str(item.get("capability", "")).strip()
            if not cap:
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": "each requirement must have a non-empty capability string"},
                )
            requirements.append(
                CapabilityRequirement(
                    capability=cap,
                    purpose=str(item.get("purpose", "")),
                    notes=str(item.get("notes", "")),
                )
            )

        # 2. Build context from current tool registry state
        def _tool_names_from_schemas(schemas: list[dict[str, Any]]) -> tuple[str, ...]:
            names: list[str] = []
            for s in schemas:
                fn = s.get("function")
                if isinstance(fn, dict):
                    name = fn.get("name")
                    if isinstance(name, str) and name:
                        names.append(name)
            return tuple(sorted(names))

        all_schemas = self._catalog.build_tool_defs(
            mode=self._mode,
            read_only=self._read_only,
        )
        available_tool_names = _tool_names_from_schemas(all_schemas)

        dynamic_schemas = self._dynamic_tools.schemas()
        dynamic_tool_names = _tool_names_from_schemas(dynamic_schemas)

        mcp_schemas = self._mcp_tools.schemas
        mcp_tool_names = _tool_names_from_schemas(mcp_schemas)

        context = CapabilityContext(
            workspace_root=self._root,
            available_tool_names=available_tool_names,
            dynamic_tool_names=dynamic_tool_names,
            mcp_tool_names=mcp_tool_names,
        )

        # 3. Build resolver and resolve
        resolver = CapabilityResolver(
            providers=[
                StaticToolProvider(),
                DynamicToolProvider(),
                InstalledMCPProvider(),
                MCPDiscoveryProvider(),
                AppRouteProvider(),
                GeneratedCodeFallbackProvider(),
            ]
        )
        resolution = resolver.resolve(
            requirements=tuple(requirements),
            context=context,
        )

        # 4. Serialize to JSON-compatible dict
        payload = {
            "ok": True,
            "requirements": [r.to_dict() for r in resolution.requirements],
            "candidates": [c.to_dict() for c in resolution.candidates],
            "selected_bindings": [b.to_dict() for b in resolution.selected_bindings],
            "allowed_tools": list(resolution.allowed_tools),
            "setup_notes": list(resolution.setup_notes),
        }
        return ToolExecResult(ok=True, payload=payload)
