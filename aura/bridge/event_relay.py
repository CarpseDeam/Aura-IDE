"""WorkerEventRelay — maps worker Event objects to PySide6 signals."""
from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QObject, Signal

from aura.bridge.event_relay_execution_ledger import EventRelayExecutionLedger
from aura.bridge.event_relay_terminal_tracking import EventRelayTerminalTracker
from aura.client import (
    AgentProcessFinished,
    AgentProcessOutput,
    AgentProcessStarted,
    ApiError,
    ContentDelta,
    Done,
    Event,
    ReasoningDelta,
    TerminalOutput,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResult,
    Usage,
)
from aura.events import (
    WORKER_COMMAND_STARTED,
    WORKER_FAILED,
    WORKER_FINAL_REPORT_FINISHED,
    WORKER_FINAL_REPORT_STARTED,
    WORKER_TODO_UPDATED,
    WORKER_TOOL_FINISHED,
    WORKER_TOOL_STARTED,
    WORKER_VALIDATION_STARTED,
    AuraEvent,
    EventBus,
)
from aura.worker_todo import UPDATE_WORKER_TODO_TOOL, parse_worker_todo_snapshot

_PASSIVE_DISPLAY_TOOLS = frozenset({UPDATE_WORKER_TODO_TOOL})


class WorkerEventRelay(QObject):
    """Relays worker ConversationManager events to Qt signals.

    Tracks write_results, api_errors, and phase_boundary side-effect state
    that _run_worker reads after the worker completes.
    """

    # Signals matching _DispatchProxy's original signal set
    reasoningDelta = Signal(str, str)        # tool_call_id, text
    contentDelta = Signal(str, str)           # tool_call_id, text
    toolCallStart = Signal(str, str, str)     # tool_call_id, worker_tool_id, name
    toolCallArgs = Signal(str, str, str)      # tool_call_id, worker_tool_id, args_chunk
    toolCallEnd = Signal(str, str)            # tool_call_id, worker_tool_id
    usage = Signal(str, str, int, int, int, int)  # tool_id, model, prompt, comp, hit, miss
    streamDone = Signal(str, str, dict)       # tool_call_id, finish_reason, full_message
    apiError = Signal(str, int, str)          # tool_call_id, status_code, message
    toolResult = Signal(str, str, str, bool, str, dict)  # tool_id, worker_tc_id, name, ok, result, extras
    diffDecided = Signal(str, str, str, str, str, str, bool)
    terminalOutput = Signal(str, str, str)    # parent_tool_id, worker_tool_id, text
    agentProcessStarted = Signal(str, str, str, str)  # parent_tool_id, process_id, label, command
    agentProcessOutput = Signal(str, str, str)  # parent_tool_id, process_id, text
    agentProcessFinished = Signal(str, str, object)  # parent_tool_id, process_id, exit_code

    def __init__(
        self,
        approval_proxy: Any,
        event_bus: EventBus,
        worker_model: str = "",
        parent: QObject | None = None,
        suppress_final_report_activity: bool = False,
    ) -> None:
        super().__init__(parent)
        self._approval_proxy = approval_proxy
        self._worker_model = worker_model
        self._event_bus = event_bus
        self.index_to_id: dict[int, str] = {}
        self.api_errors: list[str] = []
        self.phase_boundary_info: dict[str, Any] | None = None
        self.tool_results: list[dict] = []
        self.failed_tool_results: list[dict] = []
        self._terminal_tracker = EventRelayTerminalTracker(
            emit_bus_event=self._emit_bus_event,
        )
        self._ledger = EventRelayExecutionLedger(
            emit_bus_event=self._emit_bus_event,
        )
        # Current dispatch identity — set at the start of relay() and read
        # by _emit_bus_event so every EventBus fact carries the parent
        # dispatch tool_call_id as run_id and artifact_id.
        self._dispatch_tool_call_id: str = ""
        # Explicit flag to suppress final-report Activity on the event bus.
        # Used when a Worker run should not emit final-report events.
        self._suppress_final_report_activity = suppress_final_report_activity
        self.final_report_text: str = ""          # last assistant content after Done event
        self._active_tool_names: dict[str, str] = {}
        self._tool_arg_fragments: dict[str, str] = {}

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
    def write_results(self) -> list[dict[str, Any]]:
        """Applied file-mutation records, owned by _ledger."""
        return self._ledger.write_results

    @write_results.setter
    def write_results(self, value: list[dict[str, Any]]) -> None:
        self._ledger.write_results = value

    @property
    def not_applied_writes(self) -> list[dict[str, Any]]:
        """File-mutation attempts that were not applied, owned by _ledger."""
        return self._ledger.not_applied_writes

    @not_applied_writes.setter
    def not_applied_writes(self, value: list[dict[str, Any]]) -> None:
        self._ledger.not_applied_writes = value

    @property
    def read_files(self) -> set[str]:
        """Paths read via read_file / read_files / read_file_range."""
        return self._ledger.read_files

    @property
    def read_outline_files(self) -> set[str]:
        """Paths read via read_file_outline."""
        return self._ledger.read_outline_files

    @property
    def touched_files(self) -> set[str]:
        """All paths touched by applied file mutations."""
        return self._ledger.touched_files

    @touched_files.setter
    def touched_files(self, value: set[str]) -> None:
        self._ledger.touched_files = value

    @property
    def wrote_new_files(self) -> list[str]:
        """Paths of newly created files."""
        return self._ledger.wrote_new_files

    @wrote_new_files.setter
    def wrote_new_files(self, value: list[str]) -> None:
        self._ledger.wrote_new_files = value

    @property
    def edited_existing_files(self) -> list[str]:
        """Paths of existing files that were edited."""
        return self._ledger.edited_existing_files

    @edited_existing_files.setter
    def edited_existing_files(self, value: list[str]) -> None:
        self._ledger.edited_existing_files = value

    def _emit_bus_event(self, topic: str, payload: dict) -> None:
        """Emit an event on the event bus (pure-python, no Qt).

        Every emission carries the current dispatch tool_call_id as
        ``run_id`` and ``artifact_id`` so lifecycle observers and future
        projectors can correlate Worker activity to the active dispatch.
        """
        self._event_bus.emit(AuraEvent(
            topic=topic,
            payload=dict(payload),
            run_id=self._dispatch_tool_call_id,
            artifact_id=self._dispatch_tool_call_id,
        ))

    def relay(self, tool_call_id: str, ev: Event) -> None:
        """Emit the appropriate signal for the event type and track side effects."""
        self._dispatch_tool_call_id = tool_call_id
        if isinstance(ev, ReasoningDelta):
            self.reasoningDelta.emit(tool_call_id, ev.text)
        elif isinstance(ev, ContentDelta):
            self.contentDelta.emit(tool_call_id, ev.text)
        elif isinstance(ev, ToolCallStart):
            self.index_to_id[ev.index] = ev.id
            self._active_tool_names[ev.id] = ev.name
            self._tool_arg_fragments[ev.id] = ""
            self.toolCallStart.emit(tool_call_id, ev.id, ev.name)
            if ev.name in _PASSIVE_DISPLAY_TOOLS:
                return
            # Emit tool_started for activity projectors.
            # For terminal commands also emit command_started.
            self._emit_bus_event(WORKER_TOOL_STARTED, {
                "name": ev.name,
                "tool_call_id": ev.id,
            })
            if ev.name in ("run_terminal_command", "run_and_watch"):
                self._emit_bus_event(WORKER_COMMAND_STARTED, {
                    "name": ev.name,
                    "command": "",
                    "tool_call_id": ev.id,
                })
                self._emit_bus_event(WORKER_VALIDATION_STARTED, {
                    "name": ev.name,
                    "command": "",
                    "tool_call_id": ev.id,
                })
        elif isinstance(ev, ToolCallArgsDelta):
            wid = self.index_to_id.get(ev.index, "")
            if wid:
                self._tool_arg_fragments[wid] = (
                    self._tool_arg_fragments.get(wid, "") + ev.args_chunk
                )
                self.toolCallArgs.emit(tool_call_id, wid, ev.args_chunk)
        elif isinstance(ev, ToolCallEnd):
            wid = self.index_to_id.get(ev.index, "")
            if wid:
                self.toolCallEnd.emit(tool_call_id, wid)
        elif isinstance(ev, Usage):
            self.usage.emit(
                tool_call_id,
                self._worker_model,
                ev.prompt_tokens,
                ev.completion_tokens,
                ev.cache_hit_tokens,
                ev.cache_miss_tokens,
            )
        elif isinstance(ev, Done):
            if ev.full_message:
                self.streamDone.emit(tool_call_id, ev.finish_reason or "", ev.full_message)
                content = ev.full_message.get("content")
                if isinstance(content, str):
                    self.final_report_text = content
            if ev.finish_reason == "stop":
                # Only emit final-report activity events when not suppressed.
                # The caller sets suppress_final_report_activity=True to defer
                # final-report events.
                if not self._suppress_final_report_activity:
                    self._emit_bus_event(WORKER_FINAL_REPORT_STARTED, {})
                    self._emit_bus_event(WORKER_FINAL_REPORT_FINISHED, {
                        "ok": True,
                        "finish_reason": ev.finish_reason or "",
                    })
        elif isinstance(ev, ApiError):
            from aura.config import redact_secrets
            msg = f"{ev.status_code}: {ev.message}" if ev.status_code is not None else ev.message
            self.api_errors.append(redact_secrets(msg))
            self.apiError.emit(
                tool_call_id,
                ev.status_code if ev.status_code is not None else -1,
                redact_secrets(ev.message),
            )
            self._emit_bus_event(WORKER_FAILED, {
                "error": ev.message,
                "status_code": ev.status_code,
            })
        elif isinstance(ev, ToolResult):
            approval = (ev.extras or {}).get("approval")
            if approval:
                last = self._approval_proxy.consume_last_event()
                if last is not None:
                    self.diffDecided.emit(
                        tool_call_id,
                        ev.tool_call_id,
                        str(approval),
                        str(last["rel_path"]),
                        str(last["old_content"]),
                        str(last["new_content"]),
                        bool(last["is_new_file"]),
                    )
            self.toolResult.emit(
                tool_call_id, ev.tool_call_id, ev.name, ev.ok, ev.result, ev.extras or {}
            )
            try:
                parsed = json.loads(ev.result)
            except (json.JSONDecodeError, TypeError):
                parsed = {}
            if ev.name == UPDATE_WORKER_TODO_TOOL and ev.ok:
                snapshot, errors = parse_worker_todo_snapshot(parsed)
                if snapshot is not None and not errors:
                    self._emit_bus_event(
                        WORKER_TODO_UPDATED,
                        {
                            **snapshot.to_dict(),
                            "worker_tool_id": ev.tool_call_id,
                        },
                    )
            if (
                isinstance(parsed, dict)
                and parsed.get("recoverable")
                and parsed.get("phase_boundary")
            ):
                self.phase_boundary_info = parsed
            self._ledger.handle_tool_result(
                ev.name, ev.ok, parsed, ev.extras or {}
            )
            self._active_tool_names.pop(ev.tool_call_id, None)
            self._tool_arg_fragments.pop(ev.tool_call_id, None)
            if ev.name not in _PASSIVE_DISPLAY_TOOLS:
                self._emit_bus_event(WORKER_TOOL_FINISHED, {
                    "name": ev.name,
                    "tool_call_id": ev.tool_call_id,
                    "ok": ev.ok,
                })
            # Track all tool results (passive display tools excluded — they
            # emit display facts only and must not influence outcome classification)
            tr = self._tool_result_record(ev, parsed)
            if ev.name not in _PASSIVE_DISPLAY_TOOLS:
                self.tool_results.append(tr)
                if not ev.ok:
                    self.failed_tool_results.append(tr)

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
        self.phase_boundary_info = None
        self.tool_results.clear()
        self.failed_tool_results.clear()
        self._terminal_tracker.reset()
        self._ledger.reset()
        self.final_report_text = ""
        self._active_tool_names.clear()
        self._tool_arg_fragments.clear()

    def _tool_result_record(self, ev: ToolResult, parsed: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "name": ev.name,
            "ok": ev.ok,
            "result_preview": (ev.result or "")[:200],
        }
        if not isinstance(parsed, dict):
            if not ev.ok:
                record["error"] = (ev.result or "")[:500]
            return record

        fields = (
            "path",
            "rel_path",
            "error",
            "suggested_tool",
            "suggested_next_tool",
            "suggested_next_action",
            "nearest_candidates",
            "best_fuzzy_ratio",
            "available_symbols",
            "symbol_type",
            "symbol_name",
            "class_name",
            "failure_class",
            "internal_recovery_steer",
            "reject",
            "craft_issues",
            "tool_name",
            "applied",
            "deleted",
            "is_new_file",
            "start_line",
            "end_line",
            "hunk_count",
            "backup",
            "blocked_command",
            "missing_dependency",
            "missing_tool",
            "environment_setup_needed",
            "write_outcome",
            "pre_existing_environment_issues",
            "introduced_environment_issues",
            "syntax_valid",
            "operation_index",
            "failed_operation",
            "reason",
            "stale",
            "ambiguous",
            "not_found",
            "candidate_count",
            "candidates",
            "validation_classification",
            "classification",
            "counts_as_validation",
            "counts_as_product_failure",
            "user_action",
            "validation_raw_text",
            "raw_text",
            "expected_outcome",
            "validation_source",
            "validation_command_normalized",
            "normalized",
            "normalization_reason",
        )
        for key in fields:
            if key in parsed:
                record[key] = parsed[key]

        if "path" not in record and isinstance(record.get("rel_path"), str):
            record["path"] = record["rel_path"]
        if "path" not in record and isinstance((ev.extras or {}).get("rel_path"), str):
            record["path"] = (ev.extras or {}).get("rel_path")
        if "error" not in record and not ev.ok:
            record["error"] = (ev.result or "")[:500]
        record["payload"] = parsed
        return record
