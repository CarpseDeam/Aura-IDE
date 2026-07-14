from __future__ import annotations

import shutil
from pathlib import Path

from aura.skills.reader import read_skills
from aura.skills.selection import select_relevant_skills
from aura.skills.text import build_skill_context

SOURCE = Path("scripts/personal/godot_knowledge/skills")
SKILL_FILE = SOURCE / "godot_aura_workflow" / "SKILL.md"


def _skill_text() -> str:
    return SKILL_FILE.read_text(encoding="utf-8")


def _workspace_with_skills(tmp_path: Path) -> Path:
    destination = tmp_path / ".aura/skills/authored"
    destination.mkdir(parents=True)
    for source in SOURCE.iterdir():
        if source.is_dir():
            shutil.copytree(source, destination / source.name)
    return tmp_path


def _ids(skills) -> list[str]:
    return [dict(skill.origin).get("skill_id", "") for skill in skills]


def test_personal_godot_skills_are_read_as_project_authored(tmp_path: Path) -> None:
    root = _workspace_with_skills(tmp_path)
    skills = read_skills(root)
    authored = [
        skill
        for skill in skills
        if dict(skill.origin).get("skill_id", "").startswith("godot_")
    ]

    assert len(authored) == 5
    assert {dict(skill.origin)["skill_id"] for skill in authored} == {
        "godot_3d_assembly",
        "godot_aura_workflow",
        "godot_gdscript",
        "godot_mmo_performance",
        "godot_scene_architecture",
    }


def test_personal_godot_skills_route_specialized_knowledge_first(tmp_path: Path) -> None:
    root = _workspace_with_skills(tmp_path)
    skills = read_skills(root)

    mmo = select_relevant_skills(
        skills,
        task_kind="coding",
        target_files=("scripts/world/chunk.gd",),
        content="optimize MMO repeated props with MultiMesh LOD and occlusion",
    )
    gdscript = select_relevant_skills(
        skills,
        task_kind="coding",
        target_files=("scripts/player.gd",),
        content="fix this GDScript signal and physics process bug",
    )
    assembly = select_relevant_skills(
        skills,
        task_kind="coding",
        target_files=("scenes/preview.tscn",),
        content="assemble modular Node3D assets with sockets and visual iteration",
    )

    assert _ids(mmo)[0] == "godot_mmo_performance"
    assert _ids(gdscript)[0] == "godot_gdscript"
    assert _ids(assembly)[:2] == ["godot_3d_assembly", "godot_aura_workflow"]


def test_personal_godot_knowledge_is_not_imported_by_packaged_aura() -> None:
    assert SOURCE.parts[0] == "scripts"
    assert not any(
        "godot_knowledge" in path.read_text(encoding="utf-8", errors="ignore")
        for path in Path("aura").rglob("*.py")
    )


def test_godot_workflow_skill_remains_outside_packaged_aura() -> None:
    assert SKILL_FILE.parts[0] == "scripts"
    assert not any(
        "godot_aura_workflow" in path.read_text(encoding="utf-8", errors="ignore")
        for path in Path("aura").rglob("*.py")
    )


def test_godot_visual_iteration_routes_to_workflow_skill(tmp_path: Path) -> None:
    root = _workspace_with_skills(tmp_path)
    skills = read_skills(root)
    result = select_relevant_skills(
        skills,
        task_kind="visual iteration",
        target_files=("addons/aura_bridge/transport/bridge_server.gd",),
        content="build a ruined checkpoint with approach and landmark using catalog assets beneath AuraPreview",
    )
    assert _ids(result)[0] == "godot_aura_workflow"


def test_live_composition_context_selects_direct_piece_workflow(tmp_path: Path) -> None:
    root = _workspace_with_skills(tmp_path)
    context = build_skill_context(
        root,
        task_kind="visual iteration",
        target_files=("addons/aura_bridge/transport/bridge_server.gd",),
        content="Design a ruined gatehouse with unequal tower masses beneath AuraPreview.",
    )

    assert "Godot Live Building — Direct Exact Pieces" in context
    assert "edit_godot_asset_preview" in context
    assert "build_live_ruin" in context


def test_godot_workflow_makes_exact_pieces_and_direct_editor_primary() -> None:
    text = _skill_text().lower()
    assert "exact modular catalog pieces are the normal construction medium" in text
    assert "use `edit_godot_asset_preview` as the primary mutation tool" in text
    for operation in ("instantiate", "duplicate", "attach", "set_transform", "replace", "remove"):
        assert operation in text
    assert "bounded positions, offsets, and verified yaw rotations are valid" in text
    assert "not one model call per mesh piece" in text


def test_godot_workflow_assigns_composition_to_aura_and_mechanics_to_godot() -> None:
    text = _skill_text().lower()
    assert "aura owns composition, scale, sequencing, exact asset selection" in text
    assert "wall lengths, tower footprints, height, openings, asymmetry, damage" in text
    assert "godot owns catalog resolution, node creation, undoredo" in text
    assert "socket attachment calculations, validated rotations, and preview mutation" in text


def test_godot_workflow_uses_cohesive_direct_revisions_and_changed_facts() -> None:
    text = _skill_text().lower()
    assert "apply one cohesive direct editor revision" in text
    assert "one complete wall course or a vertical stack of several courses" in text
    assert "four sides and corners of a substantial tower base" in text
    assert "read the returned compact facts after every revision" in text
    assert "conservative placement warnings" in text
    assert "overlap warnings are informational" in text


def test_godot_workflow_keeps_semantic_building_optional_and_editable() -> None:
    text = _skill_text().lower()
    assert "`build_live_ruin` remains available as an optional shortcut" in text
    assert "not a prerequisite or default" in text
    assert "basic enclosure, floor, stair connection, supported span" in text
    assert "every shortcut result remains ordinary editable preview pieces" in text
    assert "direct exact pieces and existing `auraproc__...` semantic pieces may coexist" in text


def test_godot_workflow_rejects_required_semantic_hierarchy_and_named_archetype_ops() -> None:
    text = _skill_text().lower()
    assert "do not require named rooms, mass maps, supported spans, vertical profiles" in text
    assert "do not automatically inspect mass maps, vertical profiles" in text
    assert "towers, gatehouses, keeps, naves, bridges, castles, monasteries" in text
    assert "do not create named architectural operations for them" in text
    for forbidden in ("add_tower", "create_gatehouse", "build_castle", "create_monastery"):
        assert forbidden not in text


def test_godot_workflow_preserves_user_corrections_and_unsaved_preview() -> None:
    text = _skill_text().lower()
    assert "preserve the user's corrections and strong existing work" in text
    assert "instead of rebuilding from scratch" in text
    assert "never save the scene unless the user explicitly requests it" in text
    assert "capture only when the current work reaches a useful visual checkpoint" in text
    assert "without vision findings, report factual changes and let the user judge" in text


def test_godot_workflow_preserves_mechanical_safety_without_quality_scoring() -> None:
    text = _skill_text().lower()
    assert "use exact catalog asset ids" in text
    assert "unknown assets, missing sources, invalid names/transforms" in text
    assert "forbidden rotation, out-of-bounds placement, or capacity overflow" in text
    assert "do not turn overlap, embedding, incomplete construction" in text
    assert "critic, or scoring system" in text
    assert "auraPreview".lower() in text
    assert "undoredo" in text
