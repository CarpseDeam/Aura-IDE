"""Retry route ownership: the route sent during retry belongs to the retained
user turn, not a stale handler-global from another conversation.

Tests:
1. Immediate retry retains the correct route.
2. Switching/replacing conversation history cannot leak the prior route.
3. Each lane (implementation, validation, research, chat) retries with its
   own correct route.
4. Aura's own ``aura_internal`` steering messages never stand in for the
   real user turn being retried or rewound.
"""

from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication

from aura.conversation.history import History
from aura.conversation.task_router import TaskLane
from aura.gui.input_panel import SendPayload
from aura.gui.send_handler import SendHandler


class _FakeBridge:
    def __init__(self) -> None:
        self.history = History()
        self.send_calls: list[dict] = []
        self.target_file_calls: list[tuple[str, ...]] = []

    def is_running(self) -> bool:
        return False

    def set_turn_target_files(self, target_files) -> None:
        self.target_file_calls.append(tuple(target_files))

    def send(self, **kwargs) -> None:
        self.send_calls.append(kwargs)


class _FakeChat:
    def __init__(self) -> None:
        self.users: list[tuple] = []
        self.errors: list[tuple] = []
        self.assistant_started: int = 0

    def add_user(self, text: str, images=None) -> None:
        self.users.append((text, images))

    def add_error(self, title: str, message: str) -> None:
        self.errors.append((title, message))

    def scroll_to_bottom(self, force: bool = False) -> None:
        pass

    def begin_assistant(self) -> None:
        self.assistant_started += 1

    def reset(self) -> None:
        pass


class _FakeInput:
    def __init__(self) -> None:
        self.queued_messages: int = 0

    def set_queued_messages(self, count: int) -> None:
        self.queued_messages = count

    def setEnabled(self, enabled: bool) -> None:
        pass

    def set_placeholder(self, text: str) -> None:
        pass


def _make_handler(monkeypatch, tmp_path) -> SendHandler:
    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration",
        lambda provider: True,
    )
    bridge = _FakeBridge()
    chat = _FakeChat()
    handler = SendHandler(
        bridge=bridge,
        chat=chat,
        input_panel=_FakeInput(),
        settings=SimpleNamespace(
            max_tool_rounds=3,
            provider="test",
            planner_provider="legacy-test",
        ),
        workspace_root=tmp_path,
    )
    handler._get_current_model_info = lambda model: None
    return handler


def test_immediate_retry_retains_correct_route(monkeypatch, tmp_path) -> None:
    handler = _make_handler(monkeypatch, tmp_path)

    handler.handle_send(
        SendPayload("Fix the queue draining bug in send_handler.py", []),
        model="m",
        thinking="off",
    )
    assert handler._last_sent_route is not None
    assert handler._last_sent_route.lane == TaskLane.implementation

    assert handler.handle_retry_last(model="m", thinking="off") is True
    retry_route = handler._bridge.send_calls[-1]["route"]
    assert retry_route.lane == TaskLane.implementation
    assert retry_route.action == "bugfix"


def test_switching_conversation_cannot_leak_prior_route(monkeypatch, tmp_path) -> None:
    handler = _make_handler(monkeypatch, tmp_path)

    # Send an implementation message in conversation A
    handler.handle_send(
        SendPayload("Fix the bug in models.py", []),
        model="m",
        thinking="off",
    )
    assert handler._last_sent_route.lane == TaskLane.implementation

    # Simulate opening a new conversation: clear_queue resets the handler state
    handler.clear_queue()
    assert handler._last_sent_route is None

    # In the new conversation, send a chat message
    handler._bridge.history = History()  # fresh history
    handler.handle_send(
        SendPayload("hello there, how are you?", []),
        model="m",
        thinking="off",
    )
    assert handler._last_sent_route.lane == TaskLane.chat

    # Retry must use the chat route, not the leaked implementation route
    assert handler.handle_retry_last(model="m", thinking="off") is True
    retry_route = handler._bridge.send_calls[-1]["route"]
    assert retry_route.lane == TaskLane.chat
    assert retry_route.action == "chat"


def test_retried_implementation_gets_implementation_route(monkeypatch, tmp_path) -> None:
    handler = _make_handler(monkeypatch, tmp_path)
    handler.handle_send(
        SendPayload("Implement the new feature in parser.py", []),
        model="m",
        thinking="off",
    )
    assert handler.handle_retry_last(model="m", thinking="off") is True
    assert handler._bridge.send_calls[-1]["route"].lane == TaskLane.implementation


def test_retried_validation_gets_validation_route(monkeypatch, tmp_path) -> None:
    handler = _make_handler(monkeypatch, tmp_path)
    handler.handle_send(
        SendPayload("run pytest tests/test_queue_behavior.py", []),
        model="m",
        thinking="off",
    )
    assert handler.handle_retry_last(model="m", thinking="off") is True
    assert handler._bridge.send_calls[-1]["route"].lane == TaskLane.validation


def test_retried_research_gets_research_route(monkeypatch, tmp_path) -> None:
    handler = _make_handler(monkeypatch, tmp_path)
    handler.handle_send(
        SendPayload("Are there any World Cup matches today?", []),
        model="m",
        thinking="off",
    )
    assert handler.handle_retry_last(model="m", thinking="off") is True
    assert handler._bridge.send_calls[-1]["route"].lane == TaskLane.research


def test_retry_skips_trailing_internal_steering(monkeypatch, tmp_path) -> None:
    """A trailing ``aura_internal`` nudge must not be mistaken for the request."""
    handler = _make_handler(monkeypatch, tmp_path)
    handler.handle_send(
        SendPayload("Fix the crash in parser.py", []),
        model="m",
        thinking="off",
    )

    history = handler._bridge.history
    history.append_assistant({"role": "assistant", "content": "looking"})
    # Aura's own steering, appended as role="user" but marked internal.
    history.append_internal_user_text(
        "Are there any World Cup matches today? Stop searching and edit now."
    )

    assert handler.handle_retry_last(model="m", thinking="off") is True

    retry_route = handler._bridge.send_calls[-1]["route"]
    assert retry_route.lane == TaskLane.implementation
    assert retry_route.action == "bugfix"


def test_rewind_drops_response_and_internal_steering(monkeypatch, tmp_path) -> None:
    """Rewind stops at the real request, discarding the internal nudge with it."""
    handler = _make_handler(monkeypatch, tmp_path)
    handler.handle_send(
        SendPayload("Fix the crash in parser.py", []),
        model="m",
        thinking="off",
    )

    history = handler._bridge.history
    history.append_assistant({"role": "assistant", "content": "looking"})
    history.append_internal_user_text("focus on the named file")
    history.append_assistant({"role": "assistant", "content": "still looking"})

    assert handler.handle_retry_last(model="m", thinking="off") is True

    assert [m["content"] for m in history.messages] == ["Fix the crash in parser.py"]
    assert not any(m.get("aura_internal") for m in history.messages)


def test_internal_only_history_has_no_real_user_turn(monkeypatch, tmp_path) -> None:
    """Internal steering alone is not a retryable turn."""
    handler = _make_handler(monkeypatch, tmp_path)
    handler._bridge.history.append_internal_user_text("reread the changed file")

    assert handler.handle_retry_last(model="m", thinking="off") is False
    assert handler._chat.errors


def test_retried_chat_gets_chat_route(monkeypatch, tmp_path) -> None:
    handler = _make_handler(monkeypatch, tmp_path)
    handler.handle_send(
        SendPayload("hello there, how are you?", []),
        model="m",
        thinking="off",
    )
    assert handler.handle_retry_last(model="m", thinking="off") is True
    assert handler._bridge.send_calls[-1]["route"].lane == TaskLane.chat
