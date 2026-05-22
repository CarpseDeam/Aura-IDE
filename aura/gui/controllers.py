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
        self._goal: str | None = None
        self._command: str | None = None
        self._last_content: str = ""
        self._last_goal_stream: str = ""
        self._state = "running"

        # Partial TODO extraction throttle
        self._last_todo_emit: float = 0.0
        self._TODO_THROTTLE = 0.05  # 50ms

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
            elif self._tool_name == "edit_file":
                content = parsed.get("new_str", "") or parsed.get("content", "") or parsed.get("new_content", "")
            elif self._tool_name == "edit_symbol":
                content = parsed.get("new_definition", "") or parsed.get("content", "")
            elif self._tool_name == "dispatch_to_worker":
                content = parsed.get("spec", "") or parsed.get("content", "")
            elif self._tool_name == "run_research":
                content = parsed.get("objective", "") or parsed.get("goal", "") or parsed.get("content", "")

            if content and content != self._last_content:
                self._last_content = content
                self.content_updated.emit(content)
                # For run_research, the objective is also the goal
                if self._tool_name == "run_research" and content != self._goal:
                    self.goal_updated.emit(content)

            # Update tasks for update_todo_list
            if self._tool_name == "update_todo_list":
                tasks = parsed.get("tasks")
                if isinstance(tasks, list):
                    self.todo_updated.emit(tasks)

        except json.JSONDecodeError:
            # Buffer is still incomplete JSON — emit raw buffer for now
            self.args_updated.emit(self._buffer)
            
            # Streaming goal updates (for dispatch_to_worker / run_research)
            if self._goal is None:
                goal_key = "goal" if self._tool_name != "run_research" else "objective"
                goal = self._extract_partial_string(goal_key)
                if goal and goal != self._last_goal_stream:
                    self._last_goal_stream = goal
                    self.goal_updated.emit(goal)

            # Fallback extraction for streaming content
            key = None
            if self._tool_name == "write_file":
                key = "content"
            elif self._tool_name == "edit_file":
                key = "new_str"
            elif self._tool_name == "edit_symbol":
                key = "new_definition"
            elif self._tool_name == "dispatch_to_worker":
                key = "spec"
            elif self._tool_name == "run_research":
                key = "objective"
                
            if key:
                content = self._extract_partial_string(key)
                if content is not None and content != self._last_content:
                    self._last_content = content
                    self.content_updated.emit(content)

            # Partial task extraction for update_todo_list
            if self._tool_name == "update_todo_list":
                now = time.monotonic()
                if now - self._last_todo_emit >= self._TODO_THROTTLE:
                    tasks = self._extract_partial_tasks()
                    if tasks is not None:
                        self._last_todo_emit = now
                        self.todo_updated.emit(tasks)

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

    def _extract_partial_tasks(self) -> list[dict] | None:
        """Extract complete task objects from a partially-streamed JSON buffer.

        Searches for ``"tasks" : [``, then walks forward tracking brace depth
        and string-escape state to find complete ``{...}`` objects.  Returns
        ``None`` when no ``"tasks"`` key is found or no complete objects can
        be parsed.
        """
        m = re.search(r'"tasks"\s*:\s*\[', self._buffer)
        if not m:
            return None

        pos = m.end()  # right after '['
        tasks: list[dict] = []
        depth = 0
        in_string = False
        escaped = False
        obj_start: int | None = None

        while pos < len(self._buffer):
            ch = self._buffer[pos]
            pos += 1

            if escaped:
                escaped = False
                continue

            if ch == '\\':
                escaped = True
                continue

            if ch == '"':
                in_string = not in_string
                continue

            if in_string:
                continue

            if ch == '{':
                if depth == 0:
                    obj_start = pos - 1
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and obj_start is not None:
                    candidate = self._buffer[obj_start:pos]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            tasks.append(obj)
                    except json.JSONDecodeError:
                        pass
                    obj_start = None
            elif ch == ']':
                # We reached the end of the array, but check depth to be safe
                if depth == 0:
                    break

        # If we have an open object, try to extract its partial description
        if depth > 0 and obj_start is not None:
            partial_obj = self._buffer[obj_start:]
            for key in ("description", "content", "text", "task"):
                m_key = re.search(r'"' + key + r'"\s*:\s*"', partial_obj)
                if m_key:
                    start_idx = m_key.end()
                    raw_tail = partial_obj[start_idx:]
                    content_chars = []
                    esc = False
                    for c in raw_tail:
                        if esc:
                            if c == 'n': content_chars.append('\n')
                            elif c == 't': content_chars.append('\t')
                            elif c == 'r': content_chars.append('\r')
                            else: content_chars.append(c)
                            esc = False
                        elif c == '\\':
                            esc = True
                        elif c == '"':
                            break
                        else:
                            content_chars.append(c)
                    tasks.append({"description": "".join(content_chars), "status": "pending"})
                    break

        return tasks if tasks else None


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
