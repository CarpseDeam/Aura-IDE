"""Safe catalog-driven editing and inspection of a disposable Godot preview root."""

from __future__ import annotations

import math
import re

from aura.conversation.tools._types import ToolExecResult
from aura.conversation.tools.godot_preview_alignment import (
    PreviewRevisionPreparer,
    _catalog_socket,  # noqa: F401 - compatibility re-export for focused callers
)
from aura.conversation.tools.godot_preview_results import compact_post_apply_summary
from aura.conversation.tools.write_payloads import _mark_not_applied
from aura.godot_assets import resolve_godot_asset
from aura.godot_assets.capture_evidence import validate_capture_set
from aura.godot_assets.preview import analyze_preview_snapshot
from aura.godot_editor.client import GodotEditorBridgeClient, GodotEditorBridgeError
from aura.godot_editor.limits import (
    MAX_INITIAL_PLACEMENTS,
)
from aura.perception.decompiler import describe as _decompile_image


class GodotAssetPreviewHandlersMixin:
    def _handle_inspect_godot_asset_preview(self, args, approval_cb, reject_all) -> ToolExecResult:
        try:
            snapshot = GodotEditorBridgeClient(self._root).request("preview.snapshot", {})
            payload = analyze_preview_snapshot(self._root, snapshot)
        except (GodotEditorBridgeError, TypeError, ValueError) as exc:
            return ToolExecResult(ok=False, payload={"ok": False, "error": _preview_bridge_error(exc)})
        return ToolExecResult(ok=True, payload={"ok": True, "read_only": True, **payload})

    def _handle_capture_godot_asset_preview(self, args, approval_cb, reject_all) -> ToolExecResult:
        try:
            params = {}
            raw_capture_set_id = args.get("capture_set_id")
            if raw_capture_set_id is not None:
                capture_set_id = str(raw_capture_set_id)
                for ch in ("..", "/", "\\"):
                    if ch in capture_set_id:
                        return ToolExecResult(
                            ok=False,
                            payload={"ok": False, "error": f"capture_set_id must not contain '{ch}'"},
                        )
                params["capture_set_id"] = capture_set_id
            width = args.get("width", 1280)
            if width is not None:
                w = int(width)
                if w < 64 or w > 1920:
                    return ToolExecResult(ok=False, payload={"ok": False, "error": f"width must be between 64 and 1920, got {w}"})
                params["width"] = w
            height = args.get("height", 720)
            if height is not None:
                h = int(height)
                if h < 64 or h > 1080:
                    return ToolExecResult(ok=False, payload={"ok": False, "error": f"height must be between 64 and 1080, got {h}"})
                params["height"] = h
            raw_modes = args.get("modes", ["current_editor"])
            if raw_modes is not None:
                modes = list(raw_modes)
                if len(modes) < 1 or len(modes) > 4:
                    return ToolExecResult(ok=False, payload={"ok": False, "error": "modes must contain between 1 and 4 items"})
                valid_modes = {"current_editor", "overview", "top_down"}
                for m in modes:
                    if m not in valid_modes:
                        return ToolExecResult(ok=False, payload={"ok": False, "error": f"unknown mode '{m}'; valid: current_editor, overview, top_down"})
                params["modes"] = modes

            client = GodotEditorBridgeClient(self._root)
            capture_result = client.request("preview.capture", params)
            snapshot = client.request("preview.snapshot", {})
            preview_facts = analyze_preview_snapshot(self._root, snapshot)
            evidence = validate_capture_set(
                self._root, capture_result, preview_facts, _decompile_image
            )
            payload = {"ok": True, "read_only": True, **evidence}
            return ToolExecResult(ok=True, payload=payload)
        except (GodotEditorBridgeError, TypeError, ValueError, OSError) as exc:
            return ToolExecResult(
                ok=False, payload={"ok": False, "error": _preview_bridge_error(exc)}
            )

    def _handle_edit_godot_asset_preview(self, args, approval_cb, reject_all) -> ToolExecResult:
        blocked = self._live_editor_write_block("edit_godot_asset_preview")
        if blocked is not None:
            return blocked
        action = str(args.get("action") or "")
        if action not in {"instantiate", "clear", "apply"}:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {"ok": False, "error": "action must be instantiate, clear, or apply", "failure_class": "invalid_preview_action"}
                ),
            )
        try:
            params = self._prepare_preview_params(action, args)
        except (GodotEditorBridgeError, TypeError, ValueError) as exc:
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
        client = GodotEditorBridgeClient(self._root)
        try:
            result = client.request(f"preview.{action}", params)
        except GodotEditorBridgeError as exc:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {"ok": False, "error": _preview_bridge_error(exc), "failure_class": "godot_editor_bridge_error"}
                ),
            )
        payload = {
            "ok": True,
            "applied": bool(result.get("applied")),
            "action": action,
            **result,
        }
        try:
            snapshot = client.request("preview.snapshot", {})
            analyzed = analyze_preview_snapshot(self._root, snapshot)
            payload["post_apply"] = compact_post_apply_summary(
                self._root, action, params, result, analyzed
            )
        except (GodotEditorBridgeError, TypeError, ValueError) as exc:
            payload["post_apply"] = {
                "available": False,
                "error": _preview_bridge_error(exc),
            }
        return ToolExecResult(ok=True, payload=payload)

    def _prepare_preview_params(self, action: str, args: dict) -> dict:
        default_labels = {
            "instantiate": "Aura place catalog assets",
            "clear": "Aura clear asset preview",
            "apply": "Aura revise asset preview",
        }
        label = str(args.get("label") or default_labels[action])
        if action == "clear":
            return {"label": label}
        if action == "apply":
            return {"label": label, "operations": self._prepare_preview_operations(args)}
        raw_placements = args.get("placements")
        if (
            not isinstance(raw_placements, list)
            or not 1 <= len(raw_placements) <= MAX_INITIAL_PLACEMENTS
        ):
            raise ValueError(
                f"placements must contain between 1 and {MAX_INITIAL_PLACEMENTS} items"
            )
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
                    "allowed_rotations_deg": list(asset.allowed_rotations_deg),
                }
            )
        return {"label": label, "placements": placements}

    def _prepare_preview_operations(self, args: dict) -> list[dict]:
        return PreviewRevisionPreparer(self._root, GodotEditorBridgeClient).prepare(args)


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
    if (
        "unsupported action: preview." in message
        or "unsupported revision operation: duplicate" in message
        or "unsupported revision operation: attach" in message
    ):
        return (
            "The active Godot plugin is an older Aura bridge. Run install_godot_editor_bridge, "
            "then disable and re-enable Aura Editor Bridge once in Godot's Plugins panel."
        )
    return message


__all__ = ["GodotAssetPreviewHandlersMixin"]
