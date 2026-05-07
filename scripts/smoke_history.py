"""Smoke 3: unit test for History.for_api() — the multi-turn replay rule.

Required behavior:
- Assistant message with tool_calls keeps reasoning_content on the way out.
- Assistant message without tool_calls ALSO preserves reasoning_content.
- User/tool/system messages pass through.
"""
from __future__ import annotations

import sys

from aura.conversation.history import History

FAILURES: list[str] = []


def expect(label: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        FAILURES.append(label)


def main() -> int:
    h = History()
    h.set_system("you are aura")

    # Turn 1: user asks something that triggers a tool call.
    h.append_user_text("read README.md")
    h.append_assistant(
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "I should call read_file.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                }
            ],
        }
    )
    h.append_tool_result("call_1", '{"ok": true, "content": "hi"}')

    # Turn 2 (still same logical user turn): assistant answers with no tool_calls.
    h.append_assistant(
        {
            "role": "assistant",
            "content": "It says 'hi'.",
            "reasoning_content": "the file says hi",
        }
    )

    # New user turn — no tool call this time.
    h.append_user_text("thanks")
    h.append_assistant(
        {
            "role": "assistant",
            "content": "you're welcome",
            "reasoning_content": "polite reply",
        }
    )

    api_msgs = h.for_api()

    # System present.
    expect("system first", api_msgs[0]["role"] == "system" and api_msgs[0]["content"] == "you are aura")

    # Locate the tool_calls assistant message.
    tool_call_msgs = [m for m in api_msgs if m["role"] == "assistant" and m.get("tool_calls")]
    expect("one tool_calls assistant msg", len(tool_call_msgs) == 1)
    if tool_call_msgs:
        tcm = tool_call_msgs[0]
        expect(
            "tool_calls msg keeps reasoning_content",
            tcm.get("reasoning_content") == "I should call read_file.",
            detail=str(tcm),
        )

    # Locate the no-tool-call assistant messages.
    plain_msgs = [m for m in api_msgs if m["role"] == "assistant" and not m.get("tool_calls")]
    expect("two plain assistant msgs", len(plain_msgs) == 2)
    for pm in plain_msgs:
        expect(
            f"plain assistant preserves reasoning_content (content={pm.get('content')!r})",
            pm.get("reasoning_content") is not None,
            detail=str(pm),
        )

    # Tool message preserved.
    tool_msgs = [m for m in api_msgs if m["role"] == "tool"]
    expect(
        "tool result preserved",
        len(tool_msgs) == 1 and tool_msgs[0]["tool_call_id"] == "call_1",
    )

    # Round-trip: storage still has reasoning_content for the plain assistant
    # (we never lose it from local history).
    stored_plain = [m for m in h.messages if m["role"] == "assistant" and not m.get("tool_calls")]
    expect(
        "storage retains reasoning_content for plain assistants",
        all(m.get("reasoning_content") for m in stored_plain),
    )

    # ---- pruning tests ----
    test_pruning()

    print("\n-- summary --")
    if FAILURES:
        print(f"FAIL ({len(FAILURES)}): {FAILURES}")
        return 1
    print("All history tests PASS")
    return 0

def test_pruning() -> None:
    """Test suite for History.prune_for_context()."""

    # ------------------------------------------------------------------
    # 1. Token estimation sanity
    # ------------------------------------------------------------------
    print("\n-- pruning: token estimation sanity --")
    h = History()
    h.set_system("you are a helpful assistant")
    h.append_user_text("hello world")
    h.append_assistant({"role": "assistant", "content": "hi there"})
    tokens = h.estimate_tokens()
    expect(
        "estimate_tokens returns a positive int",
        isinstance(tokens, int) and tokens > 0,
        detail=f"got {tokens}",
    )
    # Rough ballpark: ~70 chars / 4 ≈ 17 tokens
    expect(
        "estimate_tokens roughly correct",
        5 <= tokens <= 100,
        detail=f"got {tokens}",
    )

    # ------------------------------------------------------------------
    # 2. No-op prune when under limit
    # ------------------------------------------------------------------
    print("\n-- pruning: no-op when under limit --")
    h2 = History()
    h2.set_system("short")
    h2.append_user_text("hi")
    h2.append_assistant({"role": "assistant", "content": "hey"})
    original_msgs = list(h2.messages)
    h2.prune_for_context(max_tokens=100_000)  # well above estimate
    expect(
        "prune is no-op when well under limit",
        h2.messages == original_msgs,
        detail=f"original: {original_msgs!r}, after: {h2.messages!r}",
    )

    # ------------------------------------------------------------------
    # 3. Tool result truncation
    # ------------------------------------------------------------------
    print("\n-- pruning: tool result truncation --")
    h3 = History()
    h3.set_system("test")
    # Turn 0 (first — preserved)
    h3.append_user_text("turn 0")
    h3.append_assistant({"role": "assistant", "content": "ok"})
    # Turn 1 (middle — eligible for truncation)
    h3.append_user_text("turn 1")
    big_content = "X" * 10_000
    h3.append_tool_result("call_abc", big_content)
    h3.append_assistant({"role": "assistant", "content": "done"})
    # Turn 2 (last — preserved)
    h3.append_user_text("turn 2")
    h3.append_assistant({"role": "assistant", "content": "bye"})
    # Use max_tokens between original estimate (~2505) and post-truncation (~142)
    # so Pass 1 (truncation) runs but Pass 2 (turn dropping) does not.
    h3.prune_for_context(max_tokens=150, keep_last_n_turns=1, max_tool_result_chars=500)
    # Find the tool message
    tool_msgs = [m for m in h3.messages if m.get("role") == "tool"]
    expect(
        "tool result truncated to <= 500 + marker",
        all(
            len(m.get("content", "")) < 2_000 for m in tool_msgs
        ),
        detail=f"tool content lengths: {[len(m.get('content','')) for m in tool_msgs]}",
    )
    big_tool = tool_msgs[0]
    expect(
        "truncated tool contains marker",
        "[... result truncated from" in big_tool.get("content", ""),
    )

    # ------------------------------------------------------------------
    # 4. Turn dropping with tight limit
    # ------------------------------------------------------------------
    print("\n-- pruning: turn dropping --")
    h4 = History()
    h4.set_system("test")
    # Create 10 turns with 500-char messages so each turn is ~250 tokens
    # Initial estimate ~2500 tokens. Use max_tokens=1200 so we need to drop
    # several middle turns. Each dropped turn saves ~170 tokens vs its summary.
    for i in range(10):
        h4.append_user_text(f"turn {i} " + "A" * 500)
        h4.append_assistant({"role": "assistant", "content": "B" * 500})
    orig_len = len(h4.messages)
    expect("10 turns = 20 messages", orig_len == 20, detail=f"got {orig_len}")
    h4.prune_for_context(max_tokens=1400, keep_last_n_turns=2, max_tool_result_chars=500)
    expect(
        "after pruning, estimated tokens under limit",
        h4.estimate_tokens() <= 1400,
        detail=f"estimate: {h4.estimate_tokens()}",
    )
    expect(
        "fewer messages after dropping turns",
        len(h4.messages) < orig_len,
        detail=f"was {orig_len}, now {len(h4.messages)}",
    )

    # ------------------------------------------------------------------
    # 5. First turn preserved
    # ------------------------------------------------------------------
    print("\n-- pruning: first turn preserved --")
    # Find the first user message — it should still exist after pruning
    first_user_msg = None
    for msg in h4.messages:
        if msg.get("role") == "user":
            first_user_msg = msg.get("content", "")
            break
    expect(
        "first turn user message still present",
        first_user_msg is not None and "turn 0" in str(first_user_msg),
        detail=f"first user content: {first_user_msg!r}",
    )

    # ------------------------------------------------------------------
    # 6. Last N turns preserved
    # ------------------------------------------------------------------
    print("\n-- pruning: last N turns preserved --")
    # With keep_last_n_turns=2, the last two user messages should survive
    user_msgs_after = [m.get("content", "") for m in h4.messages if m.get("role") == "user"]
    # The last original turn was "turn 9", and the second-to-last was "turn 8"
    any_last = any("turn 9" in str(c) for c in user_msgs_after)
    any_second_last = any("turn 8" in str(c) for c in user_msgs_after)
    expect(
        "last turn (turn 9) preserved",
        any_last,
        detail=f"user messages after: {user_msgs_after}",
    )
    expect(
        "second-to-last turn (turn 8) preserved",
        any_second_last,
        detail=f"user messages after: {user_msgs_after}",
    )


if __name__ == "__main__":
    sys.exit(main())
