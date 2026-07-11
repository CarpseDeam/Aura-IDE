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
    authored = [skill for skill in skills if dict(skill.origin).get("skill_id", "").startswith("godot_")]

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


def test_godot_workflow_skill_defines_planner_and_worker_roles() -> None:
    text = _skill_text()
    assert "#### Planner (read-only)" in text
    assert "#### Worker (owns every mutation" in text


def test_godot_workflow_skill_forbids_planner_mutations() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "not attempt mutations" in lower
    assert "not write helper scripts" in lower
    assert "not read bridge credentials" in lower


def test_godot_workflow_skill_forbids_raw_tcp_and_arbitrary_paths() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "no raw tcp" in lower
    assert "not prescribe raw resource paths" in lower


def test_godot_workflow_skill_lists_live_tool_names() -> None:
    text = _skill_text()
    for tool in [
        "inspect_godot_assets",
        "inspect_godot_editor",
        "inspect_godot_asset_preview",
        "edit_godot_asset_preview",
        "capture_godot_asset_preview",
        "critique_godot_preview_local",
    ]:
        assert tool in text


def test_godot_workflow_skill_has_no_fixed_pass_limit() -> None:
    text = _skill_text()
    assert "no fixed revision-pass limit" in text


def test_godot_workflow_skill_allows_supervised_iteration() -> None:
    text = _skill_text()
    assert "actively supervising" in text
    assert "meaningful progress" in text


def test_godot_workflow_requires_semantic_evidence_for_visual_composition() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "critique (optional" not in lower
    assert "semantic critique is required" in lower
    assert "installed and callable" in lower
    assert "not proof that a rendered composition is visually coherent" in lower
    assert "do not claim that the composition is visually successful or coherent" in lower
    assert "original creative brief" in lower


def test_godot_workflow_preserves_planner_worker_boundary_and_refinement() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "planner names the tool" in lower
    assert "does not execute semantic critique" in lower
    assert "worker alone owns" in lower
    assert "reinspect exact facts, recapture, critique again" in lower
    assert "no fixed revision-pass limit" in lower


def test_godot_workflow_uses_verdicts_without_weakening_boundaries() -> None:
    text = _skill_text().lower()
    assert "`needs_revision` requires another focused composition pass" in text
    assert "`cannot_judge` requires a more useful capture" in text
    assert "latest useful critique returns `coherent`" in text
    assert "exact structural facts do not contradict it" in text
    assert "worst reported coherence failure first" in text
    assert "preserves the reported strongest feature" in text
    assert "planner names the tool" in text
    assert "worker alone owns" in text


def test_godot_workflow_exempts_purely_structural_tasks() -> None:
    text = _skill_text().lower()
    assert "semantic critique is unnecessary for purely structural work" in text
    assert "bridge validation" in text
    assert "deterministic geometry checks" in text


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
    ids = _ids(result)
    assert ids[0] == "godot_aura_workflow"


def test_live_composition_build_skill_context_selects_workflow(tmp_path: Path) -> None:
    root = _workspace_with_skills(tmp_path)
    brief = (
        "Design The Broken Gate with one clear gate opening, two unequal tower masses, "
        "connected defensive walls, one alternate breach, a readable court, deliberate "
        "negative space, and rubble from understandable structural collapse beneath AuraPreview."
    )

    context = build_skill_context(
        root,
        task_kind="visual iteration",
        target_files=("addons/aura_bridge/transport/bridge_server.gd",),
        content=brief,
    )

    assert "Godot Visual Iteration — Planner and Worker Role Split" in context
    assert "critique_godot_preview_local" in context
