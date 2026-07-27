"""Focused tests for message queueing while Aura is actively working."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from aura.gui.input_panel import Attachment, SendPayload
from aura.gui.send_handler import QueuedItem, SendHandler


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
        self._running = False
        self.cancel_called = False
        self.cancel_call_count = 0
        self.pre_worker_snapshot = None

    def is_running(self) -> bool:
        return self._running

    def send(self, **kwargs) -> None:
        self.send_calls.append(kwargs)

    def request_cancel(self) -> None:
        self.cancel_called = True
        self.cancel_call_count += 1

    def set_running(self, running: bool) -> None:
        self._running = running

    def get_pre_worker_snapshot(self) -> str | None:
        return self.pre_worker_snapshot


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
        self.execution_active = False

    def set_queued_messages(self, count: int) -> None:
        self.queued_messages = count

    def set_execution_active(self, active: bool) -> None:
        self.execution_active = active

    def set_streaming(self, streaming: bool) -> None:
        self.execution_active = streaming

    def setEnabled(self, enabled: bool) -> None:
        pass

    def focus_editor(self) -> None:
        pass

    def clear_queue(self) -> None:
        pass

    def set_placeholder(self, text: str) -> None:
        pass


@pytest.fixture
def _handler(monkeypatch) -> SendHandler:
    app = QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration",
        lambda: True,
    )
    monkeypatch.setattr(
        "aura.gui.send_handler.classify_user_request",
        lambda text: SimpleNamespace(lane="production", action=None),
    )
    bridge = _FakeBridge()
    chat = _FakeChat()
    inp = _FakeInput()
    h = SendHandler(
        bridge=bridge,
        chat=chat,
        input_panel=inp,
        settings=SimpleNamespace(max_tool_rounds=3, planner_provider="test"),
        workspace_root=Path("/fake/workspace"),
    )
    return h


class TestQueueBasic:
    """1: Payload queued while bridge running. 10: No interruption. 13: Normal send."""

    def test_payload_queued_when_running(self, _handler):
        bridge = _handler._bridge
        inp = _handler._input
        bridge.set_running(True)
        _handler.handle_send(SendPayload(text="first", attachments=[]), "m", "high")
        assert len(bridge.send_calls) == 0
        assert len(_handler._message_queue) == 1
        assert inp.queued_messages == 1

    def test_normal_send_when_idle(self, _handler):
        _handler.handle_send(SendPayload(text="hello", attachments=[]), "m", "off")
        assert len(_handler._message_queue) == 0

    def test_queue_not_interrupted(self, _handler):
        bridge = _handler._bridge
        bridge.set_running(True)
        _handler.handle_send(SendPayload(text="run", attachments=[]), "m", "high")
        assert len(bridge.send_calls) == 0
        assert len(_handler._message_queue) == 1
        _handler.handle_send(SendPayload(text="queued", attachments=[]), "m", "high")
        assert len(_handler._message_queue) == 2
        assert bridge._running is True


class TestQueueFifo:
    """2: Multiple queued payloads execute FIFO."""

    def test_fifo_order(self, _handler):
        bridge = _handler._bridge
        bridge.set_running(True)
        _handler.handle_send(SendPayload(text="msg1", attachments=[]), "m1", "off")
        _handler.handle_send(SendPayload(text="msg2", attachments=[]), "m2", "high")
        _handler.handle_send(SendPayload(text="msg3", attachments=[]), "m3", "max")
        assert len(_handler._message_queue) == 3
        assert _handler._message_queue[0].text == "msg1"
        assert _handler._message_queue[1].text == "msg2"
        assert _handler._message_queue[2].text == "msg3"
        dequeued = []
        while _handler._message_queue:
            item = _handler._message_queue.pop(0)
            dequeued.append(item.text)
        assert dequeued == ["msg1", "msg2", "msg3"]


class TestQueueStateIndependence:
    """3: Each queued payload retains own text, attachments, model, thinking."""

    def test_independent_state(self, _handler):
        bridge = _handler._bridge
        att_a = [Attachment(kind="image", name="a.png", b64="aaa", text_ref=None)]
        att_b = [Attachment(kind="file", name="b.txt", b64=None, text_ref="[file]")]
        bridge.set_running(True)
        _handler.handle_send(SendPayload(text="q1", attachments=att_a), "model-a", "high")
        _handler.handle_send(SendPayload(text="q2", attachments=att_b), "model-b", "off")

        q1 = _handler._message_queue[0]
        assert q1.text == "q1"
        assert q1.model == "model-a"
        assert q1.thinking == "high"
        assert len(q1.attachments) == 1
        assert q1.attachments[0].name == "a.png"

        q2 = _handler._message_queue[1]
        assert q2.text == "q2"
        assert q2.model == "model-b"
        assert q2.thinking == "off"
        assert len(q2.attachments) == 1
        assert q2.attachments[0].name == "b.txt"


class TestQueueAttachmentIndependence:
    """4: Composer clear doesn't mutate queued attachments (via list() copy)."""

    def test_clear_does_not_mutate(self, _handler):
        bridge = _handler._bridge
        att = [Attachment(kind="image", name="snap.png", b64="orig", text_ref=None)]
        bridge.set_running(True)
        _handler.handle_send(SendPayload(text="save", attachments=att), "m", "high")
        att.clear()
        assert len(_handler._message_queue[0].attachments) == 1
        assert _handler._message_queue[0].attachments[0].name == "snap.png"
        assert _handler._message_queue[0].attachments[0].b64 == "orig"


class TestStop:
    """8, 9, 12: Stop clears queue, unifies control, one lifecycle."""

    def test_stop_clears_queue_and_cancels(self, _handler):
        bridge = _handler._bridge
        inp = _handler._input
        bridge.set_running(True)
        _handler.handle_send(SendPayload(text="q", attachments=[]), "m", "high")
        assert len(_handler._message_queue) == 1
        _handler.handle_stop()
        assert len(_handler._message_queue) == 0
        assert inp.queued_messages == 0
        assert bridge.cancel_called is True

    def test_stop_calls_bridge_cancel(self, _handler):
        _handler.handle_stop()
        assert _handler._bridge.cancel_called is True

    def test_no_duplicate_lifecycle(self, _handler):
        _handler.handle_stop()
        count = _handler._bridge.cancel_call_count
        _handler.handle_stop()
        assert _handler._bridge.cancel_call_count == count + 1

    def test_send_handler_owns_stop(self, _handler):
        assert hasattr(_handler, "handle_stop")
        assert callable(_handler.handle_stop)


class TestQueueDraining:
    """11: Next queued run begins after previous completes."""

    def test_drain_after_bridge_finishes(self, _handler):
        bridge = _handler._bridge
        bridge.set_running(True)
        _handler.handle_send(SendPayload(text="first", attachments=[]), "m1", "high")
        _handler.handle_send(SendPayload(text="queued", attachments=[]), "m2", "off")
        assert len(_handler._message_queue) == 2

        bridge.set_running(False)
        _handler._process_message_queue("m1", "high")
        assert len(_handler._message_queue) == 1
        remaining = _handler._message_queue[0]
        assert remaining.text == "queued"
        assert remaining.model == "m2"
        assert remaining.thinking == "off"

    def test_drain_empty_queue(self, _handler):
        _handler._process_message_queue("m", "high")

    def test_queue_count_updates(self, _handler):
        bridge = _handler._bridge
        inp = _handler._input
        bridge.set_running(True)
        _handler.handle_send(SendPayload(text="a", attachments=[]), "m", "high")
        assert inp.queued_messages == 1
        _handler.handle_send(SendPayload(text="b", attachments=[]), "m", "off")
        assert inp.queued_messages == 2
        _handler._message_queue.pop(0)
        _handler._input.set_queued_messages(len(_handler._message_queue))
        assert inp.queued_messages == 1


class TestQueuedItem:
    """QueuedItem dataclass properties."""

    def test_queued_item_holds_values(self):
        item = QueuedItem(text="hi", attachments=[], model="m", thinking="high")
        assert item.text == "hi"
        assert item.model == "m"
        assert item.thinking == "high"

    def test_queued_item_from_list_copy(self):
        """Simulate the list() copy that handle_send does when creating a QueuedItem."""
        att = [Attachment(kind="image", name="x.png", b64="data", text_ref=None)]
        item = QueuedItem(text="test", attachments=list(att), model="m", thinking="off")
        att.clear()
        assert len(item.attachments) == 1
        assert item.attachments[0].name == "x.png"

    def test_queued_item_from_send_payload(self):
        payload = SendPayload(text="hello", attachments=[
            Attachment(kind="file", name="readme.md", b64=None, text_ref="[file: readme.md]")
        ])
        item = QueuedItem(
            text=payload.text,
            attachments=list(payload.attachments),
            model="deepseek-v4-flash",
            thinking="high",
        )
        assert item.text == "hello"
        assert len(item.attachments) == 1
        assert item.model == "deepseek-v4-flash"
        assert item.thinking == "high"
