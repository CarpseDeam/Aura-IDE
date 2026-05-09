"""Tests for aura.conversation.persistence — save/load roundtrips and v1→v2 migration."""

from __future__ import annotations

import json
from pathlib import Path

from aura.conversation.history import History
from aura.conversation.persistence import (
    save_conversation,
    load_conversation,
    list_conversations,
    WorkerDispatchRecord,
    _slugify,
    conversations_dir,
)


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------

def test_slugify_simple():
    assert _slugify("Hello World") == "hello-world"


def test_slugify_special_chars():
    assert _slugify("Fix: error in config.py!!!") == "fix-error-in-config-py"


def test_slugify_empty():
    assert _slugify("") == "untitled"


def test_slugify_unicode():
    # Non-ASCII chars should be stripped, leaving only a-z0-9
    slug = _slugify("résumé fix")
    assert "r" in slug or "s" in slug  # at minimum we get something


# ---------------------------------------------------------------------------
# conversations_dir
# ---------------------------------------------------------------------------

def test_conversations_dir(tmp_path: Path):
    ws = tmp_path / "project"
    ws.mkdir()
    expected = ws / ".aura" / "conversations"
    assert conversations_dir(ws) == expected


# ---------------------------------------------------------------------------
# Save → Load roundtrip
# ---------------------------------------------------------------------------

def test_save_load_roundtrip_simple(tmp_path: Path):
    """Save a simple conversation and load it back — should be identical."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    h = History(system_prompt="Be helpful.")
    h.append_user_text("What is Python?")
    h.append_assistant({
        "role": "assistant",
        "content": "Python is a programming language.",
    })

    path = save_conversation(
        h, ws, model="deepseek-v4-flash-cut-price", thinking="high",
        provider="deepseek",
    )

    assert path.exists()
    loaded = load_conversation(path)

    assert loaded.model == "deepseek-v4-flash-cut-price"
    assert loaded.thinking == "high"
    assert loaded.provider == "deepseek"
    assert loaded.history.system_prompt == "Be helpful."
    assert len(loaded.history.messages) == 2
    assert loaded.history.messages[0]["role"] == "user"
    assert loaded.history.messages[0]["content"] == "What is Python?"
    assert loaded.history.messages[1]["content"] == "Python is a programming language."


def test_save_load_roundtrip_with_reasoning(tmp_path: Path):
    """Save a conversation with reasoning_content and verify it survives."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    h = History()
    h.append_user_text("Hello")
    h.append_assistant({
        "role": "assistant",
        "content": "Hi!",
        "reasoning_content": "User greeted me.",
    })

    path = save_conversation(
        h, ws, model="claude-sonnet-4-20250514", thinking="high",
        provider="openai",
    )
    loaded = load_conversation(path)

    assert loaded.provider == "openai"
    assert loaded.history.messages[1]["reasoning_content"] == "User greeted me."


def test_save_load_roundtrip_planner_worker_mode(tmp_path: Path):
    """Save in planner_worker_mode with dispatches and verify."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    h = History(system_prompt="Plan mode.")
    h.append_user_text("Add a function")

    dispatch = WorkerDispatchRecord(
        after_message_index=1,
        tool_call_id="call_abc",
        spec={"goal": "add function", "files": ["test.py"], "spec": "...", "acceptance": "..."},
        worker_history=[
            {"role": "user", "content": "Write the code"},
            {"role": "assistant", "content": "Here is the code"},
        ],
        result_summary="Added function successfully.",
    )

    path = save_conversation(
        h, ws,
        model="deepseek-v4-flash-cut-price",
        thinking="high",
        planner_worker_mode=True,
        planner_model="deepseek-v4-flash-cut-price",
        worker_model="deepseek-v4-flash-cut-price",
        planner_thinking="high",
        worker_thinking="max",
        worker_dispatches=[dispatch],
        provider="deepseek",
    )

    loaded = load_conversation(path)
    assert loaded.planner_worker_mode is True
    assert loaded.planner_thinking == "high"
    assert loaded.worker_thinking == "max"
    assert len(loaded.worker_dispatches) == 1
    loaded_dispatch = loaded.worker_dispatches[0]
    assert loaded_dispatch.tool_call_id == "call_abc"
    assert loaded_dispatch.result_summary == "Added function successfully."
    assert len(loaded_dispatch.worker_history) == 2


def test_save_load_v1_backward_compat(tmp_path: Path):
    """Loading a v1-format file should produce a valid LoadedConversation
    with planner_worker_mode=False."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    conv_dir = conversations_dir(ws)
    conv_dir.mkdir(parents=True)

    v1_data = {
        "version": 1,
        "model": "deepseek-chat",
        "thinking": "off",
        "system_prompt": "v1 system",
        "messages": [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
        ],
    }
    path = conv_dir / "v1-test.json"
    path.write_text(json.dumps(v1_data), encoding="utf-8")

    loaded = load_conversation(path)
    assert loaded.model == "deepseek-chat"
    assert loaded.thinking == "off"
    assert loaded.provider == "deepseek"  # default for v1
    assert loaded.planner_worker_mode is False
    assert len(loaded.history.messages) == 2


def test_save_load_no_provider_field_defaults_to_deepseek(tmp_path: Path):
    """v2 file without a provider field should default to 'deepseek'."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    conv_dir = conversations_dir(ws)
    conv_dir.mkdir(parents=True)

    data = {
        "version": 2,
        "model": "gpt-4o",
        "thinking": "off",
        "system_prompt": None,
        "messages": [],
        "planner_worker_mode": False,
        "planner_model": "gpt-4o",
        "worker_model": "gpt-4o-mini",
        "planner_thinking": "off",
        "worker_thinking": "off",
        "worker_dispatches": [],
        # No "provider" key
    }
    path = conv_dir / "no-provider.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = load_conversation(path)
    assert loaded.provider == "deepseek"


def test_list_conversations_empty(tmp_path: Path):
    ws = tmp_path / "empty_workspace"
    ws.mkdir()
    assert list_conversations(ws) == []


def test_list_conversations_returns_sorted(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    h = History()
    h.append_user_text("test")

    save_conversation(h, ws, model="deepseek-v4-flash-cut-price", thinking="high", title="first")
    save_conversation(h, ws, model="deepseek-v4-flash-cut-price", thinking="high", title="second")

    files = list_conversations(ws)
    assert len(files) == 2
    # Should be sorted by mtime descending
    assert files[0].stat().st_mtime >= files[1].stat().st_mtime
