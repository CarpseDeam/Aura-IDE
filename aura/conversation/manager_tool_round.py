"""Tool-call round execution for ConversationManager."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from aura.client import Done, Event, ToolResult
from aura.conversation.attempt_brief import build_attempt_brief, render_for_planner
from aura.conversation.completion_guard import (
    terminal_result_completed,
    tool_result_completes_action,
    worker_dispatch_is_terminal,
)
from aura.conversation.dispatch import DispatchCallback, WorkerDispatchResult
from aura.conversation.dispatch_failure import classify_failed_worker_dispatch
from aura.conversation.edit_orchestrator import EditRetryLedger
from aura.conversation.history import History
from aura.conversation.loop_detection import LoopDetector
from aura.conversation.manager_recovery import (
    update_worker_recovery_state,
    worker_recovery_block,
)
from aura.conversation.manager_send_state import _SendState
from aura.conversation.planner_refresh import PlannerRefreshState
from aura.conversation.spec_quality import validate_planner_dispatch
from aura.conversation.syntax_terminal_state import update_syntax_state_from_terminal
from aura.conversation.tool_limits import WRITE_TOOLS, limit_reached_payload
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools._types import ApprovalCallback
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.worker_recovery_payload import (
    blocked_tool_result,
    is_recoverable_phase_boundary,
    parse_tool_payload,
)
from aura.conversation.workflow_state import WorkflowStatus
from aura.events import WORKER_PRE_TOOL_GATE_DECIDED, AuraEvent, EventBus
from aura.lifecycle import HookContext, LifecycleHooks
from aura.research.policy import ANSWER_ONLY

EventCallback = Callable[[Event], None]

_LOCAL_CODE_INTENT_RE = re.compile(
    r"\b(?:fix|add|update|change|modify|edit|patch|refactor|extract|move|"
    r"create|remove|delete|rename|implement|test|py_compile|pytest|import|"
    r"module|function|class|file)\b",
    re.IGNORECASE,
)

_READ_ONLY_TOOLS = {
    "read_file",
    "read_file_outline",
    "list_directory",
    "grep_search",
    "glob",
}


@dataclass(frozen=True)
class ToolRoundOutcome:
    action: str
    flow_steering_suppressed: bool = False
    enter_silent_preflight: bool = False


class ToolRoundRunner:
    """Execute one assistant tool-call round and apply resulting state."""

    def __init__(
        self,
        *,
        history: History,
        tools: ToolRegistry,
        tool_runner: ToolRunner,
        loop_detector: LoopDetector,
        planner_refresh: PlannerRefreshState,
        lifecycle: LifecycleHooks | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._history = history
        self._tools = tools
        self._tool_runner = tool_runner
        self._loop_detector = loop_detector
        self._planner_refresh = planner_refresh
        self._lifecycle = lifecycle
        self._event_bus = event_bus

    def run(
        self,
        *,
        tool_calls: list[dict[str, Any]],
        state: _SendState,
        on_event: EventCallback,
        approval_cb: ApprovalCallback,
        cancel_event: threading.Event,
        dispatch_cb: DispatchCallback | None,
        workflow_state_cb: Callable[[str, str, str, WorkflowStatus], None] | None = None,
        cleanup_cancelled: Callable[[EventCallback], None],
        explicit_validation_commands: list[str] | None = None,
        declared_run_command: str | None = None,
    ) -> ToolRoundOutcome:
        if state.worker_needs_final_report:
            self._append_worker_final_report_tool_results(
                tool_calls=tool_calls,
                state=state,
                on_event=on_event,
            )
            return ToolRoundOutcome(action="return")

        terminal_dispatch = False
        worker_phase_boundary_info: dict[str, Any] | None = None
        enter_silent_preflight = False

        tasks: list[dict[str, Any]] = []
        for tc in tool_calls:
            fn = tc["function"]
            name = fn["name"]
            tool_call_id = tc["id"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                err = f"failed to parse tool arguments as JSON: {exc}"
                self._history.append_tool_result(
                    tool_call_id, json.dumps({"ok": False, "error": err})
                )
                on_event(
                    ToolResult(
                        tool_call_id=tool_call_id,
                        name=name,
                        ok=False,
                        result=err,
                    )
                )
                continue

            flow_block = (
                state.worker_flow.should_block_tool(name, args)
                if state.worker_flow is not None
                else None
            )
            if flow_block is not None:
                tasks.append(
                    {
                        "id": tool_call_id,
                        "name": name,
                        "args": args,
                        "flow_block": flow_block,
                    }
                )
                continue

            allowed, limit_info = state.limits.check(name)
            if not allowed:
                self._append_limit_tool_result(tool_call_id, name, limit_info, on_event)
                if is_recoverable_phase_boundary(limit_info):
                    worker_phase_boundary_info = limit_info
                continue
            state.limits.record(name)
            if state.worker_flow is not None:
                state.worker_flow.observe_tool_call(name, args)
            tasks.append({"id": tool_call_id, "name": name, "args": args})

        if cancel_event.is_set():
            cleanup_cancelled(on_event)
            return ToolRoundOutcome(action="return")

        def process_task(task: dict[str, Any]) -> dict[str, Any]:
            nonlocal terminal_dispatch, worker_phase_boundary_info
            result = self._process_task(
                task=task,
                state=state,
                on_event=on_event,
                approval_cb=approval_cb,
                cancel_event=cancel_event,
                dispatch_cb=dispatch_cb,
                workflow_state_cb=workflow_state_cb,
                explicit_validation_commands=explicit_validation_commands,
                declared_run_command=declared_run_command,
            )
            if result.pop("terminal_dispatch", False):
                terminal_dispatch = True
            phase_boundary = result.pop("_worker_phase_boundary_info", None)
            if is_recoverable_phase_boundary(phase_boundary):
                worker_phase_boundary_info = phase_boundary
            return result

        results_to_append: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures: dict[concurrent.futures.Future[dict[str, Any]], dict[str, Any]] = {}
            for task in tasks:
                if cancel_event.is_set():
                    break

                if task["name"] in _READ_ONLY_TOOLS:
                    futures[executor.submit(process_task, task)] = task
                else:
                    for fut in concurrent.futures.as_completed(futures):
                        results_to_append.append(fut.result())
                    futures.clear()

                    if cancel_event.is_set():
                        break

                    results_to_append.append(process_task(task))

            for fut in concurrent.futures.as_completed(futures):
                results_to_append.append(fut.result())

        results_by_id = {r.get("id"): r for r in results_to_append if r is not None}

        flow_steering_suppressed = False
        if state.stale_validation_notes:
            note_text = "\n".join(state.stale_validation_notes)
            self._history.append_user_text(note_text)
            flow_steering_suppressed = True

        completed_dispatch_for_final = False
        completed_tool_result_for_final = False
        planner_stale_read_files: list[str] = []
        for task in tasks:
            if cancel_event.is_set():
                cleanup_cancelled(on_event)
                return ToolRoundOutcome(action="return")

            res = results_by_id.get(task["id"])
            if not res:
                continue

            planner_stale_read_files.extend(
                str(path) for path in res.get("planner_stale_read_files", [])
            )
            if res.get("blocker"):
                self._planner_refresh.handle_post_write_notices(
                    self._history, planner_stale_read_files
                )
                blocker_reason = str(res.get("blocker_reason", ""))
                failure_constraint = res.get("failure_constraint", "")

                self._append_dispatch_blocker_message(
                    res["result"],
                    blocker_reason,
                    on_event,
                    failure_constraint,
                    attempt_brief=res.get("attempt_brief"),
                )
                return ToolRoundOutcome(action="return")
            if res.get("completed_dispatch_for_final"):
                completed_dispatch_for_final = True
            if res.get("completed_tool_result_for_final"):
                completed_tool_result_for_final = True
            if res.get("enter_silent_preflight"):
                enter_silent_preflight = True
            if state.worker_flow is not None and res.get("flow_result"):
                flow_result = res["flow_result"]
                before_write_actions = int(state.worker_flow.state.write_actions)
                before_validation_actions = int(state.worker_flow.state.validation_actions)
                state.worker_flow.observe_tool_result(
                    flow_result.get("name", task["name"]),
                    flow_result.get("args", task["args"]),
                    flow_result.get("ok"),
                    flow_result.get("result_payload"),
                )
                if (
                    state.worker_flow.state.write_actions > before_write_actions
                    or state.worker_flow.state.validation_actions > before_validation_actions
                ):
                    state.worker_flow_nudge_sent = False
            planner_constraint = str(res.get("planner_internal_constraint", "") or "")
            attempt_brief = res.get("attempt_brief")
            if attempt_brief is not None:
                self._history.append_internal_user_text(
                    render_for_planner(attempt_brief)
                )
            elif planner_constraint:
                self._history.append_internal_user_text(planner_constraint)
            if res.get("skip"):
                continue

            if "result_payload" in res:
                self._history.append_tool_result(task["id"], res["result_payload"])
                on_event(res["event"])

        self._planner_refresh.handle_post_write_notices(
            self._history, planner_stale_read_files
        )

        if worker_phase_boundary_info is not None:
            state.worker_phase_boundary_info = worker_phase_boundary_info
            if state.worker_phase_boundary_info.get("message"):
                self._history.append_user_text(str(state.worker_phase_boundary_info["message"]))
            state.worker_needs_final_report = True
            return ToolRoundOutcome(action="continue")

        if completed_dispatch_for_final:
            return ToolRoundOutcome(action="return")
        if completed_tool_result_for_final:
            state.task_completion_context = True
            return ToolRoundOutcome(action="continue")

        if terminal_dispatch:
            return ToolRoundOutcome(action="return")
        if enter_silent_preflight:
            return ToolRoundOutcome(action="continue", enter_silent_preflight=True)

        return ToolRoundOutcome(
            action="next_round",
            flow_steering_suppressed=flow_steering_suppressed,
            enter_silent_preflight=enter_silent_preflight,
        )

    def _append_worker_final_report_tool_results(
        self,
        *,
        tool_calls: list[dict[str, Any]],
        state: _SendState,
        on_event: EventCallback,
    ) -> None:
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
            self._append_limit_tool_result(tool_call_id, name, info, on_event)
        if state.stream_buffer is not None:
            state.stream_buffer.discard()

    def _process_task(
        self,
        *,
        task: dict[str, Any],
        state: _SendState,
        on_event: EventCallback,
        approval_cb: ApprovalCallback,
        cancel_event: threading.Event,
        dispatch_cb: DispatchCallback | None,
        workflow_state_cb: Callable[[str, str, str, WorkflowStatus], None] | None = None,
        explicit_validation_commands: list[str] | None,
        declared_run_command: str | None,
    ) -> dict[str, Any]:
        tool_call_id = task["id"]
        name = task["name"]
        args = task["args"]

        flow_block = task.get("flow_block")
        if isinstance(flow_block, dict):
            return blocked_tool_result(tool_call_id, name, flow_block)

        if state.mode == "worker":
            blocked = self._worker_recovery_block(
                tool_call_id=tool_call_id,
                name=name,
                args=args,
                edit_failed_shapes=state.edit_failed_shapes,
                edit_fallback_required=state.edit_fallback_required,
                recovery_block_counts=state.recovery_block_counts,
                line_range_reread_required=state.line_range_reread_required,
                worker_file_state=state.worker_file_state,
                patch_failed_cycles=state.patch_failed_cycles,
                patch_invalid_syntax_required=state.patch_invalid_syntax_required,
                edit_retry_ledger=state.edit_retry_ledger,
                syntax_repair_required=state.syntax_repair_required,
                syntax_validation_required=state.syntax_validation_required,
                write_attempts_by_path=state.write_attempts_by_path,
            )
            if blocked is not None:
                blocked_payload = parse_tool_payload(str(blocked.get("result_payload", "")))
                if is_recoverable_phase_boundary(blocked_payload):
                    blocked["_worker_phase_boundary_info"] = blocked_payload
                return blocked

        # ── Lifecycle gate: worker.pre_tool_use ─────────────────────────
        if state.mode == "worker" and self._lifecycle is not None:
            gate_result = self._run_worker_pre_tool_gate(
                tool_call_id=tool_call_id,
                name=name,
                args=args,
                state=state,
            )
            if gate_result is not None:
                if gate_result.get("blocked"):
                    return blocked_tool_result(
                        tool_call_id,
                        name,
                        gate_result["blocked_payload"],
                    )
                if "rewritten_args" in gate_result:
                    args = gate_result["rewritten_args"]
                    task = dict(task, args=args)

        if name == "dispatch_to_worker":
            return self._handle_dispatch_to_worker(
                tool_call_id=tool_call_id,
                args=args,
                state=state,
                dispatch_cb=dispatch_cb,
                workflow_state_cb=workflow_state_cb,
                on_event=on_event,
            )

        if name == "run_and_watch":
            loop_info = self._tool_runner.handle_run_and_watch(
                tool_call_id=tool_call_id,
                args=args,
                on_event=on_event,
                cancel_event=cancel_event,
                declared_run_command=declared_run_command or "",
            )
            _maybe_record_validation_to_ledger(state, loop_info)
            return {
                "id": tool_call_id,
                "skip": True,
                "completed_tool_result_for_final": terminal_result_completed(loop_info),
                "flow_result": {
                    "name": name,
                    "args": args,
                    "ok": _terminal_payload_ok(loop_info),
                    "result_payload": _terminal_payload(loop_info),
                },
            }

        if name == "run_terminal_command":
            loop_info = self._tool_runner.handle_terminal_command(
                tool_call_id=tool_call_id,
                args=args,
                on_event=on_event,
                cancel_event=cancel_event,
                mode=state.mode,
                explicit_validation_commands=explicit_validation_commands,
            )
            if state.mode == "worker":
                update_syntax_state_from_terminal(
                    args=args,
                    loop_info=loop_info,
                    workspace_root=Path(self._tools.workspace_root),
                    syntax_repair_required=state.syntax_repair_required,
                    syntax_validation_required=state.syntax_validation_required,
                    stale_validation_notes=state.stale_validation_notes,
                )
            _maybe_record_validation_to_ledger(state, loop_info)
            result = {
                "id": tool_call_id,
                "skip": True,
                "completed_tool_result_for_final": terminal_result_completed(loop_info),
                "flow_result": {
                    "name": name,
                    "args": args,
                    "ok": _terminal_payload_ok(loop_info),
                    "result_payload": _terminal_payload(loop_info),
                },
            }
            if is_recoverable_phase_boundary(loop_info):
                result["_worker_phase_boundary_info"] = loop_info
            return result

        if state.reject_all_for_turn and name in WRITE_TOOLS:
            payload = json.dumps(
                {
                    "ok": False,
                    "error": "User rejected all writes in this turn.",
                    "failure_class": "approval_rejected",
                    "applied": False,
                    "write_outcome": "not_applied_user_rejected",
                }
            )
            return {
                "id": tool_call_id,
                "result_payload": payload,
                "event": ToolResult(
                    tool_call_id=tool_call_id,
                    name=name,
                    ok=False,
                    result=payload,
                    extras={"approval": "reject_all"},
                ),
                "flow_result": {
                    "name": name,
                    "args": args,
                    "ok": False,
                    "result_payload": payload,
                },
            }

        exec_result = self._tools.execute(
            name=name,
            args=args,
            approval_cb=approval_cb,
            reject_all=False,
        )

        if exec_result.extras.get("approval") == "reject_all":
            state.reject_all_for_turn = True

        tool_msg_content = exec_result.to_tool_message_content()
        if state.mode == "worker":
            tool_msg_content = self._update_worker_recovery_state(
                name=name,
                args=args,
                ok=exec_result.ok,
                content=tool_msg_content,
                edit_failed_shapes=state.edit_failed_shapes,
                edit_fallback_required=state.edit_fallback_required,
                line_range_reread_required=state.line_range_reread_required,
                worker_file_state=state.worker_file_state,
                patch_failed_cycles=state.patch_failed_cycles,
                patch_invalid_syntax_required=state.patch_invalid_syntax_required,
                edit_retry_ledger=state.edit_retry_ledger,
                syntax_repair_required=state.syntax_repair_required,
                syntax_validation_required=state.syntax_validation_required,
                write_attempts_by_path=state.write_attempts_by_path,
                worker_app_writes=state.worker_app_writes,
            )

        loop_result = self._apply_loop_detection(
            mode=state.mode,
            name=name,
            args=args,
            ok=exec_result.ok,
            result_payload=tool_msg_content,
        )
        tool_msg_content = loop_result["content"]
        loop_info = loop_result["info"]

        result = {
            "id": tool_call_id,
            "result_payload": tool_msg_content,
            "event": ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                ok=exec_result.ok,
                result=tool_msg_content,
                extras=exec_result.extras,
            ),
            "completed_tool_result_for_final": (
                state.mode in {"planner", "single"}
                and tool_result_completes_action(name, exec_result.ok)
            ),
            "flow_result": {
                "name": name,
                "args": args,
                "ok": exec_result.ok,
                "result_payload": tool_msg_content,
            },
        }
        if state.mode == "planner" and exec_result.extras.get("planner_tool_unavailable"):
            result["planner_internal_constraint"] = str(
                exec_result.extras.get("failure_constraint", "") or ""
            )
            result["completed_tool_result_for_final"] = False
        if is_recoverable_phase_boundary(loop_info):
            result["_worker_phase_boundary_info"] = loop_info
        return result

    def _run_worker_pre_tool_gate(
        self,
        *,
        tool_call_id: str,
        name: str,
        args: dict[str, Any],
        state: _SendState,
    ) -> dict[str, Any] | None:
        """Evaluate the worker.pre_tool_use lifecycle gate.

        Returns ``None`` if no gate triggered (allow).
        Returns a dict with ``"blocked": True`` and ``"blocked_payload"`` if
        the gate blocked execution.
        Returns a dict with ``"rewritten_args"`` if the gate rewrote the
        tool call payload.
        """
        if self._lifecycle is None:
            return None

        workspace_root = getattr(self._tools, "workspace_root", None)
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
            decision = asyncio.run(self._lifecycle.ask(ctx))
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
            self._emit_worker_pre_tool_gate_decided(
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

        self._emit_worker_pre_tool_gate_decided(
            tool_call_id=tool_call_id,
            name=name,
            allowed=decision.allowed,
            blocked=decision.blocked,
            reason=decision.reason,
            rewritten=has_rewrite,
            additional_context=has_additional_context,
        )

        if decision.additional_context:
            self._history.append_internal_user_text(decision.additional_context)

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

    def _emit_worker_pre_tool_gate_decided(
        self,
        *,
        tool_call_id: str,
        name: str,
        allowed: bool,
        blocked: bool,
        reason: str,
        rewritten: bool,
        additional_context: bool,
    ) -> None:
        if self._event_bus is None:
            return
        self._event_bus.emit(
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

    def _append_limit_tool_result(
        self,
        tool_call_id: str,
        name: str,
        info: dict[str, Any],
        on_event: EventCallback,
    ) -> None:
        payload = limit_reached_payload(info)
        self._history.append_tool_result(tool_call_id, payload)
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

    def _append_pure_research_dispatch_block(
        self,
        tool_call_id: str,
        on_event: EventCallback,
    ) -> WorkerDispatchResult:
        summary = (
            "Worker was not started because this turn is a pure external "
            "research request. Run web research and answer from sourced evidence."
        )
        result = WorkerDispatchResult(
            ok=False,
            summary=summary,
            recoverable=True,
            extras={
                "dispatch_not_started": True,
                "pure_research": True,
                "research_route": "answer_only",
            },
        )
        payload = json.dumps(
            result.to_tool_payload(),
            ensure_ascii=False,
        )
        self._history.append_tool_result(tool_call_id, payload)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name="dispatch_to_worker",
                ok=True,
                result=payload,
                extras={
                    "dispatch_not_started": True,
                    "pure_research": True,
                    "recoverable": True,
                    "summary": summary,
                },
            )
        )
        return result

    def _append_dispatch_blocker_message(
        self,
        result: WorkerDispatchResult,
        reason: str,
        on_event: EventCallback,
        failure_constraint: str = "",
        attempt_brief: Any = None,
    ) -> None:
        if attempt_brief is not None:
            self._history.append_internal_user_text(
                render_for_planner(attempt_brief)
            )
        elif failure_constraint:
            self._history.append_internal_user_text(failure_constraint)
        on_event(
            Done(
                finish_reason="stop",
                full_message={
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": None,
                },
            )
        )

    def _handle_dispatch_to_worker(
        self,
        *,
        tool_call_id: str,
        args: dict[str, Any],
        state: _SendState,
        dispatch_cb: DispatchCallback | None,
        workflow_state_cb: Callable[[str, str, str, WorkflowStatus], None] | None = None,
        on_event: EventCallback,
    ) -> dict[str, Any]:
        state.planner_dispatch_attempts += 1
        dispatch_attempt = state.planner_dispatch_attempts
        previous_dispatch_tool_call_id = state.planner_visible_dispatch_tool_call_id
        if previous_dispatch_tool_call_id:
            result = self._tool_runner.handle_dispatch(
                tool_call_id=tool_call_id,
                args=args,
                on_event=on_event,
                dispatch_cb=dispatch_cb,
                workflow_state_cb=workflow_state_cb,
                planner_dispatch_attempt=dispatch_attempt,
                previous_dispatch_tool_call_id=previous_dispatch_tool_call_id,
            )
            return self._dispatch_result_to_round_result(
                tool_call_id=tool_call_id,
                result=result,
                state=state,
            )

        if (
            state.research_policy.route == ANSWER_ONLY
            and not _dispatch_args_look_like_local_code_work(args)
        ):
            result = self._append_pure_research_dispatch_block(
                tool_call_id=tool_call_id,
                on_event=on_event,
            )
            action = classify_failed_worker_dispatch(
                result=result,
            )
            blocker_reason = action["blocker_reason"]
            failure_constraint = action.get("failure_constraint", "")
            if blocker_reason or failure_constraint:
                return {
                    "id": tool_call_id,
                    "blocker": True,
                    "result": result,
                    "blocker_reason": blocker_reason,
                    "terminal_dispatch": False,
                    "failure_constraint": failure_constraint,
                }
            return {
                "id": tool_call_id,
                "skip": True,
                "completed_dispatch_for_final": False,
                "terminal_dispatch": False,
            }

        # ── Planner dispatch scope validation ─────────────────────
        if state.mode in {"planner", "single"}:
            latest_user_text = _get_latest_user_text(self._history)
            quality = validate_planner_dispatch(args, latest_user_text)
            if not quality.ok:
                result = WorkerDispatchResult(
                    ok=False,
                    summary="Planner dispatch scope incomplete.",
                    recoverable=True,
                    extras={
                        "internal_planner_handoff": True,
                        "failure_constraint": quality.failure_constraint,
                        "dispatch_not_started": True,
                        "user_visible_blocker": False,
                        "planner_dispatch_scope_incomplete": True,
                    },
                )
                return self._dispatch_result_to_round_result(
                    tool_call_id=tool_call_id,
                    result=result,
                    state=state,
                )

        result = self._tool_runner.handle_dispatch(
            tool_call_id=tool_call_id,
            args=args,
            on_event=on_event,
            dispatch_cb=dispatch_cb,
            workflow_state_cb=workflow_state_cb,
            planner_dispatch_attempt=dispatch_attempt,
            previous_dispatch_tool_call_id=previous_dispatch_tool_call_id,
        )
        if _dispatch_reached_visible_worker(result):
            state.planner_visible_dispatch_tool_call_id = tool_call_id
        return self._dispatch_result_to_round_result(
            tool_call_id=tool_call_id,
            result=result,
            state=state,
        )

    def _dispatch_result_to_round_result(
        self,
        *,
        tool_call_id: str,
        result: WorkerDispatchResult | None,
        state: _SendState,
    ) -> dict[str, Any]:
        terminal_dispatch = False
        if result is not None and not result.cancelled:
            if result.ok:
                terminal_dispatch = True
            else:
                extras = result.extras if isinstance(result.extras, dict) else {}
                failure_constraint = str(extras.get("failure_constraint") or "")
                attempt_brief = build_attempt_brief(result)
                if extras.get("internal_planner_handoff") and failure_constraint:
                    if failure_constraint in state.seen_internal_constraints:
                        d = {
                            "id": tool_call_id,
                            "blocker": True,
                            "result": result,
                            "blocker_reason": "failed",
                            "failure_constraint": failure_constraint,
                            "terminal_dispatch": False,
                        }
                        if attempt_brief is not None:
                            d["attempt_brief"] = attempt_brief
                        return d
                    state.seen_internal_constraints.add(failure_constraint)
                    d = {
                        "id": tool_call_id,
                        "skip": True,
                        "completed_dispatch_for_final": False,
                        "terminal_dispatch": False,
                        "planner_internal_constraint": failure_constraint,
                        "enter_silent_preflight": True,
                    }
                    if attempt_brief is not None:
                        d["attempt_brief"] = attempt_brief
                    return d
                action = classify_failed_worker_dispatch(
                    result=result,
                )
                blocker_reason = action["blocker_reason"]
                failure_constraint = action.get("failure_constraint", "")
                if blocker_reason or failure_constraint:
                    d = {
                        "id": tool_call_id,
                        "blocker": True,
                        "result": result,
                        "blocker_reason": blocker_reason,
                        "failure_constraint": failure_constraint,
                        "planner_stale_read_files": (
                            list(result.modified_files)
                            if result.modified_files
                            else []
                        ),
                        "terminal_dispatch": False,
                    }
                    if attempt_brief is not None:
                        d["attempt_brief"] = attempt_brief
                    return d
        return {
            "id": tool_call_id,
            "skip": True,
            "completed_dispatch_for_final": worker_dispatch_is_terminal(result),
            "planner_stale_read_files": (
                list(result.modified_files) if result and result.modified_files else []
            ),
            "terminal_dispatch": terminal_dispatch,
        }

    def _worker_recovery_block(
        self,
        *,
        tool_call_id: str,
        name: str,
        args: dict[str, Any],
        edit_failed_shapes: set[str],
        edit_fallback_required: dict[str, dict[str, Any]],
        recovery_block_counts: dict[str, int],
        line_range_reread_required: dict[str, dict[str, Any]],
        syntax_repair_required: dict[str, dict[str, Any]],
        syntax_validation_required: set[str],
        write_attempts_by_path: dict[str, int],
        worker_file_state: dict[str, dict[str, Any]] | None = None,
        patch_failed_cycles: dict[str, int] | None = None,
        patch_invalid_syntax_required: dict[str, dict[str, Any]] | None = None,
        edit_retry_ledger: EditRetryLedger | None = None,
    ) -> dict[str, Any] | None:
        return worker_recovery_block(
            self._tools.workspace_root,
            tool_call_id=tool_call_id,
            name=name,
            args=args,
            edit_failed_shapes=edit_failed_shapes,
            edit_fallback_required=edit_fallback_required,
            recovery_block_counts=recovery_block_counts,
            line_range_reread_required=line_range_reread_required,
            syntax_repair_required=syntax_repair_required,
            syntax_validation_required=syntax_validation_required,
            write_attempts_by_path=write_attempts_by_path,
            worker_file_state=worker_file_state,
            patch_failed_cycles=patch_failed_cycles,
            patch_invalid_syntax_required=patch_invalid_syntax_required,
            edit_retry_ledger=edit_retry_ledger,
        )

    def _update_worker_recovery_state(
        self,
        *,
        name: str,
        args: dict[str, Any],
        ok: bool,
        content: str,
        edit_failed_shapes: set[str],
        edit_fallback_required: dict[str, dict[str, Any]],
        line_range_reread_required: dict[str, dict[str, Any]],
        syntax_repair_required: dict[str, dict[str, Any]],
        syntax_validation_required: set[str],
        write_attempts_by_path: dict[str, int],
        worker_app_writes: set[str] | None = None,
        worker_file_state: dict[str, dict[str, Any]] | None = None,
        patch_failed_cycles: dict[str, int] | None = None,
        patch_invalid_syntax_required: dict[str, dict[str, Any]] | None = None,
        edit_retry_ledger: EditRetryLedger | None = None,
    ) -> str:
        return update_worker_recovery_state(
            self._tools.workspace_root,
            name=name,
            args=args,
            ok=ok,
            content=content,
            edit_failed_shapes=edit_failed_shapes,
            edit_fallback_required=edit_fallback_required,
            line_range_reread_required=line_range_reread_required,
            syntax_repair_required=syntax_repair_required,
            syntax_validation_required=syntax_validation_required,
            write_attempts_by_path=write_attempts_by_path,
            worker_app_writes=worker_app_writes,
            worker_file_state=worker_file_state,
            patch_failed_cycles=patch_failed_cycles,
            patch_invalid_syntax_required=patch_invalid_syntax_required,
            edit_retry_ledger=edit_retry_ledger,
        )

    def _apply_loop_detection(
        self,
        *,
        mode: str,
        name: str,
        args: dict[str, Any],
        ok: bool,
        result_payload: str,
    ) -> dict[str, Any]:
        observed = self._loop_detector.observe(
            mode=mode,
            tool_name=name,
            args=args,
            ok=ok,
            content=result_payload,
        )
        return {"content": observed.content, "info": observed.info}


def _get_latest_user_text(history: History) -> str:
    """Extract the most recent user message text from history."""
    for message in reversed(history.messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            return "\n".join(part for part in parts if part)
    return ""


def _maybe_record_validation_to_ledger(
    state: _SendState,
    loop_info: dict[str, Any] | None,
) -> None:
    """Record a validation payload into the state ledger when applicable.

    Only records when in worker mode and the terminal payload exists.
    The ledger's internal ``validation_payload_counts_as_validation``
    guard silently skips non-validation terminal results.
    """
    if state.mode != "worker":
        return
    payload = _terminal_payload(loop_info)
    if not payload:
        return
    write_snapshot = state.applied_write_count()
    state.validation_ledger.observe_tool_payload(payload, write_snapshot)


def _terminal_payload(loop_info: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(loop_info, dict):
        return {}
    payload = loop_info.get("_terminal_payload")
    return payload if isinstance(payload, dict) else {}


def _terminal_payload_ok(loop_info: dict[str, Any] | None) -> bool | None:
    payload = _terminal_payload(loop_info)
    if "ok" not in payload:
        return None
    return bool(payload.get("ok"))


def _dispatch_args_look_like_local_code_work(args: dict[str, Any]) -> bool:
    if not isinstance(args, dict):
        return False

    if not any(_looks_like_local_path(path) for path in _dispatch_local_path_candidates(args)):
        return False

    intent_text = " ".join(
        str(args.get(key) or "")
        for key in ("spec", "goal", "acceptance")
    )
    return bool(_LOCAL_CODE_INTENT_RE.search(intent_text))


def _dispatch_local_path_candidates(args: dict[str, Any]) -> list[Any]:
    candidates: list[Any] = []

    files = args.get("files")
    if isinstance(files, list):
        candidates.extend(files)

    target_regions = args.get("target_regions")
    if isinstance(target_regions, list):
        for region in target_regions:
            if isinstance(region, dict):
                candidates.append(region.get("path"))

    required_outputs = args.get("required_outputs")
    if isinstance(required_outputs, list):
        candidates.extend(required_outputs)

    for key in ("spec", "goal", "acceptance", "summary"):
        candidates.extend(_extract_local_path_mentions(str(args.get(key) or "")))

    return candidates


def _extract_local_path_mentions(text: str) -> list[str]:
    mentions: list[str] = []
    for match in re.finditer(
        r"(?<![\w:/.-])([A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+)(?![\w.-])",
        text,
    ):
        mentions.append(match.group(1))
    for match in re.finditer(
        r"(?<![\w.-])([A-Za-z0-9_.-]+\."
        r"(?:py|pyw|ts|tsx|js|jsx|json|toml|yaml|yml|md|txt|css|scss|html|"
        r"gd|cs|java|go|rs|cpp|c|h|hpp))(?![\w.-])",
        text,
    ):
        mentions.append(match.group(1))
    return mentions


def _looks_like_local_path(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    path = value.strip()
    if not path or "\n" in path or "\r" in path:
        return False
    lowered = path.lower()
    if "://" in lowered or lowered.startswith(("www.", "http:", "https:")):
        return False
    return (
        "/" in path
        or "\\" in path
        or "." in Path(path).name
        or path.startswith(".")
    )


def _dispatch_reached_visible_worker(result: WorkerDispatchResult | None) -> bool:
    if result is None:
        return False
    extras = result.extras if isinstance(result.extras, dict) else {}
    return not bool(extras.get("dispatch_not_started"))


__all__ = ["ToolRoundOutcome", "ToolRoundRunner"]
