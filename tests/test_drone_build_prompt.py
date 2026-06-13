"""Tests for aura.drones.build_prompt.build_drone_creation_prompt."""

from __future__ import annotations

from aura.drones.build_prompt import build_drone_creation_prompt
from aura.drones.build_spec import DroneBuildBrief


def test_buildable_brief_prompt_includes_build_brief_text() -> None:
    brief = DroneBuildBrief(
        response_type="brief",
        message="Build a bug scout that investigates bugs.",
        ready_to_build=True,
        build_brief="Build a bug scout that investigates bugs.",
    )
    prompt = build_drone_creation_prompt(brief)

    assert brief.build_brief in prompt


def test_prompt_includes_clarification_gate() -> None:
    brief = DroneBuildBrief(
        response_type="brief",
        message="Test message",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)
    assert "evaluate" in prompt or "Clarification Gate" in prompt


def test_prompt_mentions_build_instruction() -> None:
    brief = DroneBuildBrief(
        response_type="brief",
        message="Test message",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)
    assert "dispatch_to_worker" in prompt


def test_prompt_includes_access_setup_safety_notes() -> None:
    build_brief = "Monitor logs. Needs filesystem read access. Run every hour."
    brief = DroneBuildBrief(
        response_type="brief",
        message="Test message",
        ready_to_build=True,
        build_brief=build_brief,
    )
    prompt = build_drone_creation_prompt(brief)

    assert build_brief in prompt


def test_prompt_includes_store_no_secrets_instruction() -> None:
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)

    # The new prompt forbids creating helper scripts
    assert "DO NOT create" in prompt
    assert "save_drone_definition" in prompt


def test_prompt_includes_access_setup_harness_guidance() -> None:
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)

    # The new prompt includes a compiled build plan with allowed_tools
    assert "Compiled Build Plan" in prompt
    assert "allowed_tools" in prompt


def test_prompt_references_definition_and_store() -> None:
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)

    # The new prompt references DroneDefinition and save_drone_definition
    assert "DroneDefinition" in prompt
    assert "save_drone_definition" in prompt


def test_prompt_does_not_contain_spec_fields() -> None:
    """No references to old spec fields like kind, job, capabilities_needed, etc."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Test message",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)

    assert "capabilities_needed" not in prompt
    assert "missing_capabilities" not in prompt
    assert "likely_kind" not in prompt
    assert "likely_job" not in prompt


def test_prompt_mentions_resolve_capability() -> None:
    """Prompt must mention resolve_capability, either as a step or skip."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)
    assert "resolve_capability" in prompt


def test_prompt_mentions_capability_requirements() -> None:
    """Prompt must mention capability_requirements when brief requires external capabilities."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Send an email and then check git status.",
    )
    prompt = build_drone_creation_prompt(brief)
    assert "capability_requirements" in prompt


def test_prompt_mentions_capability_bindings() -> None:
    """Prompt must mention capability_bindings when brief requires external capabilities."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Send an email and then check git status.",
    )
    prompt = build_drone_creation_prompt(brief)
    assert "capability_bindings" in prompt


def test_prompt_mentions_setup_steps() -> None:
    """Prompt must mention setup_steps when brief requires external capabilities."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Send an email and then check git status.",
    )
    prompt = build_drone_creation_prompt(brief)
    assert "setup_steps" in prompt


def test_prompt_mentions_first_run_test() -> None:
    """Prompt must instruct to dispatch a full DroneDefinition via Worker."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Send an email and then check git status.",
    )
    prompt = build_drone_creation_prompt(brief)
    assert "save_drone_definition" in prompt
    assert "full DroneDefinition" in prompt


def test_prompt_includes_build_brief_verbatim() -> None:
    build_brief = "Monitor logs. Needs filesystem read access. Run every hour."
    brief = DroneBuildBrief(
        response_type="brief",
        message="Test message",
        ready_to_build=True,
        build_brief=build_brief,
    )
    prompt = build_drone_creation_prompt(brief)
    assert build_brief in prompt


def test_prompt_does_not_frame_routes_as_closed_list() -> None:
    """Must not present routes as a limited closed list."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)

    # The prompt uses open-ended language: allowed_tools and resolve_capability
    assert "allowed_tools" in prompt
    assert "resolve_capability" in prompt
    assert "save_drone_definition" in prompt
    # Must not say "only" for routes
    import re
    assert not re.search(
        r"only (option|route|way|alternative)", prompt, re.IGNORECASE
    )


def test_existing_harness_tools_can_be_used_directly() -> None:
    """Prompt must allow using harness tools directly from the compiled plan."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)

    # The prompt tells the Planner to use allowed_tools directly
    assert "allowed_tools" in prompt
    assert "save_drone_definition" in prompt


def test_resolve_capability_no_longer_mandatory_for_every_drone() -> None:
    """resolve_capability is not mandatory when brief has no external requirements."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Build a drone with no external capabilities.",
    )
    prompt = build_drone_creation_prompt(brief)

    # Must not say "resolve_capability for each capability_requirement"
    # when there are no requirements — the skip path is fine
    assert "skip resolve_capability" in prompt or "No external capabilities" in prompt


def test_build_drone_creation_prompt_forbids_scripts() -> None:
    """Prompt must forbid creating helper scripts."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Test message",
        ready_to_build=True,
        build_brief="Read files and check git status",
    )
    prompt = build_drone_creation_prompt(brief)
    assert "save_drone_definition" in prompt
    assert "DO NOT create" in prompt
    # The prompt should include the compiled plan, not raw schema reading instructions
    assert "Compiled Build Plan" in prompt
    assert "allowed_tools" in prompt


def test_build_drone_creation_prompt_includes_compiled_plan() -> None:
    """The prompt must include a compiled build plan section."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Test",
        ready_to_build=True,
        build_brief="Commit and push changes to the remote repository.",
    )
    prompt = build_drone_creation_prompt(brief)
    assert "Compiled Build Plan" in prompt
    assert "git_status" in prompt
    assert "run_terminal_command" in prompt
    # Must NOT ask Planner to figure out tool inventory themselves
    assert "definition.py" not in prompt


def test_vague_brief_prompts_clarifying_question() -> None:
    """A vague brief prompt tells the Planner to ask a clarifying question."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Test message",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)
    assert "clarifying question" in prompt


def test_specific_brief_prompts_dispatch_and_save() -> None:
    """A specific brief prompt contains dispatch_to_worker and save_drone_definition."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Test message",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)
    assert "dispatch_to_worker" in prompt
    assert "save_drone_definition" in prompt


def test_prompt_mentions_required_definition_fields() -> None:
    """The prompt lists required DroneDefinition fields as clarity criteria."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Test message",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)
    assert "name" in prompt
    assert "description" in prompt
    assert "instructions" in prompt
    assert "write_policy" in prompt
    assert "output_contract" in prompt
