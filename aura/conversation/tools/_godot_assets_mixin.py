"""Read-only conversation access to recognized Godot asset catalogs."""

from __future__ import annotations

from aura.conversation.tools._types import ToolExecResult
from aura.godot_assets import inspect_godot_assets


class GodotAssetHandlersMixin:
    def _handle_inspect_godot_assets(self, args, approval_cb, reject_all) -> ToolExecResult:
        try:
            payload = inspect_godot_assets(
                self._root,
                domains=args.get("domains", []),
                kinds=args.get("kinds", []),
                tags=args.get("tags", []),
                semantic_roles=args.get("semantic_roles", []),
                query=str(args.get("query") or ""),
                max_items=int(args.get("max_items", 50)),
            )
        except (TypeError, ValueError) as exc:
            payload = {"ok": False, "error": f"invalid Godot asset query: {exc}"}
        return ToolExecResult(ok=payload.get("ok", False), payload=payload)


__all__ = ["GodotAssetHandlersMixin"]
