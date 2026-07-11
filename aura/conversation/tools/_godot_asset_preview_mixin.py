"""Safe catalog-driven editing and inspection of a disposable Godot preview root."""

from __future__ import annotations

import math
import re

from aura.conversation.tools._types import ToolExecResult
from aura.conversation.tools.write_payloads import _mark_not_applied
from aura.godot_assets import resolve_godot_asset
from aura.godot_assets.capture_evidence import validate_capture_set
from aura.godot_assets.models import GodotAsset, GodotAssetSocket
from aura.godot_assets.preview import analyze_preview_snapshot
from aura.godot_editor.client import GodotEditorBridgeClient, GodotEditorBridgeError
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

    def _prepare_preview_operations(self, args: dict) -> list[dict]:
        raw_operations = args.get("operations")
        if not isinstance(raw_operations, list) or not 1 <= len(raw_operations) <= 25:
            raise ValueError("operations must contain between 1 and 25 items")
        operations: list[dict] = []
        targeted: set[str] = set()
        removed_or_replaced: set[str] = set()
        added_names: set[str] = set()
        planned_nodes: dict[str, GodotAsset] = {}
        preview_instances: list[dict] | None = None
        for index, raw in enumerate(raw_operations):
            if not isinstance(raw, dict):
                raise ValueError(f"operation {index} must be an object")
            operation = str(raw.get("operation") or "")
            if operation not in {"set_transform", "instantiate", "remove", "replace", "duplicate", "attach"}:
                raise ValueError(f"operation {index} has an unsupported operation")
            if operation == "instantiate":
                prepared = self._prepare_catalog_revision_asset(raw, index)
                if prepared["name"] in added_names:
                    raise ValueError(f"duplicate new preview name: {prepared['name']}")
                added_names.add(prepared["name"])
                operations.append({"operation": operation, **prepared})
                planned_nodes[f"AuraPreview/{prepared['name']}"] = resolve_godot_asset(
                    self._root,
                    str(raw.get("asset_id") or ""),
                    domain=str(raw.get("domain") or ""),
                )
                targeted.discard(f"AuraPreview/{prepared['name']}")
                continue

            node_path = _direct_preview_path(raw.get("node_path"), index)
            source_is_planned = node_path in planned_nodes
            if source_is_planned and operation not in {"duplicate", "attach"}:
                raise ValueError(
                    f"operation {index} cannot {operation} an unattached planned node: {node_path}"
                )
            if operation in {"duplicate", "attach"}:
                if node_path in removed_or_replaced:
                    raise ValueError(
                        f"operation {index} cannot {operation} from a removed or replaced source: {node_path}"
                    )
            else:
                if node_path in targeted:
                    raise ValueError(f"preview target appears in more than one operation: {node_path}")
                targeted.add(node_path)
            if operation == "attach":
                if "position" in raw or "rotation_degrees_y" in raw:
                    raise ValueError(
                        f"attach operation {index} derives position and rotation from sockets"
                    )
                source_asset, preview_instances = self._preview_source_asset(
                    node_path, "attach", planned_nodes, preview_instances
                )
                source_socket = _catalog_socket(
                    source_asset,
                    str(raw.get("source_socket") or ""),
                    f"attach source asset {source_asset.id}",
                )
                target_asset = resolve_godot_asset(
                    self._root,
                    str(raw.get("asset_id") or ""),
                    domain=str(raw.get("domain") or ""),
                )
                target_socket = _catalog_socket(
                    target_asset,
                    str(raw.get("target_socket") or ""),
                    f"attach target asset {target_asset.id}",
                )
                _validate_horizontal_socket(source_socket.facing, source_asset.id, source_socket.id)
                _validate_horizontal_socket(target_socket.facing, target_asset.id, target_socket.id)
                scale = _vector(raw.get("scale", [1, 1, 1]), f"operation {index} scale")
                _validate_transform_bounds({"scale": scale}, index)
                name = str(raw.get("name") or "").strip()
                if name and any(char in name for char in ".:@/\""):
                    raise ValueError(f"operation {index} has an invalid node name: {name}")
                prepared_attach = {
                    "operation": operation,
                    "node_path": node_path,
                    "source_catalog_identity": f"{source_asset.domain}:{source_asset.id}",
                    "source_resource_path": source_asset.resource_path,
                    "source_socket_position": list(source_socket.position),
                    "source_socket_facing": list(source_socket.facing),
                    "catalog_identity": f"{target_asset.domain}:{target_asset.id}",
                    "asset_id": target_asset.id,
                    "resource_path": target_asset.resource_path,
                    "target_socket_position": list(target_socket.position),
                    "target_socket_facing": list(target_socket.facing),
                    "allowed_rotations_deg": list(target_asset.allowed_rotations_deg),
                    "scale": scale,
                }
                if name:
                    if name in added_names:
                        raise ValueError(f"duplicate new preview name: {name}")
                    added_names.add(name)
                    prepared_attach["name"] = name
                operations.append(prepared_attach)
                if name:
                    planned_nodes[f"AuraPreview/{name}"] = target_asset
                    targeted.discard(f"AuraPreview/{name}")
                continue
            if operation == "duplicate":
                count = _bounded_int(raw.get("count"), f"duplicate operation {index} count", 1, 16)
                if "offset" not in raw:
                    raise ValueError(f"duplicate operation {index} requires an offset")
                offset = _vector(raw["offset"], f"duplicate operation {index} offset")
                offset_space = str(raw.get("offset_space") or "local")
                if offset_space not in {"local", "world"}:
                    raise ValueError(
                        f"duplicate operation {index} offset_space must be local or world"
                    )
                name = str(raw.get("name") or "").strip()
                if name and count != 1:
                    raise ValueError(
                        f"duplicate operation {index} can use name only when count is 1"
                    )
                if name and any(char in name for char in ".:@/\""):
                    raise ValueError(f"operation {index} has an invalid node name: {name}")
                if name and name in added_names:
                    raise ValueError(f"duplicate new preview name: {name}")
                asset, preview_instances = self._preview_source_asset(
                    node_path, "duplicate", planned_nodes, preview_instances
                )
                prepared_duplicate = {
                    "operation": operation,
                    "node_path": node_path,
                    "count": count,
                    "offset": offset,
                    "offset_space": offset_space,
                    "catalog_identity": f"{asset.domain}:{asset.id}",
                    "resource_path": asset.resource_path,
                }
                if name:
                    prepared_duplicate["name"] = name
                    added_names.add(name)
                    planned_nodes[f"AuraPreview/{name}"] = asset
                    targeted.discard(f"AuraPreview/{name}")
                operations.append(prepared_duplicate)
                continue
            if operation == "remove":
                removed_or_replaced.add(node_path)
                operations.append({"operation": operation, "node_path": node_path})
                continue
            if operation == "set_transform":
                transform = _optional_transform(raw, index)
                if not transform:
                    raise ValueError(f"set_transform operation {index} must change a transform value")
                operations.append({"operation": operation, "node_path": node_path, **transform})
                continue
            removed_or_replaced.add(node_path)
            prepared = self._prepare_catalog_revision_asset(raw, index)
            operations.append(
                {
                    "operation": operation,
                    "node_path": node_path,
                    **prepared,
                    **_optional_transform(raw, index),
                }
            )
            replacement_name = str(prepared.get("name") or node_path.split("/", 1)[1])
            if replacement_name in added_names:
                raise ValueError(f"duplicate new preview name: {replacement_name}")
            added_names.add(replacement_name)
            planned_nodes[f"AuraPreview/{replacement_name}"] = resolve_godot_asset(
                self._root,
                str(raw.get("asset_id") or ""),
                domain=str(raw.get("domain") or ""),
            )
            targeted.discard(f"AuraPreview/{replacement_name}")
        return operations

    def _preview_source_asset(
        self,
        node_path: str,
        operation: str,
        planned_nodes: dict[str, GodotAsset],
        preview_instances: list[dict] | None,
    ) -> tuple[GodotAsset, list[dict] | None]:
        planned_asset = planned_nodes.get(node_path)
        if planned_asset is not None:
            return planned_asset, preview_instances
        if preview_instances is None:
            snapshot = GodotEditorBridgeClient(self._root).request("preview.snapshot", {})
            preview_instances = list(
                analyze_preview_snapshot(self._root, snapshot).get("instances") or []
            )
        source = next(
            (item for item in preview_instances if str(item.get("path") or "") == node_path),
            None,
        )
        if source is None:
            raise ValueError(f"{operation} source does not exist: {node_path}")
        asset_id = str(source.get("asset_id") or "")
        domain = str(source.get("domain") or "")
        if not asset_id or not domain:
            raise ValueError(f"{operation} source is not a recognized catalog asset: {node_path}")
        asset = resolve_godot_asset(self._root, asset_id, domain=domain)
        resource_path = str(source.get("resource_path") or "")
        if resource_path.casefold() != asset.resource_path.casefold():
            raise ValueError(f"{operation} source catalog identity is inconsistent: {node_path}")
        return asset, preview_instances

    def _prepare_catalog_revision_asset(self, raw: dict, index: int) -> dict:
        asset = resolve_godot_asset(
            self._root, str(raw.get("asset_id") or ""), domain=str(raw.get("domain") or "")
        )
        raw_name = str(raw.get("name") or "").strip()
        operation = str(raw.get("operation") or "")
        name = raw_name or (_default_name(asset.id, index) if operation == "instantiate" else "")
        if name and any(char in name for char in ".:@/\""):
            raise ValueError(f"operation {index} has an invalid node name: {name}")
        result = {
            "catalog_identity": f"{asset.domain}:{asset.id}",
            "resource_path": asset.resource_path,
            "allowed_rotations_deg": list(asset.allowed_rotations_deg),
        }
        if name:
            result["name"] = name
        if operation == "instantiate":
            result.update(
                {
                    "position": _vector(raw.get("position", [0, 0, 0]), f"operation {index} position"),
                    "rotation_degrees_y": _number(
                        raw.get("rotation_degrees_y", 0), f"operation {index} rotation"
                    ),
                    "scale": _vector(raw.get("scale", [1, 1, 1]), f"operation {index} scale"),
                }
            )
            _validate_transform_bounds(result, index)
            _validate_allowed_rotation(result["rotation_degrees_y"], asset.allowed_rotations_deg, asset.id)
        elif "rotation_degrees_y" in raw:
            _validate_allowed_rotation(
                _number(raw["rotation_degrees_y"], f"operation {index} rotation"),
                asset.allowed_rotations_deg,
                asset.id,
            )
        return result


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


def _bounded_int(value, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer between {minimum} and {maximum}")
    if value < minimum or value > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


def _default_name(asset_id: str, index: int) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", asset_id).strip("_") or "Asset"
    return f"{stem}_{index + 1:02d}"


def _direct_preview_path(value, index: int) -> str:
    path = str(value or "").strip()
    if not path.startswith("AuraPreview/") or path.count("/") != 1:
        raise ValueError(f"operation {index} must target one direct AuraPreview child")
    name = path.split("/", 1)[1]
    if not name or any(char in name for char in ".:@/\""):
        raise ValueError(f"operation {index} has an invalid preview path")
    return path


def _optional_transform(raw: dict, index: int) -> dict:
    result: dict = {}
    if "position" in raw:
        result["position"] = _vector(raw["position"], f"operation {index} position")
    if "rotation_degrees_y" in raw:
        result["rotation_degrees_y"] = _number(
            raw["rotation_degrees_y"], f"operation {index} rotation"
        )
    if "scale" in raw:
        result["scale"] = _vector(raw["scale"], f"operation {index} scale")
    _validate_transform_bounds(result, index)
    return result


def _validate_transform_bounds(transform: dict, index: int) -> None:
    if "position" in transform and any(abs(value) > 10000 for value in transform["position"]):
        raise ValueError(f"operation {index} exceeds the 10 km preview bound")
    if "scale" in transform and any(value < 0.01 or value > 100 for value in transform["scale"]):
        raise ValueError(f"operation {index} scale must be between 0.01 and 100")


def _validate_allowed_rotation(rotation: float, allowed: tuple[float, ...], asset_id: str) -> None:
    if allowed and not any(
        math.isclose(rotation % 360, candidate % 360, abs_tol=1e-4) for candidate in allowed
    ):
        raise ValueError(f"rotation is not allowed for catalog asset {asset_id}")


def _catalog_socket(asset: GodotAsset, socket_id: str, label: str) -> GodotAssetSocket:
    wanted = socket_id.strip()
    if not wanted:
        raise ValueError(f"{label} requires a socket ID")
    matches: list[GodotAssetSocket] = []
    for socket in asset.sockets:
        if not isinstance(socket, GodotAssetSocket) or not isinstance(socket.id, str):
            raise ValueError(f"{label} contains a malformed socket")
        if socket.id == wanted:
            matches.append(socket)
    if not matches:
        raise ValueError(f"{label} has no socket named {wanted}")
    if len(matches) != 1:
        raise ValueError(f"{label} has duplicated socket ID {wanted}")
    socket = matches[0]
    if (
        not isinstance(socket.position, (list, tuple))
        or not isinstance(socket.facing, (list, tuple))
        or len(socket.position) != 3
        or len(socket.facing) != 3
    ):
        raise ValueError(f"{label} socket {wanted} is malformed")
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in (*socket.position, *socket.facing)
    ):
        raise ValueError(f"{label} socket {wanted} contains a malformed vector")
    if not all(math.isfinite(value) for value in (*socket.position, *socket.facing)):
        raise ValueError(f"{label} socket {wanted} contains a non-finite vector")
    return socket


def _validate_horizontal_socket(facing, asset_id: str, socket_id: str) -> None:
    if math.hypot(facing[0], facing[2]) < 1e-6:
        raise ValueError(
            f"catalog asset {asset_id} socket {socket_id} has no usable horizontal facing "
            "for yaw-only attachment"
        )


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
