"""Tool-round event/result emission helpers — Phase 5 extraction from manager_tool_round.py."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from aura.client import ContentDelta, Done, Event, ToolResult
from aura.conversation.attempt_brief import render_for_planner
from aura.conversation.dispatch import WorkerDispatchResult
from aura.conversation.manager_send_state import _SendState
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


def append_dispatch_blocker_message(
    *,
    context: ToolRoundEventsContext,
    result: WorkerDispatchResult,
    reason: str,
    on_event: EventCallback,
    failure_constraint: str = "",
    attempt_brief: Any = None,
    terminal_reason: str = "",
) -> None:
    """Append a dispatch-blocker message to history and emit a Done event.

    Preserves existing behaviour: appends the attempt-brief or failure
    constraint as internal user text, then emits a ``Done`` stop event.
    """
    if attempt_brief is not None:
        context.history.append_internal_user_text(
            render_for_planner(attempt_brief)
        )
    elif failure_constraint:
        context.history.append_internal_user_text(failure_constraint)
    final_message = {
        "role": "assistant",
        "content": terminal_reason,
        "reasoning_content": None,
    }
    if terminal_reason:
        context.history.append_assistant(final_message)
        on_event(ContentDelta(text=terminal_reason))
    on_event(
        Done(
            finish_reason="stop",
            full_message=final_message,
        )
    )


def append_worker_final_report_tool_results(
    *,
    context: ToolRoundEventsContext,
    tool_calls: list[dict[str, Any]],
    state: _SendState,
    on_event: EventCallback,
) -> None:
    """Append final-report placeholder results for every pending tool call.

    Preserves existing behaviour: replaces each tool call with a phase-boundary
    result payload, then discards the stream buffer.
    """
    for tc in tool_calls:
        fn = tc["function"]
        name = fn["name"]
        tool_call_id = tc["id"]
        reason = (
            str(state.worker_phase_boundary_info.get("reason"))
            if state.worker_phase_boundary_info
            else "worker_phase_boundary"
        )
        message = (
            str(state.worker_phase_boundary_info.get("message"))
            if state.worker_phase_boundary_info
            else (
                "Worker reached a recoverable phase boundary for this pass. "
                "Produce the continuation report now."
            )
        )
        info = {
            "ok": False,
            "limit_reached": bool(
                state.worker_phase_boundary_info
                and state.worker_phase_boundary_info.get("limit_reached")
            ),
            "loop_detected": bool(
                state.worker_phase_boundary_info
                and state.worker_phase_boundary_info.get("loop_detected")
            ),
            "recoverable": True,
            "phase_boundary": True,
            "reason": reason,
            "tool": name,
            "message": message,
            "counts": state.limits.to_dict(),
        }
        append_limit_tool_result(
            context=context,
            tool_call_id=tool_call_id,
            name=name,
            info=info,
            on_event=on_event,
        )
    if state.stream_buffer is not None:
        state.stream_buffer.discard()
