from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication

from aura.conversation.history import History
from aura.conversation.task_router import TaskLane
from aura.gui.input_panel import Attachment, SendPayload
from aura.gui.send_handler import SendHandler


class _FakeHistory(History):
    """Real history behaviour plus per-call recording for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.user_texts: list[str] = []
        self.user_multimodal: list[list[dict]] = []

    def append_user_text(self, text: str) -> None:
        self.user_texts.append(text)
        super().append_user_text(text)

    def append_user_multimodal(self, parts: list[dict]) -> None:
        self.user_multimodal.append(parts)
        super().append_user_multimodal(parts)


class _FakeBridge:
    def __init__(self) -> None:
        self.history = _FakeHistory()
        self.send_calls = []
        self.target_file_calls: list[tuple[str, ...]] = []

    def is_running(self) -> bool:
        return False

    def set_turn_target_files(self, target_files) -> None:
        self.target_file_calls.append(tuple(target_files))

    def send(self, **kwargs) -> None:
        self.send_calls.append(kwargs)


class _FakeChat:
    def __init__(self) -> None:
        self.users = []
        self.errors = []
        self.assistant_started = 0

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
        self.queued_messages = 0

    def set_queued_messages(self, count: int) -> None:
        self.queued_messages = count

    def setEnabled(self, enabled: bool) -> None:
        pass

    def set_placeholder(self, text: str) -> None:
        pass


def _make_handler(monkeypatch, tmp_path):
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


def test_answer_only_research_send_does_not_open_drone_workbay(monkeypatch, tmp_path):
    handler = _make_handler(monkeypatch, tmp_path)
    drone_bay_requests = []
    answer_only_research_starts = []
    handler.drone_bay_requested.connect(lambda: drone_bay_requests.append(True))
    handler.answer_only_research_started.connect(
        lambda: answer_only_research_starts.append(True)
    )

    handler.handle_send(
        SendPayload("Are there any World Cup matches today?", []),
        model="test-model",
        thinking="off",
    )

    assert drone_bay_requests == []
    assert answer_only_research_starts == [True]
    assert len(handler._bridge.send_calls) == 1
    call = handler._bridge.send_calls[0]
    assert call["route"].lane == TaskLane.research
    assert call["route"].action == "web_research"
    assert call["model"] == "test-model"
    assert call["thinking"] == "off"
    assert call["max_tool_rounds"] == 3
    assert handler._chat.errors == []
    assert handler._chat.assistant_started == 1


def test_implementation_request_reaches_bridge_as_implementation(monkeypatch, tmp_path):
    handler = _make_handler(monkeypatch, tmp_path)

    handler.handle_send(
        SendPayload("Fix the queue draining bug in send_handler.py", []),
        model="test-model",
        thinking="off",
    )

    assert len(handler._bridge.send_calls) == 1
    route = handler._bridge.send_calls[0]["route"]
    assert route.lane == TaskLane.implementation
    # "bug" names the shape of the work inside the implementation lane.
    assert route.action == "bugfix"
    # The same route is retained so a later retry keeps this classification.
    assert handler._last_sent_route is route


def test_validation_request_reaches_bridge_as_validation(monkeypatch, tmp_path):
    handler = _make_handler(monkeypatch, tmp_path)

    handler.handle_send(
        SendPayload("run pytest tests/test_queue_behavior.py", []),
        model="test-model",
        thinking="off",
    )

    assert len(handler._bridge.send_calls) == 1
    route = handler._bridge.send_calls[0]["route"]
    assert route.lane == TaskLane.validation
    assert route.action == "validation"


def test_chat_request_remains_chat(monkeypatch, tmp_path):
    handler = _make_handler(monkeypatch, tmp_path)

    handler.handle_send(
        SendPayload("hello there, how are you?", []),
        model="test-model",
        thinking="off",
    )

    assert len(handler._bridge.send_calls) == 1
    route = handler._bridge.send_calls[0]["route"]
    assert route.lane == TaskLane.chat
    assert route.action == "chat"


def test_built_in_action_bypasses_model_execution(monkeypatch, tmp_path):
    handler = _make_handler(monkeypatch, tmp_path)
    handled = []
    monkeypatch.setattr(
        handler,
        "_handle_built_in_action",
        lambda action, text="": handled.append((action, text)),
    )

    handler.handle_send(
        SendPayload("git status", []),
        model="test-model",
        thinking="off",
    )

    assert handled == [("git_status", "git status")]
    assert handler._bridge.send_calls == []
    assert handler._chat.assistant_started == 0


def test_vision_finalized_send_preserves_its_own_route(monkeypatch, tmp_path):
    handler = _make_handler(monkeypatch, tmp_path)
    # Suppress the actual decompiler thread; _on_vision_done is driven directly.
    monkeypatch.setattr(
        "aura.gui.send_handler.threading.Thread.start",
        lambda self: None,
    )
    payload = SendPayload(
        "Fix the layout bug in this screenshot",
        [Attachment(kind="image", name="shot.png", b64="abc", text_ref=None)],
    )

    handler.handle_send(payload, model="test-model", thinking="off")

    assert handler._pending_route is not None
    assert handler._pending_route.lane == TaskLane.implementation
    assert handler._bridge.send_calls == []  # still waiting on the vision thread

    handler._on_vision_done(payload, ["structural decompile text"], None)

    assert len(handler._bridge.send_calls) == 1
    route = handler._bridge.send_calls[0]["route"]
    assert route is handler._pending_route
    assert route.lane == TaskLane.implementation


def test_retry_reuses_last_sent_route(monkeypatch, tmp_path):
    handler = _make_handler(monkeypatch, tmp_path)

    handler.handle_send(
        SendPayload("Fix the queue draining bug in send_handler.py", []),
        model="test-model",
        thinking="off",
    )
    original_route = handler._last_sent_route
    assert original_route is not None
    assert original_route.lane == TaskLane.implementation

    assert handler.handle_retry_last(model="test-model", thinking="off") is True

    retry_call = handler._bridge.send_calls[-1]
    # Route is reclassified from the retained turn text, not the stale
    # handler-global _last_sent_route — but the deterministic classifier
    # reproduces the same lane and action for unchanged text.
    assert retry_call["route"].lane == TaskLane.implementation
    assert retry_call["route"].action == "bugfix"


def test_selected_provider_without_credentials_is_rejected(monkeypatch, tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    checked_providers = []

    def _has_usable_provider_configuration(provider):
        checked_providers.append(provider)
        return provider == "configured-other"

    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration",
        _has_usable_provider_configuration,
    )
    bridge = _FakeBridge()
    chat = _FakeChat()
    handler = SendHandler(
        bridge=bridge,
        chat=chat,
        input_panel=_FakeInput(),
        settings=SimpleNamespace(
            max_tool_rounds=3,
            provider="selected-no-key",
            planner_provider="legacy-test",
        ),
        workspace_root=tmp_path,
    )

    handler.handle_send(
        SendPayload("hello", []),
        model="test-model",
        thinking="off",
    )

    assert bridge.send_calls == []
    assert checked_providers == ["selected-no-key"]
    assert len(chat.errors) == 1
    title, message = chat.errors[0]
    assert title == "No AI provider configured"
    assert "Settings → API Keys" in message
    assert "Credits" not in message
