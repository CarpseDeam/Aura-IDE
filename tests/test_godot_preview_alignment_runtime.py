from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_godot_calibrated_alignment_math(tmp_path: Path) -> None:
    executable = os.environ.get("GODOT_BIN") or shutil.which("godot")
    if not executable:
        pytest.skip("GODOT_BIN or godot on PATH is required")
    project = tmp_path / "alignment_runtime"
    actions = project / "addons/aura_bridge/actions"
    actions.mkdir(parents=True)
    shutil.copyfile("aura/godot_editor/addon/actions/preview_alignment.gd", actions / "preview_alignment.gd")
    (project / "project.godot").write_text('[application]\nconfig/name="Alignment Runtime"\n', encoding="utf-8")
    (project / "test_alignment.gd").write_text(
        '''extends SceneTree
const Alignment = preload("res://addons/aura_bridge/actions/preview_alignment.gd")
var helper := Alignment.new()
var geometry := {"reference": {"catalog_identity": "ruins:wall", "local_bounds_m": [4, 4, 2], "pivot_to_center_m": [0, 2, 0]}, "piece": {"catalog_identity": "ruins:wall", "local_bounds_m": [4, 4, 2], "pivot_to_center_m": [0, 2, 0]}}
func close(a: Vector3, b: Vector3) -> bool: return a.is_equal_approx(b)
func check(reference: Node3D, ra: Array, pa: Array, expected: Vector3, offset := [0,0,0], space := "reference_local", scale := Vector3.ONE) -> void:
    var result: Dictionary = helper.calculate(reference, reference.rotation_degrees.y, scale, {"reference_anchor": ra, "piece_anchor": pa, "offset": offset, "offset_space": space}, geometry)
    if not result.get("ok", false) or not close(result["position"], expected): push_error("alignment mismatch: %s expected %s" % [result, expected]); quit(1)
func _initialize() -> void:
    var reference := Node3D.new()
    check(reference, [1,-1,0], [-1,-1,0], Vector3(4,0,0))
    check(reference, [0,1,0], [0,-1,0], Vector3(0,4,0))
    check(reference, [0,-1,1], [0,-1,-1], Vector3(0,0,2))
    check(reference, [1,-1,0], [-1,-1,0], Vector3(8,0,0), [0,0,0], "world", Vector3(2,1,1))
    for yaw in [0.0, 90.0, 180.0, 270.0]:
        reference.rotation_degrees.y = yaw
        var expected := Basis(Vector3.UP, deg_to_rad(yaw)) * Vector3(4,0,0)
        check(reference, [1,-1,0], [-1,-1,0], expected)
    reference.rotation_degrees.y = 90.0
    check(reference, [0,0,0], [0,0,0], Vector3(0,0,-1), [1,0,0], "reference_local")
    check(reference, [0,0,0], [0,0,0], Vector3(1,0,0), [1,0,0], "world")
    quit(0)
''', encoding="utf-8")
    completed = subprocess.run([executable, "--headless", "--path", str(project), "--script", "res://test_alignment.gd"], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
