"""CLI Event Adapter — parses CLI output streams into structured Aura events.

This allows CLI-based agents (like gemini, claude-code) to drive rich UI
features (tool cards, diffs, TODOs) by emitting machine-readable events
instead of just raw process output.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from aura.client.events import (
    AgentProcessOutput,
    ApiError,
    ContentDelta,
    Done,
    Event,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

logger = logging.getLogger(__name__)


GEMINI_TOOL_NAME_MAP = {
    "run_shell_command": "run_terminal_command",
}


class CLIEventAdapter:
    """Parses a process output stream and yields Aura Event objects.

    Supports:
      - Line-delimited JSON (standard stream-json format)
      - Explicit AURA_EVENT prefix for disambiguation
      - Fallback to raw text (emitted as ContentDelta)
    """

    def __init__(self) -> None:
        """Initialize the adapter."""
        self._buffer = ""
        self._is_done = False
        self._last_message: dict[str, Any] = {"role": "assistant", "content": ""}
        self._tool_calls: dict[int, dict[str, Any]] = {}

    def feed(self, chunk: str) -> Iterator[Event]:
        """Process a chunk of output and yield zero or more events."""
        if self._is_done:
            return

        self._buffer += chunk
        
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            yield from self._process_line(line)

    def finish(self, exit_code: int, stdout: str, stderr: str) -> Iterator[Event]:
        """Process remaining buffer and yield final completion events."""
        if self._is_done:
            return

        if self._buffer.strip():
            yield from self._process_line(self._buffer)
            self._buffer = ""
        
        if exit_code != 0:
            msg = stderr.strip() or stdout.strip() or f"CLI exited with code {exit_code}"
            yield ApiError(status_code=exit_code, message=msg)
            self._is_done = True
            return

        # Prepare tool_calls in last_message
        if self._tool_calls:
            calls_list = []
            for idx in sorted(self._tool_calls.keys()):
                calls_list.append(self._tool_calls[idx])
            self._last_message["tool_calls"] = calls_list
            finish_reason = "tool_calls"
        else:
            finish_reason = "stop"
            # Fallback: if no ContentDelta was yielded but there is stdout, use it.
            if not self._last_message.get("content") and stdout.strip():
                self._last_message["content"] = stdout.strip()

        yield Done(finish_reason=finish_reason, full_message=self._last_message)
        self._is_done = True

    def _process_line(self, line: str) -> Iterator[Event]:
        """Parse a single line and yield events."""
        trimmed = line.strip()
        if not trimmed:
            return

        # 1. Explicit AURA_EVENT prefix
        if trimmed.startswith("AURA_EVENT "):
            try:
                data = json.loads(trimmed[len("AURA_EVENT "):])
                yield from self._map_aura_event(data)
                return
            except json.JSONDecodeError:
                pass # Fall through to raw

        # 2. Heuristic JSON (e.g. Gemini CLI stream-json)
        if trimmed.startswith("{") and trimmed.endswith("}"):
            try:
                data = json.loads(trimmed)
                if "type" in data:
                    yield from self._map_generic_json(data)
                    return
            except json.JSONDecodeError:
                pass
                
        # Filter out common CLI noise that isn't true conversational output
        noise_patterns = [
            "Warning:",
            "Ripgrep is not available",
            "(node:",
            "MaxListenersExceededWarning:",
            "(Use `node",
        ]
        if any(trimmed.startswith(p) for p in noise_patterns):
            # Still yield it to the terminal for debugging, but don't add it to chat
            return

        # 3. Fallback: Raw text
        self._last_message["content"] += line + "\n"
        yield ContentDelta(text=line + "\n")

    def _map_aura_event(self, data: dict[str, Any]) -> Iterator[Event]:
        """Maps an Aura-native JSON event to an Event object."""
        ev_type = data.get("type")
        if ev_type == "content_delta":
            text = data.get("text", "")
            self._last_message["content"] += text
            yield ContentDelta(text=text)
        elif ev_type == "reasoning_delta":
            yield ReasoningDelta(text=data.get("text", ""))
        elif ev_type == "tool_call_start":
            idx = data.get("index", 0)
            tid = data.get("id", f"call-{idx}")
            name = data.get("name", "")
            self._tool_calls[idx] = {
                "id": tid,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": ""
                }
            }
            yield ToolCallStart(index=idx, id=tid, name=name)
        elif ev_type == "tool_call_args":
            idx = data.get("index", 0)
            chunk = data.get("args_chunk", "")
            if idx in self._tool_calls:
                self._tool_calls[idx]["function"]["arguments"] += chunk
            yield ToolCallArgsDelta(index=idx, args_chunk=chunk)
        elif ev_type == "tool_call_end":
            idx = data.get("index", 0)
            yield ToolCallEnd(index=idx)
        elif ev_type == "tool_call_result":
            # Ignore hallucinated tool call results. The ConversationManager executes the tool.
            pass
        elif ev_type == "usage":
            yield Usage(
                prompt_tokens=data.get("prompt", 0),
                completion_tokens=data.get("completion", 0),
                cache_hit_tokens=data.get("hit", 0),
                cache_miss_tokens=data.get("miss", 0)
            )

    def _map_generic_json(self, data: dict[str, Any]) -> Iterator[Event]:
        """Maps non-Aura JSON formats (like Gemini CLI) to Aura events."""
        ev_type = data.get("type")
        
        # Gemini CLI native format
        if ev_type == "tool_use":
            idx = len(self._tool_calls)
            tid = data.get("tool_id", f"call-{idx}")
            name = data.get("tool_name", "")
            name = GEMINI_TOOL_NAME_MAP.get(name, name)
            params = data.get("parameters", {})
            args_str = json.dumps(params)
            
            self._tool_calls[idx] = {
                "id": tid,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": args_str
                }
            }
            
            yield ToolCallStart(index=idx, id=tid, name=name)
            yield ToolCallArgsDelta(index=idx, args_chunk=args_str)
            yield ToolCallEnd(index=idx)
        elif ev_type == "message":
            if data.get("role") == "assistant":
                content = data.get("content", "")
                if content:
                    self._last_message["content"] += content
                    yield ContentDelta(text=content)
        elif ev_type == "error":
            yield ApiError(status_code=None, message=data.get("message", "CLI Error"))
        elif ev_type == "usage":
            yield Usage(
                prompt_tokens=data.get("prompt_tokens", 0),
                completion_tokens=data.get("completion_tokens", 0),
                cache_hit_tokens=data.get("cache_hit_tokens", 0),
                cache_miss_tokens=data.get("cache_miss_tokens", 0),
            )
