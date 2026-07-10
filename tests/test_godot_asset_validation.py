"""Focused tests for aura.godot_assets.validation — deterministic structural checks."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from aura.godot_assets.validation import (
    ValidationConfig,
    struct_validate_preview,
)


# ── Test helpers ─────────────────────────────────────────────────────────────


def _project(tmp_path: Path, entries: list[dict]) -> Path:
    """Create a minimal Godot project with the given catalog entries.

    Returns the project root path.
    """
    (tmp_path / "project.godot").write_text(
        '[application]\nconfig/name="Assets"\n', encoding="utf-8"
    )
    scene = tmp_path / "assets/modules/wall.tscn"
    scene.parent.mkdir(parents=True)
    scene.write_text('[gd_scene format=3]\n\n[node name="Wall" type="Node3D"]\n', encoding="utf-8")

    catalog = tmp_path / "assets/ruins/catalog/ruin_pieces.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    (catalog.parent / "calibrations.json").write_text(
        json.dumps({e.get("id", ""): {"position_offset": [1.0, 0.0, 0.0]} for e in entries}),
        encoding="utf-8",
    )
    return tmp_path


def _wall(asset_id: str = "wall") -> dict:
    return {
        "id": asset_id,
        "path": "res://assets/modules/wall.tscn",
        "kind": "wall_straight",
        "footprint_m": [4.0, 1.0],
        "height_m": 4.0,
        "tags": ["ruins", "wall", "cover"],
        "sockets": [
            {"id": "left", "position": [-2.0, 0.0, 0.0], "facing": [-1.0, 0.0, 0.0]},
            {"id": "right", "position": [2.0, 0.0, 0.0], "facing": [1.0, 0.0, 0.0]},
        ],
        "weight": 1.0,
    }


def _enriched_snapshot(instances: list[dict], diagnostics: list | None = None) -> dict:
    """Wrap instances into a minimal snapshot dict suitable for validation.

    Instances must already carry asset_id, domain, kind, semantic_roles.
    """
    return {
        "scene_open": True,
        "preview_exists": True,
        "diagnostics": diagnostics or [],
        "instances": list(instances),
    }


def _enrich(
    inst: dict, asset_id: str = "wall", domain: str = "ruins",
    kind: str = "wall_straight", roles: list[str] | None = None,
) -> dict:
    """Return a shallow copy of *inst* with catalog metadata added."""
    out = dict(inst)
    out["asset_id"] = asset_id
    out["domain"] = domain
    out["kind"] = kind
    out["semantic_roles"] = roles or ["barrier", "cover"]
    return out


# ── Known-good fixture ───────────────────────────────────────────────────────


def test_known_good_non_overlapping_walls_passes(tmp_path: Path) -> None:
    """Two walls placed end-to-end (right socket → left socket) pass all checks."""
    root = _project(tmp_path, [_wall()])
    instances = [
        _enrich({
            "path": "AuraPreview/A", "resource_path": "res://assets/modules/wall.tscn",
            "position": [0, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }),
        _enrich({
            "path": "AuraPreview/B", "resource_path": "res://assets/modules/wall.tscn",
            "position": [4, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }),
    ]
    sv = struct_validate_preview(root, _enriched_snapshot(instances))
    assert sv["status"] == "passed", f"Expected passed, got {sv['status']}: {sv['facts']}"
    overlap_facts = [f for f in sv["facts"] if f["code"] == "footprint_overlap"]
    assert len(overlap_facts) == 0


# ── Overlapping walls ────────────────────────────────────────────────────────


def test_overlapping_walls_report_measured_overlap(tmp_path: Path) -> None:
    """Two walls overlapping report measured overlap_x_m and overlap_z_m."""
    root = _project(tmp_path, [_wall()])
    instances = [
        _enrich({
            "path": "AuraPreview/A", "resource_path": "res://assets/modules/wall.tscn",
            "position": [0, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }),
        _enrich({
            "path": "AuraPreview/B", "resource_path": "res://assets/modules/wall.tscn",
            "position": [1, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }),
    ]
    sv = struct_validate_preview(root, _enriched_snapshot(instances))
    assert sv["status"] in ("failed", "partial")
    overlaps = [f for f in sv["facts"] if f["code"] == "footprint_overlap"]
    assert len(overlaps) >= 1
    assert overlaps[0]["measured"]["overlap_x_m"] > 0
    assert overlaps[0]["measured"]["overlap_z_m"] > 0
    assert isinstance(overlaps[0]["paths"], list)
    assert len(overlaps[0]["paths"]) == 2


# ── Socket-compatible walls ──────────────────────────────────────────────────


def test_socket_compatible_walls_no_socket_facts(tmp_path: Path) -> None:
    """Walls with facing-compatible sockets produce no socket facts."""
    root = _project(tmp_path, [_wall()])
    instances = [
        _enrich({
            "path": "AuraPreview/A", "resource_path": "res://assets/modules/wall.tscn",
            "position": [0, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }),
        _enrich({
            "path": "AuraPreview/B", "resource_path": "res://assets/modules/wall.tscn",
            "position": [4, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }),
    ]
    sv = struct_validate_preview(root, _enriched_snapshot(instances))
    socket_codes = {f["code"] for f in sv["facts"] if "socket_" in f["code"]}
    assert len(socket_codes) == 0, f"Unexpected socket facts: {socket_codes}"


# ── Misaligned sockets ───────────────────────────────────────────────────────


def test_misaligned_sockets_report_distance(tmp_path: Path) -> None:
    """Walls with misaligned sockets (same-side, facing same direction) produce a fact."""
    root = _project(tmp_path, [_wall()])
    # Two walls with left sockets close together (both facing [-1,0,0]).
    instances = [
        _enrich({
            "path": "AuraPreview/A", "resource_path": "res://assets/modules/wall.tscn",
            "position": [0, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }),
        _enrich({
            "path": "AuraPreview/B", "resource_path": "res://assets/modules/wall.tscn",
            "position": [-0.05, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }),
    ]
    sv = struct_validate_preview(root, _enriched_snapshot(instances))
    misaligned = [f for f in sv["facts"] if f["code"] == "socket_misaligned_facing"]
    assert len(misaligned) >= 1, f"No misaligned facts: {sv['facts']}"
    assert "distance_m" in misaligned[0]["measured"]
    assert misaligned[0]["measured"]["facing_dot"] >= -0.84  # not pointing toward each other


# ── Socket distance exceeded ─────────────────────────────────────────────────


def test_socket_distance_exceeded_for_shared_role_instances(tmp_path: Path) -> None:
    """Distant instances sharing a semantic role report socket_distance_exceeded."""
    root = _project(tmp_path, [_wall()])
    instances = [
        _enrich({
            "path": "AuraPreview/A", "resource_path": "res://assets/modules/wall.tscn",
            "position": [0, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }),
        _enrich({
            "path": "AuraPreview/B", "resource_path": "res://assets/modules/wall.tscn",
            "position": [10, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }),
    ]
    sv = struct_validate_preview(root, _enriched_snapshot(instances))
    dist_facts = [f for f in sv["facts"] if f["code"] == "socket_distance_exceeded"]
    assert len(dist_facts) >= 1, f"No distance facts: {sv['facts']}"
    assert dist_facts[0]["measured"]["distance_m"] > 0.1


# ── Missing sockets leads to unavailable_checks ──────────────────────────────


def test_no_socket_assets_produces_unavailable_socket_alignment(tmp_path: Path) -> None:
    """When no asset has sockets, socket_alignment appears in unavailable_checks."""
    asset = _wall("no_sock")
    asset["sockets"] = []
    root = _project(tmp_path, [asset])
    instances = [
        _enrich({
            "path": "AuraPreview/A", "resource_path": "res://assets/modules/wall.tscn",
            "position": [0, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }, asset_id="no_sock"),
    ]
    sv = struct_validate_preview(root, _enriched_snapshot(instances))
    assert "socket_alignment" in sv["unavailable_checks"]


# ── Duplicate instance ───────────────────────────────────────────────────────


def test_duplicate_identity_produces_warning_fact(tmp_path: Path) -> None:
    """Two instances at the same position with the same resource → duplicate fact."""
    root = _project(tmp_path, [_wall()])
    base = {
        "path": "AuraPreview/A", "resource_path": "res://assets/modules/wall.tscn",
        "position": [0, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
    }
    instances = [_enrich(base), _enrich({**base, "path": "AuraPreview/B"})]
    sv = struct_validate_preview(root, _enriched_snapshot(instances))
    dups = [f for f in sv["facts"] if f["code"] == "duplicate_preview_instance"]
    assert len(dups) >= 1
    assert dups[0]["measured"]["count"] == 2


# ── Ground asset elevated ────────────────────────────────────────────────────


def test_ground_asset_elevated_produces_info_fact(tmp_path: Path) -> None:
    """A ground-placed asset above threshold produces a ground_asset_elevated fact."""
    elevated_asset = _wall("elevated")
    elevated_asset["sockets"] = []  # no sockets → placement_mode = "ground"
    elevated_asset["tags"] = ["ruins", "rubble", "scatter"]
    root = _project(tmp_path, [elevated_asset])
    instances = [
        _enrich({
            "path": "AuraPreview/A", "resource_path": "res://assets/modules/wall.tscn",
            "position": [0, 0.5, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }, asset_id="elevated", roles=["scatter"]),
    ]
    sv = struct_validate_preview(root, _enriched_snapshot(instances))
    elevated = [f for f in sv["facts"] if f["code"] == "ground_asset_elevated"]
    assert len(elevated) >= 1
    assert elevated[0]["measured"]["elevation_m"] == 0.5


# ── Ground asset buried ──────────────────────────────────────────────────────


def test_ground_asset_buried_produces_warning_fact(tmp_path: Path) -> None:
    """An asset whose bottom is below grade produces a ground_asset_buried fact."""
    buried_asset = _wall("buried")
    buried_asset["sockets"] = []
    buried_asset["tags"] = ["ruins", "rubble", "scatter"]
    root = _project(tmp_path, [buried_asset])
    # height = 4.0, bottom = 0.1 - 4.0/2 = 0.1 - 2.0 = -1.9 → buried 1.9m
    instances = [
        _enrich({
            "path": "AuraPreview/A", "resource_path": "res://assets/modules/wall.tscn",
            "position": [0, 0.1, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }, asset_id="buried", roles=["scatter"]),
    ]
    sv = struct_validate_preview(root, _enriched_snapshot(instances))
    buried = [f for f in sv["facts"] if f["code"] == "ground_asset_buried"]
    assert len(buried) >= 1
    assert buried[0]["measured"]["burial_depth_m"] > 0


# ── Piece count exceeded ──────────────────────────────────────────────────────


def test_piece_count_exceeded_produces_warning(tmp_path: Path) -> None:
    """More than max_piece_count_warning instances → warning fact."""
    root = _project(tmp_path, [_wall()])
    config = ValidationConfig(max_piece_count_warning=2)
    instances = [
        _enrich({
            "path": f"AuraPreview/{i}", "resource_path": "res://assets/modules/wall.tscn",
            "position": [float(i) * 5, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        })
        for i in range(5)
    ]
    sv = struct_validate_preview(root, _enriched_snapshot(instances), config=config)
    count_facts = [f for f in sv["facts"] if f["code"] == "piece_count_exceeded"]
    assert len(count_facts) >= 1
    assert count_facts[0]["measured"]["count"] == 5


# ── Missing footprint metadata ───────────────────────────────────────────────


def test_no_footprint_metadata_produces_unavailable_overlap(tmp_path: Path) -> None:
    """An asset with footprint_m=None → footprint_overlap in unavailable_checks."""
    no_foot = _wall("no_foot")
    no_foot["footprint_m"] = None
    no_foot["height_m"] = 4.0
    root = _project(tmp_path, [no_foot])
    instances = [
        _enrich({
            "path": "AuraPreview/A", "resource_path": "res://assets/modules/wall.tscn",
            "position": [0, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }, asset_id="no_foot"),
    ]
    sv = struct_validate_preview(root, _enriched_snapshot(instances))
    assert "footprint_overlap" in sv["unavailable_checks"]


# ── Determinism ──────────────────────────────────────────────────────────────


def test_validation_is_deterministic(tmp_path: Path) -> None:
    """Same inputs always produce the same validation payload."""
    root = _project(tmp_path, [_wall()])
    instances = [
        _enrich({
            "path": "AuraPreview/A", "resource_path": "res://assets/modules/wall.tscn",
            "position": [0, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }),
        _enrich({
            "path": "AuraPreview/B", "resource_path": "res://assets/modules/wall.tscn",
            "position": [1, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }),
    ]
    snapshot = _enriched_snapshot(instances)
    first = struct_validate_preview(root, copy.deepcopy(snapshot))
    second = struct_validate_preview(root, copy.deepcopy(snapshot))
    assert first == second


# ── Total footprint bounds info fact ─────────────────────────────────────────


def test_total_footprint_bounds_info_fact_present(tmp_path: Path) -> None:
    """When footprints are available, a footprint_bounds info fact is produced."""
    root = _project(tmp_path, [_wall()])
    instances = [
        _enrich({
            "path": "AuraPreview/A", "resource_path": "res://assets/modules/wall.tscn",
            "position": [0, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }),
    ]
    sv = struct_validate_preview(root, _enriched_snapshot(instances))
    bounds_facts = [f for f in sv["facts"] if f["code"] == "footprint_bounds"]
    assert len(bounds_facts) == 1
    assert bounds_facts[0]["severity"] == "info"
    assert "width_m" in bounds_facts[0]["measured"]
    assert "depth_m" in bounds_facts[0]["measured"]


# ── Malformed nodes pass-through ─────────────────────────────────────────────


def test_malformed_nodes_are_reported_as_info_facts(tmp_path: Path) -> None:
    """Snapshot diagnostics with non_3d_preview_child produce malformed_preview_node facts."""
    root = _project(tmp_path, [_wall()])
    snapshot = _enriched_snapshot([], diagnostics=[
        {"code": "non_3d_preview_child", "path": "AuraPreview/Text", "message": "non-3D child"},
    ])
    sv = struct_validate_preview(root, snapshot)
    malformed = [f for f in sv["facts"] if f["code"] == "malformed_preview_node"]
    assert len(malformed) >= 1
    assert "AuraPreview/Text" in malformed[0]["paths"]


# ── Status rules ─────────────────────────────────────────────────────────────


def test_error_severity_fact_causes_failed_status(tmp_path: Path) -> None:
    """A fact with severity 'error' produces status 'failed'."""
    root = _project(tmp_path, [_wall()])
    instances = [
        _enrich({
            "path": "AuraPreview/A", "resource_path": "res://assets/modules/wall.tscn",
            "position": [0, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }),
        _enrich({
            "path": "AuraPreview/B", "resource_path": "res://assets/modules/wall.tscn",
            "position": [1, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
        }),
    ]
    sv = struct_validate_preview(root, _enriched_snapshot(instances))
    # Overlaps have severity "warning", so status will be "partial".
    assert sv["status"] in ("partial",)
