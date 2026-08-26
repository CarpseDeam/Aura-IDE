"""Conversation history — the exact, append-only canonical transcript.

ONE RULE — `messages` is canonical; provider clients own the wire projection.

`History.append_assistant(...)` always stores the full assistant message,
including its ``reasoning_content`` and the provider's ``reasoning_signature``.
The stored messages stay exact and durable so the transcript, the GUI,
persistence, and usage evidence always see what really happened.

`for_api()` is the canonical transcript snapshot boundary. It prepends the
system message, deep-copies the stored messages so a caller cannot mutate the
log through the snapshot, and removes only local bookkeeping markers. The
snapshot is not itself a universal wire request: each client/protocol layer may
project it into its provider's message or item format. There is no compaction
ladder, token budget, or canonical-history rewrite; a round's own plan and
reasoning remain durable for UI, persistence, diagnostics, cancellation, and
replay inspection.

How a given wire protocol encodes the snapshot is decided in the client layer,
from this unchanged canonical history. DeepSeek Responses deliberately omits
prior reasoning items and ``reasoning_content`` from every continuation.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "EXPLICIT_INSTALLED_SKILL_IDS_KEY",
    "LITERAL_COMPOSER_TEXT_KEY",
    "History",
    "is_real_user_message",
    "user_message_text",
]

# Aura-local bookkeeping key: the exact text the user typed into the composer
# for this turn, stored on the canonical user message before any attachment
# reference block is folded into its content. It is durable (persistence keeps
# the canonical message dicts verbatim) and local-only — `for_api()` strips it,
# so it never reaches a provider.
LITERAL_COMPOSER_TEXT_KEY = "aura_literal_composer_text"

# Aura-local bookkeeping key: the ordered stable installed identities the user
# explicitly selected for this turn. Persistence keeps this canonical metadata
# verbatim; ``for_api()`` strips it before any provider projection.
EXPLICIT_INSTALLED_SKILL_IDS_KEY = "aura_explicit_installed_skill_ids"


def is_real_user_message(msg: dict[str, Any]) -> bool:
    """True only for a user message that actually starts a new request.

    Aura injects its own ``role="user"`` messages (steering nudges, recovery
    notices, loop guards) marked ``aura_internal``. They belong in the request —
    the model must see them — but they are *not* new user requests, so a rewind
    never stops at one.
    """
    return (
        msg.get("role") == "user"
        and not msg.get("aura_internal")
    )


def user_message_text(msg: dict[str, Any]) -> str:
    """Plain text of a user message, flattening multimodal text parts."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _attach_literal_composer_text(
    message: dict[str, Any], literal_composer_text: str | None
) -> None:
    """Record the literal composer text on a user message, when there is one."""
    if literal_composer_text is not None:
        message[LITERAL_COMPOSER_TEXT_KEY] = literal_composer_text


def _attach_explicit_installed_skill_ids(
    message: dict[str, Any], explicit_installed_skill_ids: tuple[str, ...]
) -> None:
    """Record only the ordered stable identities, omitting empty metadata."""
    if explicit_installed_skill_ids:
        message[EXPLICIT_INSTALLED_SKILL_IDS_KEY] = list(
            explicit_installed_skill_ids
        )


def repair_tool_call_blocks(messages: list[dict[str, Any]]) -> int:
    """Remove tool-call blocks that cannot be replayed. Mutates `messages`.

    Chat APIs require every assistant message carrying ``tool_calls`` to be
    followed by tool messages for exactly those ids. An interrupted turn can
    leave a call with no result — that block poisons every later request until
    it is gone. Returns the number of messages removed.

    This is a structural repair of the stored log, run on rewind and cancel. It
    is the only edit history ever makes to itself, and it exists because such a
    block is not replayable at all, not because it is large.
    """
    removed = 0
    i = 0
    while i < len(messages):
        msg = messages[i]

        if msg.get("role") == "tool":
            del messages[i]
            removed += 1
            continue

        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            i += 1
            continue

        tool_calls = msg.get("tool_calls") or []
        expected_ids = [
            tc.get("id") for tc in tool_calls if isinstance(tc, dict) and tc.get("id")
        ]
        expected = set(expected_ids)
        seen: set[str] = set()
        valid_block = bool(expected) and len(expected) == len(expected_ids)

        j = i + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            tool_call_id = messages[j].get("tool_call_id")
            if tool_call_id not in expected or tool_call_id in seen:
                valid_block = False
            else:
                seen.add(tool_call_id)
            j += 1

        if valid_block and seen == expected:
            i = j
            continue

        removed += j - i
        del messages[i:j]

    return removed


@dataclass
class History:
    """Internal conversation log. Source of truth for UI, persistence, and replay.

    Internal entries are exact canonical dicts. ``for_api()`` returns a deep
    copied canonical snapshot with the system message in front; protocol
    clients own any subsequent wire projection.
    """

    system_prompt: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    # ---- mutation -----------------------------------------------------------

    def set_system(self, prompt: str | None) -> None:
        self.system_prompt = prompt

    def append_user_text(
        self,
        text: str,
        *,
        literal_composer_text: str | None = None,
        explicit_installed_skill_ids: tuple[str, ...] = (),
    ) -> None:
        """Append a real user turn.

        ``literal_composer_text`` is the exact text the user typed, recorded
        before attachment reference blocks were folded into ``text``. The
        composer is the only caller that can supply it.
        """
        message: dict[str, Any] = {"role": "user", "content": text}
        _attach_literal_composer_text(message, literal_composer_text)
        _attach_explicit_installed_skill_ids(
            message, explicit_installed_skill_ids
        )
        self.messages.append(message)

    def append_internal_user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text, "aura_internal": True})

    def append_user_multimodal(
        self,
        parts: list[dict[str, Any]],
        *,
        literal_composer_text: str | None = None,
        explicit_installed_skill_ids: tuple[str, ...] = (),
    ) -> None:
        """For image+text turns: parts is a list like
        [{"type":"text","text":"..."}, {"type":"image_url","image_url":{"url":"data:..."}}].

        ``literal_composer_text`` carries the same meaning as in
        :meth:`append_user_text`.
        """
        message: dict[str, Any] = {"role": "user", "content": parts}
        _attach_literal_composer_text(message, literal_composer_text)
        _attach_explicit_installed_skill_ids(
            message, explicit_installed_skill_ids
        )
        self.messages.append(message)

    def append_assistant(self, full_message: dict[str, Any]) -> None:
        """Append the *complete* assistant message — content, tool calls,
        reasoning and its signature. All of it is replayed on the next round."""
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
        """Remove tool-call blocks that cannot be replayed to chat APIs."""
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

    def latest_real_user_literal_composer_text(self) -> str | None:
        """The exact composer text of the newest genuine user request.

        ``None`` when that message carries no literal-composer metadata: a
        record written before this field existed, or a user message Aura
        appended from somewhere other than the composer. Callers that gate
        authority on what the user actually typed must treat that as "the user
        typed nothing" and never fall back to the stored message content — that
        content also carries attachment reference blocks Aura generated, which
        were never user-authored authority.
        """
        index = self.latest_real_user_index()
        if index is None:
            return None
        value = self.messages[index].get(LITERAL_COMPOSER_TEXT_KEY)
        return value if isinstance(value, str) else None

    def latest_real_user_explicit_installed_skill_ids(self) -> tuple[str, ...]:
        """Ordered explicit installed identities stored on the latest real turn.

        Older messages and malformed non-sequence metadata resolve to an empty
        tuple. This intentionally never infers selections from message content.
        """
        index = self.latest_real_user_index()
        if index is None:
            return ()
        value = self.messages[index].get(EXPLICIT_INSTALLED_SKILL_IDS_KEY)
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(item for item in value if isinstance(item, str))

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

    # ---- the provider request -----------------------------------------------

    def for_api(self) -> list[dict[str, Any]]:
        """Return a deep-copied canonical snapshot for a provider client.

        The system message (if any) first, then a deep copy of every stored
        message with local bookkeeping markers removed. Everything else in the
        canonical transcript — content, tool calls, tool results, reasoning
        content, and provider-specific diagnostic fields — remains durable for
        inspection. The client/protocol layer decides what belongs on the wire.
        """
        out: list[dict[str, Any]] = []
        if self.system_prompt:
            out.append({"role": "system", "content": self.system_prompt})
        for msg in copy.deepcopy(self.messages):
            msg.pop("aura_internal", None)
            msg.pop(LITERAL_COMPOSER_TEXT_KEY, None)
            msg.pop(EXPLICIT_INSTALLED_SKILL_IDS_KEY, None)
            out.append(msg)
        return out

    # ---- introspection ------------------------------------------------------

    def __len__(self) -> int:
        return len(self.messages)
