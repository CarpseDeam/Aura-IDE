"""HookMatcher — deterministic matching for lifecycle hook topics."""

from __future__ import annotations

from typing import Any


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

    __slots__ = ("_topic", "_phase", "_role", "_tool_name", "_is_wildcard")

    def __init__(
        self,
        topic: str,
        *,
        phase: str = "",
        role: str = "",
        tool_name: str = "",
    ) -> None:
        self._topic = topic
        self._phase = phase
        self._role = role
        self._tool_name = tool_name
        self._is_wildcard = topic == "*"

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def matches(self, ctx: "HookContext") -> bool:  # noqa: F821
        """Return ``True`` if *ctx* satisfies all criteria of this matcher."""
        # Topic: exact match or wildcard.
        if not self._is_wildcard and ctx.topic != self._topic:
            return False

        # Optional phase filter.
        if self._phase and ctx.phase != self._phase:
            return False

        # Optional role filter.
        if self._role and ctx.role != self._role:
            return False

        # Optional tool_name filter.
        if self._tool_name and ctx.tool_name != self._tool_name:
            return False

        return True

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def topic(self) -> str:
        """The topic pattern this matcher was created with."""
        return self._topic

    @property
    def phase(self) -> str:
        """Optional phase filter (empty string means no filter)."""
        return self._phase

    @property
    def role(self) -> str:
        """Optional role filter (empty string means no filter)."""
        return self._role

    @property
    def tool_name(self) -> str:
        """Optional tool_name filter (empty string means no filter)."""
        return self._tool_name

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict for serialisation."""
        return {
            "topic": self._topic,
            "phase": self._phase,
            "role": self._role,
            "tool_name": self._tool_name,
        }
