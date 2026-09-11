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

# Official V3.2 encoding plus Aura's previously supported dialect.
# https://huggingface.co/deepseek-ai/DeepSeek-V3.2/blob/main/encoding/encoding_dsv32.py
_DIALECTS = (("｜DSML｜", "function_calls"), ("｜｜DSML｜｜", "tool_calls"))
_START_TAGS = {f"<{token}{name}>": (token, f"</{token}{name}>") for token, name in _DIALECTS}


class DsmlParser:
    def __init__(self, start_index: int = 0) -> None:
        self._buffer = ""
        self._in_tool_block = False
        self._token = ""
        self._close_tag = ""
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
                if self._close_tag in self._buffer:
                    idx = self._buffer.find(self._close_tag)
                    block = self._buffer[:idx]
                    self._buffer = self._buffer[idx + len(self._close_tag) :]
                    self._in_tool_block = False
                    yield from self._parse_block(block)
                else:
                    # Still inside block, wait for more chunks to close it
                    break
            else:
                starts = [(self._buffer.find(tag), tag) for tag in _START_TAGS if tag in self._buffer]
                if starts:
                    idx, tag = min(starts)
                    if idx > 0:
                        yield ContentDelta(text=self._buffer[:idx])
                    self._buffer = self._buffer[idx + len(tag) :]
                    self._token, self._close_tag = _START_TAGS[tag]
                    self._in_tool_block = True
                else:
                    idx = self._buffer.rfind("<")
                    if idx != -1:
                        suffix = self._buffer[idx:]
                        if any(tag.startswith(suffix) for tag in _START_TAGS):
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
        token = re.escape(self._token)
        invoke_re = re.compile(
            rf'<{token}invoke\s+name="([^"]+)"\s*>(.*?)</{token}invoke\s*>', re.DOTALL
        )
        param_re = re.compile(
            rf'<{token}parameter\s+name="([^"]+)"(?:\s+string="(true|false)")?\s*>'
            rf'(.*?)</{token}parameter\s*>', re.DOTALL
        )
        invokes = list(invoke_re.finditer(block))
        if not invokes:
            if block.strip():
                yield ApiError(status_code=None, message="Malformed DSML tool block: no invoke tags found.")
            else:
                yield ApiError(status_code=None, message="Empty or whitespace-only DSML tool calls block.")
            return

        # Reject incomplete/extra structure instead of silently executing a
        # subset of the supplied arguments or calls.
        if invoke_re.sub("", block).strip():
            yield ApiError(status_code=None, message="Malformed DSML tool block: invalid invoke structure.")
            return

        decoded = []
        for match in invokes:
            name = match.group(1)
            inner_content = match.group(2)

            params = {}
            if param_re.sub("", inner_content).strip():
                yield ApiError(status_code=None, message=f"Malformed DSML arguments for {name}.")
                return
            for p_match in param_re.finditer(inner_content):
                p_name = p_match.group(1)
                is_string = p_match.group(2)
                p_val = p_match.group(3)
                if p_name in params:
                    yield ApiError(status_code=None, message=f"Duplicate DSML argument: {p_name}.")
                    return
                if is_string == "false":
                    try:
                        p_val = json.loads(p_val, parse_constant=_reject_json_constant)
                    except ValueError as exc:
                        yield ApiError(status_code=None, message=f"Invalid DSML JSON argument {p_name}: {exc}")
                        return
                # Missing string attributes retain the legacy literal-string
                # behavior. Never infer types from a string's contents.
                params[p_name] = p_val
            decoded.append((name, params))

        for name, params in decoded:
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


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"{value} is not a JSON value")
