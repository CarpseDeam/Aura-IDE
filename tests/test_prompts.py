"""Snapshot tests for Aura system prompts.

These tests ensure that the Planner and Worker prompts maintain a consistent
contract and that required sections are present.
"""

from __future__ import annotations

from aura.conversation.tools._schemas import DIAGNOSTIC_TOOL_DEF, DISPATCH_TOOL_DEF
from aura.prompts import PLANNER_SYSTEM_PROMPT, SINGLE_SYSTEM_PROMPT, WORKER_SYSTEM_PROMPT


def test_planner_worker_contract_consistency():
    """Ensure Planner and Worker agree on the handoff format."""
    plan_nomenclature = "Builder Note"

    # 1. Planner must be instructed to provide this section
    assert plan_nomenclature in PLANNER_SYSTEM_PROMPT

    # 2. Worker must be instructed to follow this section
    assert plan_nomenclature in WORKER_SYSTEM_PROMPT

    # 3. The dispatch_to_worker tool schema must reference Builder Note style
    spec_desc = DISPATCH_TOOL_DEF["function"]["parameters"]["properties"]["spec"]["description"]
    assert "Builder Note" in spec_desc


def test_planner_required_spec_sections():
    """Ensure Planner is instructed to include all required spec headings."""
    required_sections = [
        "Goal",
        "Files",
        "Builder Note",
        "Acceptance",
    ]

    for section in required_sections:
        assert section in PLANNER_SYSTEM_PROMPT


def test_worker_doctrine_is_lean_and_action_first():
    required_rules = [
        "fewest safe tool calls",
        "Inspect only the target files or regions needed",
        "Edit as soon as the correct change is clear",
        "Do not expand scope",
        "Do not make product decisions",
        "Do not narrate obvious steps",
        "Do not say done before validation passes",
    ]

    for rule in required_rules:
        assert rule in WORKER_SYSTEM_PROMPT


def test_worker_prompt_is_patch_first():
    assert "For existing files, use `patch_file` with `expected_file_hash`" in WORKER_SYSTEM_PROMPT
    assert "Put all intended hunks for one file in one `patch_file` call when practical." in WORKER_SYSTEM_PROMPT
    assert "re-read the smallest affected region and retry once with a better hunk" in WORKER_SYSTEM_PROMPT
    assert "Do not switch tools randomly" in WORKER_SYSTEM_PROMPT
    assert "Use `write_file` only for new files or intentional full-file replacement." in WORKER_SYSTEM_PROMPT
    assert "Use `apply_edit_transaction` for existing-file code changes." not in WORKER_SYSTEM_PROMPT


def test_worker_prompt_keeps_core_edit_and_validation_rules():
    required_rules = [
        "Read before editing",
        "`read_file_outline`",
        "`read_file_range`",
        "Run exact validation commands from the handoff when provided",
        "Use the cheapest focused validation that proves the change",
        "Do not run broad tests by default",
        "Touched Python files must pass `python -m py_compile`",
        "changed files and validation results",
    ]

    for rule in required_rules:
        assert rule in WORKER_SYSTEM_PROMPT


def test_tool_schema_uses_builder_note_style():
    """Ensure the dispatch_to_worker spec uses Builder Note style, not formal sections."""
    spec_desc = DISPATCH_TOOL_DEF["function"]["parameters"]["properties"]["spec"]["description"]

    assert "Builder Note" in spec_desc
    assert "implementation handoff" in spec_desc
    assert "Do not require or default to formal sections" in spec_desc


def test_dispatch_tool_schema_exposes_optional_target_regions():
    params = DISPATCH_TOOL_DEF["function"]["parameters"]
    target_regions = params["properties"]["target_regions"]

    assert "target_regions" in params["properties"]
    assert "target_regions" not in params["required"]
    assert target_regions["type"] == "array"
    assert target_regions["items"]["properties"]["path"]["type"] == "string"
    assert target_regions["items"]["properties"]["symbol"]["type"] == "string"
    assert target_regions["items"]["properties"]["start_line"]["type"] == "integer"
    assert target_regions["items"]["properties"]["end_line"]["type"] == "integer"
    assert "read_file_outline" in target_regions["description"]
    assert "read_file_range" in target_regions["description"]
    assert "expected_file_hash" in target_regions["description"]


def test_snappy_planner_worker_rules():
    """Ensure snappy workflow and lean Worker execution rules are present."""
    # Planner
    assert "Snappy workflow" in PLANNER_SYSTEM_PROMPT
    assert "fast dispatch compiler" in PLANNER_SYSTEM_PROMPT
    assert "Inspect only the minimum repo context needed" in PLANNER_SYSTEM_PROMPT
    assert "Do not narrate reasoning" in PLANNER_SYSTEM_PROMPT

    # Worker
    assert "Worker doctrine" in WORKER_SYSTEM_PROMPT
    assert "Work loop" in WORKER_SYSTEM_PROMPT
    assert "If validation fails" in WORKER_SYSTEM_PROMPT
    assert "patch once, and rerun the exact failing gate" in WORKER_SYSTEM_PROMPT

    # Continuation report still exists
    assert "continuation_report" in WORKER_SYSTEM_PROMPT


def test_worker_validation_failure_guidance_is_one_repair_loop():
    required_rules = [
        "If validation fails, run the smallest diagnostic that reveals the actual error/value, patch once, and rerun the exact failing gate.",
        "Do not repeat the same validation command unless code changed or you are verifying the exact repair.",
        "For assertion failures, do not debate expected values.",
        "Print the actual value",
        "A normal patch failure, syntax failure, or validation failure is not a Planner mismatch unless the instructed retry also fails.",
    ]

    for rule in required_rules:
        assert rule in WORKER_SYSTEM_PROMPT


def test_worker_final_response_rule_is_compact():
    assert "When complete, say `Done.` with changed files and validation results." in WORKER_SYSTEM_PROMPT


def test_worker_continuation_report_format_is_exact():
    expected_format = """<continuation_report>
<status>needs_followup</status>
<reason>tool_limit_reached</reason>
<completed>
- ...
</completed>
<modified_files>
- ...
</modified_files>
<validation>
...
</validation>
<remaining>
- ...
</remaining>
<recommended_next_step>
...
</recommended_next_step>
</continuation_report>"""

    assert expected_format in WORKER_SYSTEM_PROMPT


def test_planner_has_concise_completion_rule():
    rule = "After Worker or built-in action completes, emit one concise final response and stop."
    assert rule in PLANNER_SYSTEM_PROMPT


def test_planner_uses_research_tool_for_current_info():
    assert "research_current_info" in PLANNER_SYSTEM_PROMPT
    assert "Cite sources explicitly" in PLANNER_SYSTEM_PROMPT
    assert "do NOT fall back to your training data" in PLANNER_SYSTEM_PROMPT
    assert "internal diagnostics" in PLANNER_SYSTEM_PROMPT
    assert "Do not use `run_diagnostic_command`, Python, shell, curl, or repo tools for web research." in PLANNER_SYSTEM_PROMPT
    assert "Do not dispatch to Worker just to research." in PLANNER_SYSTEM_PROMPT
    assert "research_current_info" not in WORKER_SYSTEM_PROMPT


def test_planner_prompt_does_not_carry_worker_quality_blocks():
    """Planner should stay lightweight; Worker owns implementation quality."""
    assert "Code quality contract" not in PLANNER_SYSTEM_PROMPT

    assert "Code quality contract" in WORKER_SYSTEM_PROMPT
    assert "app-shaped code" in WORKER_SYSTEM_PROMPT
    assert "fake architecture" in WORKER_SYSTEM_PROMPT
    assert "premature abstractions" in WORKER_SYSTEM_PROMPT


def test_code_taste_block_present():
    """Ensure the compact code taste block is in Worker and Single prompts but not Planner."""
    markers = [
        "app-shaped code",
        "Match existing project style",
        "tutorial/demo scaffolding",
        "placeholders, elisions",
        "obvious narration comments/docstrings",
        "Handle realistic failures honestly",
    ]

    for marker in markers:
        assert marker in WORKER_SYSTEM_PROMPT
        assert marker in SINGLE_SYSTEM_PROMPT
        assert marker not in PLANNER_SYSTEM_PROMPT


def test_worker_prompt_is_meaningfully_shorter():
    assert len(WORKER_SYSTEM_PROMPT) < 5000


def test_worker_prompt_removed_long_examples():
    removed_example_text = [
        "Handoff Adherence Protocol",
        "Handoff Mismatch Protocol",
        "Acceptance Verification",
        "update_todo_list",
        "Snappy execution",
        "Bad docstring-heavy helper",
        "Good direct helper",
        "Bad swallowed parse error",
        "Good clear parse failure",
        "Bad helper reporting success",
        "Good helper returning the result",
        "DroneBuildBrief only exposes",
    ]

    for text in removed_example_text:
        assert text not in WORKER_SYSTEM_PROMPT


def test_validation_guidance_is_windows_safe():
    """Worker/Planner validation guidance should avoid Unix-only grep failures."""
    assert "Avoid bare `grep`" in PLANNER_SYSTEM_PROMPT

    command_desc = DIAGNOSTIC_TOOL_DEF["function"]["parameters"]["properties"]["command"]["description"]
    assert "Use 'rg' instead of bare grep" in command_desc
    assert "grep_search" in command_desc
    assert "exit 0 when the pattern is absent" in command_desc
