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

# Tools whose results carry source code that the Worker needs to read and act on.
# These get a higher truncation floor to avoid starvation during active coding tasks.
SOURCE_READ_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "read_files",
    "read_file_range",
    "grep_search",
    "find_usages",
    "read_file_outline",
})

# Minimum chars kept for source-reading tool results when under pressure.
_SOURCE_FLOOR_CHARS: int = 8_000
# Moderate cap for preserved-but-not-current turns.
_MODERATE_CHARS: int = 2_000



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

    def repair_incomplete_tool_calls(self) -> int:
        """Remove tool-call blocks that cannot be replayed to chat APIs.

        Chat APIs require every assistant message with ``tool_calls`` to be
        followed by tool messages for exactly those call IDs. Interrupted or
        partially persisted turns can leave an assistant tool-call message with
        no result, or with only some results. Such a block poisons every future
        request until it is removed.
        """
        removed = 0
        i = 0
        while i < len(self.messages):
            msg = self.messages[i]

            if msg.get("role") == "tool":
                del self.messages[i]
                removed += 1
                continue

            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                i += 1
                continue

            tool_calls = msg.get("tool_calls") or []
            expected_ids = [
                tc.get("id")
                for tc in tool_calls
                if isinstance(tc, dict) and tc.get("id")
            ]
            expected = set(expected_ids)
            seen: set[str] = set()
            valid_block = bool(expected) and len(expected) == len(expected_ids)

            j = i + 1
            while j < len(self.messages) and self.messages[j].get("role") == "tool":
                tool_call_id = self.messages[j].get("tool_call_id")
                if tool_call_id not in expected or tool_call_id in seen:
                    valid_block = False
                else:
                    seen.add(tool_call_id)
                j += 1

            if valid_block and seen == expected:
                i = j
                continue

            removed += j - i
            del self.messages[i:j]

        return removed

    def rewind_to_last_user_turn(self) -> bool:
        """Keep history through the last user message and drop its response.

        Used by retry/rerun actions. If the latest turn ended in an error,
        cancellation, partial assistant output, or a normal assistant answer,
        the next send should replay the same user request against the context
        that existed at that point.
        """
        self.repair_incomplete_tool_calls()
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                self.truncate_after(i + 1)
                return True
        return False

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

    def _get_tool_name_for_result(self, msg_idx: int) -> str | None:
        """Return the tool name for the tool-result message at msg_idx.

        Scans backwards through history to find the assistant message whose
        tool_calls contains a call matching the tool_call_id of the result.
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
        max_tokens: int = 60_000,
        keep_last_n_turns: int = 5,
        max_tool_result_chars: int = 500,
    ) -> None:
        """Prune history in-place to fit within max_tokens.

        Preserves:
        - System prompt (always)
        - The first user turn (original request context)
        - The last keep_last_n_turns user turns (recent conversation)

        Priority order — each pass only runs if still over budget:
          1. Truncate tool results in non-preserved (old middle) turns.
             Source-reading tools keep a moderate floor even here.
          2. Drop entire non-preserved turns oldest-first (summary placeholder).
             Dropping old turns is always preferable to crushing the current turn.
          3. Truncate preserved-but-not-current turns with a moderate cap.
             Source-reading tools keep an 8 KB floor.
          4. Last resort: truncate the current (active) turn.
             Source-reading tools keep an 8 KB floor; other tools get 2 KB.
          5. Truly last resort: reduce current-turn source floor to 2 KB.

        A "turn" is all messages from a user message up to (but not including)
        the next user message.
        """
        if self.estimate_tokens() <= max_tokens:
            return

        turn_starts = self._turn_indices()
        if not turn_starts:
            self._truncate_tool_results_in_range(0, len(self.messages), max_tool_result_chars)
            return

        num_turns = len(turn_starts)

        def _preserved(n: int) -> set[int]:
            p: set[int] = {0}
            for t in range(max(0, n - keep_last_n_turns), n):
                p.add(t)
            return p

        def turn_range(t: int) -> tuple[int, int]:
            s = turn_starts[t]
            e = turn_starts[t + 1] if t + 1 < num_turns else len(self.messages)
            return s, e

        preserved = _preserved(num_turns)
        current_turn_idx = num_turns - 1

        # --- Pass 1: truncate tool results in non-preserved turns ---
        # Source-reading tools keep a moderate 2 KB floor even in old turns.
        for t in range(num_turns):
            if t in preserved:
                continue
            s, e = turn_range(t)
            self._truncate_tool_results_in_range(
                s, e, max_tool_result_chars,
                source_tool_min_chars=_MODERATE_CHARS,
            )

        if self.estimate_tokens() <= max_tokens:
            return

        # --- Pass 2: drop entire non-preserved turns (oldest first) ---
        # Dropping old turns is always better than crushing the current turn.
        droppable = sorted(t for t in range(num_turns) if t not in preserved)
        for t in droppable:
            if self.estimate_tokens() <= max_tokens:
                return
            s, e = turn_range(t)
            dropped_count = e - s
            user_msg = self.messages[s]
            summary = (
                f"[Earlier conversation pruned to stay within context limit. "
                f"A turn with {dropped_count} messages was removed. "
                f"The user had said: \"{user_msg.get('content', '')[:200]}\"]"
            )
            self.messages[s:e] = [{"role": "user", "content": summary}]
            # Rebuild indices after mutation.
            turn_starts = self._turn_indices()
            num_turns = len(turn_starts)
            current_turn_idx = num_turns - 1
            preserved = _preserved(num_turns)
            droppable = sorted(t for t in range(num_turns) if t not in preserved)

        if self.estimate_tokens() <= max_tokens:
            return

        # --- Pass 3: truncate preserved turns EXCEPT the current (last) turn ---
        # Non-source tools: moderate cap (2 KB). Source tools: 8 KB floor.
        for t in range(num_turns):
            if t not in preserved or t == current_turn_idx:
                continue
            s, e = turn_range(t)
            self._truncate_tool_results_in_range(
                s, e, _MODERATE_CHARS,
                source_tool_min_chars=_SOURCE_FLOOR_CHARS,
            )

        if self.estimate_tokens() <= max_tokens:
            return

        # --- Pass 4: last resort — truncate the current (active) turn ---
        # Non-source tools: 2 KB. Source-reading tools: 8 KB floor.
        s = turn_starts[current_turn_idx]
        self._truncate_tool_results_in_range(
            s, len(self.messages), _MODERATE_CHARS,
            source_tool_min_chars=_SOURCE_FLOOR_CHARS,
        )

        if self.estimate_tokens() <= max_tokens:
            return

        # --- Pass 5: truly last resort — reduce source floor to 2 KB ---
        self._truncate_tool_results_in_range(
            turn_starts[current_turn_idx], len(self.messages),
            _MODERATE_CHARS,
            source_tool_min_chars=_MODERATE_CHARS,
        )

    def _truncate_tool_results_in_range(
        self,
        start: int,
        end: int,
        max_chars: int,
        source_tool_min_chars: int = 0,
    ) -> None:
        """Truncate tool-result messages in messages[start:end] to max_chars.

        If source_tool_min_chars > max_chars, results from SOURCE_READ_TOOLS
        are kept at max(max_chars, source_tool_min_chars) instead.

        Truncation markers include original length, new length, and tool name
        so the Worker can recover by re-reading specific ranges.
        """
        for i in range(start, min(end, len(self.messages))):
            msg = self.messages[i]
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue

            actual_max = max_chars
            tool_name: str | None = None
            if source_tool_min_chars > max_chars:
                tool_name = self._get_tool_name_for_result(i)
                if tool_name in SOURCE_READ_TOOLS:
                    actual_max = source_tool_min_chars

            if len(content) > actual_max:
                if tool_name is None:
                    tool_name = self._get_tool_name_for_result(i)
                original_len = len(content)
                msg["content"] = (
                    f"{content[:actual_max]}\n\n"
                    f"[... result truncated: {original_len} chars -> {actual_max} chars "
                    f"(tool: {tool_name or 'unknown'}). "
                    f"Use grep_search or read_file_outline to find exact line numbers, "
                    f"then use read_file_range with those line numbers to read the missing section ...]"
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
        self.repair_incomplete_tool_calls()
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
