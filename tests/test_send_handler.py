"""Tests for SendHandler — send/stop/undo logic extracted from MainWindow.

All Qt dependencies are mocked; no QApplication needed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch, ANY

import pytest

from aura.gui.input_panel import Attachment, SendPayload
from aura.gui.send_handler import SendHandler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bridge() -> Mock:
    b = Mock()
    b.is_running.return_value = False
    b.history = Mock()
    b.send = Mock()
    b.request_cancel = Mock()
    b.get_pre_worker_snapshot = Mock(return_value=None)
    b.clear_pre_worker_snapshot = Mock()
    return b


@pytest.fixture
def chat() -> Mock:
    return Mock()


@pytest.fixture
def input_panel() -> Mock:
    return Mock()


@pytest.fixture
def settings() -> Mock:
    s = Mock()
    s.provider = "deepseek"
    s.vision_enabled = False
    s.vision_endpoint = "http://localhost:5000"
    s.vision_model = "local-vision"
    s.max_tool_rounds = 10
    return s


@pytest.fixture
def workspace_root() -> Path:
    return Path("/test/workspace")


@pytest.fixture
def handler(
    bridge: Mock,
    chat: Mock,
    input_panel: Mock,
    settings: Mock,
    workspace_root: Path,
) -> SendHandler:
    return SendHandler(
        bridge=bridge,
        chat=chat,
        input_panel=input_panel,
        settings=settings,
        workspace_root=workspace_root,
        parent=None,
    )


# ---------------------------------------------------------------------------
# Undo intercept
# ---------------------------------------------------------------------------


class TestUndoIntercept:
    """Verify /undo command interception and delegation."""

    def test_undo_command_intercepted(
        self, handler: SendHandler, chat: Mock
    ) -> None:
        """Sending /undo should add a user message and trigger undo logic."""
        with patch(
            "aura.gui.send_handler.undo_last_commit", return_value=(True, "undone")
        ):
            handler.handle_send(
                SendPayload(text="/undo", attachments=[]), "model", "off"
            )
        chat.add_user.assert_called_once_with("/undo")

    def test_undo_with_snapshot_present(
        self, handler: SendHandler, bridge: Mock, chat: Mock
    ) -> None:
        """When a pre-worker snapshot exists, restore_to_snapshot should be called."""
        bridge.get_pre_worker_snapshot.return_value = "abc123"

        with patch("aura.gui.send_handler.QMessageBox.question") as mock_q:
            from PySide6.QtWidgets import QMessageBox

            mock_q.return_value = QMessageBox.Yes

            with patch(
                "aura.gui.send_handler.restore_to_snapshot",
                return_value=(True, "restored"),
            ) as mock_r:
                handler.handle_send(
                    SendPayload(text="/undo", attachments=[]), "model", "off"
                )

        mock_r.assert_called_once_with(Path("/test/workspace"), "abc123")
        bridge.clear_pre_worker_snapshot.assert_called_once_with()
        chat.add_info.assert_called_once_with("Undo", "restored")

    def test_undo_no_snapshot_falls_back(
        self, handler: SendHandler, chat: Mock
    ) -> None:
        """When no snapshot exists, undo_last_commit should be called."""
        with patch("aura.gui.send_handler.undo_last_commit") as mock_u:
            mock_u.return_value = (True, "undone")
            handler.handle_send(
                SendPayload(text="/undo", attachments=[]), "model", "off"
            )

        mock_u.assert_called_once_with(Path("/test/workspace"))
        chat.add_info.assert_called_once_with("Undo", "undone")

    def test_undo_no_workspace_root(self, handler: SendHandler, chat: Mock) -> None:
        """When workspace_root is None, an error should be shown."""
        handler._workspace_root = None
        handler.handle_send(
            SendPayload(text="/undo", attachments=[]), "model", "off"
        )
        chat.add_error.assert_called_once_with("Undo", "No workspace root set.")


# ---------------------------------------------------------------------------
# Message queueing
# ---------------------------------------------------------------------------


class TestMessageQueueing:
    """Verify message queuing when bridge is running."""

    def test_queues_when_bridge_running(
        self, handler: SendHandler, bridge: Mock, input_panel: Mock
    ) -> None:
        """Payloads should be queued when bridge is busy, not sent."""
        bridge.is_running.return_value = True

        payload1 = SendPayload(text="first", attachments=[])
        payload2 = SendPayload(text="second", attachments=[])

        handler.handle_send(payload1, "model", "off")
        assert len(handler._message_queue) == 1
        input_panel.set_queued_messages.assert_called_with(1)
        bridge.send.assert_not_called()

        handler.handle_send(payload2, "model", "off")
        assert len(handler._message_queue) == 2
        input_panel.set_queued_messages.assert_called_with(2)
        bridge.send.assert_not_called()

    def test_process_queue_drains(
        self, handler: SendHandler, bridge: Mock, input_panel: Mock
    ) -> None:
        """_process_message_queue should drain one queued payload and send it."""
        bridge.is_running.return_value = False
        payload = SendPayload(text="queued", attachments=[])
        handler._message_queue.append(payload)
        handler._message_queue.append(
            SendPayload(text="second", attachments=[])
        )

        handler._process_message_queue("model", "off")
        assert len(handler._message_queue) == 1
        input_panel.set_queued_messages.assert_called_with(1)
        bridge.send.assert_called_once()

    def test_stop_clears_queue(
        self, handler: SendHandler, bridge: Mock, input_panel: Mock
    ) -> None:
        """handle_stop should clear the queue and cancel the bridge."""
        handler._message_queue.append(SendPayload(text="pending", attachments=[]))
        handler._message_queue.append(SendPayload(text="pending2", attachments=[]))

        handler.handle_stop()

        assert len(handler._message_queue) == 0
        input_panel.set_queued_messages.assert_called_with(0)
        bridge.request_cancel.assert_called_once_with()

    def test_empty_queue_does_nothing(
        self, handler: SendHandler, bridge: Mock
    ) -> None:
        """_process_message_queue with empty queue should be a no-op."""
        handler._process_message_queue("model", "off")
        bridge.send.assert_not_called()


# ---------------------------------------------------------------------------
# Vision routing
# ---------------------------------------------------------------------------


class TestVisionRouting:
    """Verify vision fallback vs native vision routing."""

    def test_native_vision_bypasses_fallback(
        self, handler: SendHandler, bridge: Mock, settings: Mock
    ) -> None:
        """When model supports vision natively, no vision thread is spawned."""
        settings.vision_enabled = True
        with patch.dict(
            "aura.gui.send_handler.PROVIDERS",
            {
                "deepseek": Mock(
                    models={
                        "vision-model": Mock(
                            supports_vision=True,
                            id="vision-model",
                            label="Vision Model",
                            input_per_m_usd=1.0,
                            output_per_m_usd=2.0,
                            cache_hit_per_m_usd=0.5,
                        )
                    }
                )
            },
        ):
            with patch("aura.gui.send_handler.threading.Thread") as mock_thread:
                handler.handle_send(
                    SendPayload(
                        text="what is this?",
                        attachments=[
                            Attachment(
                                kind="image",
                                name="pic.png",
                                b64="abcdef",
                                text_ref=None,
                            )
                        ],
                    ),
                    "vision-model",
                    "off",
                )

        # Native vision path should append multimodal, no thread spawned
        bridge.history.append_user_multimodal.assert_called_once()
        bridge.send.assert_called_once_with(
            model="vision-model",
            thinking="off",
            max_tool_rounds=ANY,
        )
        mock_thread.assert_not_called()

    def test_no_vision_when_disabled(
        self, handler: SendHandler, bridge: Mock, settings: Mock
    ) -> None:
        """When vision is disabled, multimodal should be used even without native support."""
        settings.vision_enabled = False
        with patch.dict(
            "aura.gui.send_handler.PROVIDERS",
            {
                "deepseek": Mock(
                    models={
                        "no-vision-model": Mock(
                            supports_vision=False,
                            id="no-vision-model",
                            label="No Vision",
                            input_per_m_usd=1.0,
                            output_per_m_usd=2.0,
                            cache_hit_per_m_usd=0.5,
                        )
                    }
                )
            },
        ):
            handler.handle_send(
                SendPayload(
                    text="what is this?",
                    attachments=[
                        Attachment(
                            kind="image",
                            name="pic.png",
                            b64="abcdef",
                            text_ref=None,
                        )
                    ],
                ),
                "no-vision-model",
                "off",
            )

        # Should use multimodal since vision is disabled but images exist
        bridge.history.append_user_multimodal.assert_called_once()
        bridge.send.assert_called_once()

    def test_vision_fallback_thread_spawned(
        self, handler: SendHandler, settings: Mock, input_panel: Mock
    ) -> None:
        """When model doesn't support vision but vision is enabled, thread should spawn."""
        settings.vision_enabled = True
        with patch.dict(
            "aura.gui.send_handler.PROVIDERS",
            {
                "deepseek": Mock(
                    models={
                        "no-vision-model": Mock(
                            supports_vision=False,
                            id="no-vision-model",
                            label="No Vision",
                            input_per_m_usd=1.0,
                            output_per_m_usd=2.0,
                            cache_hit_per_m_usd=0.5,
                        )
                    }
                )
            },
        ):
            with patch("aura.gui.send_handler.threading.Thread") as mock_thread:
                mock_thread_instance = Mock()
                mock_thread.return_value = mock_thread_instance

                handler.handle_send(
                    SendPayload(
                        text="what is this?",
                        attachments=[
                            Attachment(
                                kind="image",
                                name="pic.png",
                                b64="abcdef",
                                text_ref=None,
                            )
                        ],
                    ),
                    "no-vision-model",
                    "off",
                )

        input_panel.set_placeholder.assert_called_once_with(
            "Analyzing images (local fallback)..."
        )
        input_panel.setEnabled.assert_called_once_with(False)
        mock_thread.assert_called_once()
        mock_thread_instance.start.assert_called_once()


# ---------------------------------------------------------------------------
# Multimodal assembly
# ---------------------------------------------------------------------------


class TestMultimodalAssembly:
    """Verify how text, images, and vision descriptions are assembled."""

    def test_text_only_send(
        self, handler: SendHandler, bridge: Mock
    ) -> None:
        """Plain text without attachments should call append_user_text."""
        handler.handle_send(
            SendPayload(text="hello world", attachments=[]), "model", "off"
        )
        bridge.history.append_user_text.assert_called_once_with("hello world")
        bridge.send.assert_called_once_with(
            model="model",
            thinking="off",
            max_tool_rounds=ANY,
        )

    def test_text_with_non_image_attachments(
        self, handler: SendHandler, bridge: Mock
    ) -> None:
        """File attachments with text_ref should be appended to the text."""
        handler.handle_send(
            SendPayload(
                text="check this file",
                attachments=[
                    Attachment(
                        kind="file",
                        name="src/main.py",
                        b64=None,
                        text_ref="[user attached: src/main.py]",
                    )
                ],
            ),
            "model",
            "off",
        )

        call_args = bridge.history.append_user_text.call_args[0][0]
        assert "[user attached: src/main.py]" in call_args
        assert "check this file" in call_args

    def test_native_multimodal_assembly(
        self, handler: SendHandler, bridge: Mock
    ) -> None:
        """Native vision model should use append_user_multimodal with parts."""
        with patch.dict(
            "aura.gui.send_handler.PROVIDERS",
            {
                "deepseek": Mock(
                    models={
                        "vision-model": Mock(
                            supports_vision=True,
                            id="vision-model",
                            label="Vision Model",
                            input_per_m_usd=1.0,
                            output_per_m_usd=2.0,
                            cache_hit_per_m_usd=0.5,
                        )
                    }
                )
            },
        ):
            handler.handle_send(
                SendPayload(
                    text="describe this",
                    attachments=[
                        Attachment(
                            kind="image",
                            name="pic.png",
                            b64="base64data",
                            text_ref=None,
                        )
                    ],
                ),
                "vision-model",
                "off",
            )

        bridge.history.append_user_multimodal.assert_called_once_with([
            {"type": "text", "text": "describe this"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,base64data"},
            },
        ])

    def test_vision_descriptions_injected(
        self, handler: SendHandler, bridge: Mock
    ) -> None:
        """Vision descriptions should be prepended to the text sent to the model."""
        handler._finalize_send(
            SendPayload(text="what is in this image", attachments=[]),
            "model",
            "off",
            vision_descriptions=["A cat sitting on a chair."],
            vision_error=None,
        )

        call_args = bridge.history.append_user_text.call_args[0][0]
        assert "Image 1 description" in call_args
        assert "A cat sitting on a chair." in call_args
        assert "what is in this image" in call_args

    def test_vision_error_in_text(
        self, handler: SendHandler, bridge: Mock
    ) -> None:
        """When vision fails, the error should be included in the text."""
        handler._finalize_send(
            SendPayload(text="what is this", attachments=[]),
            "model",
            "off",
            vision_descriptions=[],
            vision_error="Local vision model unavailable",
        )

        call_args = bridge.history.append_user_text.call_args[0][0]
        assert "Note" in call_args
        assert "Local vision model unavailable" in call_args

    def test_vision_error_without_descriptions_fallback(
        self, handler: SendHandler, bridge: Mock
    ) -> None:
        """When vision completely fails and there are no descriptions."""
        handler._finalize_send(
            SendPayload(text="what is this", attachments=[]),
            "model",
            "off",
            vision_descriptions=[],
            vision_error="Connection refused",
        )

        call_args = bridge.history.append_user_text.call_args[0][0]
        assert "Connection refused" in call_args
        assert "Note" in call_args

    def test_vision_disabled_with_images_uses_multimodal(
        self, handler: SendHandler, bridge: Mock, settings: Mock
    ) -> None:
        """When vision is disabled but images exist, use multimodal parts."""
        settings.vision_enabled = False
        handler._finalize_send(
            SendPayload(
                text="describe this",
                attachments=[
                    Attachment(
                        kind="image",
                        name="pic.png",
                        b64="abc123",
                        text_ref=None,
                    )
                ],
            ),
            "model",
            "off",
            vision_descriptions=[],
            vision_error=None,
        )

        bridge.history.append_user_multimodal.assert_called_once_with([
            {"type": "text", "text": "describe this"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,abc123"},
            },
        ])


# ---------------------------------------------------------------------------
# Queue and workspace root helpers
# ---------------------------------------------------------------------------


class TestQueueHelpers:
    """Verify clear_queue and set_workspace_root."""

    def test_clear_queue(self, handler: SendHandler) -> None:
        handler._message_queue.append(SendPayload(text="x", attachments=[]))
        handler._message_queue.append(SendPayload(text="y", attachments=[]))
        handler.clear_queue()
        assert handler._message_queue == []

    def test_set_workspace_root(self, handler: SendHandler) -> None:
        new_root = Path("/new/root")
        handler.set_workspace_root(new_root)
        assert handler._workspace_root == new_root


# ---------------------------------------------------------------------------
# Retry last message
# ---------------------------------------------------------------------------


class TestRetryLastMessage:
    def test_retry_last_rewinds_replays_and_sends(
        self,
        handler: SendHandler,
        bridge: Mock,
        chat: Mock,
        input_panel: Mock,
        settings: Mock,
    ) -> None:
        bridge.history.rewind_to_last_user_turn.return_value = True
        replay_cb = Mock()

        ok = handler.handle_retry_last("model", "off", replay_cb=replay_cb)

        assert ok is True
        bridge.history.rewind_to_last_user_turn.assert_called_once_with()
        input_panel.set_queued_messages.assert_called_once_with(0)
        chat.reset.assert_called_once_with()
        replay_cb.assert_called_once_with()
        chat.begin_assistant.assert_called_once_with()
        bridge.send.assert_called_once_with(
            model="model",
            thinking="off",
            max_tool_rounds=settings.max_tool_rounds,
        )

    def test_retry_last_does_nothing_while_running(
        self, handler: SendHandler, bridge: Mock
    ) -> None:
        bridge.is_running.return_value = True

        ok = handler.handle_retry_last("model", "off")

        assert ok is False
        bridge.history.rewind_to_last_user_turn.assert_not_called()
        bridge.send.assert_not_called()

    def test_retry_last_shows_error_without_user_message(
        self, handler: SendHandler, bridge: Mock, chat: Mock
    ) -> None:
        bridge.history.rewind_to_last_user_turn.return_value = False

        ok = handler.handle_retry_last("model", "off")

        assert ok is False
        chat.add_error.assert_called_once_with("Retry", "No user message to retry.")
        bridge.send.assert_not_called()
