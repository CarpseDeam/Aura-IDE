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
    h.append_tool_result("call_1", "file contents")
    api_msgs = h.for_api()
    assistant = api_msgs[1]  # no system prompt, so index 1 = first assistant
    assert assistant["reasoning_content"] == "Let me read that file."
    assert assistant["tool_calls"] == h.messages[1]["tool_calls"]
    assert api_msgs[2]["role"] == "tool"


def test_for_api_repairs_orphaned_assistant_tool_call_before_user():
    """Interrupted turns must not replay assistant tool_calls with no tool result."""
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
    h.append_user_text("Actually, do something else")

    api_msgs = h.for_api()

    assert [m["role"] for m in api_msgs] == ["user", "user"]
    assert h.messages == api_msgs


def test_for_api_repairs_partial_tool_call_results():
    """If one tool call in a multi-call assistant block is missing, drop the block."""
    h = History()
    h.append_user_text("Write two files")
    h.append_assistant({
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "write_file", "arguments": '{"path":"a.py"}'},
            },
            {
                "id": "call_2",
                "type": "function",
                "function": {"name": "write_file", "arguments": '{"path":"b.py"}'},
            },
        ],
    })
    h.append_tool_result("call_1", '{"ok": true}')
    h.append_user_text("Continue")

    api_msgs = h.for_api()

    assert [m["role"] for m in api_msgs] == ["user", "user"]
    assert all(m.get("tool_call_id") != "call_1" for m in api_msgs)


def test_for_api_keeps_complete_multi_tool_call_block():
    h = History()
    h.append_user_text("Write two files")
    h.append_assistant({
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "write_file", "arguments": '{"path":"a.py"}'},
            },
            {
                "id": "call_2",
                "type": "function",
                "function": {"name": "write_file", "arguments": '{"path":"b.py"}'},
            },
        ],
    })
    h.append_tool_result("call_1", '{"ok": true}')
    h.append_tool_result("call_2", '{"ok": true}')
    h.append_assistant({"role": "assistant", "content": "Done."})

    api_msgs = h.for_api()

    assert [m["role"] for m in api_msgs] == ["user", "assistant", "tool", "tool", "assistant"]
    assert api_msgs[1]["tool_calls"] == h.messages[1]["tool_calls"]


def test_for_api_drops_orphan_tool_message():
    h = History()
    h.append_tool_result("missing_call", "stale")
    h.append_user_text("Hello")

    api_msgs = h.for_api()

    assert api_msgs == [{"role": "user", "content": "Hello"}]
    assert h.messages == api_msgs


def test_rewind_to_last_user_turn_removes_latest_response():
    h = History()
    h.append_user_text("First")
    h.append_assistant({"role": "assistant", "content": "One"})
    h.append_user_text("Second")
    h.append_assistant({"role": "assistant", "content": "Two"})

    assert h.rewind_to_last_user_turn() is True

    assert h.messages == [
        {"role": "user", "content": "First"},
        {"role": "assistant", "content": "One"},
        {"role": "user", "content": "Second"},
    ]


def test_rewind_to_last_user_turn_keeps_latest_user_when_no_response():
    h = History()
    h.append_user_text("Retry this")

    assert h.rewind_to_last_user_turn() is True

    assert h.messages == [{"role": "user", "content": "Retry this"}]


def test_rewind_to_last_user_turn_repairs_broken_tool_block_first():
    h = History()
    h.append_user_text("First")
    h.append_assistant({
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    })
    h.append_user_text("Second")
    h.append_assistant({"role": "assistant", "content": "Two"})

    assert h.rewind_to_last_user_turn() is True

    assert h.messages == [
        {"role": "user", "content": "First"},
        {"role": "user", "content": "Second"},
    ]


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


def test_prune_truncation_marker_includes_original_length_and_tool_name():
    """Truncation markers must include original length, new length, and tool name."""
    h = History()
    h.append_user_text("Turn 1")
    # 10000 chars exceeds the 8 KB source floor, so it will be truncated to 8000.
    long_content = "y" * 10000
    h.append_assistant({
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
    })
    h.append_tool_result("c1", long_content)
    h.append_user_text("Turn 2")
    h.append_assistant({"role": "assistant", "content": "ok"})

    # With keep_last_n_turns=1 both turns are preserved; Pass 3 truncates turn 0
    # (preserved but not current) to source floor (8000 chars) since 10000 > 8000.
    h.prune_for_context(max_tokens=50, keep_last_n_turns=1, max_tool_result_chars=200)

    tool_msg = h.messages[2]
    assert "10000" in tool_msg["content"]   # original length in marker
    assert "read_file" in tool_msg["content"]  # tool name in marker


def test_prune_old_turns_dropped_before_current_turn_is_crushed():
    """Dropping old turns (Pass 2) must happen before current-turn tool results are truncated."""
    h = History()

    # Turn 0 (first) — old, lots of content
    h.append_user_text("Old task")
    h.append_assistant({
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "old1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
    })
    old_content = "old " * 2000  # 8000 chars
    h.append_tool_result("old1", old_content)
    h.append_assistant({"role": "assistant", "content": "done"})

    # Turn 1 (current)
    current_content = "current_code " * 400  # 5200 chars, not huge
    h.append_user_text("Current task")
    h.append_assistant({
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "cur1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
    })
    h.append_tool_result("cur1", current_content)

    # Set budget just tight enough to need pruning, keep_last_n_turns=1
    # Turn 0 should be dropped/summarised rather than current turn being crushed.
    h.prune_for_context(max_tokens=2000, keep_last_n_turns=1, max_tool_result_chars=200)

    # Current turn's read_file result must still be present and mostly intact
    # (either fully intact or >=8000 floor, but since it's only 5200 chars it should be untouched)
    last_tool = next(
        m for m in reversed(h.messages) if m.get("role") == "tool"
    )
    assert "current_code" in last_tool["content"], (
        "Current-turn tool result should not have been crushed to tiny preview"
    )


def test_prune_source_read_tools_get_higher_floor_in_current_turn():
    """Source-reading tool results keep the 8 KB floor when Pass 4 is the last resort.

    The budget is set so that Pass 4 runs (current turn is over budget) but satisfies
    the constraint after truncating the source tool to _SOURCE_FLOOR_CHARS — meaning
    Pass 5 never fires and the 8 KB floor is preserved.
    """
    from aura.conversation.history import _SOURCE_FLOOR_CHARS

    h = History()
    h.append_user_text("Coding task")
    source_content = "def foo():\n    pass\n" * 500  # ~10 KB → ~2500 tokens
    h.append_assistant({
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "rf1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
    })
    h.append_tool_result("rf1", source_content)

    # Budget: tight enough to trigger Pass 4 (initial estimate ~2530 tokens)
    # but satisfied after truncation to 8000 chars + marker (~2050 tokens).
    h.prune_for_context(max_tokens=2200, keep_last_n_turns=5, max_tool_result_chars=200)

    tool_msg = next(m for m in h.messages if m.get("role") == "tool")
    assert len(tool_msg["content"]) >= _SOURCE_FLOOR_CHARS, (
        f"read_file result was crushed to {len(tool_msg['content'])} chars, "
        f"expected at least {_SOURCE_FLOOR_CHARS}"
    )


def test_prune_non_source_tools_crushed_before_source_tools():
    """Under budget pressure, non-source tool results shrink to 2 KB while source
    tools keep their 8 KB floor — when the budget is satisfied after Pass 4.
    """
    h = History()
    h.append_user_text("Task")

    h.append_assistant({
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "g1", "type": "function", "function": {"name": "git_status", "arguments": "{}"}},
            {"id": "rf1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ],
    })
    non_source = "git output " * 500   # 5500 chars → ~1375 tokens
    source = "def bar():\n    pass\n" * 500   # 9500 chars → ~2375 tokens
    h.append_tool_result("g1", non_source)
    h.append_tool_result("rf1", source)

    # Budget chosen so Pass 4 runs (total ~3775 tokens > 3500)
    # and is satisfied after: git→2000+marker, rf→8000+marker → ~2580 tokens < 3500.
    h.prune_for_context(max_tokens=3500, keep_last_n_turns=5, max_tool_result_chars=200)

    msgs_by_id = {m.get("tool_call_id"): m for m in h.messages if m.get("role") == "tool"}
    git_len = len(msgs_by_id["g1"]["content"])
    rf_len = len(msgs_by_id["rf1"]["content"])

    assert git_len < rf_len, (
        f"git_status result ({git_len}) should be shorter than read_file result ({rf_len}) after pruning"
    )


def test_get_tool_name_for_result_resolves_parallel_calls():
    """_get_tool_name_for_result must resolve tool names for parallel tool calls."""
    h = History()
    h.append_user_text("task")
    h.append_assistant({
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "a1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "a2", "type": "function", "function": {"name": "grep_search", "arguments": "{}"}},
        ],
    })
    h.append_tool_result("a1", "file content")
    h.append_tool_result("a2", "grep results")

    assert h._get_tool_name_for_result(2) == "read_file"
    assert h._get_tool_name_for_result(3) == "grep_search"


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
