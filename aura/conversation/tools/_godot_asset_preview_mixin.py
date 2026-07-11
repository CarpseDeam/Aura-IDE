"""Safe catalog-driven editing and inspection of a disposable Godot preview root."""

from __future__ import annotations

import math
import re

from aura.conversation.tools._types import ToolExecResult
from aura.conversation.tools.write_payloads import _mark_not_applied
from aura.godot_assets import resolve_godot_asset
from aura.godot_assets.preview import analyze_preview_snapshot
from aura.godot_editor.client import GodotEditorBridgeClient, GodotEditorBridgeError
from aura.paths import safe_is_relative_to
from aura.perception.decompiler import describe as _decompile_image

import base64
import hashlib


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

            if not capture_result.get("ok"):
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": capture_result.get("error", "bridge returned ok=False")},
                )

            raw_captures = capture_result.get("captures")
            if not isinstance(raw_captures, list):
                return ToolExecResult(ok=False, payload={"ok": False, "error": "bridge response missing captures list"})

            scene_fingerprint = capture_result.get("scene_fingerprint", "")
            scene_path_res = capture_result.get("scene_path", "")

            if len(raw_captures) > 4:
                return ToolExecResult(ok=False, payload={"ok": False, "error": "too many captures (max 4)"})

            captures_out = []
            for cap in raw_captures:
                if not isinstance(cap, dict):
                    continue
                res_path = str(cap.get("path", ""))
                if res_path.startswith("res://"):
                    local_rel = res_path[6:].lstrip("/")
                else:
                    local_rel = res_path
                local_path = (self._root / local_rel).resolve()

                allowed_root = (self._root / ".aura" / "tmp" / "godot_previews").resolve()
                if not safe_is_relative_to(local_path, allowed_root):
                    return ToolExecResult(
                        ok=False,
                        payload={"ok": False, "error": f"capture path '{res_path}' escapes allowed preview directory"},
                    )

                if not local_path.suffix.lower() == ".png":
                    return ToolExecResult(
                        ok=False,
                        payload={"ok": False, "error": f"capture path '{res_path}' is not a PNG file"},
                    )

                if not local_path.is_file():
                    return ToolExecResult(
                        ok=False,
                        payload={"ok": False, "error": f"capture file not found: {res_path}"},
                    )

                file_size = local_path.stat().st_size
                if file_size > 20 * 1024 * 1024:
                    return ToolExecResult(
                        ok=False,
                        payload={"ok": False, "error": f"capture file exceeds 20 MiB: {res_path}"},
                    )

                cap_width = cap.get("width", 0)
                cap_height = cap.get("height", 0)
                if (isinstance(cap_width, (int, float)) and cap_width > 3840) or \
                   (isinstance(cap_height, (int, float)) and cap_height > 2160):
                    return ToolExecResult(
                        ok=False,
                        payload={"ok": False, "error": f"capture dimensions exceed 3840x2160: {res_path}"},
                    )

                png_bytes = local_path.read_bytes()
                actual_sha256 = hashlib.sha256(png_bytes).hexdigest()
                expected_sha256 = cap.get("sha256", "")
                if expected_sha256 and actual_sha256 != expected_sha256:
                    return ToolExecResult(
                        ok=False,
                        payload={"ok": False, "error": f"SHA-256 mismatch for {res_path}"},
                    )

                image_b64 = base64.b64encode(png_bytes).decode("ascii")
                visual_structure = _decompile_image(image_b64)

                captures_out.append(
                    {
                        "view": cap.get("view", ""),
                        "path": str(allowed_root.parent.parent.parent / local_rel) if local_rel else str(local_path),
                        "width": cap_width,
                        "height": cap_height,
                        "sha256": actual_sha256,
                        "evidence_kind": "local_structural_decompile",
                        "visual_structure": visual_structure,
                    }
                )

            # Resolve scene_path to workspace-relative
            scene_path_relative = ""
            if scene_path_res.startswith("res://"):
                scene_path_relative = scene_path_res[6:].lstrip("/")
            else:
                scene_path_relative = scene_path_res

            payload = {
                "ok": True,
                "read_only": True,
                "capture_set_id": params.get("capture_set_id", ""),
                "scene_path": scene_path_relative,
                "scene_fingerprint": scene_fingerprint,
                "preview": {
                    "instance_count": len(preview_facts.get("instances", [])),
                    "instances": preview_facts.get("instances", []),
                    "structural_validation": preview_facts.get("structural_validation", {}),
                },
                "captures": captures_out,
            }
            return ToolExecResult(ok=True, payload=payload)
        except (GodotEditorBridgeError, TypeError, ValueError, OSError) as exc:
            return ToolExecResult(ok=False, payload={"ok": False, "error": str(exc)})

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
