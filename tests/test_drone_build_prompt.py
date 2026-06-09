"""Tests for aura.drones.build_prompt.build_drone_creation_prompt."""

from __future__ import annotations

from aura.drones.build_prompt import build_drone_creation_prompt
from aura.drones.build_spec import DroneBuildSpec


def test_buildable_spec_prompt_includes_key_sections() -> None:
    spec = DroneBuildSpec(
        name="Bug Scout",
        kind="project_worker",
        job="Investigate bugs",
        trigger="manual",
        write_policy="read_only",
        instructions="Find and analyze bugs.",
        output_contract="Bug report.",
        build_status="buildable_now",
        success_criteria=("Bug found",),
    )
    prompt = build_drone_creation_prompt(spec)

    assert spec.name in prompt
    assert spec.kind in prompt
    assert spec.job in prompt
    assert ".aura/drones" in prompt
    assert "DroneDefinition" in prompt
    assert "DroneStore" in prompt
    assert "next_id" in prompt
    assert "default_tools_for_policy" in prompt


def test_prompt_includes_all_spec_fields() -> None:
    spec = DroneBuildSpec(
        name="Nightly Reporter",
        kind="report_drafter",
        job="Generate nightly report",
        trigger="scheduled",
        write_policy="ask_before_writes",
        instructions="Summarise logs into a nightly report.",
        output_contract="Markdown report.",
        build_status="buildable_now",
        success_criteria=("Report generated", "Email sent"),
    )
    prompt = build_drone_creation_prompt(spec)

    assert "Nightly Reporter" in prompt
    assert "report_drafter" in prompt
    assert "Generate nightly report" in prompt
    assert "scheduled" in prompt  # trigger
    assert "ask_before_writes" in prompt
    assert "Summarise logs into a nightly report." in prompt
    assert "Markdown report." in prompt
    assert "buildable_now" in prompt
    assert "Report generated" in prompt


def test_needs_capability_spec_still_includes_fields() -> None:
    spec = DroneBuildSpec(
        name="Email Watcher",
        kind="email_watcher",
        job="Watch for incoming email",
        trigger="continuous",
        write_policy="read_only",
        instructions="Monitor Gmail inbox.",
        output_contract="Alert on new mail.",
        build_status="needs_capability",
        missing_capabilities=("gmail_api",),
        success_criteria=("Email detected",),
    )
    prompt = build_drone_creation_prompt(spec)

    # The prompt builder doesn't gate on build_status, so all fields
    # should still appear.
    assert "Email Watcher" in prompt
    assert "email_watcher" in prompt
    assert "Watch for incoming email" in prompt
    assert "read_only" in prompt
    assert "Monitor Gmail inbox." in prompt
    assert "Alert on new mail." in prompt
    assert "needs_capability" in prompt
    assert "gmail_api" in prompt
