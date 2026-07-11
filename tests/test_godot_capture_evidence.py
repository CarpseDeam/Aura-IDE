from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.godot_assets.capture_evidence import validate_capture_set


def _capture(tmp_path: Path, *, width: int = 96, height: int = 64) -> tuple[dict, Path]:
    target = tmp_path / ".aura/tmp/godot_previews/pass-01/overview.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), (50, 80, 120)).save(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return (
        {
            "capture_set_id": "pass-01",
            "scene_path": "res://scenes/preview.tscn",
            "scene_fingerprint": 1234,
            "captures": [
                {
                    "view": "overview",
                    "path": "res://.aura/tmp/godot_previews/pass-01/overview.png",
                    "width": width,
                    "height": height,
                    "sha256": digest,
                }
            ],
        },
        target,
    )


def test_capture_evidence_validates_png_and_returns_only_relative_metadata(tmp_path: Path) -> None:
    capture, _target = _capture(tmp_path)
    observed: list[str] = []
    preview = {
        "preview_exists": True,
        "instances": [{"path": "AuraPreview/Wall", "position": [0, 0, 0]}],
        "structural_validation": "catalog_rules_v1",
        "diagnostic_count": 1,
        "diagnostics": [{"code": "footprint_overlap"}],
    }

    result = validate_capture_set(tmp_path, capture, preview, lambda value: observed.append(value) or "AST")

    assert result["capture_set_id"] == "pass-01"
    assert result["scene_path"] == "scenes/preview.tscn"
    assert len(result["scene_fingerprint"]) == 64
    assert result["preview"]["diagnostics"] == [{"code": "footprint_overlap"}]
    assert result["captures"][0]["path"] == ".aura/tmp/godot_previews/pass-01/overview.png"
    assert result["captures"][0]["visual_structure"] == "AST"
    assert observed and "base64" not in str(result)


def test_capture_evidence_requires_hash_and_matching_decoded_dimensions(tmp_path: Path) -> None:
    capture, _target = _capture(tmp_path)
    capture["captures"][0]["sha256"] = ""
    with pytest.raises(ValueError, match="valid SHA-256"):
        validate_capture_set(tmp_path, capture, {}, lambda _value: "")

    capture, _target = _capture(tmp_path)
    capture["captures"][0]["width"] = 100
    with pytest.raises(ValueError, match="dimensions do not match"):
        validate_capture_set(tmp_path, capture, {}, lambda _value: "")


def test_capture_evidence_rejects_non_res_and_symlink_escape(tmp_path: Path) -> None:
    capture, target = _capture(tmp_path)
    capture["captures"][0]["path"] = str(target)
    with pytest.raises(ValueError, match="must be a res:// PNG"):
        validate_capture_set(tmp_path, capture, {}, lambda _value: "")

    outside = tmp_path / "outside.png"
    Image.new("RGB", (96, 64)).save(outside)
    link = target.parent / "linked.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this Windows configuration")
    capture["captures"][0]["path"] = "res://.aura/tmp/godot_previews/pass-01/linked.png"
    capture["captures"][0]["sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="escapes"):
        validate_capture_set(tmp_path, capture, {}, lambda _value: "")


def test_capture_evidence_bounds_decompiler_output(tmp_path: Path) -> None:
    capture, _target = _capture(tmp_path)
    result = validate_capture_set(tmp_path, capture, {}, lambda _value: "x" * 20_000)

    evidence = result["captures"][0]
    assert evidence["visual_structure_truncated"] is True
    assert len(evidence["visual_structure"]) < 13_000


def test_capture_tool_accepts_unwrapped_successful_bridge_result(tmp_path: Path) -> None:
    (tmp_path / "project.godot").write_text('[application]\nconfig/name="Capture"\n', encoding="utf-8")
    capture, _target = _capture(tmp_path)
    snapshot = {"scene_open": True, "preview_exists": True, "instances": [], "diagnostics": []}
    registry = ToolRegistry(tmp_path, mode="worker")

    with (
        patch(
            "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
        ) as client_type,
        patch(
            "aura.conversation.tools._godot_asset_preview_mixin._decompile_image",
            return_value="AST",
        ),
    ):
        client_type.return_value.request.side_effect = [capture, snapshot]
        result = registry.execute(
            "capture_godot_asset_preview",
            {"capture_set_id": "pass-01", "modes": ["overview"], "width": 96, "height": 64},
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is True
    assert result.payload["capture_set_id"] == "pass-01"
    assert result.payload["captures"][0]["visual_structure"] == "AST"
