from __future__ import annotations

import json
import re
import time
from typing import Any

from PySide6.QtCore import QObject, Signal


class ToolStreamController(QObject):
    """
    Controller that manages the lifecycle of a tool call's streaming arguments.
    It sits between the bridge and the UI, handling buffering and parsing.
    """

    # Emitted once when "path" is found in partial or full JSON
    path_resolved = Signal(str)
    # Emitted whenever "goal" is found or grows (streaming)
    goal_updated = Signal(str)
    # Emitted once when "goal" is found (for dispatch_to_worker)
    goal_resolved = Signal(str)
    # Emitted once when "command" is found (for run_terminal_command)
    command_resolved = Signal(str)
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
        self._goal: str | None = None
        self._command: str | None = None
        self._last_content: str = ""
        self._last_goal_stream: str = ""
        self._last_content_emit_time: float = 0.0
        self._state = "running"

        # Regex for early extraction from partial JSON
        self._path_re = re.compile(r'"path"\s*:\s*"([^"]+)"')
        self._command_re = re.compile(r'"command"\s*:\s*"([^"]+)"')

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def goal(self) -> str | None:
        return self._goal

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

            # Update goal if we haven't already
            goal = parsed.get("goal")
            if goal and goal != self._goal:
                self._goal = goal
                self.goal_resolved.emit(goal)
                self.goal_updated.emit(goal)

            # Update command if we haven't already
            cmd = parsed.get("command")
            if cmd and cmd != self._command:
                self._command = cmd
                self.command_resolved.emit(cmd)

            # Extract content based on tool name
            content = ""
            if self._tool_name == "write_file":
                content = parsed.get("content", "") or parsed.get("text", "") or parsed.get("new_str", "")
            elif self._tool_name in {"edit_file", "edit_line_range"}:
                content = parsed.get("new_str", "") or parsed.get("content", "") or parsed.get("new_content", "")
            elif self._tool_name == "patch_file":
                edits = parsed.get("edits")
                if isinstance(edits, list):
                    content = "\n\n".join(
                        str(item.get("new", ""))
                        for item in edits
                        if isinstance(item, dict)
                    )
            elif self._tool_name == "apply_edit_transaction":
                operations = parsed.get("operations")
                if isinstance(operations, list):
                    content = "\n\n".join(
                        str(item.get("new_definition") or item.get("content") or item.get("new") or "")
                        for item in operations
                        if isinstance(item, dict)
                    )
            elif self._tool_name == "edit_symbol":
                content = parsed.get("new_definition", "") or parsed.get("content", "")
            elif self._tool_name == "dispatch_to_worker":
                content = parsed.get("spec", "") or parsed.get("content", "")
            if content and content != self._last_content:
                self._last_content = content
                if self._tool_name == "dispatch_to_worker":
                    now = time.monotonic()
                    if now - self._last_content_emit_time >= 0.08:
                        self._last_content_emit_time = now
                        self.content_updated.emit(content)
                else:
                    self.content_updated.emit(content)
        except json.JSONDecodeError:
            # Buffer is still incomplete JSON — emit raw buffer for now
            self.args_updated.emit(self._buffer)
            
            # Streaming goal updates (for dispatch_to_worker)
            if self._goal is None:
                goal_key = "goal"
                goal = self._extract_partial_string(goal_key)
                if goal and goal != self._last_goal_stream:
                    self._last_goal_stream = goal
                    self.goal_updated.emit(goal)

            # Fallback extraction for streaming content
            key = None
            if self._tool_name == "write_file":
                key = "content"
            elif self._tool_name in {"edit_file", "edit_line_range"}:
                key = "new_str"
            elif self._tool_name == "patch_file":
                key = "new"
            elif self._tool_name == "apply_edit_transaction":
                key = "new_definition"
            elif self._tool_name == "edit_symbol":
                key = "new_definition"
            elif self._tool_name == "dispatch_to_worker":
                key = "spec"
            if key:
                content = self._extract_partial_string(key)
                if content is not None and content != self._last_content:
                    self._last_content = content
                    if self._tool_name == "dispatch_to_worker":
                        now = time.monotonic()
                        if now - self._last_content_emit_time >= 0.08:
                            self._last_content_emit_time = now
                            self.content_updated.emit(content)
                    else:
                        self.content_updated.emit(content)

    def _extract_partial_string(self, key: str) -> str | None:
        """Surgically extract a JSON string value from the buffer, handling escapes."""
        # Find "key": "
        pattern = r'"' + key + r'"\s*:\s*"'
        match = re.search(pattern, self._buffer)
        if not match:
            return None
        
        start_idx = match.end()
        raw_tail = self._buffer[start_idx:]
        
        # Walk the tail to find the closing quote, respecting escapes
        content_chars = []
        escaped = False
        for char in raw_tail:
            if escaped:
                if char == 'n':
                    content_chars.append('\n')
                elif char == 't':
                    content_chars.append('\t')
                elif char == 'r':
                    content_chars.append('\r')
                elif char == '"':
                    content_chars.append('"')
                elif char == '\\':
                    content_chars.append('\\')
                else:
                    content_chars.append('\\' + char)
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                # Found the REAL closing quote
                return "".join(content_chars)
            else:
                content_chars.append(char)
        
        # Still open
        return "".join(content_chars)

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
