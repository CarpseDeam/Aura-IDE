"""WorkerEventRelay — maps worker Event objects to PySide6 signals."""
from __future__ import annotations

import json
import re
from typing import Any

from PySide6.QtCore import QObject, Signal

from aura.client import (
    ApiError,
    AgentProcessFinished,
    AgentProcessOutput,
    AgentProcessStarted,
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

TERMINAL_OUTPUT_CAPTURE_CHARS = 4000
TERMINAL_OUTPUT_PREVIEW_CHARS = 200


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

    def __init__(self, approval_proxy: Any, worker_model: str = "", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._approval_proxy = approval_proxy
        self._worker_model = worker_model
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

    def relay(self, tool_call_id: str, ev: Event) -> None:
        """Emit the appropriate signal for the event type and track side effects."""
        if isinstance(ev, ReasoningDelta):
            self.reasoningDelta.emit(tool_call_id, ev.text)
        elif isinstance(ev, ContentDelta):
            self.contentDelta.emit(tool_call_id, ev.text)
        elif isinstance(ev, ToolCallStart):
            self.index_to_id[ev.index] = ev.id
            self.toolCallStart.emit(tool_call_id, ev.id, ev.name)
        elif isinstance(ev, ToolCallArgsDelta):
            wid = self.index_to_id.get(ev.index, "")
            if wid:
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
            if ev.name == "update_todo_list":
                tasks = (ev.extras or {}).get("tasks")
                if not tasks and isinstance(parsed, dict):
                    tasks = parsed.get("tasks")
                if not isinstance(tasks, list):
                    tasks = []
                self.todoListUpdated.emit(tool_call_id, tasks)
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
                    else:
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
                if isinstance(path, str) and path:
                    self.read_files.add(path)
            if ev.ok and ev.name == "read_files" and isinstance(parsed, dict):
                paths = parsed.get("paths")
                if isinstance(paths, list):
                    for p in paths:
                        if isinstance(p, str) and p:
                            self.read_files.add(p)
            if ev.ok and ev.name == "read_file_outline" and isinstance(parsed, dict):
                path = parsed.get("path")
                if isinstance(path, str) and path:
                    self.read_outline_files.add(path)
            # Track TODO usage
            if ev.ok and ev.name == "update_todo_list":
                self.todo_used = True
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
                self.terminal_results.append(record)
                if _is_validation_terminal_record(record):
                    self.validation_results.append(record)
        elif isinstance(ev, TerminalOutput):
            self.terminalOutput.emit(tool_call_id, ev.tool_call_id, ev.text)
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


def _is_validation_terminal_record(record: dict[str, Any]) -> bool:
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
