"""Handoff generation and persistence for Continue in Fresh Chat."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def generate_handoff_prompt() -> str:
    """Return the prompt sent to the assistant to generate a handoff summary.

    The assistant sees the full conversation history and produces a structured
    markdown handoff document.
    """
    return (
        "Please review our conversation and produce a concise handoff document "
        "for continuing this work in a fresh chat. Include:\n\n"
        "- **Current goal**: What are we trying to accomplish?\n"
        "- **Project context**: What repo/app this is and important architecture notes.\n"
        "- **Decisions made**: Key decisions from this conversation.\n"
        "- **User preferences / constraints**: Things the next chat should respect.\n"
        "- **Work completed**: What was built, fixed, or validated.\n"
        "- **Current state**: What is working, what is unfinished.\n"
        "- **Relevant files**: Files discussed or changed.\n"
        "- **Validation / tests**: Commands run and results.\n"
        "- **Next useful step**: A grounded continuation point, not a giant task pile.\n\n"
        "Output ONLY the handoff markdown document — nothing before or after it."
    )


def save_handoff(workspace_root: Path, text: str) -> Path:
    """Save handoff markdown to ``.aura/handoffs/YYYY-MM-DD-HHMMSS-handoff.md``.

    Creates the directory if needed. Returns the saved path.
    """
    handoffs_dir = workspace_root / ".aura" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")
    path = handoffs_dir / f"{now}-handoff.md"
    path.write_text(text, encoding="utf-8")
    return path


def extract_handoff_text(full_message: dict) -> str:
    """Extract the handoff text from an assistant response message.

    Returns the content string, or empty string if absent.
    """
    return full_message.get("content", "") or ""
