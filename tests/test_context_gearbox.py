from __future__ import annotations

import json
from pathlib import Path

import pytest

from aura.context_gearbox.models import ComposedContext, ContextLedgerEntry, RuntimeRole
from aura.context_gearbox.runtime import (
    compose_system_prompt,
    context_gearbox_metadata,
    default_role_prompt,
    format_context_gearbox_display,
    serialize_context_ledger,
    summarize_context_ledger,
)
from aura.context_gearbox.sources import iter_registered_sources
from aura.conversation.history import History
from aura.conversation.planner_refresh import PlannerRefreshState
from aura.hazard.capture import record_hazard
from aura.prompts import (
    PLANNER_SYSTEM_PROMPT,
    SINGLE_SYSTEM_PROMPT,
    WORKER_SYSTEM_PROMPT,
    build_tier1_context,
    inject_tier1_context,
)


def _entry_by_id(composed: ComposedContext, source_id: str):
    return next(entry for entry in composed.ledger if entry.source_id == source_id)


CONTRACT_IDS = [
    "planner_dispatch_contract",
    "worker_execution_contract",
    "code_quality_contract",
    "validation_selection_contract",
    "receipt_contract",
]

SCOPED_PACK_IDS = [
    "gui_rules",
    "drone_rules",
    "provider_rules",
    "build_pipeline_rules",
]

RESEARCH_PACK_IDS = [
    "web_research_rules",
]

SKILL_PACK_IDS = [
    "skill_pack",
]

SCOPED_PACK_SKIP_REASONS = {
    "gui_rules": "target files do not match gui scope",
    "drone_rules": "target files do not match drone scope",
    "provider_rules": "target files do not match provider scope",
    "build_pipeline_rules": "target files do not match build scope",
}


def _included_contract_ids(composed: ComposedContext) -> list[str]:
    return [
        entry.source_id
        for entry in composed.ledger
        if entry.kind == "quality_contract" and entry.included
    ]


def _included_scoped_pack_ids(composed: ComposedContext) -> list[str]:
    return [
        entry.source_id
        for entry in composed.ledger
        if entry.kind == "scoped_coding_pack" and entry.included
    ]


def _seed_graduated_hazard(
    workspace_root: Path,
    *,
    task_kind: str = "bugfix",
    target_file: str = "aura/context_gearbox/sources.py",
) -> None:
    for index in range(3):
        record_hazard(
            workspace_root=workspace_root,
            model="test-model",
            status="validation_failed",
            structured_failure={"failure_class": "pytest_failure"},
            target_files=[target_file],
            task_shape={"task_kind": task_kind},
            errors=["AssertionError: context source was skipped"],
            tool_call_id=f"dispatch-{index}",
        )


def _assert_only_scoped_pack_loaded(composed: ComposedContext, source_id: str) -> None:
    assert _included_scoped_pack_ids(composed) == [source_id]
    assert source_id in composed.context_text
    for scoped_id in SCOPED_PACK_IDS:
        entry = _entry_by_id(composed, scoped_id)
        if scoped_id == source_id:
            assert entry.included is True
            assert entry.char_count > 0
        else:
            assert entry.included is False
            assert entry.reason == SCOPED_PACK_SKIP_REASONS[scoped_id]


def test_prompt_compatibility_exports_work(tmp_path):
    assert PLANNER_SYSTEM_PROMPT
    assert WORKER_SYSTEM_PROMPT
    assert SINGLE_SYSTEM_PROMPT
    assert inject_tier1_context("A {TIER1_CONTEXT} B", "ctx") == "A ctx B"
    assert isinstance(build_tier1_context(tmp_path), str)
    assert "worker_execution_contract" in build_tier1_context(tmp_path, mode="worker")


def test_planner_prompt_blocks_exact_implementation_edit_reasoning():
    prompt = default_role_prompt(RuntimeRole.PLANNER)

    assert "target seam, allowed files, constraints, non-goals" in prompt
    assert "Planner must not write code" in prompt
    assert "exact implementation/edit reasoning" in prompt
    assert "Worker owns implementation reasoning, exact edits" in prompt


def test_composer_returns_composed_context_with_core_kernel(tmp_path):
    composed = compose_system_prompt(RuntimeRole.PLANNER, "", tmp_path)

    assert isinstance(composed, ComposedContext)
    assert composed.system_prompt
    assert composed.context_text
    core_entry = _entry_by_id(composed, "core_kernel")
    assert core_entry.included is True
    assert core_entry.char_count > 0


def test_project_rules_ledger_included_and_skipped(tmp_path):
    skipped = compose_system_prompt(RuntimeRole.PLANNER, "", tmp_path)
    skipped_entry = _entry_by_id(skipped, "project_rules")
    assert skipped_entry.included is False
    assert skipped_entry.char_count == 0

    (tmp_path / "project_rules.md").write_text("Use focused tests.", encoding="utf-8")
    included = compose_system_prompt(RuntimeRole.PLANNER, "", tmp_path)
    included_entry = _entry_by_id(included, "project_rules")
    assert included_entry.included is True
    assert included_entry.char_count > 0
    assert "Use focused tests." in included.context_text


def test_repo_map_ledger_included_or_skipped(tmp_path):
    skipped = compose_system_prompt(RuntimeRole.PLANNER, "", tmp_path)
    skipped_entry = _entry_by_id(skipped, "repo_map")
    assert skipped_entry.included is False
    assert skipped_entry.char_count == 0

    (tmp_path / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    included = compose_system_prompt(RuntimeRole.PLANNER, "", tmp_path, force=True)
    included_entry = _entry_by_id(included, "repo_map")
    assert included_entry.included is True
    assert included_entry.char_count > 0


def test_role_prompts_compose_without_old_doctrine(tmp_path):
    forbidden = [
        "Current-Info Research",
        "Worker doctrine",
        "Planner doctrine",
        "Recent Drone Run Activity",
        "Available Drones",
        "continuation_report",
        "private_style",
        ".aura/project_blueprint.md",
    ]
    for role in (RuntimeRole.PLANNER, RuntimeRole.WORKER, RuntimeRole.SINGLE):
        composed = compose_system_prompt(role, "", tmp_path)
        assert composed.system_prompt
        for text in forbidden:
            assert text not in composed.system_prompt


def test_planner_composition_includes_dispatch_contract(tmp_path):
    composed = compose_system_prompt(RuntimeRole.PLANNER, "", tmp_path)

    assert "planner_dispatch_contract" in composed.context_text
    assert _included_contract_ids(composed) == ["planner_dispatch_contract"]
    entry = _entry_by_id(composed, "planner_dispatch_contract")
    assert entry.included is True
    assert entry.reason == "planner coding-harness dispatch quality contract"


def test_web_research_rules_load_for_planner_research_requests(tmp_path):
    composed = compose_system_prompt(
        RuntimeRole.PLANNER,
        "",
        tmp_path,
        task_kind="answer_only",
    )

    entry = _entry_by_id(composed, "web_research_rules")
    assert entry.included is True
    assert "web_research_rules" in composed.context_text
    assert "Pure research answers" in composed.context_text


def test_web_research_rules_do_not_load_for_normal_coding_tasks(tmp_path):
    composed = compose_system_prompt(
        RuntimeRole.PLANNER,
        "",
        tmp_path,
        task_kind="bugfix",
    )

    entry = _entry_by_id(composed, "web_research_rules")
    assert entry.included is False
    assert entry.reason == "turn is not research-shaped"
    assert "web_research_rules" not in composed.context_text


def test_worker_composition_includes_quality_contract_stack(tmp_path):
    composed = compose_system_prompt(
        RuntimeRole.WORKER,
        "",
        tmp_path,
        task_kind="bugfix",
        target_files=("aura/example.py",),
    )

    assert _included_contract_ids(composed) == [
        "worker_execution_contract",
        "code_quality_contract",
        "validation_selection_contract",
        "receipt_contract",
    ]
    for source_id in _included_contract_ids(composed):
        assert source_id in composed.context_text
        entry = _entry_by_id(composed, source_id)
        assert entry.included is True
        assert entry.char_count > 0


def test_worker_gui_target_loads_gui_rules_and_skips_unrelated_packs(tmp_path):
    composed = compose_system_prompt(
        RuntimeRole.WORKER,
        "",
        tmp_path,
        task_kind="bugfix",
        target_files=("aura/gui/main_window.py",),
    )

    _assert_only_scoped_pack_loaded(composed, "gui_rules")


def test_worker_drone_target_loads_drone_rules_and_skips_unrelated_packs(tmp_path):
    composed = compose_system_prompt(
        RuntimeRole.WORKER,
        "",
        tmp_path,
        task_kind="bugfix",
        target_files=("aura/drones/runner.py",),
    )

    _assert_only_scoped_pack_loaded(composed, "drone_rules")


@pytest.mark.parametrize(
    "target_file",
    (
        "aura/providers/openai.py",
        "aura/backends/api_agent.py",
        "aura/client/events.py",
    ),
)
def test_worker_provider_targets_load_provider_rules_and_skip_unrelated_packs(
    tmp_path,
    target_file,
):
    composed = compose_system_prompt(
        RuntimeRole.WORKER,
        "",
        tmp_path,
        task_kind="bugfix",
        target_files=(target_file,),
    )

    _assert_only_scoped_pack_loaded(composed, "provider_rules")


@pytest.mark.parametrize(
    "target_file",
    (
        "scripts/build_nuitka.py",
        "installer/aura.iss",
    ),
)
def test_worker_build_targets_load_build_rules_and_skip_unrelated_packs(
    tmp_path,
    target_file,
):
    composed = compose_system_prompt(
        RuntimeRole.WORKER,
        "",
        tmp_path,
        task_kind="bugfix",
        target_files=(target_file,),
    )

    _assert_only_scoped_pack_loaded(composed, "build_pipeline_rules")


def test_skill_pack_loads_for_worker_when_graduated_hazard_matches_terrain(
    tmp_path,
):
    _seed_graduated_hazard(tmp_path)

    composed = compose_system_prompt(
        RuntimeRole.WORKER,
        "",
        tmp_path,
        task_kind="bugfix",
        target_files=("aura/context_gearbox/sources.py",),
    )
    metadata = context_gearbox_metadata(composed.ledger)
    entry = _entry_by_id(composed, "skill_pack")

    assert entry.included is True
    assert entry.kind == "skill_pack"
    assert entry.reason == "terrain-selected skills for this context"
    assert entry.char_count > 0
    assert "### Learned Hazard Guards" in composed.context_text
    assert "context source was skipped" in composed.context_text
    assert "skill_pack" in metadata["summary"]["loaded"]
    assert any(
        item["source_id"] == "skill_pack" and item["included"] is True
        for item in metadata["ledger"]
    )
    assert any(
        item["kind"] == "individual_skill" and item["included"] is True
        for item in metadata["ledger"]
    )


def test_skill_pack_loads_bundled_skills_on_drone_terrain_skipped_on_unrelated_terrain(
    tmp_path,
):
    """Bundled skills match via directory prefix, not _paths_related's loose
    prefix collision.  Drone terrain should load the drone bundled skill;
    unrelated terrain should skip skill_pack entirely (no graduated hazards)."""
    # --- Direction 1: drone terrain loads bundled skills ---
    drone_composed = compose_system_prompt(
        RuntimeRole.WORKER,
        "",
        tmp_path,
        target_files=("aura/drones/runner.py",),
    )
    drone_entry = _entry_by_id(drone_composed, "skill_pack")

    assert drone_entry.included is True
    assert drone_entry.kind == "skill_pack"
    assert drone_entry.reason == "terrain-selected skills for this context"
    assert drone_entry.char_count > 0
    assert "### Bundled Skills" in drone_composed.context_text
    assert "Drone Work Skill" in drone_composed.context_text
    assert "### Learned Hazard Guards" not in drone_composed.context_text

    # --- Direction 2: unrelated terrain skips skill_pack ---
    unrelated_composed = compose_system_prompt(
        RuntimeRole.WORKER,
        "",
        tmp_path,
        task_kind="bugfix",
        target_files=("aura/context_gearbox/sources.py",),
    )
    unrelated_entry = _entry_by_id(unrelated_composed, "skill_pack")

    assert unrelated_entry.included is False
    assert unrelated_entry.reason == "no skills matched for this terrain"
    assert "skill_pack" not in unrelated_composed.context_text


def test_skill_pack_loads_user_authored_skill_by_content(tmp_path):
    authored_dir = tmp_path / ".aura" / "skills" / "authored"
    authored_dir.mkdir(parents=True)
    (authored_dir / "auth.json").write_text(
        json.dumps(
            {
                "text": (
                    "When editing authentication token refresh, "
                    "validate session renewal behavior."
                )
            }
        ),
        encoding="utf-8",
    )

    composed = compose_system_prompt(
        RuntimeRole.WORKER,
        "",
        tmp_path,
        task_kind="bugfix",
        content="Fix the auth token refresh regression in the login flow.",
    )
    entry = _entry_by_id(composed, "skill_pack")

    assert entry.included is True
    assert entry.kind == "skill_pack"
    assert "### Project Engineering Standards" in composed.context_text
    assert "validate session renewal behavior" in composed.context_text
    assert any(
        item.kind == "individual_skill" and item.included is True
        for item in composed.ledger
    )


def test_skill_pack_skip_when_workspace_root_is_missing():
    composed = compose_system_prompt(
        RuntimeRole.WORKER,
        "",
        None,
        task_kind="bugfix",
        target_files=("aura/context_gearbox/sources.py",),
    )
    entry = _entry_by_id(composed, "skill_pack")

    assert entry.included is False
    assert entry.reason == "no workspace root"


def test_single_coding_target_loads_matching_scoped_pack(tmp_path):
    composed = compose_system_prompt(
        RuntimeRole.SINGLE,
        "",
        tmp_path,
        task_kind="bugfix",
        target_files=("aura/gui/playground.py",),
    )

    _assert_only_scoped_pack_loaded(composed, "gui_rules")


def test_single_task_kind_hint_loads_matching_scoped_pack_without_targets(tmp_path):
    composed = compose_system_prompt(
        RuntimeRole.SINGLE,
        "",
        tmp_path,
        task_kind="gui_polish",
    )

    _assert_only_scoped_pack_loaded(composed, "gui_rules")


def test_plain_single_mode_does_not_load_scoped_coding_packs(tmp_path):
    composed = compose_system_prompt(RuntimeRole.SINGLE, "", tmp_path)

    assert _included_scoped_pack_ids(composed) == []
    for source_id in SCOPED_PACK_IDS:
        entry = _entry_by_id(composed, source_id)
        assert entry.included is False
        assert entry.reason == "single task is not coding-shaped"


def test_planner_does_not_load_scoped_coding_packs_by_default(tmp_path):
    composed = compose_system_prompt(RuntimeRole.PLANNER, "", tmp_path)

    assert _included_scoped_pack_ids(composed) == []
    for source_id in SCOPED_PACK_IDS:
        entry = _entry_by_id(composed, source_id)
        assert entry.included is False
        assert entry.reason == "not scoped to planner role"


def test_contract_ledger_order_is_deterministic_and_records_skips(tmp_path):
    composed = compose_system_prompt(RuntimeRole.PLANNER, "", tmp_path)

    # Filter out extra individual_skill entries added by the gearbox
    main_entries = [
        entry for entry in composed.ledger if entry.kind != "individual_skill"
    ]
    assert [entry.source_id for entry in main_entries] == [
        source.source_id for source in iter_registered_sources()
    ]
    contract_entries = [
        entry for entry in composed.ledger if entry.kind == "quality_contract"
    ]
    assert [entry.source_id for entry in contract_entries] == CONTRACT_IDS
    scoped_entries = [
        entry for entry in composed.ledger if entry.kind == "scoped_coding_pack"
    ]
    assert [entry.source_id for entry in scoped_entries] == SCOPED_PACK_IDS
    research_entries = [
        entry for entry in composed.ledger if entry.kind == "planner_research_pack"
    ]
    assert [entry.source_id for entry in research_entries] == RESEARCH_PACK_IDS
    skill_entries = [
        entry for entry in composed.ledger if entry.kind == "skill_pack"
    ]
    assert [entry.source_id for entry in skill_entries] == SKILL_PACK_IDS
    assert research_entries[0].included is False
    assert research_entries[0].reason == "turn is not research-shaped"
    assert skill_entries[0].included is True
    assert skill_entries[0].reason == "terrain-selected skills for this context"
    skipped = [
        entry for entry in contract_entries
        if entry.source_id != "planner_dispatch_contract"
    ]
    assert skipped
    assert all(entry.included is False for entry in skipped)
    assert all(entry.reason for entry in skipped)


def test_single_mode_does_not_load_full_worker_stack(tmp_path):
    plain = compose_system_prompt(RuntimeRole.SINGLE, "", tmp_path)
    assert _included_contract_ids(plain) == []
    assert _entry_by_id(plain, "worker_execution_contract").included is False

    coding = compose_system_prompt(
        RuntimeRole.SINGLE,
        "",
        tmp_path,
        task_kind="bugfix",
        target_files=("app.py",),
    )
    assert _included_contract_ids(coding) == [
        "code_quality_contract",
        "validation_selection_contract",
        "receipt_contract",
    ]
    assert "worker_execution_contract" not in coding.context_text


def test_contracts_do_not_appear_in_prompts_py():
    prompts_path = Path(__file__).resolve().parent.parent / "aura" / "prompts.py"
    source = prompts_path.read_text(encoding="utf-8")

    for source_id in CONTRACT_IDS + SCOPED_PACK_IDS + RESEARCH_PACK_IDS:
        assert source_id not in source


def test_context_ledger_serialization_is_deterministic_plain_data(tmp_path):
    composed = compose_system_prompt(RuntimeRole.WORKER, "", tmp_path)
    serialized = serialize_context_ledger(composed.ledger)

    # Filter out extra individual_skill entries added by the gearbox
    main_serialized = [entry for entry in serialized if entry["kind"] != "individual_skill"]
    assert [entry["source_id"] for entry in main_serialized] == [
        source.source_id for source in iter_registered_sources()
    ]
    assert serialized == serialize_context_ledger(composed.ledger)
    assert set(serialized[0]) == {
        "source_id",
        "kind",
        "role",
        "included",
        "reason",
        "char_count",
    }
    assert serialized[0]["role"] == "worker"


def test_context_ledger_serialization_keeps_skips_and_errors(tmp_path):
    composed = compose_system_prompt(RuntimeRole.WORKER, "", tmp_path)
    serialized = serialize_context_ledger(composed.ledger)

    skipped = [entry for entry in serialized if not entry["included"]]
    assert skipped
    assert any(entry["source_id"] == "planner_dispatch_contract" for entry in skipped)
    assert all("reason" in entry for entry in skipped)

    errored = serialize_context_ledger(
        [
            ContextLedgerEntry(
                source_id="broken_source",
                kind="workspace_file",
                role=RuntimeRole.PLANNER,
                reason="failed",
                included=False,
                char_count=0,
                error="RuntimeError: boom",
            )
        ]
    )
    assert errored[0]["error"] == "RuntimeError: boom"


def test_serialized_metadata_keeps_skipped_scoped_packs_with_reasons(tmp_path):
    composed = compose_system_prompt(
        RuntimeRole.WORKER,
        "",
        tmp_path,
        task_kind="bugfix",
        target_files=("aura/gui/main_window.py",),
    )
    metadata = context_gearbox_metadata(composed.ledger)
    scoped = [
        entry
        for entry in metadata["ledger"]
        if entry["source_id"] in SCOPED_PACK_IDS
    ]

    assert [entry["source_id"] for entry in scoped] == SCOPED_PACK_IDS
    skipped_scoped = [entry for entry in scoped if not entry["included"]]
    assert [entry["source_id"] for entry in skipped_scoped] == [
        "drone_rules",
        "provider_rules",
        "build_pipeline_rules",
    ]
    assert [entry["reason"] for entry in skipped_scoped] == [
        "target files do not match drone scope",
        "target files do not match provider scope",
        "target files do not match build scope",
    ]


def test_context_ledger_summary_counts_loaded_and_skipped(tmp_path):
    composed = compose_system_prompt(RuntimeRole.WORKER, "", tmp_path)
    metadata = context_gearbox_metadata(composed.ledger)
    summary = metadata["summary"]

    loaded = [entry for entry in metadata["ledger"] if entry["included"]]
    skipped = [entry for entry in metadata["ledger"] if not entry["included"]]
    assert summary["loaded_count"] == len(loaded)
    assert summary["skipped_count"] == len(skipped)
    assert summary["loaded"] == [entry["source_id"] for entry in loaded]
    assert summary["skipped"] == [
        {"source_id": entry["source_id"], "reason": entry["reason"]}
        for entry in skipped
    ]
    assert summary["display"] == (
        f"Context: {len(loaded)} loaded, {len(skipped)} skipped"
    )
    assert summarize_context_ledger(metadata["ledger"]) == summary


def test_context_gearbox_summary_counts_include_scoped_packs(tmp_path):
    composed = compose_system_prompt(
        RuntimeRole.WORKER,
        "",
        tmp_path,
        task_kind="bugfix",
        target_files=("aura/gui/main_window.py",),
    )
    metadata = context_gearbox_metadata(composed.ledger)
    summary = metadata["summary"]

    assert "gui_rules" in summary["loaded"]
    # loaded_count includes the original 7 plus individual_skill entries
    assert summary["loaded_count"] >= 7
    assert summary["skipped_count"] == 7
    assert {
        "source_id": "drone_rules",
        "reason": "target files do not match drone scope",
    } in summary["skipped"]
    assert "skill_pack" in summary["loaded"]
    assert "loaded" in summary["display"]
    assert "skipped" in summary["display"]


def test_context_gearbox_metadata_exposes_no_prompt_or_context_text(tmp_path):
    (tmp_path / "project_rules.md").write_text(
        "SECRET PROJECT RULE TEXT",
        encoding="utf-8",
    )
    composed = compose_system_prompt(RuntimeRole.PLANNER, "", tmp_path)
    payload = json.dumps(context_gearbox_metadata(composed.ledger), sort_keys=True)

    assert "SECRET PROJECT RULE TEXT" not in payload
    assert "Core kernel:" not in payload
    assert "Planner role:" not in payload
    assert composed.context_text
    assert composed.system_prompt


def test_context_gearbox_metadata_serializes_utility(tmp_path, monkeypatch):
    from aura.skills.utility import SourceUtility

    monkeypatch.setattr(
        "aura.skills.utility.derive_source_utility",
        lambda workspace_root: {
            "code_quality_contract": SourceUtility(
                source_id="code_quality_contract",
                task_kind="bugfix",
                loaded_n=10,
                not_loaded_n=12,
                lift=0.25,
                status="measured",
            )
        },
    )
    composed = compose_system_prompt(RuntimeRole.WORKER, "", tmp_path)

    metadata = context_gearbox_metadata(composed.ledger, workspace_root=tmp_path)

    assert metadata["utility"] == {
        "code_quality_contract": {
            "source_id": "code_quality_contract",
            "task_kind": "bugfix",
            "loaded_n": 10,
            "not_loaded_n": 12,
            "lift": 0.25,
            "status": "measured",
        }
    }
    json.dumps(metadata, sort_keys=True)


def test_context_gearbox_display_formats_serialized_utility():
    metadata = {
        "summary": {"display": "Context: 1 loaded, 0 skipped"},
        "utility": {
            "code_quality_contract": {
                "source_id": "code_quality_contract",
                "task_kind": "bugfix",
                "loaded_n": 10,
                "not_loaded_n": 12,
                "lift": 0.25,
                "status": "measured",
            },
            "skill_pack": {
                "source_id": "skill_pack",
                "task_kind": "bugfix",
                "loaded_n": 2,
                "not_loaded_n": 12,
                "lift": None,
                "status": "insufficient",
            },
        },
    }

    lines = format_context_gearbox_display(metadata)

    assert lines[-1] == (
        "Utility: code_quality_contract +25.0% "
        "(loaded=10, not_loaded=12) | "
        "skill_pack insufficient (loaded=2, not_loaded=12)"
    )


def test_planner_refresh_logs_compact_context_summary(tmp_path, caplog):
    state = PlannerRefreshState()
    state.configure("", tmp_path)
    history = History()

    with caplog.at_level("INFO", logger="aura.conversation.planner_refresh"):
        state.refresh_tier1_after_writes(history)

    assert history.system_prompt
    assert "planner_context_refresh_summary Context:" in caplog.text
