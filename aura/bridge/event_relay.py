"""WorkerEventRelay — maps worker Event objects to PySide6 signals."""
from __future__ import annotations

import json
import re
from typing import Any

from PySide6.QtCore import QObject, Signal

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
from aura.conversation.tool_limits import WRITE_TOOLS
from aura.conversation.validation_orchestrator import classify_validation_payload
from aura.todo_state import todo_signature, todo_task_description, todo_task_status

TERMINAL_OUTPUT_CAPTURE_CHARS = 4000
TERMINAL_OUTPUT_PREVIEW_CHARS = 200

READ_PROGRESS_TOOLS = frozenset(
    {
        "read_file",
        "read_files",
        "read_file_range",
        "read_file_outline",
        "grep_search",
        "search_codebase",
        "glob",
        "find_usages",
        "code_intel_outline",
        "code_intel_references",
        "code_intel_dependents",
    }
)
VALIDATION_PROGRESS_TOOLS = frozenset({"run_terminal_command", "run_and_watch"})
PROGRESS_TODO_LABELS = {
    "inspect": "Inspect relevant files",
    "edit": "Apply changes",
    "validate": "Run validation",
    "recover": "Handle recovery",
    "finish": "Deliver final report",
}
PROGRESS_TODO_ORDER = ("inspect", "edit", "validate", "recover", "finish")
PHASE_ACTION_WORDS = {
    "inspect": (
        "inspect",
        "read",
        "review",
        "search",
        "find",
        "locate",
        "analyze",
        "investigate",
        "understand",
    ),
    "validate": (
        "validate",
        "validation",
        "test",
        "tests",
        "check",
        "compile",
        "pytest",
        "verify",
        "verification",
    ),
    "recover": (
        "recover",
        "recovery",
        "retry",
        "failure",
        "failed",
        "blocker",
        "blocked",
    ),
    "finish": (
        "finish",
        "final",
        "report",
        "summary",
        "summarize",
        "deliver",
    ),
}
DEFAULT_WRITE_ACTION_WORDS = (
    "edit",
    "update",
    "modify",
    "change",
    "refactor",
    "extract",
)
PATH_FIELDS = ("path", "rel_path", "file", "filename", "target_path")
PATH_MENTION_RE = re.compile(r"(?<![\w.-])[\w.@~+/\\-]+(?:\.[\w+-]{1,12})(?::\d+(?::\d+)?)?")


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
    ) -> None:
        super().__init__(parent)
        self._approval_proxy = approval_proxy
        self._worker_model = worker_model
        self._suppress_todo_updates = suppress_todo_updates
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
        self._model_todo_tasks: list[Any] = []
        self._progress_todo_status: dict[str, str] = {}
        self._runtime_todo_status: dict[str, str] = {}
        self._runtime_todo_phase: dict[str, str] = {}
        self._active_tool_names: dict[str, str] = {}
        self._tool_arg_fragments: dict[str, str] = {}
        self._last_emitted_todo_signature: tuple[tuple[str, str], ...] = ()

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
            self._mark_progress_tool_started(tool_call_id, ev.name)
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
                    self._mark_progress_tool_active(tool_call_id, name, details)
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
                self._mark_progress_finished(tool_call_id)
        elif isinstance(ev, ApiError):
            from aura.config import redact_secrets
            msg = f"{ev.status_code}: {ev.message}" if ev.status_code is not None else ev.message
            self.api_errors.append(redact_secrets(msg))
            self.apiError.emit(
                tool_call_id,
                ev.status_code if ev.status_code is not None else -1,
                redact_secrets(ev.message),
            )
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
                self._model_todo_tasks = list(tasks)
                self._emit_todo_progress(tool_call_id, force=True)
            if (
                isinstance(parsed, dict)
                and parsed.get("recoverable")
                and parsed.get("phase_boundary")
            ):
                self.phase_boundary_info = parsed
            if (
                ev.name in WRITE_TOOLS
                and isinstance(parsed, dict)
                and parsed.get("ok")
                and parsed.get("applied") is True
            ):
                write_record = {
                    "tool": ev.name,
                    "path": parsed.get("path"),
                    "is_new_file": parsed.get("is_new_file", False),
                    "deleted": bool(parsed.get("deleted")),
                    "applied": True,
                    "applied_tool": parsed.get("applied_tool") or ev.name,
                    "write_outcome": parsed.get("write_outcome") or "applied",
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
                path = parsed.get("path")
                if isinstance(path, str) and path:
                    self.touched_files.add(path)
                    if parsed.get("is_new_file"):
                        self.wrote_new_files.append(path)
                    elif not parsed.get("deleted"):
                        self.edited_existing_files.append(path)
            elif ev.name in WRITE_TOOLS and isinstance(parsed, dict):
                if parsed.get("applied") is False or str(parsed.get("write_outcome") or "").startswith("not_applied_"):
                    write_record = {
                        "tool": ev.name,
                        "path": parsed.get("path") or parsed.get("rel_path"),
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
            self._mark_progress_tool_result(
                tool_call_id, ev.name, ev.ok, parsed, progress_details
            )
            self._active_tool_names.pop(ev.tool_call_id, None)
            self._tool_arg_fragments.pop(ev.tool_call_id, None)
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
                if _is_validation_terminal_record(record):
                    self.validation_results.append(record)

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
                if _is_validation_terminal_record(record):
                    self.validation_results.append(record)
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
        self._model_todo_tasks.clear()
        self._progress_todo_status.clear()
        self._runtime_todo_status.clear()
        self._runtime_todo_phase.clear()
        self._active_tool_names.clear()
        self._tool_arg_fragments.clear()
        self._last_emitted_todo_signature = ()

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

    def _mark_progress_tool_started(self, tool_call_id: str, name: str) -> None:
        key = _progress_key_for_tool(name)
        if not key:
            return
        if key == "edit":
            self._mark_progress_done("inspect")
        elif key == "validate":
            self._mark_progress_done("inspect")
            self._mark_progress_done("edit")
        self._set_progress_status(key, "active")
        self._activate_model_todo_phase(
            key, _tool_progress_details_from_payload(name, {})
        )
        self._emit_todo_progress(tool_call_id)

    def _mark_progress_tool_active(
        self,
        tool_call_id: str,
        name: str,
        details: dict[str, Any],
    ) -> None:
        key = _progress_key_for_tool(name)
        if not key:
            return
        self._activate_model_todo_phase(key, details)
        self._emit_todo_progress(tool_call_id)

    def _mark_progress_tool_result(
        self,
        tool_call_id: str,
        name: str,
        ok: bool,
        parsed: Any,
        details: dict[str, Any],
    ) -> None:
        if name == "update_todo_list":
            return
        if isinstance(parsed, dict) and parsed.get("internal_recovery_steer"):
            self._mark_recovery_progress_active(details)
            self._emit_todo_progress(tool_call_id)
            return

        key = _progress_key_for_tool(name)
        if not key:
            return

        if ok and isinstance(parsed, dict):
            if name in WRITE_TOOLS and parsed.get("applied") is False:
                self._clear_active_model_todo_phase(key)
                self._mark_recovery_progress_active(details)
            elif name in VALIDATION_PROGRESS_TOOLS and parsed.get("ok") is False:
                self._clear_active_model_todo_phase(key)
                self._mark_recovery_progress_active(details)
            else:
                self._set_progress_status(key, "done")
                self._complete_model_todo_phase(key, details)
        elif ok:
            self._set_progress_status(key, "done")
            self._complete_model_todo_phase(key, details)
        else:
            self._clear_active_model_todo_phase(key)
            self._mark_recovery_progress_active(details)
        self._emit_todo_progress(tool_call_id)

    def _mark_progress_finished(self, tool_call_id: str) -> None:
        for key, status in list(self._progress_todo_status.items()):
            if status == "active":
                self._progress_todo_status[key] = "done"
        for key, status in list(self._runtime_todo_status.items()):
            if status == "active":
                self._runtime_todo_status[key] = "done"
        self._set_progress_status("finish", "done")
        self._complete_model_todo_phase("finish", {})
        self._emit_todo_progress(tool_call_id)

    def _mark_progress_done(self, key: str) -> None:
        if key in self._progress_todo_status:
            self._progress_todo_status[key] = "done"
        self._complete_active_model_todo_phase(key)

    def _mark_recovery_progress_active(self, details: dict[str, Any]) -> None:
        self._set_progress_status("recover", "active")
        self._activate_model_todo_phase("recover", details)

    def _set_progress_status(self, key: str, status: str) -> None:
        if key in PROGRESS_TODO_LABELS and status in {"pending", "active", "done"}:
            self._progress_todo_status[key] = status

    def _emit_todo_progress(self, tool_call_id: str, *, force: bool = False) -> None:
        if self._suppress_todo_updates:
            return
        tasks = self._combined_todo_tasks()
        signature = todo_signature(tasks)
        if not force and signature == self._last_emitted_todo_signature:
            return
        self._last_emitted_todo_signature = signature
        self.todoListUpdated.emit(tool_call_id, tasks)

    def _combined_todo_tasks(self) -> list[Any]:
        tasks = list(self._model_todo_tasks)
        if tasks:
            return [self._task_with_runtime_status(task) for task in tasks]

        existing_descriptions = {
            str(task.get("description") or task.get("content") or task.get("text") or task.get("task") or "")
            for task in tasks
            if isinstance(task, dict)
        }
        for key in PROGRESS_TODO_ORDER:
            status = self._progress_todo_status.get(key)
            if not status:
                continue
            description = PROGRESS_TODO_LABELS[key]
            if description in existing_descriptions:
                continue
            tasks.append({"description": description, "status": status})
        return tasks

    def _task_with_runtime_status(self, task: Any) -> Any:
        status = self._runtime_status_for_task(task)
        if not status:
            return task
        if isinstance(task, dict):
            updated = dict(task)
            updated["status"] = status
            return updated
        return {"description": str(task), "status": status}

    def _runtime_status_for_task(self, task: Any) -> str:
        keys = _todo_task_overlay_keys(task)
        statuses = [self._runtime_todo_status.get(key) for key in keys]
        if "done" in statuses:
            return "done"
        if "active" in statuses and todo_task_status(task) != "done":
            return "active"
        return ""

    def _effective_status_for_task(self, task: Any) -> str:
        return self._runtime_status_for_task(task) or todo_task_status(task)

    def _activate_model_todo_phase(
        self,
        phase: str,
        details: dict[str, Any],
    ) -> None:
        index = self._find_model_todo_index(phase, details)
        if index is None:
            return
        task = self._model_todo_tasks[index]
        if self._effective_status_for_task(task) == "done":
            return
        self._clear_active_model_todo_phase(phase)
        self._set_model_todo_overlay(task, phase, "active", details)

    def _complete_model_todo_phase(
        self,
        phase: str,
        details: dict[str, Any],
    ) -> None:
        index = self._find_model_todo_index(phase, details)
        if index is None:
            index = self._find_active_model_todo_index(phase)
        if index is None:
            return
        task = self._model_todo_tasks[index]
        self._clear_active_model_todo_phase(phase)
        self._set_model_todo_overlay(task, phase, "done", details)

    def _complete_active_model_todo_phase(self, phase: str) -> None:
        index = self._find_active_model_todo_index(phase)
        if index is None:
            return
        task = self._model_todo_tasks[index]
        self._clear_active_model_todo_phase(phase)
        self._set_model_todo_overlay(task, phase, "done", {})

    def _find_model_todo_index(
        self,
        phase: str,
        details: dict[str, Any],
    ) -> int | None:
        if not self._model_todo_tasks:
            return None
        if phase == "edit":
            path_index = self._find_path_matched_model_todo_index(details)
            if path_index is not None:
                return path_index
            active_index = self._find_active_model_todo_index(phase)
            if active_index is not None:
                return active_index
            ordered_index = self._find_next_ordered_edit_todo_index()
            if ordered_index is not None:
                return ordered_index
        return self._find_action_matched_model_todo_index(phase, details)

    def _find_path_matched_model_todo_index(
        self,
        details: dict[str, Any],
    ) -> int | None:
        paths = _normalized_detail_paths(details)
        if not paths:
            return None

        exact_matches: list[int] = []
        suffix_matches: list[int] = []
        basename_matches: list[int] = []
        for index, task in enumerate(self._model_todo_tasks):
            task_paths = _todo_task_paths(task)
            description_path_text = _normalize_path(_todo_task_description(task))
            for path in paths:
                basename = _path_basename(path)
                if path in task_paths:
                    exact_matches.append(index)
                    continue
                if any(_paths_have_suffix_match(path, task_path) for task_path in task_paths):
                    suffix_matches.append(index)
                    continue
                if path and path in description_path_text:
                    suffix_matches.append(index)
                    continue
                if basename and any(
                    _path_basename(task_path) == basename for task_path in task_paths
                ):
                    basename_matches.append(index)

        for matches in (exact_matches, suffix_matches):
            if matches:
                return matches[0]
        unique_basename_matches = list(dict.fromkeys(basename_matches))
        if len(unique_basename_matches) == 1:
            return unique_basename_matches[0]
        return None

    def _find_action_matched_model_todo_index(
        self,
        phase: str,
        details: dict[str, Any],
    ) -> int | None:
        words = _phase_action_words(phase, details)
        if not words:
            return None
        active_match: int | None = None
        pending_match: int | None = None
        for index, task in enumerate(self._model_todo_tasks):
            task_status = self._effective_status_for_task(task)
            if task_status == "done":
                continue
            text = _normalized_todo_text(task)
            if not any(word in text for word in words):
                continue
            if task_status == "active":
                active_match = index
                break
            if pending_match is None:
                pending_match = index
        return active_match if active_match is not None else pending_match

    def _find_next_ordered_edit_todo_index(self) -> int | None:
        fallback: int | None = None
        for index, task in enumerate(self._model_todo_tasks):
            if self._effective_status_for_task(task) == "done":
                continue
            text = _normalized_todo_text(task)
            if any(word in text for word in PHASE_ACTION_WORDS["validate"]):
                continue
            if any(word in text for word in PHASE_ACTION_WORDS["finish"]):
                continue
            if any(word in text for word in PHASE_ACTION_WORDS["recover"]):
                continue
            if any(word in text for word in PHASE_ACTION_WORDS["inspect"]):
                if fallback is None:
                    fallback = index
                continue
            return index
        return fallback

    def _find_active_model_todo_index(self, phase: str) -> int | None:
        for index, task in enumerate(self._model_todo_tasks):
            keys = _todo_task_overlay_keys(task)
            if any(
                self._runtime_todo_status.get(key) == "active"
                and self._runtime_todo_phase.get(key) == phase
                for key in keys
            ):
                return index
        return None

    def _set_model_todo_overlay(
        self,
        task: Any,
        phase: str,
        status: str,
        details: dict[str, Any],
    ) -> None:
        keys = set(_todo_task_overlay_keys(task))
        for path in _normalized_detail_paths(details):
            keys.add(f"path:{path}")
        for key in keys:
            if not key:
                continue
            if self._runtime_todo_status.get(key) == "done" and status != "done":
                continue
            self._runtime_todo_status[key] = status
            self._runtime_todo_phase[key] = phase

    def _clear_active_model_todo_phase(self, phase: str) -> None:
        for key, key_phase in list(self._runtime_todo_phase.items()):
            if (
                key_phase == phase
                and self._runtime_todo_status.get(key) == "active"
            ):
                self._runtime_todo_status.pop(key, None)
                self._runtime_todo_phase.pop(key, None)


def _tool_progress_details_from_result(
    name: str,
    parsed: Any,
    args_text: str,
) -> dict[str, Any]:
    details = _tool_progress_details_from_payload(
        name, parsed if isinstance(parsed, dict) else {}
    )
    arg_details = _tool_progress_details_from_args(name, args_text)
    details["paths"] = _dedupe(
        [
            *details.get("paths", []),
            *arg_details.get("paths", []),
        ]
    )
    details["action_words"] = _dedupe(
        [
            *details.get("action_words", []),
            *arg_details.get("action_words", []),
        ]
    )
    return details


def _tool_progress_details_from_args(name: str, args_text: str) -> dict[str, Any]:
    if not args_text:
        return _tool_progress_details_from_payload(name, {})
    try:
        parsed = json.loads(args_text)
    except (json.JSONDecodeError, TypeError):
        payload: dict[str, Any] = {}
        path = _extract_json_string_field(args_text, PATH_FIELDS)
        if path:
            payload["path"] = path
        return _tool_progress_details_from_payload(name, payload)
    if not isinstance(parsed, dict):
        parsed = {}
    return _tool_progress_details_from_payload(name, parsed)


def _tool_progress_details_from_payload(
    name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "paths": _payload_paths(payload),
        "action_words": _write_action_words(name, payload),
    }


def _payload_paths(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for field in PATH_FIELDS:
        _append_path_values(paths, payload.get(field))
    _append_path_values(paths, payload.get("paths"))
    _append_path_values(paths, payload.get("files"))
    return _dedupe([path for path in paths if path])


def _append_path_values(paths: list[str], value: Any) -> None:
    if isinstance(value, str):
        normalized = _normalize_path(value)
        if normalized:
            paths.append(normalized)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _append_path_values(paths, item)
        return
    if isinstance(value, dict):
        for field in PATH_FIELDS:
            _append_path_values(paths, value.get(field))
        for key, item in value.items():
            if isinstance(key, str) and not key.startswith("__"):
                paths.append(_normalize_path(key))
            if isinstance(item, dict):
                for field in PATH_FIELDS:
                    _append_path_values(paths, item.get(field))


def _extract_json_string_field(text: str, fields: tuple[str, ...]) -> str:
    for field in fields:
        match = re.search(rf'"{re.escape(field)}"\s*:\s*"([^"]+)"', text)
        if match:
            return match.group(1)
    return ""


def _write_action_words(name: str, payload: dict[str, Any]) -> list[str]:
    if name not in WRITE_TOOLS:
        return []
    if name == "delete_file" or payload.get("deleted"):
        return ["remove", "delete"]
    if payload.get("is_new_file"):
        return ["create", "add", "new", "write"]
    if name == "write_file":
        return ["create", "update", "write", "edit", "modify"]
    return list(DEFAULT_WRITE_ACTION_WORDS)


def _phase_action_words(phase: str, details: dict[str, Any]) -> tuple[str, ...]:
    if phase == "edit":
        words = details.get("action_words")
        if isinstance(words, list) and words:
            return tuple(str(word).lower() for word in words if str(word).strip())
        return DEFAULT_WRITE_ACTION_WORDS
    return PHASE_ACTION_WORDS.get(phase, ())


def _todo_task_description(task: Any) -> str:
    return todo_task_description(task)


def _todo_task_status(task: Any) -> str:
    return todo_task_status(task)


def _todo_task_overlay_keys(task: Any) -> list[str]:
    keys: list[str] = []
    description = _normalize_todo_text(_todo_task_description(task))
    if description:
        keys.append(f"desc:{description}")
    for path in _todo_task_paths(task):
        keys.append(f"path:{path}")
    return _dedupe(keys)


def _todo_task_paths(task: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(task, dict):
        for field in PATH_FIELDS:
            _append_path_values(paths, task.get(field))
        _append_path_values(paths, task.get("paths"))
        _append_path_values(paths, task.get("files"))
    description = _todo_task_description(task)
    for match in PATH_MENTION_RE.finditer(description):
        path = _normalize_path(match.group(0))
        if path:
            paths.append(path)
    return _dedupe(paths)


def _normalized_detail_paths(details: dict[str, Any]) -> list[str]:
    paths = details.get("paths", [])
    if not isinstance(paths, list):
        return []
    return _dedupe([_normalize_path(path) for path in paths if _normalize_path(path)])


def _normalize_todo_text(text: str) -> str:
    return " ".join(str(text).lower().split())


def _normalized_todo_text(task: Any) -> str:
    return _normalize_todo_text(_todo_task_description(task))


def _normalize_path(value: Any) -> str:
    text = str(value or "").strip().strip("`\"'.,;()[]{}<>")
    if not text:
        return ""
    text = text.replace("\\", "/")
    text = re.sub(r":\d+(?::\d+)?$", "", text)
    while text.startswith("./"):
        text = text[2:]
    return text.lower()


def _path_basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path else ""


def _paths_have_suffix_match(path: str, task_path: str) -> bool:
    if not path or not task_path:
        return False
    return path.endswith(f"/{task_path}") or task_path.endswith(f"/{path}")


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _progress_key_for_tool(name: str) -> str:
    if name == "update_todo_list":
        return ""
    if name in READ_PROGRESS_TOOLS:
        return "inspect"
    if name in WRITE_TOOLS:
        return "edit"
    if name in VALIDATION_PROGRESS_TOOLS:
        return "validate"
    return ""


def _is_validation_terminal_record(record: dict[str, Any]) -> bool:
    if "counts_as_validation" in record:
        return bool(record.get("counts_as_validation"))
    if record.get("auto_validation"):
        return True
    command = str(record.get("command") or "").strip()
    if not command:
        return False
    normalized = " ".join(command.lower().split())
    python_exe = r"(?:(?:\"[^\"]*python3?(?:\.exe)?\")|(?:'[^']*python3?(?:\.exe)?')|\S*python3?(?:\.exe)?|py)"

    known_patterns = (
        rf"(^|[;&|]\s*){python_exe}\s+-m\s+py_compile\b",
        rf"(^|[;&|]\s*){python_exe}\s+-m\s+(?:pytest|unittest|ruff|mypy)\b",
        r"(^|[;&|]\s*)pytest\b",
        r"(^|[;&|]\s*)unittest\b",
        r"(^|[;&|]\s*)ruff\s+(?:check|format\s+--check)\b",
        r"(^|[;&|]\s*)mypy\b",
        r"(^|[;&|]\s*)npm\s+(?:test|run\s+(?:test|build))\b",
        r"(^|[;&|]\s*)cargo\s+(?:test|build)\b",
        r"(^|[;&|]\s*)go\s+test\b",
    )
    if any(re.search(pattern, normalized) for pattern in known_patterns):
        return True
    if _is_python_assertion_command(normalized):
        return True
    if _is_search_command_with_explicit_shell_assertion(normalized):
        return True
    return False


def _is_python_assertion_command(normalized_command: str) -> bool:
    python_exe = r"(?:(?:\"[^\"]*python3?(?:\.exe)?\")|(?:'[^']*python3?(?:\.exe)?')|\S*python3?(?:\.exe)?|py)"
    if not re.search(rf"(^|[;&|]\s*){python_exe}\s+-c\s+", normalized_command):
        return False
    return any(token in normalized_command for token in ("assert ", "raise systemexit", "sys.exit("))


def _is_search_command_with_explicit_shell_assertion(normalized_command: str) -> bool:
    if not re.search(r"^\s*(?:rg|grep|findstr)\b", normalized_command):
        return False
    return bool(
        re.search(r"&&\s*exit\s+1\s*\|\|\s*exit\s+0\b", normalized_command)
        or re.search(r"\|\|\s*exit\s+1\b", normalized_command)
    )


def _attach_validation_metadata(record: dict[str, Any], parsed: dict[str, Any]) -> None:
    should_classify = (
        bool(parsed.get("auto_validation"))
        or bool(parsed.get("validation_source"))
        or bool(parsed.get("validation_raw_text"))
        or bool(parsed.get("classification"))
        or bool(parsed.get("validation_classification"))
    )
    if not should_classify:
        return
    run = classify_validation_payload(parsed)
    record.update(run.metadata())
