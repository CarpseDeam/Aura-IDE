from __future__ import annotations

import json
import re
from typing import Any

from PySide6.QtCore import QObject, Signal


class ToolStreamController(QObject):
    """
    Controller that manages the lifecycle of a tool call's streaming arguments.
    It sits between the bridge and the UI, handling buffering and parsing.
    """

    # Emitted once when "path" is found in partial or full JSON
    path_resolved = Signal(str)
    # Emitted once when "command" is found (for run_terminal_command)
    command_resolved = Signal(str)
    # Emitted when "tasks" is found or updated (for update_todo_list)
    todo_updated = Signal(list)
    # Emitted whenever the "content" or "new_str" field grows
    content_updated = Signal(str)
    # Emitted whenever arguments are updated (pretty-printed if possible)
    args_updated = Signal(str)
    # Emitted when the tool state changes ("running", "done", "failed")
    state_changed = Signal(str)
    # Emitted when the tool call is finished with the full result
    result_finalized = Signal(dict)
    # Emitted when the tool call is finished with a formatted result string
    result_finalized_text = Signal(str)

    def __init__(self, tool_name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tool_name = tool_name
        self._buffer = ""
        self._path: str | None = None
        self._command: str | None = None
        self._last_content: str = ""
        self._state = "running"

        # Regex for early extraction from partial JSON
        self._path_re = re.compile(r'"path"\s*:\s*"([^"]+)"')
        self._command_re = re.compile(r'"command"\s*:\s*"([^"]+)"')

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def buffer(self) -> str:
        return self._buffer

    def append_fragment(self, fragment: str) -> None:
        """Append a fragment of JSON arguments and attempt to extract state."""
        self._buffer += fragment

        # 1. Early extraction via regex if not already resolved
        if self._path is None:
            m_path = self._path_re.search(self._buffer)
            if m_path:
                self._path = m_path.group(1)
                self.path_resolved.emit(self._path)

        if self._command is None:
            m_cmd = self._command_re.search(self._buffer)
            if m_cmd:
                self._command = m_cmd.group(1)
                self.command_resolved.emit(self._command)

        # 2. Try full JSON parse to get content updates and pretty-printed args
        try:
            parsed = json.loads(self._buffer)
            if not isinstance(parsed, dict):
                return

            # Emit pretty-printed args
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            self.args_updated.emit(pretty)

            # Update path if we haven't already (or if it changed, though rare)
            path = parsed.get("path")
            if path and path != self._path:
                self._path = path
                self.path_resolved.emit(path)

            # Update command if we haven't already
            cmd = parsed.get("command")
            if cmd and cmd != self._command:
                self._command = cmd
                self.command_resolved.emit(cmd)

            # Extract content based on tool name
            content = ""
            if self._tool_name == "write_file":
                content = parsed.get("content", "")
            elif self._tool_name == "edit_file":
                content = parsed.get("new_str", "")
            elif self._tool_name == "edit_symbol":
                content = parsed.get("new_definition", "")

            if content and content != self._last_content:
                self._last_content = content
                self.content_updated.emit(content)

            # Update tasks for update_todo_list
            if self._tool_name == "update_todo_list":
                tasks = parsed.get("tasks")
                if isinstance(tasks, list):
                    self.todo_updated.emit(tasks)

        except json.JSONDecodeError:
            # Buffer is still incomplete JSON — emit raw buffer for now
            self.args_updated.emit(self._buffer)
            
            # Fallback regex extraction for streaming code
            key = None
            if self._tool_name == "write_file":
                key = "content"
            elif self._tool_name == "edit_file":
                key = "new_str"
            elif self._tool_name == "edit_symbol":
                key = "new_definition"
                
            if key:
                match = re.search(r'"' + key + r'"\s*:\s*"(.*)', self._buffer, re.DOTALL)
                if match:
                    raw_tail = match.group(1)
                    content = re.sub(r'\\([n"t\\])', lambda m: {'n':'\n', '"':'"', 't':'\t', '\\':'\\'}[m.group(1)], raw_tail)
                    if content and content != self._last_content:
                        self._last_content = content
                        self.content_updated.emit(content)


    def finalize(self, ok: bool, result_text: str) -> None:
        """Finalize the tool call with the result."""
        self._state = "done" if ok else "failed"
        self.state_changed.emit(self._state)

        result_dict: dict[str, Any] = {}
        formatted_result = result_text
        try:
            result_dict = json.loads(result_text)
            if isinstance(result_dict, dict):
                formatted_result = json.dumps(result_dict, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            result_dict = {"raw_result": result_text}

        self.result_finalized.emit(result_dict)
        self.result_finalized_text.emit(formatted_result)
