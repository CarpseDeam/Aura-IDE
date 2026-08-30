"""Shared validation rules for model-facing Agent definition fields."""
from __future__ import annotations

# A delegation description is copied into the root's tool schema on every
# eligible turn.  Keep it genuinely short and predictable rather than letting
# a definition smuggle a second prompt into the roster.
MAX_AGENT_DESCRIPTION_CHARS = 500


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


__all__ = ["MAX_AGENT_DESCRIPTION_CHARS", "delegation_description_error"]
