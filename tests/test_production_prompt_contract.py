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
from aura.context_gearbox.runtime import (
    FULL_REPLACEMENT_MARKER,
    compose_system_prompt,
    diagnose_custom_prompt,
    format_custom_prompt_diagnostics,
)
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


def test_single_capsule_states_the_four_clause_production_contract() -> None:
    text = _capsule_text()

    for clause in (
        "Identify the owner and the edit surface.",
        "Act once the choice is supported by repository evidence.",
        "Validate the changed surface, and repair failures before you hand back.",
        "Report what you actually did and what actually ran.",
    ):
        assert clause in text, f"production contract missing {clause!r}"

    # The retired five-phase ceremony is gone. A phased ladder reappearing
    # means the thrash-prone DISCOVER/DECIDE/IMPLEMENT/VALIDATE/REPORT
    # workflow is being smuggled back into the capsule.
    for phase in ("DISCOVER:", "DECIDE:", "IMPLEMENT:", "VALIDATE:", "REPORT:"):
        assert phase not in text, f"retired phase marker {phase} is back"


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

    assert "Act once the choice is supported by repository evidence." in prompt
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

    # The kernel owns the rule, in intent phrasing: verify before you claim.
    assert "Do not make claims about repository contents you have not verified" in prompt
    # The old contradiction — the blanket "read files before making claims"
    # instruction alongside an "already in context; do not re-read" exemption
    # — must not come back as two separate, unqualified halves.
    assert not (
        "Read files before making claims about repository contents." in prompt
        and "already in context; do not re-read" in prompt
    )
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


def test_each_generic_rule_has_exactly_one_owner(tmp_path: Path) -> None:
    prompt = _single_prompt(_coding_workspace(tmp_path))

    single_owner = (
        # read before claiming
        r"Do not make claims about repository contents you have not verified",
        # scope discipline
        r"Do not perform speculative cleanup",
        # response length
        r"Default to concise, useful replies",
        # anti-circling
        r"do not reopen or restate it",
        # live TODO
        r"update_worker_todo",
        # validation selection
        r"discover it rather than assuming",
        # receipt honesty
        r"never claim checks that were not run",
        # code shape
        r"Write code that reads like its neighbours",
    )
    for pattern in single_owner:
        assert _count(prompt, pattern) == 1, f"{pattern!r} has multiple owners"


def test_the_removed_contradictions_cannot_come_back(tmp_path: Path) -> None:
    prompt = _single_prompt(_coding_workspace(tmp_path))

    # "read files before making claims" must never coexist with an
    # "already in context; do not re-read" exemption.
    assert not (
        "Read files before making claims about repository contents." in prompt
        and "already in context; do not re-read" in prompt
    )
    # "match the surrounding code" and "do not imitate existing structure"
    # must not reappear as separate, unqualified rules.
    assert not (
        "match the surrounding code" in prompt
        and "do not imitate existing structure" in prompt
    )


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


def test_single_prompt_stays_compact_for_many_named_files(tmp_path: Path) -> None:
    """The production regression: naming many real files must not inject their
    bodies into the system prompt. The prompt stays a compact manifest and the
    bodies only exist on disk, reachable through the read tools."""
    root = _coding_workspace(tmp_path)
    for i in range(20):
        (root / "aura" / "gui" / f"mod_{i}.py").write_text(
            "x" * 10_000, encoding="utf-8"
        )
    targets = tuple(f"aura/gui/mod_{i}.py" for i in range(20))

    composed = compose_system_prompt(
        RuntimeRole.SINGLE,
        None,
        root,
        task_kind="bugfix",
        target_files=targets,
        content="fix the batch",
    )

    assert all(f"aura/gui/mod_{i}.py" in composed.system_prompt for i in range(20))
    assert "x" * 10_000 not in composed.system_prompt
    assert len(composed.system_prompt) < 20_000


def test_single_prompt_no_longer_claims_target_contents_are_preloaded(
    tmp_path: Path,
) -> None:
    prompt = _single_prompt(_coding_workspace(tmp_path))

    assert "already in context; do not re-read" not in prompt
    assert "contents are not preloaded" in prompt
    assert "read tools before editing it" in prompt


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
    assert "Act once the choice is supported by repository evidence." in composed.system_prompt


def test_single_remains_the_implementer(tmp_path: Path) -> None:
    prompt = _single_prompt(_coding_workspace(tmp_path))

    assert "Never dispatch implementation to another coding model or agent." in prompt
    assert "### planner_dispatch_contract" not in prompt
    assert "### worker_execution_contract" not in prompt


# ── custom prompt diagnostics ───────────────────────────────────────────────


def test_blank_custom_prompt_diagnoses_as_empty() -> None:
    diagnostics = diagnose_custom_prompt(RuntimeRole.SINGLE, "   ")

    assert diagnostics.is_empty
    assert diagnostics.char_count == 0
    assert diagnostics.appended_char_count == 0
    assert diagnostics.legacy_terms == ()
    assert diagnostics.repeated_concepts == ()


def test_edited_copy_of_the_default_reports_tiny_appended_size() -> None:
    """The settings default is the role capsule itself; deduplication should
    leave only the genuinely new lines as the appended size."""
    edited = _capsule_text() + "\nPrefer QSignalSpy in tests.\n"

    diagnostics = diagnose_custom_prompt(RuntimeRole.SINGLE, edited)

    assert diagnostics.char_count > 1_000
    assert diagnostics.appended_char_count < 200
    assert not diagnostics.full_replacement


def test_full_replacement_marker_skips_concept_probing() -> None:
    custom = f"{FULL_REPLACEMENT_MARKER}You decide everything, Aura."
    diagnostics = diagnose_custom_prompt(RuntimeRole.SINGLE, custom)

    assert diagnostics.full_replacement
    # "appended" is the whole prompt and "repeats a canonical block" is not
    # a meaningful question for a prompt that owns the entire system prompt.
    assert diagnostics.repeated_concepts == ()


def test_legacy_vocabulary_is_flagged_but_the_current_todo_tool_is_not() -> None:
    diagnostics = diagnose_custom_prompt(
        RuntimeRole.SINGLE, "We use dispatch_to_worker and update_worker_todo."
    )

    assert "dispatch_to_worker" in diagnostics.legacy_terms
    assert not any(
        "update_worker_todo" in term for term in diagnostics.legacy_terms
    )


def test_diagnostics_output_is_pure_ascii() -> None:
    """The diagnostics line goes to log handlers that may use the Windows
    console code page; a stray non-ASCII character from the custom prompt must
    never leak into it."""
    diagnostics = diagnose_custom_prompt(RuntimeRole.SINGLE, "Üse à custom prompt — don't.")

    line = format_custom_prompt_diagnostics(diagnostics, effective_prompt_chars=5_540)
    assert line.isascii()
    assert "Üse" not in line
