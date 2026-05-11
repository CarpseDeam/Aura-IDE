"""Main application window: three-pane splitter, toolbar, chat + input."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QSplitter,
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
from aura.gui.conv_persistence import ConversationPersistence
from aura.git_ops import git_init, is_git_repo
from aura.gui.chat_view import ChatView
from aura.gui.input_panel import InputPanel, SendPayload
from aura.gui.settings_dialog import SettingsDialog
from aura.gui.onboarding_dialog import OnboardingDialog
from aura.gui.status_bar import AuraStatusBar
from aura.gui.left_pane import LeftPane
from aura.gui.main_window_toolbar import MainWindowToolbar
from aura.gui.aura_widget import AuraPlayground
from aura.gui.worker_handler import WorkerEventHandler
from aura.gui.window_chrome import WindowChromeMixin

_THINKING_LABEL = {"off": "Off", "high": "High", "max": "Max"}


class MainWindow(WindowChromeMixin, QMainWindow):
    # Thread-safe signals for cross-thread communication.
    _vision_done = Signal(object, list, object)   # SendPayload, list[str], str|None

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(icon_path())))
        self.resize(1400, 900)

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

        # Right pane: worker activity (embedded, not a separate window)
        self._playground = AuraPlayground(parent=self)

        # Worker event handler — owns session usage, forwards bridge signals
        # to chat / playground UI components.
        self._worker_handler = WorkerEventHandler(
            bridge=self._bridge,
            chat=self._chat,
            playground=self._playground,
            settings=self._settings,
            parent=self,
        )
        self._worker_handler.usage_updated.connect(self._refresh_status_bar)
        self._worker_handler.worker_started.connect(lambda: self._input.set_streaming(False))

        # Conversation persistence (auto-save, load, restore, replay).
        self._persistence = ConversationPersistence(
            bridge=self._bridge,
            chat=self._chat,
            playground=self._playground,
            input_panel=self._input,
            left_pane=self._left_pane,
            settings=self._settings,
            parent=self,
        )
        self._persistence.needs_status_refresh.connect(self._refresh_status_bar)

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

        # Worker signal wiring (delegated to WorkerEventHandler).
        self._worker_handler.connect_bridge_signals()

        # Mermaid diagram detection from chat → playground
        self._chat.mermaid_detected.connect(self._playground.add_mermaid_artifact)

        self._vision_done.connect(self._on_vision_done)

        self._update_workspace_label()
        self._refresh_status_bar()

        # Restore most recent conversation if enabled.
        if self._settings.restore_last_conversation:
            self._persistence.restore_last(self._workspace_root)

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
            session_usage=self._worker_handler.session_usage
        )

    def _reset_session_usage(self) -> None:
        self._worker_handler.reset_session_usage()

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
        self._persistence._current_conversation_path = None
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
        self._persistence.new_conversation()
        self._message_queue.clear()
        self._input.set_queued_messages(0)
        self._reset_session_usage()

    def _on_open_conversation(self) -> None:
        if self._bridge.is_running():
            QMessageBox.information(
                self, APP_NAME, "Wait for the current response to finish, or click Stop."
            )
            return
        loaded = self._persistence.open_conversation(self._workspace_root, self)
        if loaded is not None:
            self._message_queue.clear()
            self._input.set_queued_messages(0)
            self._reset_session_usage()

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
                tc.get("function", {}).get("name") in ("dispatch_to_worker", "run_research")
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
        self._persistence.auto_save(
            workspace_root=self._workspace_root,
            model=self.current_model(),
            thinking=self.current_thinking(),
            worker_model=self.current_worker_model(),
            worker_thinking=self.current_worker_thinking(),
            provider=self._settings.provider,
        )

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
        bucket = self._worker_handler.session_usage.setdefault(
            model_id, {"hit": 0, "miss": 0, "out": 0}
        )
        bucket["hit"] += hit
        bucket["miss"] += miss
        bucket["out"] += completion
        self._refresh_status_bar()

# ----- persistence (delegated to ConversationPersistence) --------------
