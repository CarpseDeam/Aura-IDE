"""Terminal tool round handling — Phase 2 extraction from manager_tool_round.py."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.work_artifact.model import ValidationCommandSpec

from aura.conversation.completion_guard import terminal_result_completed
from aura.conversation.syntax_terminal_state import update_syntax_state_from_terminal
from aura.conversation.worker_recovery_payload import is_recoverable_phase_boundary
from aura.conversation.worker_tool_validation import observe_worker_tool_validation


def terminal_payload(loop_info: dict[str, Any] | None) -> dict[str, Any]:
    """Extract the terminal payload dict from *loop_info*."""
    if not isinstance(loop_info, dict):
        return {}
    payload = loop_info.get("_terminal_payload")
    return payload if isinstance(payload, dict) else {}


def terminal_payload_ok(loop_info: dict[str, Any] | None) -> bool | None:
    """Return the `ok` value from the terminal payload, or None if absent."""
    payload = terminal_payload(loop_info)
    if "ok" not in payload:
        return None
    return bool(payload.get("ok"))


def handle_run_and_watch_round(
    *,
    tool_call_id: str,
    args: dict[str, Any],
    state: Any,
    tool_runner: Any,
    on_event: Any,
    cancel_event: Any,
    declared_run_command: str,
) -> dict[str, Any]:
    """Handle a ``run_and_watch`` tool round.

    Preserves existing behaviour: runs the tool, records validation
    to the ledger, and returns the same dict shape as the original
    inline branch in ``_process_task``.
    """
    loop_info = tool_runner.handle_run_and_watch(
        tool_call_id=tool_call_id,
        args=args,
        on_event=on_event,
        cancel_event=cancel_event,
        declared_run_command=declared_run_command,
    )
    observe_worker_tool_validation(state, loop_info)
    return {
        "id": tool_call_id,
        "skip": True,
        "completed_tool_result_for_final": terminal_result_completed(loop_info),
        "flow_result": {
            "name": "run_and_watch",
            "args": args,
            "ok": terminal_payload_ok(loop_info),
            "result_payload": terminal_payload(loop_info),
        },
    }


def handle_run_terminal_command_round(
    *,
    tool_call_id: str,
    args: dict[str, Any],
    state: Any,
    tool_runner: Any,
    workspace_root: Path,
    on_event: Any,
    cancel_event: Any,
    explicit_validation_commands: list[ValidationCommandSpec] | None,
) -> dict[str, Any]:
    """Handle a ``run_terminal_command`` tool round.

    Preserves existing behaviour: runs the tool, updates syntax state
    in worker mode, records validation to the ledger, propagates
    recoverable phase boundary info, and returns the same dict shape
    as the original inline branch in ``_process_task``.
    """
    loop_info = tool_runner.handle_terminal_command(
        tool_call_id=tool_call_id,
        args=args,
        on_event=on_event,
        cancel_event=cancel_event,
        mode=state.mode,
        explicit_validation_commands=explicit_validation_commands,
    )

    if getattr(state, "mode", None) == "worker":
        update_syntax_state_from_terminal(
            args=args,
            loop_info=loop_info,
            workspace_root=workspace_root,
            syntax_repair_required=state.syntax_repair_required,
            syntax_validation_required=state.syntax_validation_required,
            stale_validation_notes=getattr(state, "stale_validation_notes", None),
        )

    observe_worker_tool_validation(state, loop_info)

    result = {
        "id": tool_call_id,
        "skip": True,
        "completed_tool_result_for_final": terminal_result_completed(loop_info),
        "flow_result": {
            "name": "run_terminal_command",
            "args": args,
            "ok": terminal_payload_ok(loop_info),
            "result_payload": terminal_payload(loop_info),
        },
    }

    if is_recoverable_phase_boundary(loop_info):
        result["_worker_phase_boundary_info"] = loop_info

    return result
