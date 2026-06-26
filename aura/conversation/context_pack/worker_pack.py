"""Orchestration: assemble a complete Worker Context Pack string."""

from __future__ import annotations

from pathlib import Path

from aura.conversation.context_pack.budget import BudgetTracker
from aura.conversation.context_pack.dependency_hints import find_dependency_hints
from aura.conversation.context_pack.file_summary import summarize_file
from aura.conversation.context_pack.test_hints import find_test_hints


def assemble_worker_context_pack(
    workspace_root: Path,
    *,
    files: list[str],
    goal: str,
    spec: str,
    acceptance: str,
    validation_commands: list[str] | None = None,
    max_chars: int = 12000,
) -> str:
    """Build a compact Worker Context Pack string.

    The pack gives a Worker a head-start on context without extra tool rounds.
    """
    tracker = BudgetTracker(max_chars)

    # Header
    header_lines = [
        "Worker Context Pack",
        "",
        f"Goal: {goal}",
        f"Files: {', '.join(files)}",
    ]
    tracker.add_section("\n".join(header_lines))

    # File summaries
    for rel_path in files:
        section = summarize_file(workspace_root, rel_path)
        section_text = _format_section(section)
        tracker.add_section(section_text)

    # Test hints
    test_section = find_test_hints(workspace_root, files)
    tracker.add_section(_format_section(test_section))

    # Dependency hints
    dep_section = find_dependency_hints(workspace_root, files)
    tracker.add_section(_format_section(dep_section))

    # Validation commands
    if validation_commands:
        cmd_lines = ["Validation Commands:"]
        for cmd in validation_commands:
            cmd_lines.append(f"  {cmd}")
        tracker.add_section("\n".join(cmd_lines))

    # Final note
    tracker.add_section(
        "Note: This pack is starting context only. Use tools when incomplete or stale."
    )

    return tracker.content


def _format_section(section) -> str:
    """Format a ``ContextPackSection`` into a plain-text block."""
    lines: list[str] = []
    lines.append(section.heading)
    lines.append("")
    for body_line in section.body_lines:
        lines.append(body_line)
    if section.caveat:
        lines.append("")
        lines.append(f"Caveat: {section.caveat}")
    return "\n".join(lines)
