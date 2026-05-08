"""Conversation history with the DeepSeek thinking-mode replay rule.

THE TRAP — single point of truth.

The API requires that `reasoning_content` be passed back on ALL assistant
messages that contain it, whether or not `tool_calls` is present.  Omitting it
can result in:
    400 — "The reasoning_content in the thinking mode must be passed back to the API."

`History.append_assistant(...)` ALWAYS stores the full message (including
reasoning_content). `History.for_api()` is the only place that decides what to
strip on the way out — and the rule is: never strip `reasoning_content`.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from aura.config import MAX_CONTEXT_TOKENS, TRUNCATE_TOOL_RESULT_CHARS


@dataclass
class History:
    """Internal conversation log. Source of truth for the GUI and the API.

    Internal entries are exact dicts ready to send. The only transformation the
    API needs is in for_api(), which always preserves reasoning_content on
    assistant messages.
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

    # ---- token estimation & pruning -----------------------------------------

    def estimate_tokens(self) -> int:
        """Rough token count for the full history (system + all messages).

        Approximation: len(text) / 4. Good enough for sliding-window pruning;
        DeepSeek's actual tokenizer is BPE-based, but the ratio is close enough
        that 60K chars / 4 = 15K tokens keeps us safely under 64K.
        """
        total = 0
        if self.system_prompt:
            total += len(self.system_prompt) // 4
        for msg in self.messages:
            total += self._msg_token_estimate(msg)
        return total

    def _msg_token_estimate(self, msg: dict) -> int:
        """Estimate tokens for a single message dict."""
        tokens = 0
        content = msg.get("content")
        if isinstance(content, str):
            tokens += len(content) // 4
        elif isinstance(content, list):
            # Multimodal content list (user messages with images)
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    tokens += len(part.get("text", "")) // 4
        rc = msg.get("reasoning_content")
        if isinstance(rc, str):
            tokens += len(rc) // 4
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                tokens += len(json.dumps(tc)) // 4
        return tokens

    def _turn_indices(self) -> list[int]:
        """Return the message indices where each user-turn begins.

        A "turn" is a user message plus all assistant/tool messages that follow
        until the next user message. The returned list contains the index of each
        user message in self.messages.  If the list is empty there are no turns.
        """
        indices: list[int] = []
        for i, msg in enumerate(self.messages):
            if msg.get("role") == "user":
                indices.append(i)
        return indices

    def prune_for_context(
        self,
        max_tokens: int = 60_000,
        keep_last_n_turns: int = 5,
        max_tool_result_chars: int = 500,
    ) -> None:
        """Prune history in-place to fit within max_tokens.

        Preserves:
        - System prompt (always)
        - The first user turn (to retain the user's original request/context)
        - The last keep_last_n_turns user turns (the recent conversation)

        Strategy:
        1. If already under limit, return immediately.
        2. For turns NOT in the preserved set, truncate large tool results
           (role=="tool") to max_tool_result_chars + a summary marker.
        3. If still over limit, drop entire middle turns (oldest first) until
           under the budget.

        A "turn" is all messages from a user message up to (but not including)
        the next user message. Assistant and tool messages belong to the turn
        that starts with the user message immediately preceding them.
        """
        if self.estimate_tokens() <= max_tokens:
            return

        turn_starts = self._turn_indices()
        if not turn_starts:
            # No user messages — nothing to prune. Just truncate large tool
            # results globally as a fallback.
            self._truncate_tool_results_in_range(0, len(self.messages), max_tool_result_chars)
            return

        num_turns = len(turn_starts)
        preserved: set[int] = set()

        # Always preserve the first turn.
        preserved.add(0)

        # Preserve the last keep_last_n_turns turns.
        for t in range(max(0, num_turns - keep_last_n_turns), num_turns):
            preserved.add(t)

        # --- Pass 1: truncate large tool results in non-preserved turns ---
        for turn_idx in range(num_turns):
            if turn_idx in preserved:
                continue
            start = turn_starts[turn_idx]
            end = turn_starts[turn_idx + 1] if turn_idx + 1 < num_turns else len(self.messages)
            self._truncate_tool_results_in_range(start, end, max_tool_result_chars)

        if self.estimate_tokens() <= max_tokens:
            return

        # --- Pass 1.5: if still over budget, aggressively truncate tool results
        # in preserved turns too (the current turn may have ballooned).
        if self.estimate_tokens() > max_tokens:
            aggressive_chars = max_tool_result_chars // 2
            for turn_idx in range(num_turns):
                if turn_idx not in preserved:
                    continue  # already done above
                start = turn_starts[turn_idx]
                end = turn_starts[turn_idx + 1] if turn_idx + 1 < num_turns else len(self.messages)
                self._truncate_tool_results_in_range(start, end, aggressive_chars)

        if self.estimate_tokens() <= max_tokens:
            return

        # --- Pass 2: drop entire middle turns ---
        # Find turns that are neither first nor in the last N.
        droppable = sorted(
            [t for t in range(num_turns) if t not in preserved],
            reverse=True,  # drop oldest middle turns first
        )
        for turn_idx in droppable:
            if self.estimate_tokens() <= max_tokens:
                return
            start = turn_starts[turn_idx]
            end = turn_starts[turn_idx + 1] if turn_idx + 1 < num_turns else len(self.messages)
            # Replace messages in this range with a single user message
            # that summarizes what was dropped, so the model knows context
            # was removed.
            dropped_count = end - start
            user_msg = self.messages[start]  # the user message that started this turn
            summary = (
                f"[Earlier conversation pruned to stay within context limit. "
                f"A turn with {dropped_count} messages was removed. "
                f"The user had said: \"{user_msg.get('content', '')[:200]}\"]"
            )
            replacement = {"role": "user", "content": summary}
            self.messages[start:end] = [replacement]
            # Rebuild turn indices since we mutated the list.
            turn_starts = self._turn_indices()
            # Rebuild droppable — recalculate preserved and droppable
            num_turns = len(turn_starts)
            preserved = {0} | set(range(max(0, num_turns - keep_last_n_turns), num_turns))
            droppable = sorted(
                [t for t in range(num_turns) if t not in preserved],
                reverse=True,
            )

    def _truncate_tool_results_in_range(
        self, start: int, end: int, max_chars: int
    ) -> None:
        """Truncate tool-result messages in messages[start:end] to max_chars."""
        for i in range(start, min(end, len(self.messages))):
            msg = self.messages[i]
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > max_chars:
                truncated = content[:max_chars]
                msg["content"] = (
                    f"{truncated}\n\n[... result truncated from "
                    f"{len(content)} to {max_chars} chars to save context ...]"
                )

    # ---- API view -----------------------------------------------------------

    def for_api(self) -> list[dict[str, Any]]:
        """Build the messages array for the next API call.

        Rules:
        - Always include system message (if set) first.
        - For assistant messages: always keep reasoning_content if present,
          regardless of whether tool_calls exists.
        - User and tool messages are passed through verbatim.
        """
        # Safety: prune before building API view so we never send a
        # context-exceeding payload.
        self.prune_for_context()

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
            rc = msg.get("reasoning_content")
            if rc:
                api_msg["reasoning_content"] = rc
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                api_msg["tool_calls"] = copy.deepcopy(tool_calls)
            out.append(api_msg)

        return out

    # ---- introspection ------------------------------------------------------

    def __len__(self) -> int:
        return len(self.messages)
