"""Tool-round event/result emission helpers — Phase 5 extraction from manager_tool_round.py."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from aura.client import Event, ToolResult
from aura.conversation.tool_limits import limit_reached_payload

EventCallback = Callable[[Event], None]


@dataclass(frozen=True)
class ToolRoundEventsContext:
    """Dependencies needed by tool-round event emission helpers."""

    history: Any


def append_limit_tool_result(
    *,
    context: ToolRoundEventsContext,
    tool_call_id: str,
    name: str,
    info: dict[str, Any],
    on_event: EventCallback,
) -> None:
    """Append a limit-reached tool result to history and emit the event.

    Preserves existing behaviour: builds a limit-reached payload from *info*,
    appends it to history, and fires a ``ToolResult`` event.
    """
    payload = limit_reached_payload(info)
    context.history.append_tool_result(tool_call_id, payload)
    on_event(
        ToolResult(
            tool_call_id=tool_call_id,
            name=name,
            ok=False,
            result=payload,
            extras={
                "limit_reached": bool(info.get("limit_reached")),
                "recoverable": bool(info.get("recoverable")),
                "phase_boundary": bool(info.get("phase_boundary")),
                "reason": str(info.get("reason", "")),
            },
        )
    )
