from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication

from aura.gui.input_panel import SendPayload
from aura.gui.send_handler import SendHandler


class _FakeHistory:
    def __init__(self) -> None:
        self.user_texts = []
        self.user_multimodal = []

    def append_user_text(self, text: str) -> None:
        self.user_texts.append(text)

    def append_user_multimodal(self, parts: list[dict]) -> None:
        self.user_multimodal.append(parts)


class _FakeBridge:
    def __init__(self) -> None:
        self.history = _FakeHistory()
        self.send_calls = []

    def is_running(self) -> bool:
        return False

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


class _FakeInput:
    def set_queued_messages(self, count: int) -> None:
        self.queued_messages = count


def test_answer_only_research_send_does_not_open_drone_workbay(monkeypatch, tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration",
        lambda: True,
    )
    bridge = _FakeBridge()
    chat = _FakeChat()
    handler = SendHandler(
        bridge=bridge,
        chat=chat,
        input_panel=_FakeInput(),
        settings=SimpleNamespace(max_tool_rounds=3, planner_provider="test"),
        workspace_root=tmp_path,
    )
    handler._get_current_model_info = lambda model: None
    drone_bay_requests = []
    handler.drone_bay_requested.connect(lambda: drone_bay_requests.append(True))

    handler.handle_send(
        SendPayload("Are there any World Cup matches today?", []),
        model="test-model",
        thinking="off",
    )

    assert drone_bay_requests == []
    assert bridge.send_calls == [
        {"model": "test-model", "thinking": "off", "max_tool_rounds": 3}
    ]
    assert chat.errors == []
    assert chat.assistant_started == 1
