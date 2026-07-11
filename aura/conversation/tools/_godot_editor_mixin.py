"""Conversation tools for Aura's authenticated live Godot editor bridge."""

from __future__ import annotations

import json

from aura.conversation.tools._types import ApprovalRequest, ToolExecResult
from aura.conversation.tools.write_payloads import _mark_not_applied
from aura.godot_editor.api_query import query_godot_api_offline
from aura.godot_editor.client import GodotEditorBridgeClient, GodotEditorBridgeError
from aura.godot_editor.installer import install_editor_bridge


class GodotEditorHandlersMixin:
    """Install, observe, and manipulate the active Godot editor."""

    def _handle_inspect_godot_editor(self, args, approval_cb, reject_all) -> ToolExecResult:
        try:
            result = GodotEditorBridgeClient(self._root).request(
                "scene.snapshot",
                {
                    "include_properties": bool(args.get("include_properties", True)),
                    "max_nodes": int(args.get("max_nodes", 500)),
                },
            )
        except (GodotEditorBridgeError, TypeError, ValueError) as exc:
            message = str(exc)
            payload = {"ok": False, "error": message}
            if "not installed" in message.lower():
                payload.update(
                    {
                        "bridge_installed": False,
                        "suggested_next_tool": "install_godot_editor_bridge",
                        "suggested_next_action": (
                            "Dispatch a Worker to call the bundled installer; do not author a new addon."
                        ),
                    }
                )
            return ToolExecResult(ok=False, payload=payload)
        return ToolExecResult(ok=True, payload={"ok": True, **result})

    def _handle_inspect_godot_api(self, args, approval_cb, reject_all) -> ToolExecResult:
        params = {
            "class_name": str(args.get("class_name") or ""),
            "member_query": str(args.get("member_query") or ""),
            "include_inherited": bool(args.get("include_inherited", False)),
            "max_items": int(args.get("max_items", 50)),
        }
        source = "live_godot_classdb"
        try:
            result = GodotEditorBridgeClient(self._root).request("api.describe", params)
        except GodotEditorBridgeError:
            try:
                result = query_godot_api_offline(self._root, params)
                source = "configured_godot_classdb"
            except (RuntimeError, TypeError, ValueError) as exc:
                return ToolExecResult(ok=False, payload={"ok": False, "error": str(exc)})
        except (TypeError, ValueError) as exc:
            return ToolExecResult(ok=False, payload={"ok": False, "error": str(exc)})
        return ToolExecResult(
            ok=True,
            payload={
                "ok": True,
                "read_only": True,
                "source": source,
                "note": "ClassDB covers engine classes; script-defined class_name types require project code inspection.",
                **result,
            },
        )

    def _handle_edit_godot_editor(self, args, approval_cb, reject_all) -> ToolExecResult:
        blocked = self._live_editor_write_block("edit_godot_editor")
        if blocked is not None:
            return blocked
        action = str(args.get("action") or "")
        action_map = {
            "apply": "scene.apply",
            "select": "scene.select",
            "save": "scene.save",
        }
        if action not in action_map:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {"ok": False, "error": "action must be apply, select, or save", "failure_class": "invalid_godot_editor_action"}
                ),
            )
        params = {key: value for key, value in args.items() if key != "action"}
        rejected = self._approve_live_editor_change(
            "edit_godot_editor", f"godot-editor:{action}", params, approval_cb, reject_all
        )
        if rejected is not None:
            return rejected
        try:
            result = GodotEditorBridgeClient(self._root).request(action_map[action], params)
        except GodotEditorBridgeError as exc:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {"ok": False, "error": str(exc), "failure_class": "godot_editor_bridge_error"}
                ),
            )
        return ToolExecResult(ok=True, payload={"ok": True, "applied": True, "action": action, **result})

    def _handle_install_godot_editor_bridge(self, args, approval_cb, reject_all) -> ToolExecResult:
        blocked = self._live_editor_write_block("install_godot_editor_bridge")
        if blocked is not None:
            return blocked
        try:
            port = int(args.get("port", 17891))
        except (TypeError, ValueError):
            port = 0
        enable_plugin = bool(args.get("enable_plugin", False))
        rejected = self._approve_live_editor_change(
            "install_godot_editor_bridge",
            "addons/aura_bridge",
            {
                "install": "Aura EditorPlugin",
                "port": port,
                "enable_in_project_godot": enable_plugin,
            },
            approval_cb,
            reject_all,
        )
        if rejected is not None:
            return rejected
        try:
            result = install_editor_bridge(self._root, port, enable_plugin=enable_plugin)
        except (OSError, RuntimeError, ValueError) as exc:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {"ok": False, "error": str(exc), "failure_class": "godot_editor_bridge_install_failed"}
                ),
            )
        return ToolExecResult(
            ok=True,
            payload={
                "ok": True,
                "applied": True,
                "installed": True,
                "plugin_enabled": result.plugin_enabled,
                "port": result.port,
                "files": list(result.files),
                "next_step": (
                    "In Godot, open Project > Project Settings > Plugins and enable Aura Editor Bridge."
                    if not result.plugin_enabled
                    else "Open or restart this project in Godot; the bridge starts with the editor."
                ),
            },
        )

    def _live_editor_write_block(self, tool_name: str) -> ToolExecResult | None:
        if self._read_only:
            error = "Read-Only Mode is enabled — live Godot editor changes are disabled."
            failure_class = "read_only"
        elif self._mode == "planner":
            error = "Planner cannot change the Godot editor directly; dispatch this work to a Worker."
            failure_class = "planner_write_forbidden"
        else:
            return None
        return ToolExecResult(
            ok=False,
            payload=_mark_not_applied(
                {"ok": False, "error": error, "tool": tool_name, "failure_class": failure_class}
            ),
        )

    @staticmethod
    def _approve_live_editor_change(tool_name, rel_path, proposed, approval_cb, reject_all):
        if reject_all:
            decision = "reject_all"
        else:
            request = ApprovalRequest(
                tool_name=tool_name,
                rel_path=rel_path,
                old_content="",
                new_content=json.dumps(proposed, indent=2, ensure_ascii=False),
                is_new_file=False,
            )
            decision = approval_cb(request).action
        if decision not in {"reject", "reject_all"}:
            return None
        return ToolExecResult(
            ok=False,
            payload=_mark_not_applied(
                {
                    "ok": False,
                    "error": "User rejected this live Godot editor change.",
                    "rejected": True,
                    "failure_class": "approval_rejected",
                },
                "approval_rejected",
            ),
        )


__all__ = ["GodotEditorHandlersMixin"]
