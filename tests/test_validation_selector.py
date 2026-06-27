"""Tests for aura.validation.selector — deterministic validation selection."""

from __future__ import annotations

import json
from pathlib import Path

from aura.validation.selector import ValidationPlan, select_validation_plan


def _plan(
    target_files: list[str] | None = None,
    changed_files: list[str] | None = None,
    task_kind: str = "unknown",
    context_gearbox: dict | None = None,
) -> ValidationPlan:
    return select_validation_plan(
        target_files=target_files or [],
        changed_files=changed_files,
        task_kind=task_kind,
        context_gearbox=context_gearbox,
    )


# ── GUI validation ────────────────────────────────────────────────────


def test_gui_files_select_gui_plan():
    plan = _plan(target_files=["aura/gui/main_window.py"])
    assert plan["kind"] == "gui"
    assert plan["confidence"] == "focused"
    assert "python -m compileall aura/gui" in plan["commands"]
    assert "python -m aura --selfcheck" in plan["commands"]


def test_gui_assets_select_gui_plan():
    plan = _plan(target_files=["aura/assets/icons.py"])
    assert plan["kind"] == "gui"
    assert plan["confidence"] == "focused"


# ── Drone validation ──────────────────────────────────────────────────


def test_drone_files_select_drone_plan():
    plan = _plan(target_files=["aura/drones/web_research.py"])
    assert plan["kind"] == "drone"
    assert plan["confidence"] == "focused"
    assert "python -m compileall aura/drones" in plan["commands"]
    assert "python -m aura --selfcheck" in plan["commands"]


# ── Provider / backend / client validation ────────────────────────────


def test_provider_files_select_provider_plan():
    plan = _plan(target_files=["aura/providers/deepseek.py"])
    assert plan["kind"] == "provider"
    assert plan["confidence"] == "focused"
    assert "python -m compileall aura/providers aura/backends aura/client" in plan["commands"]
    assert "python -m aura --selfcheck" in plan["commands"]


def test_backend_files_select_provider_plan():
    plan = _plan(target_files=["aura/backends/api.py"])
    assert plan["kind"] == "provider"


def test_client_files_select_provider_plan():
    plan = _plan(target_files=["aura/client/deepseek.py"])
    assert plan["kind"] == "provider"


# ── Build validation ──────────────────────────────────────────────────


def test_build_script_select_build_plan():
    plan = _plan(target_files=["scripts/build_nuitka.py"])
    assert plan["kind"] == "build"
    assert plan["confidence"] == "focused"
    assert "python -m compileall scripts/" in plan["commands"]
    assert "python -m aura --selfcheck" in plan["commands"]
    assert plan["skipped"] == [
        "packaging build skipped \u2014 use --package explicitly to run full build"
    ]


def test_pyproject_select_build_plan():
    plan = _plan(target_files=["pyproject.toml"])
    assert plan["kind"] == "build"


# ── General Python validation ─────────────────────────────────────────


def test_generic_python_selects_general_plan():
    plan = _plan(target_files=["aura/utils.py"])
    assert plan["kind"] == "general_python"
    assert plan["confidence"] == "general"
    assert "python -m compileall aura" in plan["commands"]
    assert "python -m aura --selfcheck" in plan["commands"]


# ── Not applicable ────────────────────────────────────────────────────


def test_doc_only_plan_is_not_applicable():
    plan = _plan(target_files=["docs/notes.md", "README.md"])
    assert plan["kind"] == "not_applicable"
    assert plan["confidence"] == "skipped"
    assert plan["commands"] == []
    assert plan["skipped"] == [
        "validation not applicable \u2014 no Python files changed"
    ]


# ── Context gearbox influence ─────────────────────────────────────────


def test_context_pack_can_influence():
    """A loaded context pack can steer the plan even without matching paths."""
    plan = _plan(
        target_files=["aura/utils.py"],
        context_gearbox={"summary": {"loaded": ["drone_rules"]}},
    )
    assert plan["kind"] == "drone"


def test_path_evidence_wins_over_vague_task_kind():
    """Matching file paths should always win over a vague task_kind."""
    plan = _plan(
        target_files=["aura/gui/x.py"],
        task_kind="refactor",
    )
    assert plan["kind"] == "gui"


# ── Deduplication ─────────────────────────────────────────────────────


def test_duplicate_commands_deduped():
    """Multiple files in the same lane produce deduped commands."""
    plan = _plan(target_files=["aura/gui/a.py", "aura/gui/b.py"])
    assert plan["kind"] == "gui"
    # Two unique commands: compileall + selfcheck
    assert len(plan["commands"]) == 2
    seen = set()
    for cmd in plan["commands"]:
        seen.add(cmd)
    assert len(seen) == len(plan["commands"])


# ── Serialization ─────────────────────────────────────────────────────


def test_plan_serializes_as_plain_data():
    """The returned plan must be JSON-serializable."""
    plan = _plan(target_files=["aura/gui/main_window.py"])
    encoded = json.dumps(plan, sort_keys=True)
    assert isinstance(encoded, str)
    decoded = json.loads(encoded)
    assert decoded["kind"] == "gui"
    assert decoded["confidence"] == "focused"


# ── No prompt text leak ───────────────────────────────────────────────


def test_no_prompt_text_in_selector_metadata():
    """The returned plan must not contain prompt fragments or system text."""
    plan = _plan(target_files=["aura/gui/main_window.py"])
    payload = json.dumps(plan).lower()
    assert "worker role:" not in payload
    assert "core kernel:" not in payload
    assert "code_quality_contract" not in payload
    assert "receipt_contract" not in payload
    assert "validation_selection_contract" not in payload


# ── Worker extras integration guard ───────────────────────────────────


def test_worker_extras_can_carry_validation_selector():
    """Simulate how extras dict carries validation_selector without breaking existing keys."""
    extras = {
        "writes": [],
        "errors": [],
        "caveats": [],
        "task_shape": {"task_kind": "gui_polish"},
    }
    validation_selector = select_validation_plan(
        target_files=["aura/gui/main_window.py"],
    )
    extras["validation_selector"] = validation_selector
    assert extras["validation_selector"]["kind"] == "gui"
    assert extras["validation_selector"]["confidence"] == "focused"
    # Existing keys are unchanged
    assert extras["writes"] == []
    assert extras["errors"] == []
    assert extras["task_shape"]["task_kind"] == "gui_polish"


# ── Edge cases ────────────────────────────────────────────────────────


def test_empty_target_files_falls_to_not_applicable():
    plan = _plan(target_files=[])
    assert plan["kind"] == "not_applicable"
    assert plan["commands"] == []


def test_changed_files_also_count_for_selection():
    """changed_files should be considered alongside target_files."""
    plan = _plan(
        target_files=["aura/utils.py"],
        changed_files=["aura/gui/window.py"],
    )
    assert plan["kind"] == "gui"


def test_mixed_python_and_non_python_general():
    """Mixed .py and non-.py files should select general Python if no
    scoped lane matches."""
    plan = _plan(target_files=["aura/utils.py", "README.md"])
    assert plan["kind"] == "general_python"
    assert "python -m compileall aura" in plan["commands"]


def test_changed_files_trigger_scoped_lane():
    """Scoped lane detection from changed_files alone."""
    plan = _plan(
        target_files=["docs/notes.md"],
        changed_files=["aura/drones/web_research.py"],
    )
    assert plan["kind"] == "drone"


def test_media_ui_file_triggers_gui():
    plan = _plan(target_files=["media/ui/screenshot.png"])
    assert plan["kind"] == "gui"


def test_media_ui_assets_triggers_gui():
    plan = _plan(target_files=["media/ui_assets/icon.svg"])
    assert plan["kind"] == "gui"


def test_nuitka_path_triggers_build():
    """nuitka/ dir at root triggers build validation via nuitka/* pattern."""
    plan = _plan(target_files=["nuitka/build_config.py"])
    assert plan["kind"] == "build"


def test_installer_path_triggers_build():
    plan = _plan(target_files=["installer/setup.nsi"])
    assert plan["kind"] == "build"


def test_aura_drone_gui_pattern():
    """Files under aura/gui/* are caught by GUI first (ordered rules)."""
    plan = _plan(target_files=["aura/gui/dronelist.py"])
    assert plan["kind"] == "gui"


def test_provider_settings_pattern():
    """Files matching aura/*provider*settings*.py trigger provider plan."""
    plan = _plan(target_files=["aura/user_provider_settings.py"])
    assert plan["kind"] == "provider"
