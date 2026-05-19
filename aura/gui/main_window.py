"""Main application window: three-pane splitter, toolbar, chat + input."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer
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
from aura.config import (
    APP_NAME,
    PROVIDERS,
    AppSettings,
    ThinkingMode,
    icon_path,
    load_settings,
    load_workspace_root,
    save_settings,
    save_workspace_root,
)
from aura.git_ops import git_init, is_git_repo
from aura.gui.edge_rails import EdgeTabRail, TerminalTabState
from aura.gui.playground import AuraPlayground
from aura.gui.widgets.aura_glow import AuraWidget
from aura.gui.chat_view import ChatView
from aura.gui.checkpoint_dialog import CheckpointDialog
from aura.gui.conv_persistence import ConversationPersistence
from aura.gui.input_panel import InputPanel, SendPayload
from aura.gui.left_pane import LeftPane
from aura.gui.main_window_toolbar import MainWindowToolbar
from aura.gui.onboarding_dialog import OnboardingDialog
from aura.gui.send_handler import SendHandler
from aura.gui.settings_dialog import SettingsDialog
from aura.gui.status_bar import AuraStatusBar
from aura.gui.update_dialog import UpdateDialog, UpdateWorker
from aura.gui.window_chrome import WindowChromeMixin
from aura.gui.worker_handler import WorkerEventHandler
from aura.prompts import SINGLE_SYSTEM_PROMPT
from aura.updater import UpdateStatus


class MainWindow(WindowChromeMixin, QMainWindow):

    def __init__(self) -> None:
        super().__init__()
        self._checkpoint_dialog: CheckpointDialog | None = None
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(icon_path())))
        self.resize(1500, 920)

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
        self._bridge.set_planner_provider(self._settings.planner_provider)
        self._bridge.set_worker_provider(self._settings.worker_provider)
        self._bridge.set_workspace_root(self._workspace_root)
        self._apply_planner_worker_mode_to_bridge(self._settings.planner_worker_mode)
        self._bridge.set_show_planner_reasoning(self._settings.show_planner_reasoning)
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

        # ----- toolbar ----
        self._toolbar = MainWindowToolbar(self._settings, self)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._toolbar)
        self._toolbar.new_conversation_requested.connect(self._on_new_conversation)
        self._toolbar.open_conversation_requested.connect(self._on_open_conversation)
        self._toolbar.read_only_toggled.connect(self._on_read_only_toggled)
        self._toolbar.auto_dispatch_toggled.connect(self._on_auto_dispatch_toggled)
        self._toolbar.auto_approve_toggled.connect(self._on_auto_approve_toggled)
        self._toolbar.update_requested.connect(self._on_open_update)
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
        self._left_pane.populate_models(self._settings.planner_provider, self._settings.worker_provider)
        self._left_pane.change_root_requested.connect(self._on_change_root)
        self._left_pane.planner_model_changed.connect(lambda: self._refresh_status_bar())
        self._left_pane.planner_thinking_changed.connect(lambda: self._refresh_status_bar())
        self._left_pane.worker_model_changed.connect(self._on_sidebar_worker_model_changed)
        self._left_pane.worker_thinking_changed.connect(self._on_sidebar_worker_thinking_changed)
        self._tree = self._left_pane.tree()
        splitter.addWidget(self._left_pane)

        # Middle pane: chat + input
        center = QWidget(self)
        center.setMinimumWidth(360)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(20, 0, 20, 16)
        center_layout.setSpacing(0)

        self._chat = ChatView()
        self._chat.setParent(self)
        if self._settings.planner_worker_mode:
            self._chat.set_compact_tools(True)
        center_layout.addWidget(self._chat, 1)

        self._input = InputPanel(self._workspace_root, parent=self)

        # Send handler — owns message queue, vision routing, undo logic.
        self._send_handler = SendHandler(
            bridge=self._bridge,
            chat=self._chat,
            input_panel=self._input,
            settings=self._settings,
            workspace_root=self._workspace_root,
            parent=self,
        )

        # Right pane: worker activity (embedded, not a separate window)
        self._playground = AuraPlayground(
            parent=self,
            terminal_window_geometry=self._settings.terminal_window_geometry,
        )
        self._playground.set_workspace_root(self._workspace_root)
        self._playground.set_read_only_mode(False)
        self._playground_aura = AuraWidget(
            self._playground, glow_color="#00e5ff", glow_spread=24, parent=self
        )
        self._playground.set_aura_wrapper(self._playground_aura)

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
        splitter.addWidget(self._playground_aura)

        # Sensible initial distribution: left is narrow, chat is comfortable,
        # and the workspace opens as the primary work surface.
        w = self.width()
        left_w = 220
        center_w = 520
        right_w = max(560, w - left_w - center_w)
        splitter.setSizes([left_w, center_w, right_w])

        # Keep the sidebar stable and let the workspace receive most extra room.
        splitter.setStretchFactor(0, 0)  # workspace tree: fixed
        splitter.setStretchFactor(1, 1)  # chat: stable reading/planning column
        splitter.setStretchFactor(2, 2)  # workspace: primary work surface

        self.setCentralWidget(splitter)

        # Make the central widget and splitter transparent so the gradient shows through
        splitter.setStyleSheet("background: transparent;")
        self.centralWidget().setStyleSheet("background: transparent;")
        self.centralWidget().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

        # Edge tab rail — terminal + checkpoint tabs
        self._edge_rail = EdgeTabRail(self)
        self._terminal_tab = self._edge_rail.terminal_tab
        self._terminal_container = self._edge_rail.terminal_container
        self._corner_widget = self._edge_rail.corner_widget
        self._edge_rail.terminalTabToggled.connect(self._on_terminal_toggle)
        # Wire checkpoint tab click to the existing handler on MainWindow.
        checkpoint_tab = self._edge_rail.checkpoint_tab
        if checkpoint_tab is not None:
            checkpoint_tab.clicked.connect(lambda: self._on_open_checkpoints())

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
        self._chat.retry_requested.connect(self._on_retry)

        self._input.sent.connect(lambda p: self._send_handler.handle_send(p, self.current_model(), self.current_thinking()))
        self._input.stop_requested.connect(self._send_handler.handle_stop)
        self._input.retry_requested.connect(self._on_retry)
        self._tree.file_activated.connect(self._playground.open_file)
        self._playground.focused_action_requested.connect(self._on_focused_action_requested)
        terminal_window = self._playground.terminal_window()
        terminal_window.terminal_started.connect(self._on_terminal_started)
        terminal_window.terminal_finished.connect(self._on_terminal_finished)
        terminal_window.visibility_changed.connect(self._on_terminal_visibility_changed)
        terminal_window.terminal_cleared.connect(self._on_terminal_cleared)
        terminal_window.geometry_saved.connect(self._on_terminal_geometry_saved)

        # Worker signal wiring (delegated to WorkerEventHandler).
        self._worker_handler.connect_bridge_signals()

        # Mermaid diagram detection from chat → playground
        self._chat.mermaid_detected.connect(self._playground.add_mermaid_artifact)

        self._update_workspace_label()
        self._refresh_status_bar()
        self._position_edge_tabs()

        # Restore most recent conversation if enabled.
        if self._settings.restore_last_conversation:
            # Defer restoration so the UI paints and becomes interactive first.
            QTimer.singleShot(100, lambda: self._persistence.restore_last(self._workspace_root))

        # Check for updates in the background.
        QTimer.singleShot(2000, self._check_for_updates)

    def _check_for_updates(self) -> None:
        """Run a background update check."""
        self._update_worker = UpdateWorker("check")
        self._update_thread = QThread(self)
        self._update_worker.moveToThread(self._update_thread)
        self._update_thread.started.connect(self._update_worker.run)
        self._update_worker.finished.connect(self._on_background_update_finished)
        self._update_worker.finished.connect(self._update_thread.quit)
        self._update_worker.finished.connect(self._update_worker.deleteLater)
        self._update_thread.finished.connect(self._update_thread.deleteLater)
        self._update_thread.start()

    def _on_background_update_finished(self, status: UpdateStatus) -> None:
        if status.state == "behind":
            # Visually mark the Update button
            self._toolbar.set_update_available(True)

    def showEvent(self, event) -> None:
        """Triggered when the window is shown. Used for first-launch onboarding."""
        super().showEvent(event)
        self._position_edge_tabs()
        if not self._settings.first_launch_done:
            # We use a 0ms timer to ensure the event loop processes the window
            # show COMPLETELY before popping the modal dialog.
            QTimer.singleShot(0, self._show_onboarding)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_edge_tabs()

    def _position_edge_tabs(self) -> None:
        rail = self._edge_rail
        if rail is None:
            return

        rail_w = rail.width()
        rail_h = rail.sizeHint().height()
        margin_bottom = self.statusBar().height() + 28
        x = self.width() - rail_w
        y = max(0, self.height() - rail_h - margin_bottom)
        rail.setFixedHeight(rail_h)
        rail.move(x, y)
        rail.raise_()

    def _on_terminal_toggle(self, checked: bool) -> None:
        self._playground.toggle_terminal_window()
        self._sync_terminal_checked_state()
        self._position_edge_tabs()

    def _on_terminal_started(self) -> None:
        self._edge_rail.set_state("running")

    def _on_terminal_finished(self, exit_code: int) -> None:
        if exit_code == 0:
            self._edge_rail.set_state("success")
            QTimer.singleShot(1200, self._dim_terminal_tab_after_success)
        else:
            self._edge_rail.set_state("failure")

    def _on_terminal_visibility_changed(self, _visible: bool) -> None:
        self._sync_terminal_checked_state()
        self._edge_rail.set_is_terminal_open(self._playground.is_terminal_window_open())
        self._edge_rail.set_state(self._edge_rail.state)
        self._position_edge_tabs()

    def _on_terminal_cleared(self) -> None:
        self._edge_rail.set_state("dim")

    def _on_terminal_geometry_saved(self, geometry: str) -> None:
        if self._settings.terminal_window_geometry == geometry:
            return
        self._settings.terminal_window_geometry = geometry
        save_settings(self._settings)

    def _dim_terminal_tab_after_success(self) -> None:
        if self._edge_rail.state == "success":
            self._edge_rail.set_state("dim")

    def _sync_terminal_checked_state(self) -> None:
        tab = self._edge_rail.terminal_tab
        if tab is None:
            return
        is_open = self._playground.is_terminal_window_open()
        tab.setChecked(is_open)
        self._edge_rail.set_is_terminal_open(is_open)

    def _show_onboarding(self) -> None:
        dlg = OnboardingDialog(
            self,
            workspace_path=str(self._workspace_root) if self._workspace_root else "",
            on_change_workspace=self._onboarding_change_workspace,
        )
        result = dlg.exec()
        if dlg.open_settings_requested:
            self._settings.first_launch_done = True
            from aura.config import save_settings
            save_settings(self._settings)
            self._on_open_settings()
            return
        if result == QDialog.DialogCode.Accepted:
            self._settings.first_launch_done = True
            from aura.config import save_settings
            save_settings(self._settings)
            if dlg.selected_mission_text:
                self._input.set_text(dlg.selected_mission_text)

    def _onboarding_change_workspace(self) -> str | None:
        """Called from onboarding dialog to change workspace. Returns new path or None."""
        start = str(self._workspace_root) if self._workspace_root else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose workspace root", start)
        if not chosen:
            return None
        path = Path(chosen)
        self._workspace_root = path
        self._bridge.set_workspace_root(path)
        self._input.set_workspace_root(path)
        self._send_handler.set_workspace_root(path)
        self._playground.set_workspace_root(path)
        self._tree.set_root(path)
        save_workspace_root(path)
        self._update_workspace_label()
        self._refresh_status_bar()
        return str(path)

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
        self._checkpoint_dialog = None
        self._bridge.set_workspace_root(path)
        self._input.set_workspace_root(path)
        self._send_handler.set_workspace_root(path)
        self._playground.set_workspace_root(path)
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
        self._playground.set_read_only_mode(checked)

    def _on_focused_action_requested(self, prompt: str) -> None:
        payload = SendPayload(text=prompt, attachments=[])
        self._send_handler.handle_send(payload, self.current_model(), self.current_thinking())

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
        self._send_handler.clear_queue()
        self._input.set_text("")
        self._input.set_attachments([])
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
            self._send_handler.clear_queue()
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
            self._settings = dlg.result_settings()
            self._send_handler.update_settings(self._settings)
            self._persistence.update_settings(self._settings)
            self._worker_handler.update_settings(self._settings)
            self._toolbar.update_settings(self._settings)
            
            # Always refresh combos to pick up dynamically fetched models
            self._left_pane.populate_models(
                self._settings.planner_provider,
                self._settings.worker_provider,
            )
            self._bridge.set_planner_provider(self._settings.planner_provider)
            self._bridge.set_worker_provider(self._settings.worker_provider)

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
            self._bridge.set_show_planner_reasoning(self._settings.show_planner_reasoning)
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

    def _on_open_update(self) -> None:
        dlg = UpdateDialog(self)
        dlg.exec()

    def _on_open_checkpoints(self) -> None:
        if self._workspace_root is None or not self._workspace_root.exists():
            QMessageBox.information(
                self,
                "Checkpoints",
                "Choose a workspace before opening checkpoint history.",
            )
            return

        if not is_git_repo(self._workspace_root):
            QMessageBox.information(
                self,
                "Checkpoints",
                "This workspace is not a git repository yet.\n\n"
                "Aura checkpoints are based on git commits.",
            )
            return

        if (
            self._checkpoint_dialog is None
            or self._checkpoint_dialog.workspace_root() != self._workspace_root
        ):
            self._checkpoint_dialog = CheckpointDialog(self._workspace_root, self)
            self._checkpoint_dialog.setModal(False)
            self._checkpoint_dialog.setWindowModality(Qt.WindowModality.NonModal)
            self._checkpoint_dialog.setWindowFlag(Qt.WindowType.Tool, True)

        if self._checkpoint_dialog.isVisible():
            self._checkpoint_dialog.hide()
            return

        self._checkpoint_dialog.refresh()
        self._checkpoint_dialog.show()
        self._checkpoint_dialog.raise_()
        self._checkpoint_dialog.activateWindow()
        self._tree.set_root(self._workspace_root)

    def _apply_planner_worker_mode_to_bridge(self, enabled: bool) -> None:
        # HACK: Force planner/worker mode to always be enabled. The user is
        # complaining about not seeing the rich playground UI, which is only
        # active in this mode. This ensures all agent activity is routed
        # through the AuraPlayground for the expected rich feedback.
        effective_enabled = True

        self._bridge.set_planner_worker_mode(effective_enabled)
        if effective_enabled:
            prompt = self._settings.planner_system_prompt or PLANNER_SYSTEM_PROMPT
        else:
            # This branch is now effectively dead code.
            prompt = self._settings.system_prompt or SINGLE_SYSTEM_PROMPT
        self._bridge.set_system_prompt(prompt)
        if hasattr(self, "_chat"):
            self._chat.set_compact_tools(effective_enabled)

    def _on_started(self) -> None:
        self._input.set_streaming(True)

    def _on_finished(self) -> None:
        self._input.set_streaming(False)
        self._chat.assistant_done()
        self._chat.stop_current_aura()
        self._input.focus_editor()
        self._send_handler.process_message_queue(self.current_model(), self.current_thinking())

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
            planner_provider=self._settings.planner_provider,
            worker_provider=self._settings.worker_provider,
        )

    def _on_tool_result(self, tool_id: str, name: str, ok: bool, result: str, extras: dict) -> None:
        self._chat.set_tool_result(tool_id, ok, result)

        # Terminal dispatches don't trigger _on_stream_done, so we must auto-save here
        # to ensure the worker's result is persisted before the app is closed.
        if name in ("dispatch_to_worker", "run_research"):
            self._persistence.auto_save(
                workspace_root=self._workspace_root,
                model=self.current_model(),
                thinking=self.current_thinking(),
                worker_model=self.current_worker_model(),
                worker_thinking=self.current_worker_thinking(),
                provider=self._settings.provider,
                planner_provider=self._settings.planner_provider,
                worker_provider=self._settings.worker_provider,
            )

    def _on_diff_decided(
        self,
        tool_call_id: str,
        decision: str,
        rel_path: str,
        old: str,
        new: str,
        is_new_file: bool,
    ) -> None:
        self._chat.show_code_diff(tool_call_id, rel_path, old, new, decision)
        self._chat.add_diff_card(tool_call_id, rel_path, old, new, decision, is_new_file)

    def _on_api_error(self, status: int, message: str) -> None:
        title = f"API Error {status}" if status > 0 else "Error"
        self._chat.add_error(title, message, show_retry=True)
        self._chat.stop_current_aura()

    def _on_retry(self) -> None:
        self._send_handler.handle_retry_last(
            self.current_model(),
            self.current_thinking(),
            replay_cb=lambda: self._persistence.replay_history(synchronous=True),
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
