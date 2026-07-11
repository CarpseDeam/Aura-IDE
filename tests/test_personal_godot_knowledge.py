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


# ---------------------------------------------------------------------------
# Skill discovery and routing (preserved)
# ---------------------------------------------------------------------------


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

    assert "Godot Live Building — Fast Interactive Loop" in context
    assert "describe_godot_preview_local" in context


# ---------------------------------------------------------------------------
# Tool naming: describe_godot_preview_local present, critique_godot_preview_local absent
# ---------------------------------------------------------------------------


def test_godot_workflow_skill_names_describe_not_critique() -> None:
    text = _skill_text()
    assert "describe_godot_preview_local" in text
    assert "critique_godot_preview_local" not in text


def test_godot_workflow_skill_lists_live_tool_names() -> None:
    text = _skill_text()
    for tool in [
        "inspect_godot_assets",
        "inspect_godot_editor",
        "inspect_godot_asset_preview",
        "edit_godot_asset_preview",
        "capture_godot_asset_preview",
        "describe_godot_preview_local",
    ]:
        assert tool in text, f"{tool} not found in skill text"


# ---------------------------------------------------------------------------
# Same-turn continued building
# ---------------------------------------------------------------------------


def test_godot_workflow_directs_same_turn_continued_building() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "immediately continues" in lower
    assert "same tool loop" in lower or "same request" in lower
    assert "do not stop after each burst" in lower


def test_godot_workflow_says_not_to_invoke_vision_per_individual_piece() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "not after every individual wall" in lower or "not after every individual piece" in lower


def test_godot_workflow_favors_several_connected_pieces_per_apply() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "several meaningful connected pieces" in lower
    assert "one atomic" in lower


# ---------------------------------------------------------------------------
# Interactive Mode and Planner/Worker boundaries
# ---------------------------------------------------------------------------


def test_godot_workflow_defines_interactive_mode_role() -> None:
    text = _skill_text()
    assert "#### Interactive Mode" in text
    assert "DeepSeek is the builder and sole decision-maker" in text


def test_godot_workflow_defines_planner_and_worker_roles() -> None:
    text = _skill_text()
    assert "##### Planner (read-only)" in text
    assert "##### Worker (owns every mutation" in text


def test_godot_workflow_forbids_planner_mutations() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "not attempt mutations" in lower
    assert "not write helper scripts" in lower
    assert "not read bridge credentials" in lower


# ---------------------------------------------------------------------------
# Safety constraints preserved
# ---------------------------------------------------------------------------


def test_godot_workflow_forbids_raw_tcp_and_arbitrary_paths() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "no raw tcp" in lower
    assert "not prescribe raw resource paths" in lower


def test_godot_workflow_forbids_helper_builders_and_implicit_save() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "no helper builders" in lower
    assert "never save the scene unless explicitly requested" in lower


def test_godot_workflow_preserves_catalog_only_assets() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "catalog-only asset ids" in lower or "catalog asset ids" in lower
    assert "no arbitrary" in lower and ".tscn" in lower


# ---------------------------------------------------------------------------
# Critic language absent
# ---------------------------------------------------------------------------


def test_godot_workflow_has_no_critic_verdict_language() -> None:
    """Verify critic concepts are not used as positive workflow instructions.

    Words like "verdict", "score", "coherent" may appear in prohibitions
    ("Do not require a verdict / score / critic approval").  That is correct.
    This test verifies that critic-driven workflow language (mandatory critique,
    revision passes, verdict-gated progression) is absent.
    """
    text = _skill_text().lower()

    # Critic workflow mechanics — must never appear as positive instructions
    forbidden_mechanics = [
        "needs_revision",
        "cannot_judge",
        "coherence_check",
        "critical_failure",
        "strongest_feature",
        "critique again",
        "semantic critique is required",
    ]
    for word in forbidden_mechanics:
        assert word not in text, f"Critic mechanic '{word}' found in skill"

    # Verify the old "critique (optional" escape hatch is gone
    assert "critique (optional" not in text

    # "coherent" should not appear as a quality bar
    assert "must be coherent" not in text
    assert "until coherent" not in text
    assert "declared coherent" not in text
    assert "visually coherent" not in text

    # The skill must describe the fast build loop, not a critique loop
    assert "describe locally" in text
    assert "continue" in text


def test_godot_workflow_has_no_semantic_critique_requirement() -> None:
    text = _skill_text().lower()
    assert "semantic critique is required" not in text
    assert "not proof that a rendered composition is visually coherent" not in text
    assert "do not claim that the composition is visually successful" not in text


def test_godot_workflow_has_no_planner_names_tool_language() -> None:
    """The old critic workflow had 'Planner names the tool' — verify it's gone."""
    text = _skill_text().lower()
    assert "planner names the tool" not in text


# ---------------------------------------------------------------------------
# Positive signals
# ---------------------------------------------------------------------------


def test_godot_workflow_describes_fast_interactive_loop() -> None:
    text = _skill_text()
    assert "inspect once" in text
    assert "build a connected burst" in text
    assert "describe locally" in text
    assert "build the next burst" in text


def test_godot_workflow_allows_later_user_modifications() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "add a tower" in lower
    assert "make it more run down" in lower
    assert "modify the existing live" in lower


def test_godot_workflow_preserves_aura_preview_root_and_undo_redo() -> None:
    text = _skill_text()
    assert "AuraPreview" in text
    assert "UndoRedo" in text
    assert "atomic" in text
