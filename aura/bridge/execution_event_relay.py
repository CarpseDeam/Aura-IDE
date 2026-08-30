"""ExecutionEventRelay — maps production events to Qt signals."""
from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QObject, Signal

from aura.agents.telemetry import delegation_usage_signal
from aura.bridge.execution_event_ledger import EventRelayExecutionLedger
from aura.bridge.execution_event_terminal_tracking import EventRelayTerminalTracker
from aura.client import (
    AgentProcessFinished,
    AgentProcessOutput,
    AgentProcessStarted,
    ApiError,
    ContentDelta,
    Done,
    Event,
    FileEditLifecycle,
    ReasoningDelta,
    TerminalCommandStarted,
    TerminalOutput,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResult,
    Usage,
    WorkspaceReconcileRequested,
)
from aura.events import (
    EXECUTION_COMMAND_STARTED,
    TASK_CHECKLIST_UPDATED,
    AuraEvent,
    EventBus,
)
from aura.task_checklist import UPDATE_TASK_CHECKLIST_TOOL, parse_task_checklist_snapshot


class ExecutionEventRelay(QObject):
    """Relays production ConversationManager events to Qt signals."""

    # Signals consumed by the production execution projector.
    reasoningDelta = Signal(str, str)        # tool_call_id, text
    contentDelta = Signal(str, str)           # tool_call_id, text
    toolCallStart = Signal(str, str, str)     # tool_call_id, execution_tool_id, name
    toolCallArgs = Signal(str, str, str)      # tool_call_id, execution_tool_id, args_chunk
    toolCallEnd = Signal(str, str)            # tool_call_id, execution_tool_id
    usage = Signal(str, str, int, int, int, int)  # tool_id, model, prompt, comp, hit, miss
    delegationUsage = Signal(str, str, str, int, int, int, int)
    streamDone = Signal(str, str, dict)       # tool_call_id, finish_reason, full_message
    apiError = Signal(str, int, str)          # tool_call_id, status_code, message
    toolResult = Signal(str, str, str, bool, str, dict)  # tool_id, execution_tc_id, name, ok, result, extras
    diffDecided = Signal(str, str, str, str, str, str, bool)
    fileEditLifecycle = Signal(str, str, str, str, list, str)
    # run_id, tool_call_id, tool_name, phase, changes (list[dict]), reason
    workspaceReconcileRequested = Signal(str, str)  # run_id, tool_call_id
    terminalCommandStarted = Signal(str, str, str, str)  # parent, tool, command, cwd
    terminalOutput = Signal(str, str, str)    # parent_tool_id, execution_tool_id, text
    agentProcessStarted = Signal(str, str, str, str)  # parent_tool_id, process_id, label, command
    agentProcessOutput = Signal(str, str, str)  # parent_tool_id, process_id, text
    agentProcessFinished = Signal(str, str, object)  # parent_tool_id, process_id, exit_code

    def __init__(
        self,
        approval_proxy: Any,
        event_bus: EventBus,
        execution_model: str = "",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._approval_proxy = approval_proxy
        self._execution_model = execution_model
        self._event_bus = event_bus
        self.index_to_id: dict[int, str] = {}
        self.api_errors: list[str] = []
        self._terminal_tracker = EventRelayTerminalTracker(
            emit_bus_event=self._emit_bus_event,
        )
        self._ledger = EventRelayExecutionLedger()
        # Active production run identity, attached to every EventBus fact.
        self._run_id: str = ""
        self._active_tool_names: dict[str, str] = {}
        self._tool_arg_fragments: dict[str, str] = {}

    def set_model(self, model: str) -> None:
        """Set the model id reported on usage events for the active run."""
        self._execution_model = str(model or "")

    @property
    def terminal_results(self) -> list[dict]:
        """Terminal command result records, owned by _terminal_tracker."""
        return self._terminal_tracker.terminal_results

    @property
    def validation_results(self) -> list[dict]:
        """Validation-classified terminal records, owned by _terminal_tracker."""
        return self._terminal_tracker.validation_results

    # ------------------------------------------------------------------
    # Execution-ledger properties  (delegated to _ledger)
    # ------------------------------------------------------------------

    @property
    def not_applied_writes(self) -> list[dict[str, Any]]:
        """File-mutation attempts that were not applied, owned by _ledger."""
        return self._ledger.not_applied_writes

    @not_applied_writes.setter
    def not_applied_writes(self, value: list[dict[str, Any]]) -> None:
        self._ledger.not_applied_writes = value

    def _emit_bus_event(self, topic: str, payload: dict) -> None:
        """Emit an event on the event bus (pure-python, no Qt).

        Every emission carries the active production run identity.
        """
        self._event_bus.emit(AuraEvent(
            topic=topic,
            payload=dict(payload),
            run_id=self._run_id,
        ))

    def relay(self, run_id: str, ev: Event) -> None:
        """Emit the appropriate signal for the event type and track side effects."""
        self._run_id = run_id
        tool_call_id = run_id
        if isinstance(ev, ReasoningDelta):
            self.reasoningDelta.emit(tool_call_id, ev.text)
        elif isinstance(ev, ContentDelta):
            self.contentDelta.emit(tool_call_id, ev.text)
        elif isinstance(ev, ToolCallStart):
            self.index_to_id[ev.index] = ev.id
            self._active_tool_names[ev.id] = ev.name
            self._tool_arg_fragments[ev.id] = ""
            self.toolCallStart.emit(tool_call_id, ev.id, ev.name)
        elif isinstance(ev, ToolCallArgsDelta):
            wid = self.index_to_id.get(ev.index, "")
            if wid:
                self._tool_arg_fragments[wid] = (
                    self._tool_arg_fragments.get(wid, "") + ev.args_chunk
                )
                self.toolCallArgs.emit(tool_call_id, wid, ev.args_chunk)
        elif isinstance(ev, TerminalCommandStarted):
            self.terminalCommandStarted.emit(
                tool_call_id,
                ev.tool_call_id,
                ev.command,
                ev.cwd,
            )
            tool_name = self._active_tool_names.get(ev.tool_call_id, "shell")
            self._emit_bus_event(EXECUTION_COMMAND_STARTED, {
                "name": tool_name,
                "command": ev.command,
                "cwd": ev.cwd,
                "starting_cwd": ev.cwd,
                "tool_call_id": ev.tool_call_id,
            })
        elif isinstance(ev, ToolCallEnd):
            wid = self.index_to_id.get(ev.index, "")
            if wid:
                self.toolCallEnd.emit(tool_call_id, wid)
        elif isinstance(ev, Usage):
            self.usage.emit(
                tool_call_id,
                self._execution_model,
                ev.prompt_tokens,
                ev.completion_tokens,
                ev.cache_hit_tokens,
                ev.cache_miss_tokens,
            )
        elif isinstance(ev, Done):
            if ev.full_message:
                self.streamDone.emit(tool_call_id, ev.finish_reason or "", ev.full_message)
        elif isinstance(ev, ApiError):
            from aura.config import redact_secrets
            msg = f"{ev.status_code}: {ev.message}" if ev.status_code is not None else ev.message
            self.api_errors.append(redact_secrets(msg))
            self.apiError.emit(
                tool_call_id,
                ev.status_code if ev.status_code is not None else -1,
                redact_secrets(ev.message),
            )
        elif isinstance(ev, FileEditLifecycle):
            self.fileEditLifecycle.emit(
                tool_call_id,
                ev.tool_call_id,
                ev.tool_name,
                ev.phase,
                [
                    {
                        "change_id": change.change_id,
                        "path": change.path,
                        "action": change.action,
                        "old_content": change.old_content,
                        "new_content": change.new_content,
                    }
                    for change in ev.changes
                ],
                ev.reason,
            )
        elif isinstance(ev, WorkspaceReconcileRequested):
            self.workspaceReconcileRequested.emit(tool_call_id, ev.tool_call_id)
        elif isinstance(ev, ToolResult):
            child_usage = delegation_usage_signal(ev.extras)
            if child_usage is not None:
                provider, model, prompt, completion, hit, miss = child_usage
                self.delegationUsage.emit(
                    ev.tool_call_id,
                    provider,
                    model,
                    prompt,
                    completion,
                    hit,
                    miss,
                )
            approval = (ev.extras or {}).get("approval")
            if approval:
                last = self._approval_proxy.consume_last_event()
                if last is not None:
                    # Emit once per changed file so a multi-file patch_file
                    # transaction is represented truthfully rather than as a
                    # single file; an ordinary single-file write has exactly
                    # one entry here and behaves exactly as before.
                    changes = last.get("changes") or [last]
                    for change in changes:
                        self.diffDecided.emit(
                            tool_call_id,
                            ev.tool_call_id,
                            str(approval),
                            str(change["rel_path"]),
                            str(change["old_content"]),
                            str(change["new_content"]),
                            bool(change["is_new_file"]),
                        )
            self.toolResult.emit(
                tool_call_id, ev.tool_call_id, ev.name, ev.ok, ev.result, ev.extras or {}
            )
            try:
                parsed = json.loads(ev.result)
            except (json.JSONDecodeError, TypeError):
                parsed = {}
            if ev.name == UPDATE_TASK_CHECKLIST_TOOL and ev.ok:
                snapshot, errors = parse_task_checklist_snapshot(parsed)
                if snapshot is not None and not errors:
                    self._emit_bus_event(
                        TASK_CHECKLIST_UPDATED,
                        {
                            **snapshot.to_dict(),
                            "tool_call_id": ev.tool_call_id,
                        },
                    )
            self._ledger.handle_tool_result(
                ev.name, ev.ok, parsed, ev.extras or {}
            )
            self._active_tool_names.pop(ev.tool_call_id, None)
            self._tool_arg_fragments.pop(ev.tool_call_id, None)

            # Track terminal command results, then classify the subset that is meaningful validation.
            self._terminal_tracker.handle_tool_result(ev.name, parsed)
        elif isinstance(ev, TerminalOutput):            self.terminalOutput.emit(tool_call_id, ev.tool_call_id, ev.text)
        elif isinstance(ev, AgentProcessStarted):
            self.agentProcessStarted.emit(
                tool_call_id, ev.process_id, ev.label, ev.command
            )
        elif isinstance(ev, AgentProcessOutput):
            self.agentProcessOutput.emit(tool_call_id, ev.process_id, ev.text)
        elif isinstance(ev, AgentProcessFinished):
            self.agentProcessFinished.emit(tool_call_id, ev.process_id, ev.exit_code)

    def reset(self) -> None:
        """Clear all tracking fields so the relay can be reused."""
        self.index_to_id.clear()
        self.api_errors.clear()
        self._terminal_tracker.reset()
        self._ledger.reset()
        self._active_tool_names.clear()
        self._tool_arg_fragments.clear()
