"""HookMatcher — deterministic matching for lifecycle hook topics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class HookMatcher:
    """A deterministic matcher used by notify and gate registries.

    Supported matching:

    * exact topic
    * wildcard topic ``"*"``
    * optional *phase*
    * optional *role*
    * optional *tool_name*

    Every criterion that is set (non-empty) must match for the matcher
    to return ``True``.
    """

    topic: str
    phase: str = ""
    role: str = ""
    tool_name: str = ""
    _is_wildcard: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_is_wildcard", self.topic == "*")

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def matches(self, ctx: "HookContext") -> bool:  # noqa: F821
        """Return ``True`` if *ctx* satisfies all criteria of this matcher."""
        # Topic: exact match or wildcard.
        if not self._is_wildcard and ctx.topic != self.topic:
            return False

        # Optional phase filter.
        if self.phase and ctx.phase != self.phase:
            return False

        # Optional role filter.
        if self.role and ctx.role != self.role:
            return False

        # Optional tool_name filter.
        if self.tool_name and ctx.tool_name != self.tool_name:
            return False

        return True

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict for serialisation."""
        return {
            "topic": self.topic,
            "phase": self.phase,
            "role": self.role,
            "tool_name": self.tool_name,
        }
