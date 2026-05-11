"""Main application window: three-pane splitter, toolbar, chat + input."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QRadialGradient
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.bridge import ConversationBridge
from aura.bridge.qt_bridge import PLANNER_SYSTEM_PROMPT
from aura.prompts import SINGLE_SYSTEM_PROMPT
from aura.config import (
    APP_NAME,
    PROVIDERS,
    AppSettings,
    ModelInfo,
    ThinkingMode,
    icon_path,
    load_settings,
    load_workspace_root,
    save_workspace_root,
)
from aura.conversation.persistence import (
    LoadedConversation,
    load_conversation,
    most_recent_conversation,
    save_conversation,
)
from aura.git_ops import ensure_aura_gitignored, git_init, is_git_repo
from aura.gui.chat_view import ChatView
from aura.gui.input_panel import InputPanel, SendPayload
from aura.gui.settings_dialog import SettingsDialog
from aura.gui.spec_edit_dialog import SpecApprovalDialog, SpecEditDialog
from aura.gui.onboarding_dialog import OnboardingDialog
from aura.gui.status_bar import AuraStatusBar
from aura.gui.left_pane import LeftPane
from aura.gui.main_window_toolbar import MainWindowToolbar
from aura.gui.aura_widget import AuraPlayground, GlassSwitch

_THINKING_LABEL = {"off": "Off", "high": "High", "max": "Max"}


class MainWindow(QMainWindow):
    # Thread-safe signals for cross-thread communication.
    _vision_done = Signal(object, list, object)   # SendPayload, list[str], str|None
    _save_succeeded = Signal(Path)                # Path
    _save_failed = Signal(str)                    # error message

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(icon_path())))
        self.resize(1400, 900)

        # Drag-to-move state
        self._dragging = False
        self._drag_start_pos = None

        # Settings.
        self._settings: AppSettings = load_settings()

        # Workspace.
        self._workspace_root: Path | None = load_workspace_root()
        if self._workspace_root is None:
            self._workspace_root = Path.cwd()

        # Bridge — provider-aware.
        self._bridge = ConversationBridge(
            parent_widget=self,
            provider=self._settings.provider,
        )
        self._bridge.set_workspace_root(self._workspace_root)
        self._apply_planner_worker_mode_to_bridge(self._settings.planner_worker_mode)
        self._bridge.set_worker_model(self._settings.default_worker_model)
        self._bridge.set_worker_thinking(self._settings.default_worker_thinking)
        self._bridge.set_temperature(self._settings.temperature)
        self._bridge.set_worker_temperature(self._settings.worker_temperature)
        self._bridge.set_custom_system_prompts(
            self._settings.system_prompt,
            self._settings.planner_system_prompt,
            self._settings.worker_system_prompt,
        )
        self._bridge.set_auto_commit_enabled(self._settings.auto_commit_enabled)
        self._bridge.set_auto_dispatch(self._settings.auto_dispatch)
        self._bridge.set_auto_approve(self._settings.auto_approve)
        # Persistence state.
        self._current_conversation_path: Path | None = None

        # Session usage accumulators (per-model so cost is exact when mixing).
        self._session_usage: dict[str, dict[str, int]] = {}

        # Queued messages sent while worker is running.
        self._message_queue: list[SendPayload] = []

        # ----- toolbar ----
        self._toolbar = MainWindowToolbar(self._settings, self)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._toolbar)
        self._toolbar.new_conversation_requested.connect(self._on_new_conversation)
        self._toolbar.open_conversation_requested.connect(self._on_open_conversation)
        self._toolbar.read_only_toggled.connect(self._on_read_only_toggled)
        self._toolbar.auto_dispatch_toggled.connect(self._on_auto_dispatch_toggled)
        self._toolbar.auto_approve_toggled.connect(self._on_auto_approve_toggled)
        self._toolbar.settings_requested.connect(self._on_open_settings)
        self._toolbar.minimize_requested.connect(self.showMinimized)
        self._toolbar.maximize_requested.connect(self._toggle_maximize)
        self._toolbar.close_requested.connect(self.close)

        # ----- status bar -----
        self._status_bar = AuraStatusBar(self)
        self.setStatusBar(self._status_bar)

        # ----- splitter ----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(3)

        # Left pane: workspace label + change root + tree + model config.
        self._left_pane = LeftPane(self._workspace_root, parent=self)
        self._left_pane.populate_models(self._settings.provider)
        self._left_pane.change_root_requested.connect(self._on_change_root)
        self._left_pane.planner_model_changed.connect(lambda: self._refresh_status_bar())
        self._left_pane.planner_thinking_changed.connect(lambda: self._refresh_status_bar())
        self._left_pane.worker_model_changed.connect(self._on_sidebar_worker_model_changed)
        self._left_pane.worker_thinking_changed.connect(self._on_sidebar_worker_thinking_changed)
        self._tree = self._left_pane.tree()
        splitter.addWidget(self._left_pane)

        # Middle pane: chat + input
        center = QWidget(self)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(20, 0, 20, 16)
        center_layout.setSpacing(0)

        self._chat = ChatView()
        self._chat.setParent(self)
        if self._settings.planner_worker_mode:
            self._chat.set_compact_tools(True)
        center_layout.addWidget(self._chat, 1)

        self._input = InputPanel(self._workspace_root, parent=self)
        # Apply default model / thinking from settings.
        if self._settings.planner_worker_mode:
            self.set_model(self._settings.default_planner_model)
            self.set_thinking(self._settings.default_planner_thinking)
        else:
            self.set_model(self._settings.default_model)
            self.set_thinking(self._settings.default_thinking)
        self.set_worker_model(self._settings.default_worker_model)
        self.set_worker_thinking(self._settings.default_worker_thinking)
        self._set_sidebar_planner_worker_mode(self._settings.planner_worker_mode)
        center_layout.addWidget(self._input)

        splitter.addWidget(center)

        # Right pane: worker activity (embedded, not a separate window)
        self._playground = AuraPlayground(parent=self)
        splitter.addWidget(self._playground)

        w = self.width()
        splitter.setSizes([min(200, w // 8), (w - min(200, w // 8)) // 2, (w - min(200, w // 8)) // 2])
        splitter.setStretchFactor(0, 0)  # workspace tree doesn't stretch
        splitter.setStretchFactor(1, 1)  # chat gets 1/2 of stretch
        splitter.setStretchFactor(2, 1)  # worker gets 1/2 of stretch

        self.setCentralWidget(splitter)

        # Make the central widget and splitter transparent so the gradient shows through
        splitter.setStyleSheet("background: transparent;")
        self.centralWidget().setStyleSheet("background: transparent;")
        self.centralWidget().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

        # Frameless window — no native title bar
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)

        # ----- wire bridge ↔ view -----
        self._bridge.started.connect(self._on_started)
        self._bridge.finished.connect(self._on_finished)
        self._bridge.reasoningDelta.connect(self._chat.append_reasoning)
        self._bridge.contentDelta.connect(self._chat.append_content)
        self._bridge.toolCallStart.connect(self._chat.add_tool_call)
        self._bridge.toolCallArgs.connect(self._chat.append_tool_args)
        self._bridge.toolCallEnd.connect(lambda _id: None)
        self._bridge.toolResult.connect(self._on_tool_result)
        self._bridge.diffDecided.connect(self._on_diff_decided)
        self._bridge.streamDone.connect(self._on_stream_done)
        self._bridge.apiError.connect(self._on_api_error)
        self._bridge.usageWithModel.connect(self._on_usage)

        self._input.sent.connect(self._on_send)
        self._input.stop_requested.connect(self._on_stop)

        # Planner / worker dispatch flow.
        self._bridge.workerDispatchRequested.connect(self._on_worker_dispatch_requested)
        self._bridge.workerStarted.connect(self._on_worker_started)
        self._bridge.workerFinished.connect(self._on_worker_finished)
        self._bridge.workerCancelled.connect(self._on_worker_cancelled)
        self._bridge.workerReasoningDelta.connect(self._on_worker_reasoning)
        self._bridge.workerContentDelta.connect(self._on_worker_content)
        self._bridge.workerToolCallStart.connect(self._on_worker_tool_call_start)
        self._bridge.workerToolCallArgs.connect(self._on_worker_tool_args)
        self._bridge.workerToolCallEnd.connect(lambda _t, _w: None)
        self._bridge.workerToolResult.connect(self._on_worker_tool_result)
        self._bridge.workerDiffDecided.connect(self._on_worker_diff_decided)
        self._bridge.workerApiError.connect(self._on_worker_api_error)
        self._bridge.workerUsage.connect(self._on_worker_usage)
        self._bridge.workerTodoListUpdated.connect(self._on_worker_todo_list_updated)
        self._bridge.workerTerminalOutput.connect(self._on_worker_terminal_output)
        self._bridge.terminalOutput.connect(self._on_terminal_output)

        # Mermaid diagram detection from chat → playground
        self._chat.mermaid_detected.connect(self._playground.add_mermaid_artifact)

        self._vision_done.connect(self._on_vision_done)
        self._save_succeeded.connect(self._set_current_conv_path)
        self._save_failed.connect(lambda msg: self._chat.add_error("Could not save conversation", msg))

        self._update_workspace_label()
        self._refresh_status_bar()

        # Restore most recent conversation if enabled.
        if self._settings.restore_last_conversation:
            self._maybe_restore_last_conversation()

    def showEvent(self, event) -> None:
        """Triggered when the window is shown. Used for first-launch onboarding."""
        super().showEvent(event)
        if not self._settings.first_launch_done:
            # We use a 0ms timer to ensure the event loop processes the window
            # show COMPLETELY before popping the modal dialog.
            QTimer.singleShot(0, self._show_onboarding)

    def _show_onboarding(self) -> None:
        dlg = OnboardingDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._settings.first_launch_done = True
            from aura.config import save_settings
            save_settings(self._settings)

    # ----- paintEvent: radial gradient background --------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = self.rect().center()
        center.setY(int(self.height() * 0.15))
        radius = max(self.width(), self.height()) * 0.8
        gradient = QRadialGradient(center, radius)
        gradient.setColorAt(0.0, QColor(30, 34, 46, 255))
        gradient.setColorAt(0.4, QColor(18, 20, 26, 255))
        gradient.setColorAt(1.0, QColor(6, 8, 12, 255))
        painter.fillRect(self.rect(), gradient)
        painter.end()
        super().paintEvent(event)

    # ----- drag-to-move on toolbar -----------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            tb_geo = self._toolbar.geometry()
            if tb_geo.contains(event.position().toPoint()):
                # Don't drag if clicking on interactive toolbar widgets (buttons, switches, etc.)
                pos = self._toolbar.mapFrom(self, event.position().toPoint())
                child = self._toolbar.childAt(pos)
                if child is not None:
                    # Check ancestors to see if we've clicked inside an interactive widget
                    curr = child
                    is_interactive = False
                    while curr and curr != self._toolbar:
                        if isinstance(curr, (QToolButton, GlassSwitch)):
                            is_interactive = True
                            break
                        curr = curr.parent()
                    
                    if is_interactive:
                        super().mousePressEvent(event)
                        return
                
                self._drag_start_pos = event.globalPosition().toPoint()
                self._dragging = True
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if getattr(self, '_dragging', False):
            delta = event.globalPosition().toPoint() - self._drag_start_pos
            self.move(self.pos() + delta)
            self._drag_start_pos = event.globalPosition().toPoint()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if getattr(self, '_dragging', False):
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ----- window state helpers -------------------------------------------

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self._toolbar.update_maximize_icon(self.isMaximized())

    def changeEvent(self, event) -> None:
        if event.type() == event.Type.WindowStateChange:
            if hasattr(self, '_toolbar'):
                self._toolbar.update_maximize_icon(self.isMaximized())
        super().changeEvent(event)

    # ----- provider-aware model combo helpers -----------------------------

    def _model_label(self, model_id: str) -> str:
        """Look up a model's human-readable label from any provider."""
        for cfg in PROVIDERS.values():
            if model_id in cfg.models:
                return cfg.models[model_id].label
        return model_id

    # ----- model / thinking accessors ------------------------------------

    def current_model(self) -> str:
        return self._left_pane.current_planner_model()

    def current_thinking(self) -> ThinkingMode:
        return self._left_pane.current_planner_thinking()

    def current_worker_model(self) -> str:
        return self._left_pane.current_worker_model()

    def current_worker_thinking(self) -> ThinkingMode:
        return self._left_pane.current_worker_thinking()

    def set_model(self, model: str) -> None:
        self._left_pane.set_planner_model(model)

    def set_thinking(self, thinking: ThinkingMode) -> None:
        self._left_pane.set_planner_thinking(thinking)

    def set_worker_model(self, model: str) -> None:
        self._left_pane.set_worker_model(model)

    def set_worker_thinking(self, thinking: ThinkingMode) -> None:
        self._left_pane.set_worker_thinking(thinking)

    def _on_sidebar_worker_model_changed(self, model: str) -> None:
        self._bridge.set_worker_model(model)
        self._refresh_status_bar()

    def _on_sidebar_worker_thinking_changed(self, thinking: str) -> None:
        self._bridge.set_worker_thinking(thinking)  # type: ignore[arg-type]
        self._refresh_status_bar()

    def _set_sidebar_planner_worker_mode(self, enabled: bool) -> None:
        self._left_pane.set_planner_worker_mode(enabled)

    # ----- status bar -----------------------------------------------------

    def _refresh_status_bar(self) -> None:
        ws = str(self._workspace_root) if self._workspace_root else "(none)"
        self._status_bar.refresh(
            workspace_root=ws,
            model_id=self.current_model(),
            thinking=self.current_thinking(),
            session_usage=self._session_usage
        )

    def _reset_session_usage(self) -> None:
        self._session_usage.clear()
        self._refresh_status_bar()

    # ----- handlers -------------------------------------------------------

    def _on_change_root(self) -> None:
        start = str(self._workspace_root) if self._workspace_root else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose workspace root", start)
        if not chosen:
            return
        path = Path(chosen)
        self._workspace_root = path
        self._bridge.set_workspace_root(path)
        self._input.set_workspace_root(path)
        self._tree.set_root(path)
        save_workspace_root(path)
        # New workspace — drop any current conversation pointer (different .aura/).
        self._current_conversation_path = None
        self._update_workspace_label()
        self._refresh_status_bar()

        # Offer to initialize git if the workspace is not a git repo.
        if not is_git_repo(path):
            reply = QMessageBox.question(
                self,
                "Not a Git Repository",
                "This workspace is not a git repository.\n\n"
                "Aura uses git for auto-commit and undo.\n"
                "Would you like to run 'git init' and create an initial commit?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                ok, msg = git_init(path)
                if ok:
                    QMessageBox.information(
                        self, "Git Repository", msg
                    )
                else:
                    QMessageBox.warning(
                        self, "Git Init Failed", msg
                    )

    def _update_workspace_label(self) -> None:
        self._left_pane.update_workspace_label(self._workspace_root)

    def _on_read_only_toggled(self, checked: bool) -> None:
        self._bridge.set_read_only(checked)
        self._toolbar.set_read_only(checked)

    def _on_auto_dispatch_toggled(self, checked: bool) -> None:
        self._settings.auto_dispatch = checked
        self._bridge.set_auto_dispatch(checked)
        self._toolbar.refresh_auto_toggle_tooltips()
        from aura.config import save_settings
        save_settings(self._settings)

    def _on_auto_approve_toggled(self, checked: bool) -> None:
        self._settings.auto_approve = checked
        self._bridge.set_auto_approve(checked)
        self._toolbar.refresh_auto_toggle_tooltips()
        from aura.config import save_settings
        save_settings(self._settings)

    def _on_new_conversation(self) -> None:
        if self._bridge.is_running():
            QMessageBox.information(
                self, APP_NAME, "Wait for the current response to finish, or click Stop."
            )
            return
        self._bridge.reset_history()
        self._bridge.clear_pre_worker_snapshot()
        self._chat.reset()
        self._current_conversation_path = None
        self._reset_session_usage()
        self._playground.clear()
        self._message_queue.clear()
        self._input.set_queued_messages(0)

    def _on_open_conversation(self) -> None:
        if self._bridge.is_running():
            QMessageBox.information(
                self, APP_NAME, "Wait for the current response to finish, or click Stop."
            )
            return
        if self._workspace_root is None:
            return
        start = str(self._workspace_root / ".aura" / "conversations")
        Path(start).mkdir(parents=True, exist_ok=True)
        ensure_aura_gitignored(self._workspace_root)
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Open Conversation", start, "Conversations (*.json)"
        )
        if not chosen:
            return
        try:
            loaded = load_conversation(Path(chosen))
        except Exception as exc:
            QMessageBox.warning(
                self, APP_NAME, f"Could not open conversation:\n{exc}"
            )
            return
        self._apply_loaded_conversation(loaded)

    def _on_open_settings(self) -> None:
        dlg = SettingsDialog(
            settings=self._settings,
            workspace_root=self._workspace_root,
            on_change_root=self._on_change_root,
            parent=self,
        )
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            old_provider = self._settings.provider
            self._settings = dlg.result_settings()
            
            # Always refresh combos to pick up dynamically fetched models
            self._left_pane.populate_models(self._settings.provider)
            
            if self._settings.provider != old_provider:
                self._bridge.set_provider(self._settings.provider)
            # Apply to current widgets.
            if self._settings.planner_worker_mode:
                self.set_model(self._settings.default_planner_model)
                self.set_thinking(self._settings.default_planner_thinking)
            else:
                self.set_model(self._settings.default_model)
                self.set_thinking(self._settings.default_thinking)
            self.set_worker_model(self._settings.default_worker_model)
            self.set_worker_thinking(self._settings.default_worker_thinking)
            self._set_sidebar_planner_worker_mode(self._settings.planner_worker_mode)
            self._apply_planner_worker_mode_to_bridge(self._settings.planner_worker_mode)
            self._bridge.set_worker_model(self._settings.default_worker_model)
            self._bridge.set_worker_thinking(self._settings.default_worker_thinking)
            self._bridge.set_temperature(self._settings.temperature)
            self._bridge.set_worker_temperature(self._settings.worker_temperature)
            self._bridge.set_custom_system_prompts(
                self._settings.system_prompt,
                self._settings.planner_system_prompt,
                self._settings.worker_system_prompt,
            )
            self._bridge.set_auto_commit_enabled(self._settings.auto_commit_enabled)
            self._bridge.set_auto_dispatch(self._settings.auto_dispatch)
            self._bridge.set_auto_approve(self._settings.auto_approve)
            self._toolbar.set_auto_dispatch(self._settings.auto_dispatch)
            self._toolbar.set_auto_approve(self._settings.auto_approve)
            self._refresh_status_bar()

    def _apply_planner_worker_mode_to_bridge(self, enabled: bool) -> None:
        self._bridge.set_planner_worker_mode(enabled)
        if enabled:
            prompt = self._settings.planner_system_prompt or PLANNER_SYSTEM_PROMPT
        else:
            prompt = self._settings.system_prompt or SINGLE_SYSTEM_PROMPT
        self._bridge.set_system_prompt(prompt)
        if hasattr(self, "_chat"):
            self._chat.set_compact_tools(enabled)

    def _on_worker_model_changed(self, model: str) -> None:
        self._bridge.set_worker_model(model)
        self._refresh_status_bar()

    def _on_worker_thinking_changed(self, thinking: str) -> None:
        self._bridge.set_worker_thinking(thinking)  # type: ignore[arg-type]
        self._refresh_status_bar()

    def _get_current_model_info(self) -> ModelInfo | None:
        """Helper to get metadata for the currently selected planner model."""
        cfg = PROVIDERS.get(self._settings.provider)
        if not cfg:
            return None
        return cfg.models.get(self.current_model())

    def _on_send(self, payload: SendPayload) -> None:
        # Intercept /undo command
        if payload.text.strip().lower() == "/undo":
            self._chat.add_user("/undo")
            self._on_undo()
            return

        if self._bridge.is_running():
            self._message_queue.append(payload)
            self._input.set_queued_messages(len(self._message_queue))
            return

        # Check if the current model supports native vision
        m_info = self._get_current_model_info()
        native_vision = m_info.supports_vision if m_info else False

        # Prepare history append: image attachments go via multimodal content array.
        text = payload.text
        # Add text refs from non-image attachments to the text body so the model knows.
        text_refs = [a.text_ref for a in payload.attachments if a.text_ref]
        if text_refs:
            ref_block = "\n".join(text_refs)
            text = f"{text}\n\n{ref_block}".strip() if text else ref_block
        image_atts = [a for a in payload.attachments if a.kind == "image" and a.b64]

        # --- Vision routing ---
        vision_descriptions: list[str] = []
        vision_error: str | None = None

        if image_atts and not native_vision and self._settings.vision_enabled:
            # Fall back to local vision model for descriptive middleman
            self._input.set_placeholder("Analyzing images (local fallback)...")
            self._input.setEnabled(False)
            
            def _run_vision():
                nonlocal vision_error
                try:
                    from aura.vision import VisionClient
                    client = VisionClient(
                        endpoint=self._settings.vision_endpoint,
                        model=self._settings.vision_model,
                    )
                    for a in image_atts:
                        desc = client.describe(a.b64)
                        vision_descriptions.append(desc)
                except Exception as exc:
                    vision_error = (
                        f"Local vision model unavailable "
                        f"({self._settings.vision_model}): {exc}"
                    )
                
                # Marshal back to GUI thread to actually send the message
                self._vision_done.emit(payload, vision_descriptions, vision_error)

            import threading
            threading.Thread(target=_run_vision, daemon=True).start()
            return  # Wait for _on_vision_done

        # Either no images, native vision supported, or local vision disabled
        self._finalize_send(payload, vision_descriptions, vision_error)

    def _on_vision_done(self, payload: SendPayload, descriptions: list[str], error: str | None) -> None:
        self._input.setEnabled(True)
        self._input.set_placeholder("")
        self._finalize_send(payload, descriptions, error)

    def _finalize_send(self, payload: SendPayload, vision_descriptions: list[str], vision_error: str | None) -> None:
        image_atts = [a for a in payload.attachments if a.kind == "image" and a.b64]
        text = payload.text
        text_refs = [a.text_ref for a in payload.attachments if a.text_ref]
        if text_refs:
            ref_block = "\n".join(text_refs)
            text = f"{text}\n\n{ref_block}".strip() if text else ref_block

        # Determine if we should send a native multimodal payload
        m_info = self._get_current_model_info()
        native_vision = m_info.supports_vision if m_info else False

        if native_vision and image_atts:
            # Construct native multimodal parts
            parts = []
            if text:
                parts.append({"type": "text", "text": text})
            for a in image_atts:
                # Note: PySide6 QWebEngine/Base64 handling ensures valid PNG/JPG data
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{a.b64}"}
                })
            self._bridge.history.append_user_multimodal(parts)
            display_text = text
        elif vision_descriptions:
            # Build vision block from local fallback
            vision_block_parts = []
            for i, desc in enumerate(vision_descriptions):
                vision_block_parts.append(
                    f"[Image {i + 1} description via local vision model:]\n{desc}"
                )
            vision_block = "\n\n---\n\n".join(vision_block_parts)

            if vision_error:
                vision_block += f"\n\n[Vision error: {vision_error}]"

            # Final text for the model
            final_text = f"{vision_block}\n\n[User's question:]\n{text}" if text else vision_block
            display_text = final_text
            self._bridge.history.append_user_text(final_text)
        elif vision_error and not vision_descriptions:
            # Vision completely failed — fall back to sending text-only with error note
            final_text = f"{text}\n\n[Note: {vision_error}]" if text else f"[Vision error: {vision_error}]"
            display_text = final_text
            self._bridge.history.append_user_text(final_text)
        else:
            # No images or vision disabled (keep old multimodal-400-fallback behavior for safety)
            if image_atts and not self._settings.vision_enabled:
                parts = []
                if text:
                    parts.append({"type": "text", "text": text})
                for a in image_atts:
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{a.b64}"}
                    })
                self._bridge.history.append_user_multimodal(parts)
                display_text = text
            else:
                display_text = text
                self._bridge.history.append_user_text(text)

        self._chat.add_user(display_text, [a.b64 for a in image_atts] or None)
        self._chat.scroll_to_bottom(force=True)
        self._chat.begin_assistant()

        self._bridge.send(
            model=self.current_model(),
            thinking=self.current_thinking(),
        )

    def _on_stop(self) -> None:
        self._bridge.request_cancel()
        self._message_queue.clear()
        self._input.set_queued_messages(0)

    def _on_started(self) -> None:
        self._input.set_streaming(True)

    def _on_finished(self) -> None:
        self._input.set_streaming(False)
        self._chat.assistant_done()
        self._chat.stop_current_aura()
        self._input.focus_editor()
        self._process_message_queue()

    def _process_message_queue(self) -> None:
        """Send the next queued message, if any."""
        if not self._message_queue:
            return
        payload = self._message_queue.pop(0)
        self._input.set_queued_messages(len(self._message_queue))
        self._on_send(payload)

    def _on_stream_done(self, finish_reason: str, full_message: dict) -> None:
        # If the model produced tool calls, it's not actually done — the bridge
        # will execute them and loop back. Keep the aura alive.
        tool_calls = full_message.get("tool_calls") or []
        if tool_calls:
            # Finalize markdown but keep the aura pulsing.
            self._chat.finalize_markdown_only()
            # If any call is a dispatch, transition to "coding" (cyan)
            has_dispatch = any(
                tc.get("function", {}).get("name") == "dispatch_to_worker"
                for tc in tool_calls
            )
            if has_dispatch:
                self._chat.hold_aura_coding()
            # Note: For non-dispatch tool calls, we keep the current aura state
            # (which is usually already "coding" if a tool call was emitted).
        else:
            # No tool calls — this is the final turn.
            self._chat.assistant_done()
        # Auto-save after each assistant turn — including partial tool-call rounds.
        self._auto_save_conversation()

    def _on_tool_result(self, tool_id: str, name: str, ok: bool, result: str, extras: dict) -> None:
        self._chat.set_tool_result(tool_id, ok, result)

    def _on_diff_decided(
        self,
        tool_call_id: str,
        decision: str,
        rel_path: str,
        old: str,
        new: str,
        is_new_file: bool,
    ) -> None:
        self._chat.add_diff_card(tool_call_id, rel_path, old, new, decision, is_new_file)

    # ---- planner/worker dispatch slots -----------------------------------

    def _on_worker_dispatch_requested(
        self,
        tool_call_id: str,
        goal: str,
        files: list,
        spec: str,
        acceptance: str,
    ) -> None:
        if self._bridge.auto_dispatch:
            self._bridge.user_dispatched(tool_call_id, goal, list(files), spec, acceptance)
            return
        dlg = SpecApprovalDialog(goal, list(files), spec, acceptance, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._bridge.user_dispatched(
                tool_call_id, dlg.goal(), dlg.files(), dlg.spec(), dlg.acceptance()
            )
        else:
            self._bridge.user_cancelled_dispatch(tool_call_id)

    def _on_dispatch_clicked(self, tool_call_id: str) -> None:
        card = self._chat.get_spec_card(tool_call_id)
        if card is None:
            return
        goal, files, spec, acceptance = card.current_spec()
        self._bridge.user_dispatched(tool_call_id, goal, files, spec, acceptance)

    def _on_edit_spec_clicked(self, tool_call_id: str) -> None:
        card = self._chat.get_spec_card(tool_call_id)
        if card is None:
            return
        goal, files, spec, acceptance = card.current_spec()
        dlg = SpecEditDialog(goal, files, spec, acceptance, parent=self)
        if dlg.exec() == SpecEditDialog.DialogCode.Accepted:
            card.update_spec(dlg.goal(), dlg.files(), dlg.spec(), dlg.acceptance())

    def _on_cancel_dispatch_clicked(self, tool_call_id: str) -> None:
        self._bridge.user_cancelled_dispatch(tool_call_id)

    def _on_worker_started(self, tool_call_id: str) -> None:
        # Baton pass: stop the planner's aura so the worker's playground
        # takes over the visual pulse. The planner aura was held alive by
        # finalize_markdown_only() + hold_aura_coding() in _on_stream_done.
        self._chat.stop_current_aura()
        self._playground.begin_assistant()
        self._input.set_streaming(False)

    def _on_worker_finished(self, tool_call_id: str, ok: bool, summary: str) -> None:
        self._playground.worker_finished(ok, summary)

    def _on_worker_cancelled(self, tool_call_id: str) -> None:
        self._playground.worker_cancelled()

    def _on_worker_reasoning(self, tool_call_id: str, text: str) -> None:
        self._playground.append_reasoning(text)

    def _on_worker_content(self, tool_call_id: str, text: str) -> None:
        self._playground.append_content(text)

    def _on_worker_tool_call_start(self, tool_call_id: str, worker_tool_id: str, name: str) -> None:
        self._playground.add_tool_call(worker_tool_id, name)

    def _on_worker_tool_args(self, tool_call_id: str, worker_tool_id: str, fragment: str) -> None:
        self._playground.append_tool_args(worker_tool_id, fragment)

    def _on_worker_tool_result(
        self,
        parent_tool_id: str,
        worker_tool_id: str,
        name: str,
        ok: bool,
        result: str,
        extras: dict,
    ) -> None:
        self._playground.set_tool_result(worker_tool_id, ok, result)

    def _on_worker_diff_decided(
        self,
        parent_tool_id: str,
        worker_tool_id: str,
        decision: str,
        rel_path: str,
        old: str,
        new: str,
        is_new_file: bool,
    ) -> None:
        self._playground.add_diff_card(worker_tool_id, rel_path, old, new, decision, is_new_file)

    def _on_worker_api_error(self, tool_call_id: str, status: int, message: str) -> None:
        title = f"API Error {status}" if status > 0 else "Worker Error"
        self._playground.add_error(f"{title}: {message}")

    def _on_view_worker_clicked(self, tool_call_id: str) -> None:
        pass

    def _on_worker_usage(
        self,
        _tool_call_id: str,
        model_id: str,
        prompt: int,
        completion: int,
        hit: int,
        miss: int,
    ) -> None:
        if hit == 0 and miss == 0:
            miss = prompt
        bucket = self._session_usage.setdefault(
            model_id, {"hit": 0, "miss": 0, "out": 0}
        )
        bucket["hit"] += hit
        bucket["miss"] += miss
        bucket["out"] += completion
        self._refresh_status_bar()

    def _on_worker_todo_list_updated(self, tool_call_id: str, tasks: list) -> None:
        """Route the worker's TODO list update to the Playground's pinned widget."""
        self._playground.update_todo_list(tasks)

    def _on_terminal_output(self, tool_call_id: str, text: str) -> None:
        """Route terminal output (single mode) to the ChatView's TerminalCard."""
        self._chat.append_terminal_output(tool_call_id, text)

    def _on_worker_terminal_output(self, parent_tool_id: str, worker_tool_id: str, text: str) -> None:
        """Route terminal output (worker mode) to the Playground's TerminalCard."""
        self._playground.append_terminal_output(worker_tool_id, text)

    def _on_undo(self) -> None:
        """Handle /undo command — restore to pre-worker snapshot or git reset last commit."""
        from aura.git_ops import undo_last_commit, restore_to_snapshot

        ws_root = self._workspace_root
        if ws_root is None:
            self._chat.add_error("Undo", "No workspace root set.")
            return

        # Check for pre-worker snapshot first (more reliable)
        snapshot_sha = self._bridge.get_pre_worker_snapshot()
        if snapshot_sha is not None:
            # Confirm destructive restore
            reply = QMessageBox.question(
                self,
                "Restore to Pre-Worker State",
                "This will discard ALL changes since the worker started "
                "(including any intervening commits). Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                ok, message = restore_to_snapshot(ws_root, snapshot_sha)
                self._bridge.clear_pre_worker_snapshot()
                if ok:
                    self._chat.add_info("Undo", message)
                else:
                    self._chat.add_error("Undo", message)
        else:
            # Fall back to simple undo_last_commit
            ok, message = undo_last_commit(ws_root)
            if ok:
                self._chat.add_info("Undo", message)
            else:
                self._chat.add_error("Undo", message)

    def _on_api_error(self, status: int, message: str) -> None:
        title = f"API Error {status}" if status > 0 else "Error"
        self._chat.add_error(title, message, show_retry=True)
        self._chat.stop_current_aura()

    def _on_retry(self) -> None:
        if self._bridge.is_running():
            return
        self._chat.begin_assistant()
        self._bridge.send(
            model=self.current_model(),
            thinking=self.current_thinking(),
        )

    def _on_usage(
        self, model_id: str, prompt: int, completion: int, hit: int, miss: int
    ) -> None:
        # Some servers don't surface the cache split — fall back so we still meter cost.
        if hit == 0 and miss == 0:
            miss = prompt
        bucket = self._session_usage.setdefault(
            model_id, {"hit": 0, "miss": 0, "out": 0}
        )
        bucket["hit"] += hit
        bucket["miss"] += miss
        bucket["out"] += completion
        self._refresh_status_bar()

    # ----- persistence ----------------------------------------------------

    def _auto_save_conversation(self) -> None:
        if self._workspace_root is None:
            return
        if not self._bridge.history.messages:
            return

        # Deep copy data for thread safety
        import copy
        history_copy = copy.deepcopy(self._bridge.history)
        dispatch_records_copy = list(self._bridge.dispatch_records)
        workspace_root = self._workspace_root
        model = self.current_model()
        thinking = self.current_thinking()
        worker_model = self.current_worker_model()
        worker_thinking = self.current_worker_thinking()
        existing_path = self._current_conversation_path
        pwm = self._bridge.planner_worker_mode
        provider = self._settings.provider

        def _run_save():
            try:
                path = save_conversation(
                    history=history_copy,
                    workspace_root=workspace_root,
                    model=model,
                    thinking=thinking,
                    existing_path=existing_path,
                    planner_worker_mode=pwm,
                    planner_model=model,
                    worker_model=worker_model,
                    planner_thinking=thinking,
                    worker_thinking=worker_thinking,
                    worker_dispatches=dispatch_records_copy,
                    provider=provider,
                )
                # Update the current path pointer on the GUI thread
                self._save_succeeded.emit(path)
            except OSError as exc:
                self._save_failed.emit(str(exc))

        import threading
        threading.Thread(target=_run_save, daemon=True).start()

    @Slot(Path)
    def _set_current_conv_path(self, path: Path) -> None:
        self._current_conversation_path = path

    def _maybe_restore_last_conversation(self) -> None:
        if self._workspace_root is None:
            return
        path = most_recent_conversation(self._workspace_root)
        if path is None:
            return
        try:
            loaded = load_conversation(path)
        except Exception:
            return
        self._apply_loaded_conversation(loaded)

    def _apply_loaded_conversation(self, loaded: LoadedConversation) -> None:
        pwm = loaded.planner_worker_mode
        default_prompt = PLANNER_SYSTEM_PROMPT if pwm else SINGLE_SYSTEM_PROMPT
        self._bridge.history.system_prompt = (
            loaded.history.system_prompt or default_prompt
        )
        self._bridge.history.messages = list(loaded.history.messages)
        self._current_conversation_path = loaded.path
        self._reset_session_usage()

        # Propagate custom prompts to bridge for future mode switches
        self._bridge.set_custom_system_prompts(
            self._settings.system_prompt,
            self._settings.planner_system_prompt,
            self._settings.worker_system_prompt,
        )
        self._bridge.set_temperature(self._settings.temperature)
        self._bridge.set_worker_temperature(self._settings.worker_temperature)

        # If the loaded conversation has a different provider, update the bridge.
        if loaded.provider != self._settings.provider:
            self._settings.provider = loaded.provider
            self._bridge.set_provider(loaded.provider)
            self._left_pane.populate_models(loaded.provider)

        # Sync mode (without overwriting the system prompt we just set).
        self._bridge.set_planner_worker_mode(pwm)
        if pwm:
            self.set_model(loaded.planner_model)
            self.set_thinking(loaded.planner_thinking)
            self.set_worker_model(loaded.worker_model)
            self.set_worker_thinking(loaded.worker_thinking)
            self._bridge.set_worker_model(loaded.worker_model)
            self._bridge.set_worker_thinking(loaded.worker_thinking)
        else:
            self.set_model(loaded.model)
            self.set_thinking(loaded.thinking)
        self._set_sidebar_planner_worker_mode(pwm)
        self._chat.reset()
        self._playground.clear()
        self._message_queue.clear()
        self._input.set_queued_messages(0)
        self._bridge.clear_pre_worker_snapshot()
        self._replay_history_into_view()
        self._refresh_status_bar()

    def _replay_history_into_view(self) -> None:
        """Best-effort visual replay of a loaded history.

        We intentionally don't try to recreate diff cards (the underlying
        before/after content isn't stored — only the resulting tool-message
        from the registry). Tool calls are surfaced as cards with their
        recorded args + result so the conversation reads coherently.
        """
        msgs = self._bridge.history.messages
        if not msgs:
            return

        # Cancel any in-flight replay
        if not hasattr(self, "_active_replay_id"):
            self._active_replay_id = 0
        self._active_replay_id += 1
        my_id = self._active_replay_id

        # Index tool results by tool_call_id for inline pairing.
        tool_results: dict[str, str] = {}
        for m in msgs:
            if m.get("role") == "tool":
                tcid = m.get("tool_call_id")
                if isinstance(tcid, str):
                    tool_results[tcid] = m.get("content", "")

        self._chat.begin_bulk_update()
        
        # Filter out tool messages as they are paired into assistant cards.
        process_msgs = [m for m in msgs if m.get("role") != "tool"]
        msg_iter = iter(process_msgs)

        def process_chunk():
            if self._active_replay_id != my_id:
                return

            chunk_size = 10
            try:
                for _ in range(chunk_size):
                    m = next(msg_iter)
                    role = m.get("role")
                    if role == "user":
                        content = m.get("content")
                        if isinstance(content, str):
                            self._chat.add_user(content)
                        elif isinstance(content, list):
                            text_parts = [
                                p.get("text", "")
                                for p in content
                                if isinstance(p, dict) and p.get("type") == "text"
                            ]
                            self._chat.add_user("\n".join(text_parts))
                    elif role == "assistant":
                        self._chat.begin_assistant()
                        rc = m.get("reasoning_content")
                        if rc:
                            self._chat.append_reasoning(rc)
                        content = m.get("content")
                        if isinstance(content, str) and content:
                            self._chat.append_content(content)
                        for tc in m.get("tool_calls") or []:
                            tcid = tc.get("id", "")
                            fn = tc.get("function", {})
                            name = fn.get("name", "")
                            args_str = fn.get("arguments", "")
                            
                            self._chat.add_tool_call(tcid, name)
                            if args_str:
                                self._chat.append_tool_args(tcid, args_str)
                            
                            if tcid in tool_results:
                                ok = True
                                try:
                                    parsed = json.loads(tool_results[tcid])
                                    if isinstance(parsed, dict) and parsed.get("ok") is False:
                                        ok = False
                                except json.JSONDecodeError:
                                    pass
                                self._chat.set_tool_result(tcid, ok, tool_results[tcid])
                        self._chat.assistant_done()
                
                # Schedule next chunk
                QTimer.singleShot(0, process_chunk)
            except StopIteration:
                if self._active_replay_id == my_id:
                    self._chat.end_bulk_update()

        process_chunk()

