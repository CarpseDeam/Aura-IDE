"""Tests for aura.gui.conv_persistence — ConversationPersistence class."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aura.gui.conv_persistence import ConversationPersistence


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_bridge():
    b = MagicMock()
    b.history.messages = [{"role": "user", "content": "hello"}]
    b.history.system_prompt = "Be helpful."
    b.planner_worker_mode = False
    b.dispatch_records = []
    return b


@pytest.fixture
def mock_chat():
    return MagicMock()


@pytest.fixture
def mock_playground():
    return MagicMock()


@pytest.fixture
def mock_input():
    return MagicMock()


@pytest.fixture
def mock_left_pane():
    return MagicMock()


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.temperature = 0.7
    s.worker_temperature = 0.5
    s.provider = "deepseek"
    s.system_prompt = ""
    s.planner_system_prompt = ""
    s.worker_system_prompt = ""
    return s


@pytest.fixture
def persistence(mock_bridge, mock_chat, mock_playground, mock_input,
                 mock_left_pane, mock_settings):
    return ConversationPersistence(
        bridge=mock_bridge,
        chat=mock_chat,
        playground=mock_playground,
        input_panel=mock_input,
        left_pane=mock_left_pane,
        settings=mock_settings,
    )


@pytest.fixture
def mock_loaded():
    """A minimal LoadedConversation-like object."""
    loaded = MagicMock()
    loaded.path = Path("/some/path.json")
    loaded.model = "deepseek-v4-flash-cut-price"
    loaded.thinking = "high"
    loaded.planner_worker_mode = False
    loaded.planner_model = "deepseek-v4-flash-cut-price"
    loaded.worker_model = "deepseek-v4-flash-cut-price"
    loaded.planner_thinking = "high"
    loaded.worker_thinking = "max"
    loaded.provider = "deepseek"
    loaded.worker_dispatches = []
    history = MagicMock()
    history.messages = [{"role": "user", "content": "hello"}]
    history.system_prompt = "Be helpful."
    loaded.history = history
    return loaded


# ---------------------------------------------------------------------------
# Test 1: new_conversation resets state
# ---------------------------------------------------------------------------


def test_new_conversation_resets_state(persistence, mock_bridge, mock_chat,
                                        mock_playground):
    """Calling new_conversation() resets bridge, chat, playground, and path."""
    persistence._active_replay_id = 7

    persistence.new_conversation()

    mock_bridge.reset_history.assert_called_once()
    mock_bridge.clear_pre_worker_snapshot.assert_called_once()
    mock_chat.reset.assert_called_once()
    mock_playground.clear.assert_called_once()
    assert persistence.current_conversation_path is None
    assert persistence._active_replay_id == 8


# ---------------------------------------------------------------------------
# Test 2: auto_save spawns a daemon thread
# ---------------------------------------------------------------------------


@patch("aura.gui.conv_persistence.threading.Thread")
def test_auto_save_spawns_thread(mock_thread, persistence):
    """auto_save creates a daemon thread and starts it."""
    mock_thread_instance = MagicMock()
    mock_thread.return_value = mock_thread_instance

    persistence.auto_save(
        workspace_root=Path("/ws"),
        model="m1",
        thinking="high",
        worker_model="wm1",
        worker_thinking="max",
        provider="deepseek",
        planner_provider="deepseek",
        worker_provider="deepseek",
    )

    mock_thread.assert_called_once()
    args, kwargs = mock_thread.call_args
    assert kwargs.get("daemon") is True
    assert callable(kwargs.get("target"))
    mock_thread_instance.start.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: auto_save skipped when no messages
# ---------------------------------------------------------------------------


@patch("aura.gui.conv_persistence.threading.Thread")
def test_auto_save_skipped_when_no_messages(mock_thread, persistence,
                                             mock_bridge):
    """auto_save returns early when there are no messages."""
    mock_bridge.history.messages = []

    persistence.auto_save(
        workspace_root=Path("/ws"),
        model="m1",
        thinking="high",
        worker_model="wm1",
        worker_thinking="max",
        provider="deepseek",
        planner_provider="deepseek",
        worker_provider="deepseek",
    )

    mock_thread.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: apply_loaded sets history and path
# ---------------------------------------------------------------------------


def test_apply_loaded_sets_history(persistence, mock_bridge, mock_loaded):
    """apply_loaded sets bridge history and the current path."""
    persistence.apply_loaded(mock_loaded)

    assert mock_bridge.history.messages == mock_loaded.history.messages
    assert persistence.current_conversation_path == mock_loaded.path


# ---------------------------------------------------------------------------
# Test 5: apply_loaded replays history
# ---------------------------------------------------------------------------


def test_apply_loaded_replays_history(persistence, mock_bridge, mock_chat,
                                       mock_loaded):
    """apply_loaded calls chat.reset then replay, adding user messages."""
    mock_loaded.history.messages = [
        {"role": "user", "content": "Hello"},
        {
            "role": "assistant",
            "content": "Hi there!",
            "reasoning_content": None,
            "tool_calls": [],
        },
    ]

    with patch("aura.gui.conv_persistence.QTimer.singleShot") as mock_timer:
        mock_timer.side_effect = lambda ms, func: func()
        persistence.apply_loaded(mock_loaded)

    mock_chat.reset.assert_called_once()
    mock_chat.add_user.assert_called_with("Hello")


# ---------------------------------------------------------------------------
# Test 6: apply_loaded switches provider if different
# ---------------------------------------------------------------------------


def test_apply_loaded_switches_provider_if_different(persistence, mock_bridge,
                                                      mock_left_pane,
                                                      mock_settings,
                                                      mock_loaded):
    """If loaded.provider differs from settings, bridge and pane are updated."""
    mock_settings.provider = "deepseek"
    mock_loaded.provider = "openai"
    mock_loaded.planner_provider = "openai"
    mock_loaded.worker_provider = "openai"

    persistence.apply_loaded(mock_loaded)

    mock_bridge.set_planner_provider.assert_called_once_with("openai")
    mock_bridge.set_worker_provider.assert_called_once_with("openai")
    mock_left_pane.populate_models.assert_called_once_with("openai", "openai")
    assert mock_settings.provider == "openai"


# ---------------------------------------------------------------------------
# Test 7: restore_last noop when no conversation
# ---------------------------------------------------------------------------


@patch("aura.gui.conv_persistence.most_recent_conversation")
def test_restore_last_noop_when_no_conversation(mock_mrc, persistence):
    """restore_last returns without calling apply_loaded when no file exists."""
    mock_mrc.return_value = None

    with patch.object(persistence, "apply_loaded") as mock_apply:
        persistence.restore_last(Path("/ws"))
        mock_apply.assert_not_called()


# ---------------------------------------------------------------------------
# Test 8: restore_last loads conversation
# ---------------------------------------------------------------------------


@patch("aura.gui.conv_persistence.load_conversation")
@patch("aura.gui.conv_persistence.most_recent_conversation")
def test_restore_last_loads_conversation(mock_mrc, mock_load, persistence):
    """restore_last loads and applies the most recent conversation."""
    mock_mrc.return_value = Path("/ws/.aura/conversations/latest.json")
    mock_loaded = MagicMock()
    mock_load.return_value = mock_loaded

    with patch.object(persistence, "apply_loaded") as mock_apply:
        persistence.restore_last(Path("/ws"))
        mock_apply.assert_called_once_with(mock_loaded)


# ---------------------------------------------------------------------------
# Test 9: save_succeeded updates path
# ---------------------------------------------------------------------------


def test_save_succeeded_updates_path(persistence):
    """The save_succeeded slot sets current_conversation_path."""
    assert persistence.current_conversation_path is None

    persistence.save_succeeded.emit(Path("/some/path.json"))

    assert persistence.current_conversation_path == Path("/some/path.json")


# ---------------------------------------------------------------------------
# Test 10: open_conversation shows error on fail
# ---------------------------------------------------------------------------


@patch("aura.gui.conv_persistence.QFileDialog.getOpenFileName")
@patch("aura.gui.conv_persistence.load_conversation")
@patch("aura.gui.conv_persistence.QMessageBox.warning")
def test_open_conversation_shows_error_on_fail(mock_warning, mock_load,
                                                mock_file_dialog,
                                                persistence):
    """When load_conversation raises, a warning is shown and None returned."""
    # Simulate the user picking a file
    mock_file_dialog.return_value = ("/ws/.aura/conversations/test.json", "")
    mock_load.side_effect = ValueError("Corrupted file")

    result = persistence.open_conversation(
        workspace_root=Path("/ws"),
        parent_widget=MagicMock(),
    )

    mock_warning.assert_called_once()
    assert result is None
