"""Tests for aura.drones.build_prompt.build_drone_creation_prompt."""

from __future__ import annotations

from aura.drones.build_prompt import build_drone_creation_prompt
from aura.drones.build_spec import DroneBuildBrief


def test_buildable_brief_prompt_includes_build_brief_text() -> None:
    brief = DroneBuildBrief(
        response_type="brief",
        message="Plan ready.",
        ready_to_build=True,
        build_brief="Build a bug scout that investigates bugs.",
    )
    prompt = build_drone_creation_prompt(brief)

    assert brief.build_brief in prompt
    assert "Build a bug scout" in prompt


def test_prompt_includes_approved_language() -> None:
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Build a nightly reporter.",
    )
    prompt = build_drone_creation_prompt(brief)

    assert "approved" in prompt.lower()


def test_prompt_mentions_design_and_create() -> None:
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)

    assert "design and create" in prompt.lower() or "build the drone" in prompt.lower()


def test_prompt_includes_access_setup_safety_notes() -> None:
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Monitor logs. Needs filesystem read access. Run every hour.",
    )
    prompt = build_drone_creation_prompt(brief)

    # The build_brief content should appear verbatim
    assert "Monitor logs" in prompt
    assert "Needs filesystem read access" in prompt
    assert "Run every hour" in prompt


def test_prompt_includes_store_no_secrets_instruction() -> None:
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)

    assert "store no secrets" in prompt.lower()


def test_prompt_includes_access_setup_harness_guidance() -> None:
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)

    assert "access, setup, safety, and harness" in prompt.lower()
    assert "runtime access" in prompt.lower() or "connector" in prompt.lower()


def test_prompt_references_definition_and_store() -> None:
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)

    assert "DroneDefinition" in prompt
    assert "DroneStore" in prompt
    assert ".aura/drones" in prompt


def test_prompt_does_not_contain_spec_fields() -> None:
    """No references to old spec fields like kind, job, capabilities_needed, etc."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Ready.",
        ready_to_build=True,
        build_brief="Build a drone.",
    )
    prompt = build_drone_creation_prompt(brief)

    assert "capabilities_needed" not in prompt
    assert "missing_capabilities" not in prompt
    assert "build_status" not in prompt
    assert "output_contract" not in prompt
    assert "Do not add external browser" not in prompt
    assert "Gmail" not in prompt
    assert "scheduler capabilities" not in prompt
