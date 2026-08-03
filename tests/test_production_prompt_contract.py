"""Production SINGLE gets one compact authoritative contract, and only that.

Extreme reasoning verbosity in production came from the prompt itself: a
procedural constitution assembled from a role capsule, a response-discipline
block, three generic coding contracts, and a 300-line repository outline.  Every
concept the agent needed had two or three owners, so the model re-derived which
one governed before each act — reconstructing the task, restating structure,
redrafting files, and re-checking whether it had already validated.

Adding more anti-circling prose to that pile is what failed for months.  These
tests pin the opposite: one owner per rule, a bounded repository contribution,
and a materially smaller effective prompt.  They test composition mechanics —
what text reaches the model and how much of it — never whether prose "sounds"
repetitive.
"""
from __future__ import annotations

import re
from pathlib import Path

from aura.context_gearbox import sources
from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import (
    FULL_REPLACEMENT_MARKER,
    build_context_text,
    compose_system_prompt,
    diagnose_custom_prompt,
    format_custom_prompt_diagnostics,
    format_prompt_composition,
)
from aura.roles import load_bundled_role_capsule

# The composed default SINGLE prompt measured on this repository before the
# consolidation: 25,594 characters, of which the repository outline alone was
# 16,856 and the duplicated instruction blocks another 2,207.
BASELINE_PROMPT_CHARS = 25_594


def _coding_workspace(tmp_path: Path) -> Path:
    gui = tmp_path / "aura" / "gui"
    gui.mkdir(parents=True)
    (gui / "main_window.py").write_text("WINDOW = 1\n", encoding="utf-8")
    return tmp_path


def _single_prompt(workspace_root: Path) -> str:
    return _single_composed(workspace_root).system_prompt


def _single_composed(workspace_root: Path, custom: str | None = None):
    return compose_system_prompt(
        RuntimeRole.SINGLE,
        custom,
        workspace_root,
        model="deepseek-reasoner",
        task_kind="implementation",
        target_files=("aura/gui/main_window.py",),
        content="fix the gui button",
    )


def _capsule_text() -> str:
    capsule = load_bundled_role_capsule(RuntimeRole.SINGLE)
    assert capsule is not None
    return capsule.content


def _count(prompt: str, pattern: str) -> int:
    return len(re.findall(pattern, prompt, flags=re.IGNORECASE))


# ── 1. one compact authoritative production contract ────────────────────────


def test_the_capsule_owns_the_whole_durable_production_loop() -> None:
    """Every behaviour the normal coding loop needs is stated by one owner."""
    text = _capsule_text().lower()

    durable = {
        "own the request end to end": "you own one request end to end",
        "scope preserved": "keep the user's intent",
        "minimum evidence, then act": "minimum evidence you need for the next concrete action",
        "make the change": "when the request needs a repository change, make it",
        "act over speculative inspection": (
            "prefer acting and correcting from real tool output over more looking"
        ),
        "continue after writes": "keep going after a write",
        "recover from failures": "correct the approach, and act again",
        "focused validation": "run the focused validation",
        "truthful result": "finish with a compact receipt",
        "structured outcomes": "report_already_satisfied",
    }
    for behaviour, clause in durable.items():
        assert clause in text, f"the capsule no longer owns {behaviour}"


def test_the_capsule_carries_no_inspection_first_language() -> None:
    """The prompt no longer teaches the behaviour the runtime then punished.

    "Inspect until", hunting an authoritative owner, proving blast radius,
    gathering enough certainty, named discovery phases, and procedural read
    discipline are all gone — a conventional coding agent is told to act on the
    minimum evidence its next step needs.
    """
    text = _capsule_text().lower()

    for retired in (
        "inspect until",
        "authoritative owner",
        "blast radius",
        "enough certainty",
        "gather enough",
        "discovery phase",
        "batch independent reads",
        "named unresolved question",
        "settled decision",
        "before you edit",
        "before editing",
        "understand once",
        "read discipline",
    ):
        assert retired not in text, f"inspection-first language is back: {retired!r}"


def test_the_contract_stays_a_capsule_not_a_procedural_manual() -> None:
    """Less prompt is the repair. A capsule that regrows is the defect."""
    text = _capsule_text()

    assert len(text) < 2_600, f"the SINGLE capsule regrew to {len(text)} chars"
    # No phase ladder, no checkpoint alternation, no second orchestration layer.
    for retired in (
        "DISCOVER:",
        "DECIDE:",
        "IMPLEMENT:",
        "VALIDATE:",
        "REPORT:",
        "decision checkpoint",
        "commit_implementation_decision",
        "continue_implementation_discovery",
        "dispatch_to_worker",
        "Work Artifact",
    ):
        assert retired not in text, f"retired ceremony {retired!r} is back"


def test_the_capsule_imposes_no_call_or_reasoning_budget() -> None:
    """A budget contradicts the runtime and makes the model stop early silently."""
    text = _capsule_text().lower()

    for budget in (
        "one or two tool calls",
        "two tool calls",
        "at most",
        "no more than",
        "stop after",
        "reasoning budget",
        "token budget",
    ):
        assert budget not in text, f"a budget is back in the capsule: {budget!r}"


def test_single_remains_the_implementer(tmp_path: Path) -> None:
    prompt = _single_prompt(_coding_workspace(tmp_path))

    assert "Never dispatch implementation to another model or agent." in prompt
    assert "### planner_dispatch_contract" not in prompt
    assert "### worker_execution_contract" not in prompt


# ── 2. no concept has a second owner ────────────────────────────────────────


def test_the_generic_contract_blocks_no_longer_reach_single(tmp_path: Path) -> None:
    """The capsule owns code shape, validation, and receipts, so the separate
    injected copies are gone rather than merely reworded."""
    composed = _single_composed(_coding_workspace(tmp_path))

    for block in (
        "### code_quality_contract",
        "### validation_selection_contract",
        "### receipt_contract",
        "Response discipline:",
    ):
        assert block not in composed.system_prompt, f"{block} still reaches SINGLE"

    included = {entry.source_id for entry in composed.ledger if entry.included}
    for source_id in (
        "code_quality_contract",
        "validation_selection_contract",
        "receipt_contract",
    ):
        assert source_id not in included


def test_worker_still_receives_the_generic_contracts(tmp_path: Path) -> None:
    """Consolidation is a SINGLE-role change; the legacy Worker path is intact."""
    context = build_context_text(
        RuntimeRole.WORKER,
        _coding_workspace(tmp_path),
        task_kind="implementation",
        target_files=("aura/gui/main_window.py",),
    )

    included = {entry.source_id for entry in context.ledger if entry.included}
    assert {
        "code_quality_contract",
        "validation_selection_contract",
        "receipt_contract",
    } <= included


def test_each_durable_concept_has_exactly_one_owner(tmp_path: Path) -> None:
    """The composed prompt states each rule once, counted over the whole text."""
    prompt = _single_prompt(_coding_workspace(tmp_path))

    single_owner = (
        # verify before claiming — the core kernel
        r"Do not make claims about repository contents you have not verified",
        # workspace scope — the core kernel
        r"Work inside the selected workspace",
        # scope preservation — the capsule
        r"No speculative cleanup",
        # act over speculative inspection — the capsule
        r"Prefer acting and correcting from real tool output",
        # make the change — the capsule
        r"When the request needs a repository change, make it",
        # code shape — the capsule
        r"reads like its neighbours",
        # failure recovery — the capsule
        r"correct the approach, and act again",
        # validation selection — the capsule
        r"the focused validation this repository actually runs",
        # receipt honesty — the capsule
        r"Never claim a check that did not run",
        # live TODO — the capsule
        r"update_worker_todo",
        # target-file read rule — the manifest block
        r"read each file you will edit with the read tools",
    )
    for pattern in single_owner:
        assert _count(prompt, pattern) == 1, f"{pattern!r} has {_count(prompt, pattern)} owners"


def test_scoped_packs_stay_scope_specific_instead_of_restating_the_capsule(
    tmp_path: Path,
) -> None:
    """A scoped pack earns its place only by saying something the capsule cannot."""
    prompt = _single_prompt(_coding_workspace(tmp_path))

    assert "### gui_rules" in prompt
    assert _count(prompt, r"Preserve existing signal wiring and data flow") == 1
    # Generic coaching belongs to the capsule, never to a scoped pack.
    for generic in ("avoid broad rewrites", "no placeholders", "be concise"):
        assert generic not in sources.GUI_RULES.lower()


# ── 3. an empty custom prompt stays empty ───────────────────────────────────


def test_empty_custom_prompt_contributes_nothing(tmp_path: Path) -> None:
    """The persisted default is empty and must add no header and no characters."""
    root = _coding_workspace(tmp_path)

    for empty in (None, "", "   \n  "):
        composed = _single_composed(root, empty)
        assert "### Custom Instructions" not in composed.system_prompt

    assert _single_composed(root, None).system_prompt == _single_composed(root, "").system_prompt

    diagnostics = diagnose_custom_prompt(RuntimeRole.SINGLE, "   ")
    assert diagnostics.is_empty
    assert diagnostics.char_count == 0
    assert diagnostics.appended_char_count == 0


def test_custom_prompts_remain_additive_over_the_contract(tmp_path: Path) -> None:
    composed = _single_composed(_coding_workspace(tmp_path), "Always prefer the Qt signal path.")

    assert "### Custom Instructions" in composed.system_prompt
    assert "Always prefer the Qt signal path." in composed.system_prompt
    assert "When the request needs a repository change, make it." in composed.system_prompt


# ── 4-5. project rules and the target-file manifest survive ─────────────────


def test_project_rules_remain_present(tmp_path: Path) -> None:
    """User/repository policy is not derivable from anything else in the prompt."""
    root = _coding_workspace(tmp_path)
    (root / "project_rules.md").write_text("Never touch the vendor tree.\n", encoding="utf-8")

    composed = _single_composed(root)

    assert "### Project Rules" in composed.system_prompt
    assert "Never touch the vendor tree." in composed.system_prompt


def test_target_file_manifest_stays_a_compact_path_list(tmp_path: Path) -> None:
    """Naming many real files must not inject their bodies into the prompt."""
    root = _coding_workspace(tmp_path)
    for i in range(20):
        (root / "aura" / "gui" / f"mod_{i}.py").write_text("x" * 10_000, encoding="utf-8")
    targets = tuple(f"aura/gui/mod_{i}.py" for i in range(20))

    composed = compose_system_prompt(
        RuntimeRole.SINGLE,
        None,
        root,
        task_kind="implementation",
        target_files=targets,
        content="fix the batch",
    )

    assert all(f"aura/gui/mod_{i}.py" in composed.system_prompt for i in range(20))
    assert "x" * 10_000 not in composed.system_prompt
    assert "Contents are not preloaded" in composed.system_prompt
    manifest = next(
        entry for entry in composed.ledger if entry.source_id == "target_file_contents"
    )
    assert manifest.char_count < 1_500


# ── 6-7. progressive disclosure still works, bodies stay out ────────────────


def test_skill_index_discloses_progressively(tmp_path: Path) -> None:
    root = _coding_workspace(tmp_path)
    skill_dir = root / ".aura" / "skills" / "authored" / "gui_thread"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ntask_kinds: [implementation]\npath_globs: [\"aura/gui/\"]\n---\n"
        "Marshal cross-thread widget updates through signals.\n"
        "Never touch the widget from a non-GUI thread.\n",
        encoding="utf-8",
    )

    composed = _single_composed(root)

    # The one-line description is indexed; the body is not preloaded.
    assert "Marshal cross-thread widget updates through signals." in composed.system_prompt
    assert "Never touch the widget from a non-GUI thread." not in composed.system_prompt
    assert "load_skills" in composed.system_prompt
    loaded = {entry.source_id for entry in composed.ledger if entry.included}
    assert {"core_kernel", "skill_pack"} <= loaded


def test_eager_guards_are_bounded_and_point_at_load_skills(tmp_path: Path) -> None:
    """A learned hazard guard stays eager but contributes a warning, not a manual."""
    from aura.skills.models import Skill, SkillProvenance
    from aura.skills.text import _EAGER_GUARD_CHAR_CAP, eager_guard_text

    def guard(text: str) -> Skill:
        return Skill(
            text=text,
            task_kinds=("implementation",),
            path_globs=(),
            model=None,
            provenance=SkillProvenance.FAILURE_GRADUATED,
            origin=(),
        )

    long_guard = guard(
        "Never call queue_free() twice.\n" + "Then do the long procedure.\n" * 100
    )
    injected = eager_guard_text(long_guard)

    assert "Never call queue_free() twice." in injected
    assert len(injected) < _EAGER_GUARD_CHAR_CAP + 200
    assert len(injected) < len(long_guard.text) * 0.5
    assert "load_skills" in injected

    assert eager_guard_text(guard("Never call queue_free() twice.")) == (
        "Never call queue_free() twice."
    )


# ── 8-9. bounded repository structure and a smaller effective prompt ────────


def test_repository_structure_contribution_is_bounded(tmp_path: Path) -> None:
    """SINGLE gets a directory index, not a truncated 300-line file outline."""
    root = tmp_path
    for package in range(12):
        pkg = root / "aura" / f"pkg_{package}"
        pkg.mkdir(parents=True)
        for module in range(12):
            (pkg / f"mod_{module}.py").write_text(
                "\n".join(f"def fn_{n}(a, b, c):\n    return a\n" for n in range(20)),
                encoding="utf-8",
            )

    composed = compose_system_prompt(
        RuntimeRole.SINGLE,
        None,
        root,
        task_kind="implementation",
        target_files=("aura/pkg_0/mod_0.py",),
        content="fix it",
    )
    repo_entry = next(entry for entry in composed.ledger if entry.source_id == "repo_map")

    assert repo_entry.included
    assert "### Repository layout" in composed.system_prompt
    # Directory names survive; per-file signatures do not.
    assert "aura/pkg_0/" in composed.system_prompt
    assert "def fn_0(a, b, c)" not in composed.system_prompt
    assert repo_entry.char_count < 3_000, (
        f"the repository block is back to a per-turn tax ({repo_entry.char_count} chars)"
    )


def test_effective_prompt_is_materially_smaller_than_the_baseline() -> None:
    """Measured against the real workspace this repair was diagnosed on."""
    composed = compose_system_prompt(
        RuntimeRole.SINGLE,
        None,
        Path(__file__).resolve().parent.parent,
        model="deepseek-reasoner",
        task_kind="implementation",
        target_files=("aura/gui/worker_handler.py",),
        content="add a godot scene export step to the drone runner",
    )

    assert len(composed.system_prompt) < BASELINE_PROMPT_CHARS * 0.4, (
        f"effective prompt is {len(composed.system_prompt)} chars against a "
        f"{BASELINE_PROMPT_CHARS}-char baseline"
    )


def test_composition_diagnostic_reports_every_block_it_charged_for(
    tmp_path: Path,
) -> None:
    """The one live line that makes a regrowing block visible the turn it happens."""
    composed = _single_composed(_coding_workspace(tmp_path))

    line = format_prompt_composition(composed)

    assert line.isascii(), "the composition line reaches Windows console handlers"
    assert "role=single" in line
    assert f"total={len(composed.system_prompt):,}" in line
    assert "role_capsule=" in line
    assert "core_kernel=186" in line
    # Excluded sources are not charged for.
    assert "code_quality_contract=" not in line
    for entry in composed.ledger:
        if entry.included and entry.kind != "individual_skill":
            assert f"{entry.source_id}={entry.char_count:,}" in line


# ── custom prompt diagnostics ───────────────────────────────────────────────


def test_edited_copy_of_the_default_reports_tiny_appended_size() -> None:
    edited = _capsule_text() + "\nPrefer QSignalSpy in tests.\n"

    diagnostics = diagnose_custom_prompt(RuntimeRole.SINGLE, edited)

    assert diagnostics.char_count > 1_000
    assert diagnostics.appended_char_count < 200
    assert not diagnostics.full_replacement


def test_full_replacement_marker_skips_concept_probing() -> None:
    custom = f"{FULL_REPLACEMENT_MARKER}You decide everything, Aura."
    diagnostics = diagnose_custom_prompt(RuntimeRole.SINGLE, custom)

    assert diagnostics.full_replacement
    assert diagnostics.repeated_concepts == ()


def test_legacy_vocabulary_is_flagged_but_the_current_todo_tool_is_not() -> None:
    diagnostics = diagnose_custom_prompt(
        RuntimeRole.SINGLE, "We use dispatch_to_worker and update_worker_todo."
    )

    assert "dispatch_to_worker" in diagnostics.legacy_terms
    assert not any("update_worker_todo" in term for term in diagnostics.legacy_terms)


def test_diagnostics_output_is_pure_ascii() -> None:
    diagnostics = diagnose_custom_prompt(RuntimeRole.SINGLE, "Üse à custom prompt — don't.")

    line = format_custom_prompt_diagnostics(diagnostics, effective_prompt_chars=5_540)
    assert line.isascii()
    assert "Üse" not in line
