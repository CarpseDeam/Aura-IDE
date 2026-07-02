"""WorkerEventRelay — maps worker Event objects to PySide6 signals.

Non-canonical progress/TODO overlay machinery has been extracted into
EventRelayProgressTodo in aura/bridge/event_relay_progress_todo.py.
During canonical DispatchSession campaigns _suppress_todo_updates is set
and all TODO emissions from this relay are dropped — the visible TODO
rail is projected by DispatchTodoController from event-bus lifecycle events.
Worker Activity (via WorkerActivityController) is the correct execution
heartbeat for both canonical and non-canonical paths.
"""
from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QObject, Signal

from aura.bridge.event_relay_errors import (
    _attach_validation_metadata,
    _is_validation_terminal_record,
)
from aura.bridge.event_relay_progress_todo import EventRelayProgressTodo
from aura.bridge.event_relay_write_tracking import (
    TERMINAL_OUTPUT_CAPTURE_CHARS,
    TERMINAL_OUTPUT_PREVIEW_CHARS,
    _file_mutation_was_applied,
    _is_file_mutation_tool,
    _result_path,
    _tool_progress_details_from_args,
    _tool_progress_details_from_result,
)
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
    WORKER_COMMAND_FINISHED,
    WORKER_COMMAND_STARTED,
    WORKER_FAILED,
    WORKER_FILE_CHANGED,
    WORKER_FINAL_REPORT_FINISHED,
    WORKER_FINAL_REPORT_STARTED,
    WORKER_TOOL_FINISHED,
    WORKER_TOOL_STARTED,
    WORKER_VALIDATION_FINISHED,
    WORKER_VALIDATION_STARTED,
    AuraEvent,
    EventBus,
)


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
    todoListUpdated = Signal(str, list)       # tool_call_id, tasks
    terminalOutput = Signal(str, str, str)    # parent_tool_id, worker_tool_id, text
    agentProcessStarted = Signal(str, str, str, str)  # parent_tool_id, process_id, label, command
    agentProcessOutput = Signal(str, str, str)  # parent_tool_id, process_id, text
    agentProcessFinished = Signal(str, str, object)  # parent_tool_id, process_id, exit_code

    def __init__(
        self,
        approval_proxy: Any,
        worker_model: str = "",
        parent: QObject | None = None,
        suppress_todo_updates: bool = False,
        event_bus: EventBus | None = None,
    ) -> None:
        super().__init__(parent)
        self._approval_proxy = approval_proxy
        self._worker_model = worker_model
        self._event_bus = event_bus
        self.index_to_id: dict[int, str] = {}
        self.write_results: list[dict[str, Any]] = []
        self.not_applied_writes: list[dict[str, Any]] = []
        self.api_errors: list[str] = []
        self.phase_boundary_info: dict[str, Any] | None = None
        self.tool_results: list[dict] = []
        self.failed_tool_results: list[dict] = []
        self.terminal_results: list[dict] = []
        self.validation_results: list[dict] = []
        # Execution ledger
        self.read_files: set[str] = set()         # paths read via read_file/read_files
        self.read_outline_files: set[str] = set() # paths read via read_file_outline
        self.touched_files: set[str] = set()      # all paths touched by writes
        self.wrote_new_files: list[str] = []      # paths of newly created files
        self.edited_existing_files: list[str] = []  # paths of existing files that were edited
        self.todo_used: bool = False              # whether update_todo_list was called
        self.final_report_text: str = ""          # last assistant content after Done event
        self._active_tool_names: dict[str, str] = {}
        self._tool_arg_fragments: dict[str, str] = {}
        self._progress_todo = EventRelayProgressTodo(
            suppress_todo_updates=suppress_todo_updates,
            emit_todo=lambda tc_id, tasks: self.todoListUpdated.emit(tc_id, tasks),
        )

    def _emit_bus_event(self, topic: str, payload: dict) -> None:
        """Emit an event on the optional event bus (pure-python, no Qt)."""
        if self._event_bus is None:
            return
        self._event_bus.emit(AuraEvent(topic=topic, payload=dict(payload)))

    def relay(self, tool_call_id: str, ev: Event) -> None:
        """Emit the appropriate signal for the event type and track side effects."""
        if isinstance(ev, ReasoningDelta):
            self.reasoningDelta.emit(tool_call_id, ev.text)
        elif isinstance(ev, ContentDelta):
            self.contentDelta.emit(tool_call_id, ev.text)
        elif isinstance(ev, ToolCallStart):
            self.index_to_id[ev.index] = ev.id
            self._active_tool_names[ev.id] = ev.name
            self._tool_arg_fragments[ev.id] = ""
            self.toolCallStart.emit(tool_call_id, ev.id, ev.name)
            self._progress_todo.mark_progress_tool_started(tool_call_id, ev.name)
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
                name = self._active_tool_names.get(wid, "")
                if name:
                    details = _tool_progress_details_from_args(
                        name, self._tool_arg_fragments[wid]
                    )
                    self._progress_todo.mark_progress_tool_active(tool_call_id, name, details)
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
                self._progress_todo.mark_progress_finished(tool_call_id)
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
            progress_details = _tool_progress_details_from_result(
                ev.name,
                parsed,
                self._tool_arg_fragments.get(ev.tool_call_id, ""),
            )
            if ev.name == "update_todo_list":
                tasks = (ev.extras or {}).get("tasks")
                if not tasks and isinstance(parsed, dict):
                    tasks = parsed.get("tasks")
                if not isinstance(tasks, list):
                    tasks = []
                self.todo_used = True
                self._progress_todo.set_model_todo_tasks(tasks)
                self._progress_todo.emit_todo_progress(tool_call_id, force=True)
            if (
                isinstance(parsed, dict)
                and parsed.get("recoverable")
                and parsed.get("phase_boundary")
            ):
                self.phase_boundary_info = parsed
            if (
                _file_mutation_was_applied(ev.name, ev.ok, parsed, ev.extras or {})
            ):
                path = _result_path(parsed, ev.extras or {})
                is_new_file = bool(parsed.get("is_new_file", False))
                deleted = bool(parsed.get("deleted"))
                write_record = {
                    "tool": ev.name,
                    "path": path,
                    "is_new_file": is_new_file,
                    "deleted": deleted,
                    "applied": True,
                    "applied_tool": parsed.get("applied_tool") or ev.name,
                    "write_outcome": parsed.get("write_outcome") or ("deleted" if deleted else "applied"),
                    "backup": parsed.get("backup"),
                }
                if parsed.get("pre_existing_environment_issues"):
                    write_record["pre_existing_environment_issues"] = parsed.get("pre_existing_environment_issues")
                if parsed.get("craft_metadata"):
                    write_record["craft_metadata"] = parsed.get("craft_metadata")
                if "start_line" in parsed:
                    write_record["start_line"] = parsed.get("start_line")
                if "end_line" in parsed:
                    write_record["end_line"] = parsed.get("end_line")
                if "hunk_count" in parsed:
                    write_record["hunk_count"] = parsed.get("hunk_count")
                if "operation_count" in parsed:
                    write_record["operation_count"] = parsed.get("operation_count")
                self.write_results.append(write_record)
                if path:
                    action = "created" if is_new_file else ("deleted" if deleted else "modified")
                    self._emit_bus_event(WORKER_FILE_CHANGED, {
                        "path": path,
                        "action": action,
                        "tool": ev.name,
                    })
                    self.touched_files.add(path)
                    if is_new_file:
                        self.wrote_new_files.append(path)
                    elif not deleted:
                        self.edited_existing_files.append(path)
            elif _is_file_mutation_tool(ev.name) and isinstance(parsed, dict):
                if parsed.get("applied") is False or str(parsed.get("write_outcome") or "").startswith("not_applied_"):
                    write_record = {
                        "tool": ev.name,
                        "path": _result_path(parsed, ev.extras or {}),
                        "applied": False,
                        "write_outcome": parsed.get("write_outcome") or "not_applied_edit_mechanics_blocked",
                        "failure_class": parsed.get("failure_class", ""),
                        "error": parsed.get("error", ""),
                        "craft_issues": parsed.get("craft_issues", []),
                        "pre_existing_environment_issues": parsed.get("pre_existing_environment_issues", []),
                        "introduced_environment_issues": parsed.get("introduced_environment_issues", []),
                    }
                    if parsed.get("craft_metadata"):
                        write_record["craft_metadata"] = parsed.get("craft_metadata")
                    for key in (
                        "operation_index",
                        "failed_operation",
                        "reason",
                        "stale",
                        "ambiguous",
                        "not_found",
                        "candidate_count",
                        "candidates",
                    ):
                        if key in parsed:
                            write_record[key] = parsed[key]
                    self.not_applied_writes.append(write_record)
            # Track reads for read-before-edit enforcement
            if ev.ok and ev.name == "read_file" and isinstance(parsed, dict):
                path = parsed.get("path")
                if (
                    parsed.get("ok") is True
                    and parsed.get("truncated") is not True
                    and isinstance(path, str)
                    and path
                ):
                    self.read_files.add(path)
            if ev.ok and ev.name == "read_files" and isinstance(parsed, dict):
                files = parsed.get("files")
                if isinstance(files, dict):
                    for path_key, result in files.items():
                        if not isinstance(result, dict):
                            continue
                        path = result.get("path") or path_key
                        if (
                            result.get("ok") is True
                            and result.get("truncated") is not True
                            and isinstance(path, str)
                            and path
                        ):
                            self.read_files.add(path)
            if ev.ok and ev.name == "read_file_range" and isinstance(parsed, dict):
                path = parsed.get("path")
                if (
                    parsed.get("ok") is True
                    and isinstance(parsed.get("content_hash"), str)
                    and isinstance(path, str)
                    and path
                ):
                    self.read_files.add(path)
            if ev.ok and ev.name == "read_file_outline" and isinstance(parsed, dict):
                path = parsed.get("path")
                if isinstance(path, str) and path:
                    self.read_outline_files.add(path)
            # Track TODO usage
            if ev.ok and ev.name == "update_todo_list":
                self.todo_used = True
            self._progress_todo.mark_progress_tool_result(
                tool_call_id, ev.name, ev.ok, parsed, progress_details
            )
            self._active_tool_names.pop(ev.tool_call_id, None)
            self._tool_arg_fragments.pop(ev.tool_call_id, None)
            self._emit_bus_event(WORKER_TOOL_FINISHED, {
                "name": ev.name,
                "tool_call_id": ev.tool_call_id,
                "ok": ev.ok,
            })
            # Track all tool results
            tr = self._tool_result_record(ev, parsed)
            self.tool_results.append(tr)
            if not ev.ok:
                self.failed_tool_results.append(tr)

            # Track terminal command results, then classify the subset that is meaningful validation.
            if (
                ev.name == "run_terminal_command"
                and isinstance(parsed, dict)
                and "command" in parsed
                and "exit_code" in parsed
                and "ok" in parsed
            ):
                output = str(parsed.get("output") or "")
                record = {
                    "command": parsed.get("command", ""),
                    "ok": parsed.get("ok", False),
                    "exit_code": parsed.get("exit_code", -1),
                    "output": output[:TERMINAL_OUTPUT_CAPTURE_CHARS],
                    "output_preview": output[:TERMINAL_OUTPUT_PREVIEW_CHARS],
                }
                if parsed.get("auto_validation"):
                    record["auto_validation"] = True
                _attach_validation_metadata(record, parsed)
                self.terminal_results.append(record)
                self._emit_bus_event(WORKER_COMMAND_FINISHED, {
                    "command": record["command"],
                    "exit_code": record["exit_code"],
                    "ok": record["ok"],
                })
                is_validation = _is_validation_terminal_record(record)
                if is_validation:
                    self.validation_results.append(record)
                    self._emit_bus_event(WORKER_VALIDATION_FINISHED, {
                        "command": record["command"],
                        "ok": record["ok"],
                        "exit_code": record["exit_code"],
                    })

            if (
                ev.name == "run_and_watch"
                and isinstance(parsed, dict)
                and "command" in parsed
                and "exit_code" in parsed
                and "ok" in parsed
            ):
                output = str(parsed.get("output") or "")
                record = {
                    "command": parsed.get("command", ""),
                    "ok": parsed.get("ok", False),
                    "exit_code": parsed.get("exit_code", -1),
                    "output": output[:TERMINAL_OUTPUT_CAPTURE_CHARS],
                    "output_preview": output[:TERMINAL_OUTPUT_PREVIEW_CHARS],
                }
                _attach_validation_metadata(record, parsed)
                self.terminal_results.append(record)
                self._emit_bus_event(WORKER_COMMAND_FINISHED, {
                    "command": record["command"],
                    "exit_code": record["exit_code"],
                    "ok": record["ok"],
                })
                if _is_validation_terminal_record(record):
                    self.validation_results.append(record)
                    self._emit_bus_event(WORKER_VALIDATION_FINISHED, {
                        "command": record["command"],
                        "ok": record["ok"],
                        "exit_code": record["exit_code"],
                    })
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
        self.write_results.clear()
        self.not_applied_writes.clear()
        self.api_errors.clear()
        self.phase_boundary_info = None
        self.tool_results.clear()
        self.failed_tool_results.clear()
        self.terminal_results.clear()
        self.validation_results.clear()
        self.read_files.clear()
        self.read_outline_files.clear()
        self.touched_files.clear()
        self.wrote_new_files.clear()
        self.edited_existing_files.clear()
        self.todo_used = False
        self.final_report_text = ""
        self._active_tool_names.clear()
        self._tool_arg_fragments.clear()
        self._progress_todo.reset()

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