"""Approval-gated conversation tool for structured Godot scene edits."""

from __future__ import annotations

from aura.conversation.tools._types import ToolExecResult
from aura.conversation.tools.write_payloads import _mark_not_applied
from aura.godot_scene_editor import GodotSceneEditError, edit_godot_scene


class GodotSceneHandlersMixin:
    """Expose the pure scene transformer through Aura's normal write layer."""

    def _handle_edit_godot_scene(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {
                        "ok": False,
                        "error": "Read-Only Mode is enabled — scene edits are disabled.",
                        "failure_class": "read_only",
                    }
                ),
            )
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {
                        "ok": False,
                        "error": "Planner cannot edit scenes directly; dispatch the scene changes to a Worker.",
                        "failure_class": "planner_write_forbidden",
                    }
                ),
            )

        path_arg = args.get("path", "")
        target = self._resolve_in_root(path_arg)
        if target.suffix.lower() != ".tscn":
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {
                        "ok": False,
                        "path": str(path_arg),
                        "error": "edit_godot_scene only accepts .tscn text scenes.",
                        "failure_class": "invalid_godot_scene_path",
                    }
                ),
            )
        try:
            content = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {
                        "ok": False,
                        "path": str(path_arg),
                        "error": "Godot scene does not exist.",
                        "failure_class": "file_not_found",
                    }
                ),
            )

        operations = args.get("operations")
        if not isinstance(operations, list):
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {
                        "ok": False,
                        "path": str(path_arg),
                        "error": "operations must be an array.",
                        "failure_class": "invalid_scene_operation",
                    }
                ),
            )
        try:
            transformed = edit_godot_scene(content, operations)
        except GodotSceneEditError as exc:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {
                        "ok": False,
                        "path": str(path_arg),
                        "error": str(exc),
                        "failure_class": "invalid_scene_operation",
                    }
                ),
            )

        result = self._handle_write(
            "write_file",
            {
                "path": path_arg,
                "content": transformed.content,
                "full_replace_existing": True,
                "replacement_reason": "structured Godot scene node operations",
            },
            approval_cb,
            reject_all,
        )
        if result.ok:
            result.payload["applied_tool"] = "edit_godot_scene"
            result.payload["operation_count"] = len(transformed.operations)
            result.payload["scene_operations"] = list(transformed.operations)
        return result


__all__ = ["GodotSceneHandlersMixin"]
