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

    assert "Godot Live Building — Procedural Co-Building" in context
    assert "build_live_ruin" in context


# ---------------------------------------------------------------------------
# Rapid construction and task-driven visual evidence
# ---------------------------------------------------------------------------


def test_godot_workflow_describes_rapid_supervised_semantic_construction() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "#### Rapid Supervised Construction" in text
    assert "build_live_ruin" in text
    assert "one compact worker item" in lower
    assert "snapshot reconstruction, validation, topology, stable handles" in lower
    assert "short factual receipt" in lower
    assert "wait for the user's next direction" in lower


def test_godot_workflow_skill_lists_live_tool_names() -> None:
    text = _skill_text()
    for tool in [
        "inspect_godot_assets",
        "inspect_godot_editor",
        "inspect_godot_asset_preview",
        "inspect_live_ruin_contract",
        "build_live_ruin",
        "edit_godot_asset_preview",
        "capture_godot_asset_preview",
        "critique_godot_preview_local",
    ]:
        assert tool in text, f"{tool} not found in skill text"


def test_godot_workflow_does_not_require_visual_proof_for_supervised_edits() -> None:
    text = _skill_text().lower()
    assert "do not automatically call `capture_godot_asset_preview` or `critique_godot_preview_local`" in text
    assert "capture merely to prove that a semantic edit succeeded" in text
    assert "vision is not the default recovery path" in text


def test_godot_workflow_requires_evidence_for_visual_judgment_and_styling() -> None:
    text = _skill_text().lower()
    assert "#### visual checkpoints and styling work" in text
    assert "capture a useful view with `capture_godot_asset_preview`" in text
    assert "call `critique_godot_preview_local` when it is installed and callable" in text
    assert "never claim visual coherence or quality without findings from a vision-capable tool" in text
    assert "capturing an image alone is not visual analysis" in text
    assert "styling" in text and "silhouette" in text and "believable damage" in text


def test_godot_workflow_selects_exact_wall_piece_ids_without_hidden_motifs() -> None:
    text = _skill_text().lower()
    assert "#### exact wall-piece selection" in text
    assert "select exact assets by `asset_id`" in text
    assert "tags may filter catalog results but never silently choose an asset" in text
    assert "never calculate or supply resource paths, world positions, rotations, scales" in text
    assert "successful validation proves safe, reconstructable placement" in text
    assert "does not prove coherent rhythm, hierarchy, visual quality" in text


# ---------------------------------------------------------------------------
# Same-turn continued building
# ---------------------------------------------------------------------------


def test_godot_workflow_directs_same_turn_continued_building() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "current request" in lower
    assert "do not pause after every wall" in lower
    assert "wait for the user's next direction" in lower


def test_godot_workflow_says_not_to_invoke_vision_per_individual_piece() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "not mandatory proof after each construction mutation" in lower


def test_godot_workflow_favors_one_semantic_step_with_project_owned_pieces() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "one cohesive semantic operation" in lower
    assert "one model call per mesh piece" in lower
    assert "one atomic godot undoredo action" in lower


def test_small_bounded_edits_still_use_one_semantic_call_and_wait() -> None:
    text = _skill_text().lower()
    assert "one instruction → one build_live_ruin call → short receipt → wait" in text
    assert "one wall run, bounded room, floor region, level, upper fragment, stair, opening" in text
    assert "one `build_live_ruin` call containing one cohesive semantic operation" in text


def test_large_places_use_progressive_zone_batches_in_one_worker_item() -> None:
    text = _skill_text().lower()
    assert "#### progressive large construction" in text
    assert "citadels, castles, fortress districts, monasteries, multi-zone ruins" in text
    assert "several connected `build_live_ruin` calls inside that same worker item" in text
    for zone in [
        "approach, gatehouse", "outer court", "inner court", "central keep",
        "major wing", "tower section", "stair and upper-route connection",
    ]:
        assert zone in text
    assert "do not force the entire place into one comprehensive `build_live_ruin` call" in text
    assert "apply each successful zone immediately" in text


def test_large_build_inspects_contract_once_without_source_or_probe_discovery() -> None:
    text = _skill_text().lower()
    assert "inspect `inspect_live_ruin_contract` once" in text
    assert "semantic contract or current handles are unknown" in text
    assert "do not inspect project source code to discover semantic operation syntax" in text
    assert "do not create disposable probe walls, rooms, or openings" in text
    assert "operation schemas, grammar, live reconstruction, and valid candidates as authoritative" in text


def test_large_build_uses_returned_references_and_corrects_only_failed_zone() -> None:
    text = _skill_text().lower()
    assert "compact post-apply handles, created or modified spaces" in text
    assert "piece-count delta, openings, connections, and validation diagnostics" in text
    assert "use the structured diagnostic to correct only the failed zone" in text
    assert "failed zone applies nothing from that call" in text
    assert "successful earlier zone calls remain" in text


def test_large_build_has_no_vision_or_pause_between_structural_batches() -> None:
    text = _skill_text().lower()
    assert "do not call `capture_godot_asset_preview`, `critique_godot_preview_local`, or any vision tool between structural batches" in text
    assert "do not pause for user input between zones" in text
    assert "do not return a receipt after each zone" in text
    assert "one concise final receipt" in text


# ---------------------------------------------------------------------------
# Action-first construction within the single Interactive Mode
# ---------------------------------------------------------------------------


def test_contract_inspection_leads_directly_to_first_build_without_source_or_catalog_preflight() -> None:
    text = _skill_text().lower()
    assert "call `inspect_live_ruin_contract` at most once per request" in text
    assert "after the contract returns, make the first useful `build_live_ruin` call immediately" in text
    assert "do not inspect v_ruins constructor source, catalog files" in text
    assert "before that call or during ordinary semantic construction" in text


def test_successful_semantic_result_leads_directly_to_next_build_call() -> None:
    text = _skill_text().lower()
    assert "after a successful build call, continue directly to the next `build_live_ruin` call" in text
    assert "returned mass map, vertical profiles, handles, spaces, connections, and diagnostics" in text
    assert "without another inspection" in text


def test_interactive_live_build_calls_are_interleaved_with_model_observation() -> None:
    text = _skill_text().lower()
    assert "exactly one `build_live_ruin` call in each assistant tool-call round" in text
    assert "exactly one cohesive semantic operation in that call" in text
    assert "model must receive the completed call's compact post-apply state" in text
    assert "do not place future live-build calls beside it in the same assistant message" in text


def test_interrupted_progress_resumes_from_reconstructed_handles_without_duplicates() -> None:
    text = _skill_text().lower()
    assert "if an api interruption ends the turn after a successful step" in text
    assert "reconstruct current state with `inspect_live_ruin_contract`" in text
    assert "never repeat an already-present stable handle" in text


def test_exact_transform_inspection_is_not_used_for_ordinary_semantic_planning() -> None:
    text = _skill_text().lower()
    assert "exact node transforms, or implementation details" in text
    assert "do not inspect the preview to determine exact coordinates for the next semantic operation" in text


def test_coarse_first_visible_iteration_beats_prolonged_preflight_design() -> None:
    text = _skill_text().lower()
    assert "prefer a coarse but valid, meaningful architectural chunk" in text
    assert "over prolonged preflight design" in text
    assert "use visible iteration as the planning mechanism" in text
    assert "apply a meaningful chunk, observe its returned semantic result, then apply the next chunk" in text


def test_additional_read_only_inspection_requires_an_unresolved_concrete_diagnostic() -> None:
    text = _skill_text().lower()
    assert "perform another read-only inspection only when a concrete structured diagnostic" in text
    assert "cannot be resolved from its returned valid candidates" in text
    assert "never create probe geometry to learn behavior" in text


# ---------------------------------------------------------------------------
# Interactive Mode and Planner/Worker boundaries
# ---------------------------------------------------------------------------


def test_godot_workflow_defines_interactive_mode_role() -> None:
    text = _skill_text()
    assert "#### Interactive Mode" in text
    assert "Use one Interactive Mode for live building" in text
    assert "not separate execution modes" in text
    assert "DeepSeek chooses structural intent" in text
    assert "project code owns exact mesh positions" in text


def test_godot_workflow_defines_planner_and_worker_roles() -> None:
    text = _skill_text()
    assert "##### Planner (read-only)" in text
    assert "##### Worker (owns every mutation" in text


def test_godot_workflow_forbids_planner_mutations() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "not attempt mutations" in lower
    assert "read bridge credentials" in lower
    assert "author another execution path" in lower


# ---------------------------------------------------------------------------
# Safety constraints preserved
# ---------------------------------------------------------------------------


def test_godot_workflow_forbids_raw_tcp_and_arbitrary_paths() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "no raw tcp" in lower
    assert "prescribe raw resource paths" in lower


def test_godot_workflow_forbids_helper_builders_and_implicit_save() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "helper generator authored during a live composition" in lower
    assert "never save the scene unless explicitly requested" in lower


def test_godot_workflow_preserves_catalog_only_assets() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "catalog-only asset ids" in lower or "catalog asset ids" in lower
    assert "no arbitrary" in lower and ".tscn" in lower


# ---------------------------------------------------------------------------
# Visual critique remains checkpoint-driven
# ---------------------------------------------------------------------------


def test_godot_workflow_has_no_mandatory_critic_loop() -> None:
    text = _skill_text().lower()
    forbidden_mechanics = [
        "needs_revision",
        "cannot_judge",
        "coherence_check",
        "critical_failure",
        "strongest_feature",
        "critique again",
        "semantic critique is required",
        "until coherent",
        "declared coherent",
    ]
    for word in forbidden_mechanics:
        assert word not in text, f"Critic mechanic '{word}' found in skill"
    assert "unsolicited visual refinement loop" in text
    assert "task-driven checkpoints" in text


# ---------------------------------------------------------------------------
# Positive signals
# ---------------------------------------------------------------------------


def test_godot_workflow_describes_fast_interactive_loop() -> None:
    text = _skill_text()
    assert "inspect once" in text
    assert "dispatch one compact Worker item" in text
    assert "semantic construction returns exact facts" in text
    assert "report and wait for the next direction" in text


def test_godot_workflow_allows_later_user_modifications() -> None:
    text = _skill_text()
    lower = text.lower()
    assert "extend the east room" in lower
    assert "breach the rear wall" in lower
    assert "modify the named live structure" in lower


def test_godot_workflow_exposes_semantic_vocabulary_and_forbids_mesh_transforms() -> None:
    text = _skill_text()
    lower = text.lower()
    for operation in [
        "create_run", "turn_run", "extend_run", "create_enclosure",
        "insert_opening", "attach_room", "extend_room", "add_upper_level", "add_supported_span", "apply_damage",
    ]:
        assert operation in text
    assert "add_tower" not in text
    assert "never calculate a transform for every mesh" in lower
    assert "stable handles" in lower


def test_godot_workflow_requires_real_architectural_composition() -> None:
    text = _skill_text().lower()
    assert "hierarchy of real connected spaces, wall courses, upper levels, supported spans" in text
    assert "primary low or wide mass" in text
    assert "secondary taller or narrower masses" in text
    assert "the centre lies between the flank centres" in text
    assert "a requested upper connector lists and touches both supports" in text
    assert "do not call the composition complete because three rooms exist" in text
    assert "do not encode fixed dimensions or mandatory symmetry" in text
    assert "choose returned structural continuation candidates before windows or decorative candidates" in text
    assert "complete structural massing and silhouette before windows" in text
    assert "never select it automatically after creating a large hall" in text
    assert "if the user says no towers or no roof yet, do not add them" in text
    assert "stop after one meaningful component or visual checkpoint" in text


def test_godot_workflow_preserves_aura_preview_root_and_undo_redo() -> None:
    text = _skill_text()
    assert "AuraPreview" in text
    assert "UndoRedo" in text
    assert "atomic" in text
