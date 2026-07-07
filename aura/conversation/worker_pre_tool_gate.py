"""Worker pre-tool lifecycle gate — Phase 3 extraction from manager_tool_round.py."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from aura.events import WORKER_PRE_TOOL_GATE_DECIDED, AuraEvent
from aura.lifecycle import HookContext


@dataclass(frozen=True)
class WorkerPreToolGateContext:
    """Dependencies needed to run the worker.pre_tool_use lifecycle gate."""

    history: Any
    tools: Any
    lifecycle: Any
    event_bus: Any


def run_worker_pre_tool_gate(
    *,
    context: WorkerPreToolGateContext,
    tool_call_id: str,
    name: str,
    args: dict[str, Any],
    state: Any,
) -> dict[str, Any] | None:
    """Evaluate the worker.pre_tool_use lifecycle gate.

    Returns ``None`` if no gate triggered (allow).
    Returns a dict with ``"blocked": True`` and ``"blocked_payload"`` if
    the gate blocked execution.
    Returns a dict with ``"rewritten_args"`` if the gate rewrote the
    tool call payload.
    """
    if context.lifecycle is None:
        return None

    workspace_root = getattr(context.tools, "workspace_root", None)
    ctx = HookContext(
        topic="worker.pre_tool_use",
        category="gate",
        phase="pre_tool_use",
        role="worker",
        tool_call_id=tool_call_id,
        tool_name=name,
        payload={
            "tool_call_id": tool_call_id,
            "tool_name": name,
            "args": dict(args),
            "mode": state.mode,
            "workspace_root": str(workspace_root) if workspace_root is not None else "",
            "worker_file_state": {
                str(path): dict(file_state)
                for path, file_state in state.worker_file_state.items()
                if isinstance(file_state, dict)
            },
            "loaded_target_files": list(state.loaded_target_files),
            "dispatched_target_files": list(state.dispatched_target_files),
            "dispatch_tool_call_id": "",
        },
    )
    try:
        decision = asyncio.run(context.lifecycle.ask(ctx))
    except Exception:
        logging.getLogger(__name__).exception(
            "lifecycle_gate_ask_failed topic=worker.pre_tool_use "
            "tool_call_id=%s tool_name=%s",
            tool_call_id,
            name,
        )
        blocked_payload = {
            "ok": False,
            "blocked": True,
            "failure_class": "lifecycle_gate_blocked",
            "reason": "lifecycle_gate_handler_error",
            "tool": name,
            "recoverable": True,
            "phase_boundary": False,
        }
        _emit_worker_pre_tool_gate_decided(
            event_bus=context.event_bus,
            tool_call_id=tool_call_id,
            name=name,
            allowed=False,
            blocked=True,
            reason="lifecycle_gate_handler_error",
            rewritten=False,
            additional_context=False,
        )
        return {"blocked": True, "blocked_payload": blocked_payload}

    has_additional_context = bool(decision.additional_context)
    has_rewrite = False
    rewritten_args: dict[str, Any] | None = None
    if decision.updated_payload is not None:
        new_args = decision.updated_payload.get("args")
        if isinstance(new_args, dict):
            rewritten_args = new_args
            has_rewrite = True

    _emit_worker_pre_tool_gate_decided(
        event_bus=context.event_bus,
        tool_call_id=tool_call_id,
        name=name,
        allowed=decision.allowed,
        blocked=decision.blocked,
        reason=decision.reason,
        rewritten=has_rewrite,
        additional_context=has_additional_context,
    )

    if decision.additional_context:
        context.history.append_internal_user_text(decision.additional_context)

    if decision.blocked:
        metadata_payload = decision.metadata.get("blocked_payload")
        if isinstance(metadata_payload, dict):
            blocked_payload = dict(metadata_payload)
            blocked_payload.setdefault("ok", False)
            blocked_payload.setdefault("blocked", True)
            blocked_payload.setdefault(
                "failure_class",
                decision.reason or "worker_pre_tool_use_blocked",
            )
            blocked_payload.setdefault(
                "reason",
                decision.reason or str(blocked_payload.get("failure_class") or ""),
            )
            blocked_payload.setdefault("tool", name)
            blocked_payload.setdefault("recoverable", True)
            blocked_payload.setdefault("phase_boundary", False)
        else:
            blocked_payload = {
                "ok": False,
                "blocked": True,
                "failure_class": "lifecycle_gate_blocked",
                "reason": decision.reason or "worker_pre_tool_use_blocked",
                "tool": name,
                "recoverable": True,
                "phase_boundary": False,
            }
        return {"blocked": True, "blocked_payload": blocked_payload}

    if rewritten_args is not None:
        return {"rewritten_args": rewritten_args}

    return None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _emit_worker_pre_tool_gate_decided(
    *,
    event_bus: Any,
    tool_call_id: str,
    name: str,
    allowed: bool,
    blocked: bool,
    reason: str,
    rewritten: bool,
    additional_context: bool,
) -> None:
    """Emit a WORKER_PRE_TOOL_GATE_DECIDED event if an event bus is present."""
    if event_bus is None:
        return
    event_bus.emit(
        AuraEvent(
            topic=WORKER_PRE_TOOL_GATE_DECIDED,
            run_id=tool_call_id,
            artifact_id=tool_call_id,
            payload={
                "tool_call_id": tool_call_id,
                "tool_name": name,
                "allowed": allowed,
                "blocked": blocked,
                "reason": reason,
                "rewritten": rewritten,
                "additional_context": additional_context,
            },
        )
    )
