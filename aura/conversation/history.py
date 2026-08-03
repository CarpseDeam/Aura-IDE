"""Conversation history with the DeepSeek thinking-mode replay rule.

THE TRAP — single point of truth.

The API requires that `reasoning_content` be passed back on ALL assistant
messages that contain it, whether or not `tool_calls` is present.  Omitting it
can result in:
    400 — "The reasoning_content in the thinking mode must be passed back to the API."

`History.append_assistant(...)` ALWAYS stores the full message (including
reasoning_content). `History.for_api()` is the only place that decides what to
send — and the rule is: never strip `reasoning_content`.

SECOND RULE — `messages` is canonical.

Fitting a context window is a property of one outbound request, not of the
conversation. `for_api()` / `build_api_payload()` compact a deep copy (see
`aura.conversation.api_view`); the stored messages stay exact and durable so
the transcript, the GUI, and any later turn still see what really happened.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from aura.config import MAX_CONTEXT_TOKENS
from aura.conversation.api_view import (
    SOURCE_READ_TOOLS,
    ApiView,
    build_api_view,
    compact_result_content,
    estimate_message_tokens,
    estimate_tokens,
    is_real_user_message,
    is_step_boundary_message,
    repair_tool_call_blocks,
    user_message_text,
)

__all__ = ["History", "SOURCE_READ_TOOLS"]


@dataclass
class History:
    """Internal conversation log. Source of truth for the GUI and the API.

    Internal entries are exact dicts ready to send. The only transformation the
    API needs is in for_api(), which always preserves reasoning_content on
    assistant messages and never edits the stored log.
    """

    system_prompt: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    # ---- mutation -----------------------------------------------------------

    def set_system(self, prompt: str | None) -> None:
        self.system_prompt = prompt

    def append_user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def append_internal_user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text, "aura_internal": True})

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

    def repair_incomplete_tool_calls(self) -> int:
        """Remove tool-call blocks that cannot be replayed to chat APIs.

        This is a durable structural repair of the stored log, used on rewind.
        The API view runs the same repair on its own copy, so a request is
        never poisoned even when the canonical log is left untouched.
        """
        return repair_tool_call_blocks(self.messages)

    def latest_real_user_index(self) -> int | None:
        """Index of the newest genuine user request, or None if there is none.

        Aura's own steering messages are ``role="user"`` but carry
        ``aura_internal``; they never define the turn being retried or rewound.
        """
        for i in range(len(self.messages) - 1, -1, -1):
            if is_real_user_message(self.messages[i]):
                return i
        return None

    def latest_real_user_text(self) -> str | None:
        """Text of the newest genuine user request, multimodal parts flattened."""
        index = self.latest_real_user_index()
        if index is None:
            return None
        return user_message_text(self.messages[index])

    def rewind_to_last_user_turn(self) -> bool:
        """Keep history through the last user message and drop its response.

        Used by retry/rerun actions. If the latest turn ended in an error,
        cancellation, partial assistant output, or a normal assistant answer,
        the next send should replay the same user request against the context
        that existed at that point. Internal steering appended after that
        request is part of the discarded response, not a turn to stop at.
        """
        self.repair_incomplete_tool_calls()
        index = self.latest_real_user_index()
        if index is None:
            return False
        self.truncate_after(index + 1)
        return True

    # ---- token estimation ---------------------------------------------------

    def estimate_tokens(self) -> int:
        """Rough token count for the full history (system + all messages).

        Approximation: len(text) / 4. Good enough for budget decisions;
        DeepSeek's tokenizer is BPE-based but the ratio is close enough.
        """
        return estimate_tokens(self.messages, self.system_prompt)

    def _msg_token_estimate(self, msg: dict) -> int:
        """Estimate tokens for a single message dict."""
        return estimate_message_tokens(msg)

    def _turn_indices(self) -> list[int]:
        """Return the message indices where each user-turn begins."""
        return [i for i, m in enumerate(self.messages) if m.get("role") == "user"]

    def _get_tool_name_for_result(self, msg_idx: int) -> str | None:
        """Return the tool name for the tool-result message at msg_idx.

        Scans backwards to find the assistant message whose tool_calls contains
        a call matching the tool_call_id of the result.
        """
        target_id = self.messages[msg_idx].get("tool_call_id")
        if not target_id:
            return None
        for i in range(msg_idx - 1, -1, -1):
            msg = self.messages[i]
            if msg.get("role") == "assistant":
                for tc in (msg.get("tool_calls") or []):
                    if tc.get("id") == target_id:
                        fn = tc.get("function")
                        return fn.get("name") if isinstance(fn, dict) else None
        return None

    def prune_for_context(
        self,
        max_tokens: int = MAX_CONTEXT_TOKENS,
        keep_last_n_turns: int = 5,
        max_tool_result_chars: int = 500,
    ) -> None:
        """Destructively shrink the stored log to fit `max_tokens`.

        NOT on the send path — `for_api()` compacts a copy instead. This remains
        for callers that genuinely want the stored log reduced (offline tools,
        smoke scripts). It reuses the structure-preserving compaction, so tool
        results stay valid JSON here too.
        """
        if self.estimate_tokens() <= max_tokens:
            return

        view = build_api_view(
            self.system_prompt,
            self.messages,
            max_tokens,
            keep_last_n_turns=keep_last_n_turns,
        )
        # The completed-step boundary is a transient ``aura_internal`` marker
        # that ``_render`` emits without its flag; it must never be stored.
        self.messages = [
            m
            for m in view.messages
            if m.get("role") != "system" and not is_step_boundary_message(m)
        ]

    def _truncate_tool_results_in_range(
        self,
        start: int,
        end: int,
        max_chars: int,
        source_tool_min_chars: int = 0,
    ) -> None:
        """Compact tool-result messages in messages[start:end] to max_chars.

        Structure-preserving: a JSON result stays valid JSON. Results from
        SOURCE_READ_TOOLS keep the higher `source_tool_min_chars` allowance.
        """
        for i in range(start, min(end, len(self.messages))):
            msg = self.messages[i]
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue

            tool_name = self._get_tool_name_for_result(i)
            allowance = max_chars
            if source_tool_min_chars > max_chars and tool_name in SOURCE_READ_TOOLS:
                allowance = source_tool_min_chars

            new_content, changed = compact_result_content(content, allowance, tool_name)
            if changed:
                msg["content"] = new_content

    # ---- API view -----------------------------------------------------------

    def build_api_payload(
        self,
        max_tokens: int | None = None,
        *,
        effect_for=None,
    ) -> ApiView:
        """Build the outbound view plus its compaction diagnostics.

        `max_tokens` is the active model's working-set budget. `effect_for` is
        the authoritative tool-effect lookup that decides which completed
        blocks may retire (the send path passes the registry's
        ``declared_effect``). Nothing here mutates `self.messages`.
        """
        budget = max_tokens if max_tokens and max_tokens > 0 else MAX_CONTEXT_TOKENS
        return build_api_view(
            self.system_prompt, self.messages, budget, effect_for=effect_for
        )

    def for_api(self, max_tokens: int | None = None) -> list[dict[str, Any]]:
        """Build the messages array for the next API call.

        Rules:
        - Always include system message (if set) first.
        - For assistant messages: always keep reasoning_content if present,
          regardless of whether tool_calls exists.
        - User and tool messages are passed through verbatim.
        - Stored history is never modified.
        """
        return self.build_api_payload(max_tokens).messages

    # ---- introspection ------------------------------------------------------

    def __len__(self) -> int:
        return len(self.messages)
