from __future__ import annotations

from pathlib import Path

from aura.context_gearbox.models import ComposedContext, RuntimeRole
from aura.context_gearbox.runtime import compose_system_prompt
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


def _included_contract_ids(composed: ComposedContext) -> list[str]:
    return [
        entry.source_id
        for entry in composed.ledger
        if entry.kind == "quality_contract" and entry.included
    ]


def test_prompt_compatibility_exports_work(tmp_path):
    assert PLANNER_SYSTEM_PROMPT
    assert WORKER_SYSTEM_PROMPT
    assert SINGLE_SYSTEM_PROMPT
    assert inject_tier1_context("A {TIER1_CONTEXT} B", "ctx") == "A ctx B"
    assert isinstance(build_tier1_context(tmp_path), str)
    assert "worker_execution_contract" in build_tier1_context(tmp_path, mode="worker")


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


def test_contract_ledger_order_is_deterministic_and_records_skips(tmp_path):
    composed = compose_system_prompt(RuntimeRole.PLANNER, "", tmp_path)

    contract_entries = [
        entry for entry in composed.ledger if entry.kind == "quality_contract"
    ]
    assert [entry.source_id for entry in contract_entries] == CONTRACT_IDS
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

    for source_id in CONTRACT_IDS:
        assert source_id not in source
