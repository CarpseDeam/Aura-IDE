"""Validate Godot preview captures and produce bounded local structural evidence."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any, Callable

from PIL import Image, UnidentifiedImageError

from aura.paths import safe_is_relative_to

MAX_CAPTURE_COUNT = 4
MAX_PNG_BYTES = 20 * 1024 * 1024
MAX_WIDTH = 1920
MAX_HEIGHT = 1080
MAX_VISUAL_STRUCTURE_CHARS = 12_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_capture_set(
    workspace_root: Path,
    capture_result: dict[str, Any],
    preview_facts: dict[str, Any],
    decompile: Callable[[str], str],
) -> dict[str, Any]:
    """Validate bridge metadata/files before invoking the local image decompiler."""
    root = workspace_root.resolve()
    capture_set_id = _safe_capture_id(capture_result.get("capture_set_id"))
    scene_path = _scene_path(capture_result.get("scene_path"))
    raw_captures = capture_result.get("captures")
    if not isinstance(raw_captures, list) or not 1 <= len(raw_captures) <= MAX_CAPTURE_COUNT:
        raise ValueError("bridge captures must contain between 1 and 4 entries")

    allowed_root = (root / ".aura" / "tmp" / "godot_previews").resolve()
    captures: list[dict[str, Any]] = []
    seen_views: set[str] = set()
    for index, raw in enumerate(raw_captures):
        if not isinstance(raw, dict):
            raise ValueError(f"capture {index} must be an object")
        view = str(raw.get("view") or "")
        if view not in {"current_editor", "overview", "top_down"} or view in seen_views:
            raise ValueError(f"capture {index} has an invalid or duplicate view")
        seen_views.add(view)
        local_path, relative_path = _capture_path(root, allowed_root, raw.get("path"))
        expected_width = _bounded_int(raw.get("width"), "capture width", 64, MAX_WIDTH)
        expected_height = _bounded_int(raw.get("height"), "capture height", 64, MAX_HEIGHT)
        expected_sha256 = str(raw.get("sha256") or "").lower()
        if not _SHA256_RE.fullmatch(expected_sha256):
            raise ValueError(f"capture {index} is missing a valid SHA-256 digest")
        try:
            file_size = local_path.stat().st_size
        except OSError as exc:
            raise ValueError(f"capture file is unavailable: {relative_path}") from exc
        if file_size <= 0 or file_size > MAX_PNG_BYTES:
            raise ValueError(f"capture file size is outside the allowed range: {relative_path}")
        try:
            png_bytes = local_path.read_bytes()
        except OSError as exc:
            raise ValueError(f"capture file could not be read: {relative_path}") from exc
        actual_sha256 = hashlib.sha256(png_bytes).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError(f"SHA-256 mismatch for {relative_path}")
        width, height = _decoded_png_dimensions(png_bytes, relative_path)
        if (width, height) != (expected_width, expected_height):
            raise ValueError(
                f"decoded PNG dimensions do not match bridge metadata for {relative_path}"
            )

        image_b64 = base64.b64encode(png_bytes).decode("ascii")
        try:
            structure = str(decompile(image_b64))
        finally:
            del image_b64
            del png_bytes
        truncated = len(structure) > MAX_VISUAL_STRUCTURE_CHARS
        if truncated:
            structure = structure[:MAX_VISUAL_STRUCTURE_CHARS] + "\n…[visual structure truncated]"
        captures.append(
            {
                "view": view,
                "path": relative_path,
                "width": width,
                "height": height,
                "sha256": actual_sha256,
                "evidence_kind": "local_structural_decompile",
                "visual_structure": structure,
                "visual_structure_truncated": truncated,
            }
        )

    return {
        "capture_set_id": capture_set_id,
        "scene_path": scene_path,
        "scene_fingerprint": semantic_scene_fingerprint(scene_path, preview_facts),
        "bridge_scene_fingerprint": capture_result.get("scene_fingerprint"),
        "preview": {
            "instance_count": len(preview_facts.get("instances") or []),
            "instances": preview_facts.get("instances") or [],
            "structural_validation": preview_facts.get("structural_validation"),
            "diagnostic_count": int(preview_facts.get("diagnostic_count") or 0),
            "diagnostics": preview_facts.get("diagnostics") or [],
        },
        "captures": captures,
    }


def semantic_scene_fingerprint(scene_path: str, preview_facts: dict[str, Any]) -> str:
    """Hash canonical exact preview facts rather than Godot's process-local generic hash."""
    evidence = {
        "scene_path": scene_path,
        "preview_exists": bool(preview_facts.get("preview_exists")),
        "instances": preview_facts.get("instances") or [],
        "diagnostics": preview_facts.get("diagnostics") or [],
    }
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_capture_id(value: Any) -> str:
    capture_id = str(value or "").strip()
    if not capture_id or len(capture_id) > 100 or any(token in capture_id for token in ("..", "/", "\\")):
        raise ValueError("bridge returned an invalid capture_set_id")
    return capture_id


def _scene_path(value: Any) -> str:
    raw = str(value or "")
    if not raw.startswith("res://") or ".." in Path(raw[6:]).parts:
        raise ValueError("bridge returned an invalid scene_path")
    return raw[6:].lstrip("/")


def _capture_path(root: Path, allowed_root: Path, value: Any) -> tuple[Path, str]:
    resource_path = str(value or "")
    prefix = "res://.aura/tmp/godot_previews/"
    if not resource_path.startswith(prefix) or not resource_path.lower().endswith(".png"):
        raise ValueError("capture path must be a res:// PNG beneath .aura/tmp/godot_previews")
    relative = resource_path[6:].lstrip("/")
    local_path = (root / Path(relative)).resolve()
    if not safe_is_relative_to(local_path, allowed_root):
        raise ValueError(f"capture path escapes the allowed preview directory: {resource_path}")
    if not local_path.is_file():
        raise ValueError(f"capture file not found: {relative}")
    return local_path, Path(relative).as_posix()


def _decoded_png_dimensions(png_bytes: bytes, relative_path: str) -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            if image.format != "PNG":
                raise ValueError(f"capture is not a decoded PNG: {relative_path}")
            width, height = image.size
            image.verify()
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError(f"capture is not a valid PNG: {relative_path}") from exc
    if not 64 <= width <= MAX_WIDTH or not 64 <= height <= MAX_HEIGHT:
        raise ValueError(f"decoded PNG dimensions are outside allowed bounds: {relative_path}")
    return width, height


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer between {minimum} and {maximum}")
    return value


__all__ = ["semantic_scene_fingerprint", "validate_capture_set"]
