"""Tests for aura.conversation.history — the thinking-mode replay trap."""

from __future__ import annotations

from aura.conversation.history import History


# ---------------------------------------------------------------------------
# The Trap: reasoning_content MUST be preserved in for_api()
# ---------------------------------------------------------------------------

def test_for_api_preserves_reasoning_content():
    """If an assistant message has reasoning_content, for_api() MUST include it."""
    h = History(system_prompt="You are a helpful assistant.")
    h.append_user_text("Hello")
    h.append_assistant({
        "role": "assistant",
        "content": "Hi there!",
        "reasoning_content": "The user said hello, I should reply warmly.",
    })
    api_msgs = h.for_api()
    assert len(api_msgs) == 3  # system, user, assistant
    assistant = api_msgs[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "Hi there!"
    assert assistant["reasoning_content"] == "The user said hello, I should reply warmly."


def test_for_api_preserves_reasoning_content_with_tool_calls():
    """reasoning_content MUST be preserved even when tool_calls are present."""
    h = History()
    h.append_user_text("Read config.py")
    h.append_assistant({
        "role": "assistant",
        "content": None,
        "reasoning_content": "Let me read that file.",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"config.py"}'},
            }
        ],
    })
    api_msgs = h.for_api()
    assistant = api_msgs[1]  # no system prompt, so index 1 = first assistant
    assert assistant["reasoning_content"] == "Let me read that file."
    assert assistant["tool_calls"] == h.messages[1]["tool_calls"]


def test_for_api_no_reasoning_content_not_added():
    """If reasoning_content is absent, the key should NOT be added to the API view."""
    h = History()
    h.append_user_text("Hi")
    h.append_assistant({"role": "assistant", "content": "Hello!"})
    api_msgs = h.for_api()
    assistant = api_msgs[0]  # no system prompt
    assert "reasoning_content" not in assistant


def test_for_api_includes_system_prompt_when_set():
    """When system_prompt is set, it becomes the first message in for_api()."""
    h = History(system_prompt="Be concise.")
    api_msgs = h.for_api()
    assert len(api_msgs) == 1
    assert api_msgs[0] == {"role": "system", "content": "Be concise."}


def test_for_api_no_system_prompt_when_none():
    """When system_prompt is None, for_api() should not include a system message."""
    h = History()
    h.append_user_text("Hi")
    api_msgs = h.for_api()
    assert len(api_msgs) == 1
    assert api_msgs[0]["role"] == "user"


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def test_estimate_tokens_empty():
    """Empty history should estimate 0 tokens."""
    h = History()
    assert h.estimate_tokens() == 0


def test_estimate_tokens_with_system_and_messages():
    """Token estimate should scale with content length."""
    h = History(system_prompt="You are helpful.")  # 17 chars → ~4 tokens
    h.append_user_text("Hello, how are you?")  # 19 chars → ~4 tokens
    h.append_assistant({
        "role": "assistant",
        "content": "I'm doing well!",
        "reasoning_content": "I should respond politely and briefly.",
    })
    # ~ (17 + 19 + 16 + 41) / 4 = ~93/4 ≈ 23 tokens
    tokens = h.estimate_tokens()
    assert tokens > 0
    assert tokens < 100


def test_estimate_tokens_multimodal_user_message():
    """Token estimation should handle multimodal content lists."""
    h = History()
    h.append_user_multimodal([
        {"type": "text", "text": "Describe this image:"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aaaa"}},
    ])
    tokens = h.estimate_tokens()
    assert tokens >= len("Describe this image:") // 4


# ---------------------------------------------------------------------------
# Append / mutation
# ---------------------------------------------------------------------------

def test_append_user_text():
    h = History()
    h.append_user_text("Hello")
    assert len(h) == 1
    assert h.messages[0] == {"role": "user", "content": "Hello"}


def test_append_assistant_deep_copies():
    """append_assistant should deep-copy so mutations to the original dict
    don't affect stored history."""
    original = {"role": "assistant", "content": "Hi"}
    h = History()
    h.append_assistant(original)
    original["content"] = "MUTATED"
    assert h.messages[0]["content"] == "Hi"


def test_append_tool_result():
    h = History()
    h.append_tool_result("call_1", "file content here")
    assert len(h) == 1
    assert h.messages[0]["role"] == "tool"
    assert h.messages[0]["tool_call_id"] == "call_1"
    assert h.messages[0]["content"] == "file content here"


def test_truncate_after():
    h = History()
    for i in range(5):
        h.append_user_text(f"msg {i}")
    h.truncate_after(2)
    assert len(h) == 2
    assert h.messages[0]["content"] == "msg 0"
    assert h.messages[1]["content"] == "msg 1"


def test_pop_if_empty_assistant_message_removes_empty():
    h = History()
    h.append_assistant({"role": "assistant", "content": None})
    assert len(h) == 1
    h.pop_if_empty_assistant_message()
    assert len(h) == 0


def test_pop_if_empty_assistant_message_keeps_nonempty():
    h = History()
    h.append_assistant({"role": "assistant", "content": "Not empty"})
    h.pop_if_empty_assistant_message()
    assert len(h) == 1


def test_pop_if_empty_assistant_message_keeps_with_tool_calls():
    h = History()
    h.append_assistant({
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
    })
    h.pop_if_empty_assistant_message()
    assert len(h) == 1  # tool_calls present, so NOT empty


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def test_prune_does_nothing_when_under_budget():
    h = History(system_prompt="Be brief.")
    h.append_user_text("Short question")
    h.append_assistant({"role": "assistant", "content": "Short answer"})
    before = len(h)
    before_msgs = [dict(m) for m in h.messages]
    h.prune_for_context(max_tokens=1_000_000)
    assert len(h) == before
    assert h.messages == before_msgs


def test_prune_truncates_large_tool_results():
    h = History()
    h.append_user_text("Search for something")
    long_content = "x" * 10000
    h.append_assistant({
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{}"}}],
    })
    h.append_tool_result("call_1", long_content)
    h.append_assistant({"role": "assistant", "content": "Done."})
    h.append_user_text("New question")
    h.append_assistant({"role": "assistant", "content": "Final answer"})

    # With 2 turns, keep_last_n_turns=1, the first turn's tool result should be truncated
    h.prune_for_context(max_tokens=50, keep_last_n_turns=1, max_tool_result_chars=200)
    # The long tool result in the first turn should be truncated
    tool_msg = h.messages[2]
    assert tool_msg["role"] == "tool"
    assert len(tool_msg["content"]) < len(long_content)
    assert "[... result truncated" in tool_msg["content"]


# ---------------------------------------------------------------------------
# __len__
# ---------------------------------------------------------------------------

def test_len_reflects_message_count():
    h = History()
    assert len(h) == 0
    h.append_user_text("a")
    assert len(h) == 1
    h.append_assistant({"role": "assistant", "content": "b"})
    assert len(h) == 2
