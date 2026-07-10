"""Safe catalog-driven editing and inspection of a disposable Godot preview root."""

from __future__ import annotations

import math
import re

from aura.conversation.tools._types import ToolExecResult
from aura.conversation.tools.write_payloads import _mark_not_applied
from aura.godot_assets import resolve_godot_asset
from aura.godot_assets.preview import analyze_preview_snapshot
from aura.godot_editor.client import GodotEditorBridgeClient, GodotEditorBridgeError


class GodotAssetPreviewHandlersMixin:
    def _handle_inspect_godot_asset_preview(self, args, approval_cb, reject_all) -> ToolExecResult:
        try:
            snapshot = GodotEditorBridgeClient(self._root).request("preview.snapshot", {})
            payload = analyze_preview_snapshot(self._root, snapshot)
        except (GodotEditorBridgeError, TypeError, ValueError) as exc:
            return ToolExecResult(ok=False, payload={"ok": False, "error": _preview_bridge_error(exc)})
        return ToolExecResult(ok=True, payload={"ok": True, "read_only": True, **payload})

    def _handle_edit_godot_asset_preview(self, args, approval_cb, reject_all) -> ToolExecResult:
        blocked = self._live_editor_write_block("edit_godot_asset_preview")
        if blocked is not None:
            return blocked
        action = str(args.get("action") or "")
        if action not in {"instantiate", "clear"}:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {"ok": False, "error": "action must be instantiate or clear", "failure_class": "invalid_preview_action"}
                ),
            )
        try:
            params = self._prepare_preview_params(action, args)
        except (TypeError, ValueError) as exc:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {"ok": False, "error": str(exc), "failure_class": "invalid_asset_placement"}
                ),
            )
        rejected = self._approve_live_editor_change(
            "edit_godot_asset_preview", f"godot-editor:preview-{action}", params, approval_cb, reject_all
        )
        if rejected is not None:
            return rejected
        try:
            result = GodotEditorBridgeClient(self._root).request(f"preview.{action}", params)
        except GodotEditorBridgeError as exc:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {"ok": False, "error": _preview_bridge_error(exc), "failure_class": "godot_editor_bridge_error"}
                ),
            )
        return ToolExecResult(ok=True, payload={"ok": True, "applied": bool(result.get("applied")), "action": action, **result})

    def _prepare_preview_params(self, action: str, args: dict) -> dict:
        label = str(args.get("label") or ("Aura place catalog assets" if action == "instantiate" else "Aura clear asset preview"))
        if action == "clear":
            return {"label": label}
        raw_placements = args.get("placements")
        if not isinstance(raw_placements, list) or not 1 <= len(raw_placements) <= 64:
            raise ValueError("placements must contain between 1 and 64 items")
        placements = []
        used_names: set[str] = set()
        for index, raw in enumerate(raw_placements):
            if not isinstance(raw, dict):
                raise ValueError(f"placement {index} must be an object")
            asset = resolve_godot_asset(
                self._root, str(raw.get("asset_id") or ""), domain=str(raw.get("domain") or "")
            )
            position = _vector(raw.get("position", [0, 0, 0]), f"placement {index} position")
            scale = _vector(raw.get("scale", [1, 1, 1]), f"placement {index} scale")
            if any(abs(value) > 10000 for value in position):
                raise ValueError(f"placement {index} exceeds the 10 km preview bound")
            if any(value < 0.01 or value > 100 for value in scale):
                raise ValueError(f"placement {index} scale must be between 0.01 and 100")
            rotation = _number(raw.get("rotation_degrees_y", 0), f"placement {index} rotation")
            if asset.allowed_rotations_deg and not any(
                math.isclose(rotation % 360, allowed % 360, abs_tol=1e-4)
                for allowed in asset.allowed_rotations_deg
            ):
                raise ValueError(f"rotation is not allowed for catalog asset {asset.id}")
            name = str(raw.get("name") or _default_name(asset.id, index)).strip()
            if not name or name in used_names or any(char in name for char in ".:@/\""):
                raise ValueError(f"placement name is invalid or duplicated: {name}")
            used_names.add(name)
            placements.append(
                {
                    "catalog_identity": f"{asset.domain}:{asset.id}",
                    "resource_path": asset.resource_path,
                    "name": name,
                    "position": position,
                    "rotation_degrees_y": rotation,
                    "scale": scale,
                }
            )
        return {"label": label, "placements": placements}


def _number(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _vector(value, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{label} must contain three numbers")
    return [_number(component, label) for component in value]


def _default_name(asset_id: str, index: int) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", asset_id).strip("_") or "Asset"
    return f"{stem}_{index + 1:02d}"


def _preview_bridge_error(exc: Exception) -> str:
    message = str(exc)
    if "unsupported action: preview." in message:
        return (
            "The active Godot plugin is an older Aura bridge. Run install_godot_editor_bridge, "
            "then disable and re-enable Aura Editor Bridge once in Godot's Plugins panel."
        )
    return message


__all__ = ["GodotAssetPreviewHandlersMixin"]
