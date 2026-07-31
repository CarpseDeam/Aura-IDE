"""The composed SINGLE prompt states each generic rule exactly once.

Circular production planning came from the same instruction arriving three or
four times in one prompt — read-before-editing, focused validation, and the
honest receipt each had several owners, and none of them told the agent to stop
reconsidering a decision the repository already supported.  These tests pin the
consolidated contract and the single-owner split.
"""
from __future__ import annotations

import re
from pathlib import Path

from aura.context_gearbox import sources
from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import compose_system_prompt
from aura.roles import load_bundled_role_capsule


def _coding_workspace(tmp_path: Path) -> Path:
    gui = tmp_path / "aura" / "gui"
    gui.mkdir(parents=True)
    (gui / "main_window.py").write_text("WINDOW = 1\n", encoding="utf-8")
    return tmp_path


def _single_prompt(workspace_root: Path) -> str:
    return compose_system_prompt(
        RuntimeRole.SINGLE,
        None,
        workspace_root,
        task_kind="bugfix",
        target_files=("aura/gui/main_window.py",),
        content="fix the gui button",
    ).system_prompt


def _capsule_text() -> str:
    capsule = load_bundled_role_capsule(RuntimeRole.SINGLE)
    assert capsule is not None
    return capsule.content


# ── the production contract ─────────────────────────────────────────────────


def test_single_capsule_states_the_five_phase_production_contract() -> None:
    text = _capsule_text()

    for phase in ("DISCOVER:", "DECIDE:", "IMPLEMENT:", "VALIDATE:", "REPORT:"):
        assert phase in text, f"production contract missing {phase}"

    # Ordered, so the capsule reads as one pass rather than a menu.
    positions = [
        text.index(phase)
        for phase in ("DISCOVER:", "DECIDE:", "IMPLEMENT:", "VALIDATE:", "REPORT:")
    ]
    assert positions == sorted(positions)


def test_single_capsule_forbids_reopening_settled_decisions() -> None:
    text = _capsule_text().lower()

    assert "do not reopen or restate it" in text
    assert "new tool output contradicts it" in text
    assert "do not narrate internal deliberation" in text
    assert "full proposed patch before editing" in text
    assert "current action, genuinely new evidence, and the immediate next action" in text
    assert "edit within one or two tool calls" in text
    assert "must answer a named unresolved question" in text
    assert "batch independent reads" in text
    assert "stop searching once the owner and the edit surface are known" in text


def test_anti_circling_rules_reach_the_composed_production_prompt(tmp_path: Path) -> None:
    prompt = _single_prompt(_coding_workspace(tmp_path))

    assert "DISCOVER: read enough to identify the owner and constraints." in prompt
    assert "do not reopen or restate it" in prompt


def test_production_contract_is_not_a_phase_state_machine() -> None:
    """The contract stays a compact capsule, not a planner/worker workflow."""
    text = _capsule_text()

    assert "dispatch_to_worker" not in text
    assert "Work Artifact" not in text
    assert len(text) < 6000


# ── one authoritative owner per generic rule ────────────────────────────────


def _count(prompt: str, pattern: str) -> int:
    return len(re.findall(pattern, prompt, flags=re.IGNORECASE))


def test_focused_validation_has_one_owner_in_the_composed_prompt(tmp_path: Path) -> None:
    prompt = _single_prompt(_coding_workspace(tmp_path))

    assert "### validation_selection_contract" in prompt
    # Choosing *which* check to run is stated once, by the validation contract.
    assert _count(prompt, r"discover it rather than assuming") == 1
    assert "Keep validation focused on the changed surface" not in prompt
    assert "Validate UI-adjacent changes with focused tests" not in prompt


def test_receipt_honesty_has_one_owner_in_the_composed_prompt(tmp_path: Path) -> None:
    prompt = _single_prompt(_coding_workspace(tmp_path))

    assert "### receipt_contract" in prompt
    assert _count(prompt, r"never claim checks that were not run") == 1
    assert _count(prompt, r"verified by <command>") == 1


def test_read_before_editing_has_one_owner_in_the_composed_prompt(tmp_path: Path) -> None:
    prompt = _single_prompt(_coding_workspace(tmp_path))

    # The kernel owns "do not claim what you have not read"; the capsule's
    # DISCOVER phase owns "read enough to find the owner".  Neither is
    # re-paraphrased by the coding contracts.
    assert "Read files before making claims about repository contents." in prompt
    assert "Read them before editing" not in prompt
    assert "Do not describe the repository from memory" not in prompt


def test_scoped_packs_are_not_duplicated_by_bundled_skills(tmp_path: Path) -> None:
    prompt = _single_prompt(_coding_workspace(tmp_path))

    assert "### gui_rules" in prompt
    assert "### GUI Work Skill" not in prompt
    assert _count(prompt, r"Preserve existing signal wiring and data flow") == 1


def test_scoped_pack_rules_are_scope_specific_not_generic_restatements() -> None:
    assert "avoid broad rewrites" not in sources.GUI_RULES
    assert "Prefer narrow edits over broad rewrites" in sources.CODE_QUALITY_CONTRACT


def test_response_discipline_does_not_restate_progress_message_shape(
    tmp_path: Path,
) -> None:
    prompt = _single_prompt(_coding_workspace(tmp_path))

    assert "Response discipline:" in prompt
    assert "emphasize target, decision, next step, and validation" not in prompt


# ── preserved behaviour ─────────────────────────────────────────────────────


def test_single_still_loads_per_turn_skills_and_project_rules(tmp_path: Path) -> None:
    root = _coding_workspace(tmp_path)
    (root / "project_rules.md").write_text("Never touch the vendor tree.\n", encoding="utf-8")
    skill_dir = root / ".aura" / "skills" / "authored" / "gui_thread"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ntask_kinds: [bugfix]\npath_globs: [\"aura/gui/\"]\n---\n"
        "Marshal cross-thread widget updates through signals.\n",
        encoding="utf-8",
    )

    composed = compose_system_prompt(
        RuntimeRole.SINGLE,
        None,
        root,
        task_kind="bugfix",
        target_files=("aura/gui/main_window.py",),
        content="fix the gui button",
    )

    assert "Never touch the vendor tree." in composed.system_prompt
    assert "Marshal cross-thread widget updates through signals." in composed.system_prompt
    loaded = {entry.source_id for entry in composed.ledger if entry.included}
    assert {"core_kernel", "project_rules", "skill_pack"} <= loaded


def test_custom_prompts_remain_additive_over_the_contract(tmp_path: Path) -> None:
    composed = compose_system_prompt(
        RuntimeRole.SINGLE,
        "Always prefer the Qt signal path.",
        _coding_workspace(tmp_path),
        task_kind="bugfix",
        target_files=("aura/gui/main_window.py",),
        content="fix the gui button",
    )

    assert "### Custom Instructions" in composed.system_prompt
    assert "Always prefer the Qt signal path." in composed.system_prompt
    assert "DISCOVER: read enough to identify the owner and constraints." in composed.system_prompt


def test_single_remains_the_implementer(tmp_path: Path) -> None:
    prompt = _single_prompt(_coding_workspace(tmp_path))

    assert "Never dispatch implementation to another coding model or agent." in prompt
    assert "### planner_dispatch_contract" not in prompt
    assert "### worker_execution_contract" not in prompt
