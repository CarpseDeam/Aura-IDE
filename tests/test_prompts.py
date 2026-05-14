"""Snapshot tests for Aura system prompts.

These tests ensure that the Planner and Worker prompts maintain a consistent
contract and that required sections are present.
"""

from __future__ import annotations

from aura.prompts import PLANNER_SYSTEM_PROMPT, WORKER_SYSTEM_PROMPT
from aura.conversation.tools._schemas import DISPATCH_TOOL_DEF


def test_planner_worker_contract_consistency():
    """Ensure Planner and Worker agree on the spec format."""
    plan_nomenclature = "File-by-File Implementation Plan"
    
    # 1. Planner must be instructed to provide this section
    assert plan_nomenclature in PLANNER_SYSTEM_PROMPT
    
    # 2. Worker must be instructed to follow this section
    assert plan_nomenclature in WORKER_SYSTEM_PROMPT
    
    # 3. The dispatch_to_worker tool schema must require it
    spec_desc = DISPATCH_TOOL_DEF["function"]["parameters"]["properties"]["spec"]["description"]
    assert plan_nomenclature in spec_desc


def test_planner_required_spec_sections():
    """Ensure Planner is instructed to include all required spec headings."""
    required_sections = [
        "Core Behavior",
        "Failure Behavior",
        "Code Shape",
        "File-by-File Implementation Plan",
        "Acceptance Checks",
        "Non-Goals",
    ]
    
    for section in required_sections:
        assert section in PLANNER_SYSTEM_PROMPT


def test_worker_adherence_protocol():
    """Ensure Worker has a clear adherence protocol for the Planner's spec."""
    assert "Spec Adherence Protocol" in WORKER_SYSTEM_PROMPT
    assert "File-by-File Implementation Plan" in WORKER_SYSTEM_PROMPT
    assert "Acceptance Verification" in WORKER_SYSTEM_PROMPT


def test_tool_schema_matches_planner_instructions():
    """Ensure the dispatch_to_worker schema matches the Planner's prompt instructions."""
    spec_desc = DISPATCH_TOOL_DEF["function"]["parameters"]["properties"]["spec"]["description"]
    
    required_sections = [
        "Core Behavior",
        "Failure Behavior",
        "Code Shape",
        "File-by-File Implementation Plan",
        "Acceptance Checks",
        "Non-Goals",
    ]
    
    for section in required_sections:
        assert section in spec_desc


def test_snappy_planner_worker_rules():
    """Ensure snappy workflow and execution rules are present."""
    # Planner
    assert "Snappy workflow" in PLANNER_SYSTEM_PROMPT
    assert "Planner tempo" in PLANNER_SYSTEM_PROMPT
    assert "inspect only the minimum files needed" in PLANNER_SYSTEM_PROMPT
    
    # Worker
    assert "Snappy execution" in WORKER_SYSTEM_PROMPT
    assert "update_todo_list" in WORKER_SYSTEM_PROMPT
    assert "The TODO list is the visible execution plan" in WORKER_SYSTEM_PROMPT
    assert "Do not emit prose or XML planning" in WORKER_SYSTEM_PROMPT
    
    # Continuation report still exists
    assert "continuation_report" in WORKER_SYSTEM_PROMPT
