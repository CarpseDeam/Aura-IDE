"""Conversation history with the DeepSeek thinking-mode replay rule.

THE TRAP — single point of truth.

If an assistant turn contained `tool_calls`, its `reasoning_content` MUST be
passed back to the API in all subsequent requests, otherwise the API returns:
    400 — "The reasoning_content in the thinking mode must be passed back to the API."

If the assistant turn did NOT contain `tool_calls`, `reasoning_content` from prior
turns is ignored by the API; sending it is harmless but pointless. We strip it for
cleanliness.

`History.append_assistant(...)` ALWAYS stores the full message (including
reasoning_content). `History.for_api()` is the only place that decides what to
strip on the way out.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class History:
    """Internal conversation log. Source of truth for the GUI and the API.

    Internal entries are exact dicts ready to send. The only transformation the
    API needs is the reasoning_content strip in for_api().
    """

    system_prompt: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    # ---- mutation -----------------------------------------------------------

    def set_system(self, prompt: str | None) -> None:
        self.system_prompt = prompt

    def append_user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def append_user_multimodal(
        self, parts: list[dict[str, Any]]
    ) -> None:
        """For image+text turns: parts is a list like
        [{"type":"text","text":"..."}, {"type":"image_url","image_url":{"url":"data:..."}}].
        """
        self.messages.append({"role": "user", "content": parts})

    def append_assistant(self, full_message: dict[str, Any]) -> None:
        """Append the *complete* assistant message — keep reasoning_content in
        storage even if not currently relevant; for_api() decides what to send."""
        self.messages.append(copy.deepcopy(full_message))

    def append_tool_result(self, tool_call_id: str, content: str) -> None:
        self.messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        )

    def truncate_after(self, index: int) -> None:
        """Drop messages at `index` and beyond (used on cancel / rewind)."""
        self.messages = self.messages[:index]

    def pop_if_empty_assistant_message(self) -> None:
        """Remove the last message if it's an empty assistant message."""
        if not self.messages:
            return
        last = self.messages[-1]
        if last.get("role") != "assistant":
            return
        if last.get("content") or last.get("reasoning_content") or last.get("tool_calls"):
            return
        self.messages.pop()

    # ---- API view -----------------------------------------------------------

    def for_api(self) -> list[dict[str, Any]]:
        """Build the messages array for the next API call.

        Rules:
        - Always include system message (if set) first.
        - For assistant messages: keep reasoning_content ONLY if tool_calls is
          present on that message. Otherwise strip it.
        - User and tool messages are passed through verbatim.
        """
        out: list[dict[str, Any]] = []
        if self.system_prompt:
            out.append({"role": "system", "content": self.system_prompt})

        for msg in self.messages:
            if msg.get("role") != "assistant":
                out.append(copy.deepcopy(msg))
                continue
            api_msg: dict[str, Any] = {
                "role": "assistant",
                "content": msg.get("content"),
            }
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                api_msg["tool_calls"] = copy.deepcopy(tool_calls)
                rc = msg.get("reasoning_content")
                if rc:
                    api_msg["reasoning_content"] = rc
            # else: strip reasoning_content entirely — not needed by API.
            out.append(api_msg)

        return out

    # ---- introspection ------------------------------------------------------

    def __len__(self) -> int:
        return len(self.messages)
