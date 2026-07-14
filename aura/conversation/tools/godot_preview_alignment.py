"""Prepare ordered catalog-backed direct preview revisions and alignment payloads."""

from __future__ import annotations

import math
import re
from typing import Any

from aura.godot_assets import resolve_godot_asset
from aura.godot_assets.alignment import CalibratedGeometry, validate_anchor
from aura.godot_assets.models import GodotAsset, GodotAssetSocket
from aura.godot_assets.preview import analyze_preview_snapshot
from aura.godot_editor.limits import MAX_DUPLICATE_COUNT, MAX_REVISION_OPERATIONS

_OPERATIONS = {"set_transform", "instantiate", "remove", "replace", "duplicate", "attach"}


class PreviewRevisionPreparer:
    """Own public validation and one ordered factual plan for a revision."""

    def __init__(self, project_root, bridge_client_type) -> None:
        self.root = project_root
        self.bridge_client_type = bridge_client_type
        self.operations: list[dict] = []
        self.targeted: set[str] = set()
        self.discarded: set[str] = set()
        self.read: set[str] = set()
        self.added_names: set[str] = set()
        self.planned: dict[str, GodotAsset] = {}
        self._instances: list[dict] | None = None

    def prepare(self, args: dict) -> list[dict]:
        raw_operations = args.get("operations")
        if not isinstance(raw_operations, list) or not 1 <= len(raw_operations) <= MAX_REVISION_OPERATIONS:
            raise ValueError(f"operations must contain between 1 and {MAX_REVISION_OPERATIONS} items")
        for index, raw in enumerate(raw_operations):
            self._prepare_one(raw, index)
        return self.operations

    def _prepare_one(self, raw: Any, index: int) -> None:
        if not isinstance(raw, dict):
            raise ValueError(f"operation {index} must be an object")
        action = str(raw.get("operation") or "")
        if action not in _OPERATIONS:
            raise ValueError(f"operation {index} has an unsupported operation")
        if action == "instantiate":
            self._instantiate(raw, index)
            return
        path = _direct_preview_path(raw.get("node_path"), index)
        if path in self.planned and action not in {"duplicate", "attach"}:
            raise ValueError(f"operation {index} cannot {action} an unattached planned node: {path}")
        self._track_access(path, action, index)
        if action == "duplicate":
            self._duplicate(raw, index, path)
        elif action == "attach":
            self._attach(raw, index, path)
        elif action == "remove":
            self.discarded.add(path)
            self.operations.append({"operation": action, "node_path": path})
        elif action == "set_transform":
            self._set_transform(raw, index, path)
        else:
            self._replace(raw, index, path)

    def _instantiate(self, raw: dict, index: int) -> None:
        asset = self._resolve_requested_asset(raw)
        prepared = self._catalog_asset_fields(raw, index, asset, instantiate=True)
        self._unique_output(prepared["name"], index)
        relative = self._relative_payload(raw, index, asset)
        if relative:
            prepared.update(relative)
        self.operations.append({"operation": "instantiate", **prepared})
        self.planned[f"AuraPreview/{prepared['name']}"] = asset

    def _duplicate(self, raw: dict, index: int, path: str) -> None:
        count = _bounded_int(raw.get("count"), f"duplicate operation {index} count", 1, MAX_DUPLICATE_COUNT)
        has_numeric = "offset" in raw
        has_alignment = "alignment_step" in raw
        if has_numeric == has_alignment:
            raise ValueError(f"duplicate operation {index} requires exactly one of offset or alignment_step")
        asset = self._source_asset(path, "duplicate")
        prepared = {
            "operation": "duplicate", "node_path": path, "count": count,
            "catalog_identity": _identity(asset), "resource_path": asset.resource_path,
        }
        requested_name = _node_name(raw.get("name"), index, optional=True)
        if requested_name and count != 1:
            raise ValueError(f"duplicate operation {index} can use name only when count is 1")
        if requested_name:
            self._unique_output(requested_name, index)
            prepared["name"] = requested_name
            self.planned[f"AuraPreview/{requested_name}"] = asset
        if has_numeric:
            prepared["offset"] = _vector(raw["offset"], f"duplicate operation {index} offset")
            space = str(raw.get("offset_space") or "local")
            if space not in {"local", "world"}:
                raise ValueError(f"duplicate operation {index} offset_space must be local or world")
            prepared["offset_space"] = space
        else:
            step = _alignment_spec(raw["alignment_step"], f"duplicate operation {index} alignment_step")
            geometry = CalibratedGeometry.from_asset(asset, role="duplicate")
            prepared["alignment_step"] = step
            prepared["_alignment_geometry"] = {
                "reference": geometry.to_bridge_dict(), "piece": geometry.to_bridge_dict()
            }
            if count > 1:
                output_names = self._reserve_duplicate_names(path.split("/", 1)[1], count)
                prepared["_output_names"] = output_names
                for name in output_names:
                    self.planned[f"AuraPreview/{name}"] = asset
        self.operations.append(prepared)

    def _attach(self, raw: dict, index: int, path: str) -> None:
        if "position" in raw or "rotation_degrees_y" in raw:
            raise ValueError(f"attach operation {index} derives position and rotation from sockets")
        source_asset = self._source_asset(path, "attach")
        target_asset = self._resolve_requested_asset(raw)
        source_socket = _catalog_socket(source_asset, str(raw.get("source_socket") or ""), f"attach source asset {source_asset.id}")
        target_socket = _catalog_socket(target_asset, str(raw.get("target_socket") or ""), f"attach target asset {target_asset.id}")
        _validate_horizontal_socket(source_socket.facing, source_asset.id, source_socket.id)
        _validate_horizontal_socket(target_socket.facing, target_asset.id, target_socket.id)
        scale = _vector(raw.get("scale", [1, 1, 1]), f"operation {index} scale")
        _validate_transform_bounds({"scale": scale}, index)
        name = _node_name(raw.get("name"), index, optional=True)
        prepared = {
            "operation": "attach", "node_path": path,
            "source_catalog_identity": _identity(source_asset), "source_resource_path": source_asset.resource_path,
            "source_socket_position": list(source_socket.position), "source_socket_facing": list(source_socket.facing),
            "catalog_identity": _identity(target_asset), "asset_id": target_asset.id, "resource_path": target_asset.resource_path,
            "target_socket_position": list(target_socket.position), "target_socket_facing": list(target_socket.facing),
            "allowed_rotations_deg": list(target_asset.allowed_rotations_deg), "scale": scale,
        }
        if name:
            self._unique_output(name, index)
            prepared["name"] = name
            self.planned[f"AuraPreview/{name}"] = target_asset
        self.operations.append(prepared)

    def _set_transform(self, raw: dict, index: int, path: str) -> None:
        transform = _optional_transform(raw, index, allow_position="relative_to" not in raw)
        asset = None
        if "relative_to" in raw or "rotation_degrees_y" in transform:
            asset = self._source_asset(path, "set_transform")
        relative = self._relative_payload(raw, index, asset) if asset is not None else {}
        if not transform and not relative:
            raise ValueError(f"set_transform operation {index} must change a transform value")
        if "rotation_degrees_y" in transform and asset is not None:
            _validate_allowed_rotation(transform["rotation_degrees_y"], asset.allowed_rotations_deg, asset.id)
        prepared = {"operation": "set_transform", "node_path": path, **transform, **relative}
        if asset is not None:
            prepared["allowed_rotations_deg"] = list(asset.allowed_rotations_deg)
        self.operations.append(prepared)

    def _replace(self, raw: dict, index: int, path: str) -> None:
        asset = self._resolve_requested_asset(raw)
        prepared = self._catalog_asset_fields(raw, index, asset, instantiate=False)
        prepared.update(_optional_transform(raw, index, allow_position="relative_to" not in raw))
        prepared.update(self._relative_payload(raw, index, asset))
        self.operations.append({"operation": "replace", "node_path": path, **prepared})
        self.discarded.add(path)
        name = str(prepared.get("name") or path.split("/", 1)[1])
        self._unique_output(name, index)
        self.planned[f"AuraPreview/{name}"] = asset

    def _relative_payload(self, raw: dict, index: int, piece_asset: GodotAsset) -> dict:
        if "relative_to" not in raw:
            return {}
        if "position" in raw:
            raise ValueError(f"operation {index} cannot provide both relative_to and position")
        spec = _relative_spec(raw["relative_to"], f"operation {index} relative_to")
        reference_path = spec["node_path"]
        if reference_path in self.targeted and reference_path not in self.planned:
            raise ValueError(
                f"operation {index} cannot align from a source already targeted for mutation: {reference_path}"
            )
        reference_asset = self._source_asset(spec["node_path"], "relative alignment reference")
        if reference_path not in self.planned:
            self.read.add(reference_path)
        return {
            "relative_to": spec,
            "_alignment_geometry": {
                "reference": CalibratedGeometry.from_asset(reference_asset, role="reference").to_bridge_dict(),
                "piece": CalibratedGeometry.from_asset(piece_asset, role="target").to_bridge_dict(),
            },
        }

    def _catalog_asset_fields(self, raw: dict, index: int, asset: GodotAsset, *, instantiate: bool) -> dict:
        name = _node_name(raw.get("name"), index, optional=True)
        if instantiate and not name:
            name = _default_name(asset.id, index)
        result = {"catalog_identity": _identity(asset), "resource_path": asset.resource_path, "allowed_rotations_deg": list(asset.allowed_rotations_deg)}
        if name:
            result["name"] = name
        if instantiate:
            result["rotation_degrees_y"] = _number(raw.get("rotation_degrees_y", 0), f"operation {index} rotation")
            result["scale"] = _vector(raw.get("scale", [1, 1, 1]), f"operation {index} scale")
            if "relative_to" not in raw:
                result["position"] = _vector(raw.get("position", [0, 0, 0]), f"operation {index} position")
            _validate_transform_bounds(result, index)
            _validate_allowed_rotation(result["rotation_degrees_y"], asset.allowed_rotations_deg, asset.id)
        elif "rotation_degrees_y" in raw:
            _validate_allowed_rotation(_number(raw["rotation_degrees_y"], f"operation {index} rotation"), asset.allowed_rotations_deg, asset.id)
        return result

    def _resolve_requested_asset(self, raw: dict) -> GodotAsset:
        return resolve_godot_asset(self.root, str(raw.get("asset_id") or ""), domain=str(raw.get("domain") or ""))

    def _source_asset(self, path: str, operation: str) -> GodotAsset:
        if path in self.planned:
            return self.planned[path]
        if path in self.discarded:
            raise ValueError(f"{operation} source was removed or replaced: {path}")
        source = next((item for item in self._snapshot_instances() if str(item.get("path") or "") == path), None)
        if source is None:
            raise ValueError(f"{operation} source does not exist (forward references are not allowed): {path}")
        asset_id, domain = str(source.get("asset_id") or ""), str(source.get("domain") or "")
        if not asset_id or not domain:
            raise ValueError(f"{operation} source is not a recognized catalog asset: {path}")
        asset = resolve_godot_asset(self.root, asset_id, domain=domain)
        if str(source.get("resource_path") or "").casefold() != asset.resource_path.casefold():
            raise ValueError(f"{operation} source catalog identity is inconsistent: {path}")
        return asset

    def _snapshot_instances(self) -> list[dict]:
        if self._instances is None:
            snapshot = self.bridge_client_type(self.root).request("preview.snapshot", {})
            self._instances = list(analyze_preview_snapshot(self.root, snapshot).get("instances") or [])
        return self._instances

    def _track_access(self, path: str, action: str, index: int) -> None:
        if action in {"duplicate", "attach"}:
            if path in self.discarded:
                raise ValueError(f"operation {index} cannot {action} from a removed or replaced source: {path}")
            if path in self.targeted:
                raise ValueError(f"operation {index} cannot {action} from a source already targeted for mutation: {path}")
            self.read.add(path)
        else:
            if path in self.targeted:
                raise ValueError(f"preview target appears in more than one operation: {path}")
            if path in self.read:
                raise ValueError(f"operation {index} cannot mutate a path already read earlier in the batch: {path}")
            self.targeted.add(path)

    def _unique_output(self, name: str, index: int) -> None:
        if name in self.added_names:
            raise ValueError(f"duplicate new preview name: {name}")
        self.added_names.add(name)

    def _reserve_duplicate_names(self, source_name: str, count: int) -> list[str]:
        names = set(self.added_names)
        names.update(str(item.get("path") or "").split("/", 1)[-1] for item in self._snapshot_instances())
        result = []
        suffix = 1
        for _ in range(count):
            candidate = f"{source_name}_copy_{suffix:02d}"
            while candidate in names:
                suffix += 1
                candidate = f"{source_name}_copy_{suffix:02d}"
            names.add(candidate)
            self.added_names.add(candidate)
            result.append(candidate)
            suffix += 1
        return result


def _alignment_spec(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    result = {
        "reference_anchor": list(validate_anchor(value.get("reference_anchor"), f"{label} reference_anchor")),
        "piece_anchor": list(validate_anchor(value.get("piece_anchor"), f"{label} piece_anchor")),
        "offset": _vector(value.get("offset", [0, 0, 0]), f"{label} offset"),
        "offset_space": str(value.get("offset_space") or "reference_local"),
    }
    if result["offset_space"] not in {"reference_local", "world"}:
        raise ValueError(f"{label} offset_space must be reference_local or world")
    if any(abs(component) > 10000 for component in result["offset"]):
        raise ValueError(f"{label} offset exceeds the 10 km preview bound")
    return result


def _relative_spec(value: Any, label: str) -> dict:
    result = _alignment_spec(value, label)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    result["node_path"] = _direct_preview_path(value.get("node_path"), 0, label=label)
    return result


def _optional_transform(raw: dict, index: int, *, allow_position: bool) -> dict:
    result = {}
    if "position" in raw:
        if not allow_position:
            raise ValueError(f"operation {index} cannot provide both relative_to and position")
        result["position"] = _vector(raw["position"], f"operation {index} position")
    if "rotation_degrees_y" in raw:
        result["rotation_degrees_y"] = _number(raw["rotation_degrees_y"], f"operation {index} rotation")
    if "scale" in raw:
        result["scale"] = _vector(raw["scale"], f"operation {index} scale")
    _validate_transform_bounds(result, index)
    return result


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _vector(value: Any, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{label} must contain three numbers")
    return [_number(component, label) for component in value]


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer between {minimum} and {maximum}")
    return value


def _node_name(value: Any, index: int, *, optional: bool) -> str:
    name = str(value or "").strip()
    if not name and optional:
        return ""
    if not name or any(char in name for char in '.:@/"'):
        raise ValueError(f"operation {index} has an invalid node name: {name}")
    return name


def _default_name(asset_id: str, index: int) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", asset_id).strip("_") or "Asset"
    return f"{stem}_{index + 1:02d}"


def _direct_preview_path(value: Any, index: int, *, label: str = "operation") -> str:
    path = str(value or "").strip()
    if not path.startswith("AuraPreview/") or path.count("/") != 1:
        raise ValueError(f"{label} {index if label == 'operation' else ''} must target one direct AuraPreview child".replace("  ", " "))
    name = path.split("/", 1)[1]
    if not name or any(char in name for char in '.:@/"'):
        raise ValueError(f"{label} has an invalid preview path")
    return path


def _validate_transform_bounds(transform: dict, index: int) -> None:
    if "position" in transform and any(abs(value) > 10000 for value in transform["position"]):
        raise ValueError(f"operation {index} exceeds the 10 km preview bound")
    if "scale" in transform and any(value < 0.01 or value > 100 for value in transform["scale"]):
        raise ValueError(f"operation {index} scale must be between 0.01 and 100")


def _validate_allowed_rotation(rotation: float, allowed: tuple[float, ...], asset_id: str) -> None:
    if allowed and not any(math.isclose(rotation % 360, candidate % 360, abs_tol=1e-4) for candidate in allowed):
        raise ValueError(f"rotation is not allowed for catalog asset {asset_id}")


def _catalog_socket(asset: GodotAsset, socket_id: str, label: str) -> GodotAssetSocket:
    wanted = socket_id.strip()
    matches = [socket for socket in asset.sockets if isinstance(socket, GodotAssetSocket) and socket.id == wanted]
    if not wanted:
        raise ValueError(f"{label} requires a socket ID")
    if not matches:
        raise ValueError(f"{label} has no socket named {wanted}")
    if len(matches) != 1:
        raise ValueError(f"{label} has duplicated socket ID {wanted}")
    socket = matches[0]
    if len(socket.position) != 3 or len(socket.facing) != 3 or not all(math.isfinite(float(v)) for v in (*socket.position, *socket.facing)):
        raise ValueError(f"{label} socket {wanted} is malformed")
    return socket


def _validate_horizontal_socket(facing, asset_id: str, socket_id: str) -> None:
    if math.hypot(facing[0], facing[2]) < 1e-6:
        raise ValueError(f"catalog asset {asset_id} socket {socket_id} has no usable horizontal facing for yaw-only attachment")


def _identity(asset: GodotAsset) -> str:
    return f"{asset.domain}:{asset.id}"


__all__ = ["PreviewRevisionPreparer"]
