"""DSML parsing for provider tool calls.

Some providers stream tool calls as literal DSML markup rather than native
JSON tool_calls. This parser intercepts that markup and converts it to standard
Aura ToolCall events.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from aura.client.events import (
    ApiError,
    ContentDelta,
    Event,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
)

# Regex to match invoke blocks and their parameters
_INVOKE_RE = re.compile(
    r'<\|\s*DSML\s*\|\s*invoke\s+name="([^"]+)"\s*>(.*?)</\|\s*DSML\s*\|\s*invoke\s*>',
    re.DOTALL,
)
_PARAM_RE = re.compile(
    r'<\|\s*DSML\s*\|\s*parameter\s+name="([^"]+)"[^>]*>(.*?)</\|\s*DSML\s*\|\s*parameter\s*>',
    re.DOTALL,
)

_START_TAG = "<| DSML | tool_calls>"
_CLOSE_TAG = "</| DSML | tool_calls>"


class DsmlParser:
    def __init__(self, start_index: int = 0) -> None:
        self._buffer = ""
        self._in_tool_block = False
        self._parsed_calls: list[dict[str, Any]] = []
        self._next_index = start_index
        self._next_id = 0

    def get_tool_calls(self) -> list[dict[str, Any]]:
        """Return the list of standard OpenAI-style tool calls parsed so far."""
        return list(self._parsed_calls)

    def push(self, chunk: str) -> Iterator[Event]:
        """Process a chunk of text, yielding ContentDelta or tool call events."""
        self._buffer += chunk

        while self._buffer:
            if self._in_tool_block:
                if _CLOSE_TAG in self._buffer:
                    idx = self._buffer.find(_CLOSE_TAG)
                    block = self._buffer[:idx]
                    self._buffer = self._buffer[idx + len(_CLOSE_TAG) :]
                    self._in_tool_block = False
                    yield from self._parse_block(block)
                else:
                    # Still inside block, wait for more chunks to close it
                    break
            else:
                if _START_TAG in self._buffer:
                    idx = self._buffer.find(_START_TAG)
                    if idx > 0:
                        yield ContentDelta(text=self._buffer[:idx])
                    self._buffer = self._buffer[idx + len(_START_TAG) :]
                    self._in_tool_block = True
                else:
                    idx = self._buffer.rfind("<")
                    if idx != -1:
                        suffix = self._buffer[idx:]
                        if _START_TAG.startswith(suffix):
                            # Partial match, yield prefix and keep suffix
                            if idx > 0:
                                yield ContentDelta(text=self._buffer[:idx])
                            self._buffer = suffix
                            break
                    # No partial match, yield whole buffer
                    yield ContentDelta(text=self._buffer)
                    self._buffer = ""

    def flush(self) -> Iterator[Event]:
        """Flush any remaining buffered content.

        If we are left inside an unclosed DSML block, it is malformed.
        """
        if self._in_tool_block:
            yield ApiError(status_code=None, message="Stream ended with unclosed DSML tool calls block.")
            self._buffer = ""
            self._in_tool_block = False
        elif self._buffer:
            yield ContentDelta(text=self._buffer)
            self._buffer = ""

    def _parse_block(self, block: str) -> Iterator[Event]:
        invokes = list(_INVOKE_RE.finditer(block))
        if not invokes:
            if block.strip():
                yield ApiError(status_code=None, message="Malformed DSML tool block: no invoke tags found.")
            else:
                yield ApiError(status_code=None, message="Empty or whitespace-only DSML tool calls block.")
            return

        for match in invokes:
            name = match.group(1)
            inner_content = match.group(2)

            params = {}
            for p_match in _PARAM_RE.finditer(inner_content):
                p_name = p_match.group(1)
                p_val = p_match.group(2)
                params[p_name] = p_val

            args_json = json.dumps(params)
            call_id = f"call_dsml_{self._next_id}"
            self._next_id += 1

            self._parsed_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": args_json},
                }
            )

            idx = self._next_index
            self._next_index += 1

            yield ToolCallStart(index=idx, id=call_id, name=name)
            yield ToolCallArgsDelta(index=idx, args_chunk=args_json)
            yield ToolCallEnd(index=idx)
