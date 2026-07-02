"""HookContext — an immutable snapshot of lifecycle state passed to every handler."""

from __future__ import annotations

from typing import Any

from aura.events.event import AuraEvent


class HookContext:
    """Immutable context snapshot delivered to lifecycle hook handlers.

    Fields carry identity context (run, campaign, step, tool-call) so that
    handlers can make decisions without reaching into other subsystems.
    """

    __slots__ = (
        "topic",
        "category",
        "phase",
        "role",
        "run_id",
        "campaign_id",
        "step_id",
        "tool_call_id",
        "parent_tool_call_id",
        "tool_name",
        "payload",
        "metadata",
    )

    def __init__(
        self,
        *,
        topic: str,
        category: str,
        phase: str = "",
        role: str = "",
        run_id: str = "",
        campaign_id: str = "",
        step_id: str = "",
        tool_call_id: str = "",
        parent_tool_call_id: str = "",
        tool_name: str = "",
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.topic = topic
        self.category = category
        self.phase = phase
        self.role = role
        self.run_id = run_id
        self.campaign_id = campaign_id
        self.step_id = step_id
        self.tool_call_id = tool_call_id
        self.parent_tool_call_id = parent_tool_call_id
        self.tool_name = tool_name
        self.payload: dict[str, Any] = payload if payload is not None else {}
        self.metadata: dict[str, Any] = metadata if metadata is not None else {}

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_event(
        cls,
        event: AuraEvent,
        category: str = "notify",
        **overrides: Any,
    ) -> "HookContext":
        """Build a ``HookContext`` from an ``AuraEvent``.

        Copies *topic*, *payload*, *run_id*, *campaign_id*, and *step_id*
        from the event, then applies explicit *overrides*.
        """
        kwargs: dict[str, Any] = {
            "topic": event.topic,
            "category": category,
            "run_id": event.run_id,
            "campaign_id": event.campaign_id,
            "step_id": event.step_id,
            "payload": dict(event.payload),
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict for serialisation or logging."""
        return {
            "topic": self.topic,
            "category": self.category,
            "phase": self.phase,
            "role": self.role,
            "run_id": self.run_id,
            "campaign_id": self.campaign_id,
            "step_id": self.step_id,
            "tool_call_id": self.tool_call_id,
            "parent_tool_call_id": self.parent_tool_call_id,
            "tool_name": self.tool_name,
            "payload": dict(self.payload),
            "metadata": dict(self.metadata),
        }
