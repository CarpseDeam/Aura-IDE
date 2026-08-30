"""Shared validation rules for model-facing Agent definition fields."""
from __future__ import annotations

# A delegation description is copied into the root's tool schema on every
# eligible turn.  Keep it genuinely short and predictable rather than letting
# a definition smuggle a second prompt into the roster.
MAX_AGENT_NAME_CHARS = 100
MAX_AGENT_DESCRIPTION_CHARS = 500

# A workflow name is a label in a picker, so it follows the same shape as an
# agent's: one line, present, and short enough to read at a glance.
MAX_WORKFLOW_NAME_CHARS = 100


def agent_name_error(value: object) -> str:
    """Return the one shared display-name validation error, if any."""
    if not isinstance(value, str) or not value.strip():
        return "An agent needs a name."
    text = value.strip()
    if "\n" in text or "\r" in text:
        return "An agent name must be a single line."
    if len(text) > MAX_AGENT_NAME_CHARS:
        return f"An agent name must be at most {MAX_AGENT_NAME_CHARS} characters."
    return ""


def delegation_description_error(value: object) -> str:
    """Return a user-facing validation error, or ``""`` when valid."""
    if not isinstance(value, str) or not value.strip():
        return "An agent needs a short description."
    text = value.strip()
    if "\n" in text or "\r" in text:
        return "An agent description must be a single line."
    if len(text) > MAX_AGENT_DESCRIPTION_CHARS:
        return (
            "An agent description must be at most "
            f"{MAX_AGENT_DESCRIPTION_CHARS} characters."
        )
    return ""


def workflow_name_error(value: object) -> str:
    """Return the one shared workflow-name validation error, if any."""
    if not isinstance(value, str) or not value.strip():
        return "A workflow needs a name."
    text = value.strip()
    if "\n" in text or "\r" in text:
        return "A workflow name must be a single line."
    if len(text) > MAX_WORKFLOW_NAME_CHARS:
        return f"A workflow name must be at most {MAX_WORKFLOW_NAME_CHARS} characters."
    return ""


__all__ = [
    "MAX_AGENT_DESCRIPTION_CHARS",
    "MAX_AGENT_NAME_CHARS",
    "MAX_WORKFLOW_NAME_CHARS",
    "agent_name_error",
    "delegation_description_error",
    "workflow_name_error",
]
