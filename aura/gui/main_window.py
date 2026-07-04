"""Main application window: three-pane splitter, toolbar, chat + input."""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

from PySide6.QtCore import QByteArray, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QDialog,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
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
    get_api_key,
    has_usable_provider_configuration,
    icon_path,
    load_settings,
    load_workspace_root,
    save_settings,
)
from aura.git_ops import is_git_repo
from aura.gui._screen import clamp_to_screen
from aura.gui.chat_view import ChatView
from aura.gui.checkpoint_dialog import CheckpointDialog
from aura.gui.conv_persistence import ConversationPersistence
from aura.gui.debug_report_handler import DebugReportHandler
from aura.gui.drones.drone_reports_window import DroneReportsWindow
from aura.gui.edge_rails import EdgeTabRail
from aura.gui.input_panel import InputPanel, SendPayload
from aura.gui.gui_event_probe import install_gui_event_probe
from aura.gui.left_pane import LeftPane
from aura.gui.main_window_balance import MainWindowBalanceController
from aura.gui.main_window_companion import MainWindowCompanionController
from aura.gui.main_window_drones import MainWindowDroneController
from aura.gui.main_window_handoff import MainWindowHandoffController
from aura.gui.main_window_settings import MainWindowSettingsController
from aura.gui.main_window_terminal import MainWindowTerminalController
from aura.gui.main_window_toolbar import MainWindowToolbar
from aura.gui.main_window_update import MainWindowUpdateController
from aura.gui.main_window_workspace import MainWindowWorkspaceController
from aura.gui.onboarding_dialog import OnboardingDialog
from aura.gui.playground import AuraPlayground
from aura.gui.send_handler import SendHandler
from aura.gui.silent_research_guard import SilentResearchUiGuard
from aura.gui.status_bar import AuraStatusBar
from aura.gui.update_dialog import UpdateDialog
from aura.gui.widgets.aura_glow import AuraWidget
from aura.gui.window_chrome import WindowChromeMixin
from aura.gui.worker_handler import WorkerEventHandler
from aura.prompts import SINGLE_SYSTEM_PROMPT


class _ShrinkableStack(QStackedWidget):
    """QStackedWidget that only considers the current (visible) page for
    minimumSizeHint and sizeHint. Prevents hidden pages from forcing the
    stack wider than the active content.
    """
    def minimumSizeHint(self):
        w = self.currentWidget()
        if w is not None:
            return w.minimumSizeHint()
        return super().minimumSizeHint()
    def sizeHint(self):
        w = self.currentWidget()
        if w is not None:
            return w.sizeHint()
        return super().sizeHint()


class MainWindow(WindowChromeMixin, QMainWindow):
    droneRunFinishedOnUiThread = Signal(str)
    droneStatusChangedOnUiThread = Signal(str, str, str)  # run_id, drone_name, status
    droneReceiptReadyOnUiThread = Signal(object, str)

    def __init__(self) -> None:
        super().__init__()
        self._checkpoint_dialog: CheckpointDialog | None = None
        self._answer_only_ui_guard: SilentResearchUiGuard | None = None
        self._use_native_chrome = os.environ.get("AURA_NATIVE_CHROME") == "1"
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(icon_path())))
        clamp_to_screen(self, 1500, 920)

        # Settings.
        self._settings: AppSettings = load_settings()

        # Workspace.
        self._workspace_root: Path | None = load_workspace_root()

        # Bridge — provider-aware.
        self._bridge = ConversationBridge(
            parent_widget=self,
            provider=self._settings.provider,
        )
        self._bridge.set_planner_provider(self._settings.planner_provider)
        self._bridge.set_worker_provider(self._settings.worker_provider)
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
        self._bridge.set_auto_dispatch(self._settings.auto_dispatch)
        self._bridge.set_auto_approve(self._settings.auto_approve)

        # ----- toolbar ----
        self._toolbar = MainWindowToolbar(self._settings, self)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._toolbar)
        self._settings_controller = MainWindowSettingsController(self)
        self._debug_report_handler = DebugReportHandler(window=self, parent=self)
        self._toolbar.new_conversation_requested.connect(self._on_new_conversation)
        self._toolbar.open_conversation_requested.connect(self._on_open_conversation)
        self._toolbar.read_only_toggled.connect(self._on_read_only_toggled)
        self._toolbar.auto_dispatch_toggled.connect(self._settings_controller.on_auto_dispatch_toggled)
        self._toolbar.auto_approve_toggled.connect(self._settings_controller.on_auto_approve_toggled)
        self._toolbar.auto_summon_drones_toggled.connect(self._settings_controller.on_auto_summon_drones_toggled)
        self._toolbar.update_requested.connect(self._on_open_update)
        self._toolbar.settings_requested.connect(self._settings_controller.open_settings)
        self._toolbar.logs_requested.connect(self._open_logs_folder)
        self._toolbar.debug_report_requested.connect(self._debug_report_handler.on_send_debug_report)
        self._toolbar.minimize_requested.connect(self.showMinimized)
        self._toolbar.maximize_requested.connect(self._toggle_maximize)
        self._toolbar.close_requested.connect(self.close)

        self._update_controller = MainWindowUpdateController(self._toolbar, parent=self)

        # ----- status bar -----
        self._status_bar = AuraStatusBar(
            self,
            show_resize_grip=not self._use_native_chrome,
        )
        self.setStatusBar(self._status_bar)

        self._balance_controller = MainWindowBalanceController(self)
        self._balance_controller.balance_changed.connect(self._refresh_status_bar)
        self._status_bar.credits_chip_clicked.connect(self._settings_controller.open_credits_popout)

        self._drone_controller = MainWindowDroneController(self)
        self._terminal_controller = MainWindowTerminalController(self)

        # ----- splitter ----
        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setHandleWidth(3)

        # Left pane: workspace label + change root + tree + model config.
        self._left_pane = LeftPane(self._workspace_root, parent=self)
        self._left_pane.populate_models(self._settings.planner_provider, self._settings.worker_provider)
        self._workspace_controller = MainWindowWorkspaceController(self)
        self._left_pane.change_root_requested.connect(self._workspace_controller.on_change_root)
        self._left_pane.project_selected.connect(self._workspace_controller._on_project_selected)
        self._left_pane.new_project_requested.connect(self._workspace_controller.on_create_new_project)
        self._left_pane.planner_model_changed.connect(lambda: self._refresh_status_bar())
        self._left_pane.planner_thinking_changed.connect(lambda: self._refresh_status_bar())
        self._left_pane.worker_model_changed.connect(self._on_sidebar_worker_model_changed)
        self._left_pane.worker_thinking_changed.connect(self._on_sidebar_worker_thinking_changed)
        self._left_pane.drone_selected.connect(lambda folder: self._drone_controller.on_drone_folder_selected(folder.name))
        self._left_pane.new_drone_requested.connect(self._drone_controller.on_create_drone)
        self._main_splitter.addWidget(self._left_pane)

        # Center column: stacked launchpad / workspace view
        self._center_stack = _ShrinkableStack(self)
        self._center_stack.setMinimumWidth(0)
        self._center_stack.setStyleSheet("background: transparent;")

        # Page 0: Project Launchpad (shown when no workspace)
        from aura.gui.project_launchpad import ProjectLaunchpad
        self._launchpad = ProjectLaunchpad(self)
        self._center_stack.addWidget(self._launchpad)

        # Wire launchpad signals
        self._launchpad.open_existing_requested.connect(
            self._workspace_controller.on_open_existing
        )
        self._launchpad.create_new_requested.connect(
            self._workspace_controller.on_create_new_project
        )
        self._launchpad.create_demo_requested.connect(
            self._workspace_controller.on_create_demo_project
        )

        # Page 1: Chat + Input (normal workspace view)
        center = QWidget()
        center.setMinimumWidth(280)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(20, 0, 20, 16)
        center_layout.setSpacing(0)

        self._chat = ChatView()
        self._chat.setParent(self)
        self._chat.droneRunFocusRequested.connect(self._drone_controller.on_focus_drone_run)
        if self._settings.planner_worker_mode:
            self._chat.set_compact_tools(True)
        center_layout.addWidget(self._chat, 1)

        self._center_stack.addWidget(center)

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
        self._send_handler.drone_bay_requested.connect(self._drone_controller.on_drone_bay_requested)

        # Companion (mobile control plane)
        self._companion_controller = MainWindowCompanionController(self)

        # Right pane: worker activity (embedded, not a separate window)
        self._playground = AuraPlayground(
            parent=self,
            terminal_window_geometry=self._settings.terminal_window_geometry,
        )
        self._playground_aura = AuraWidget(
            self._playground,
            glow_color="#00e5ff",
            glow_spread=24,
            parent=self,
        )
        self._playground.set_aura_wrapper(self._playground_aura)
        self._playground.set_workspace_root(self._workspace_root)
        self._playground.set_read_only_mode(False)


        # Floating Drone Reports window. Active run cards live here instead of
        # consuming space in the Worker/workspace area.
        self._drone_reports_window = DroneReportsWindow(
            self,
            initial_geometry=self._settings.drone_reports_window_geometry,
        )
        self._drone_reports_window.geometry_saved.connect(
            self._terminal_controller._on_drone_reports_geometry_saved
        )
        self._drone_reports_window.visibility_changed.connect(
            lambda _visible: self._drone_controller.sync_drone_tab_checked()
        )
        self._send_handler.answer_only_research_started.connect(
            self._prepare_answer_only_research_ui
        )

        self.droneRunFinishedOnUiThread.connect(self._drone_controller.on_drone_finished, Qt.ConnectionType.QueuedConnection)
        self.droneStatusChangedOnUiThread.connect(self._drone_controller.on_drone_status_changed)
        self.droneReceiptReadyOnUiThread.connect(self._drone_controller.on_drone_receipt)

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
        self._worker_handler.usage_updated.connect(lambda: self._balance_controller.refresh(self._settings))
        self._worker_handler.worker_started.connect(lambda: self._input.set_streaming(False))
        self._playground.stop_worker_requested.connect(self._bridge.request_cancel)
        self._worker_handler.worker_running_changed.connect(self._playground.set_worker_running)
        self._gui_event_probe = install_gui_event_probe(self, self._bridge)

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

        # Handoff flow controller
        self._handoff_controller = MainWindowHandoffController(
            bridge=self._bridge,
            send_handler=self._send_handler,
            chat=self._chat,
            input_panel=self._input,
            persistence=self._persistence,
            get_workspace_root=lambda: self._workspace_root,
            get_model=self.current_model,
            get_thinking=self.current_thinking,
            reset_session_usage=self._reset_session_usage,
            parent_widget=self,
            parent=self,
        )

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

        # Show appropriate initial page
        self._center_stack.setCurrentIndex(0 if self._workspace_root is None else 1)

        # Add to splitter (replacing previous center addWidget with stack)
        self._main_splitter.addWidget(self._center_stack)
        self._main_splitter.addWidget(self._playground_aura)

        # Sensible initial distribution: left is narrow, chat is comfortable,
        # and the workspace opens as the primary work surface.
        w = self.width()
        left_w = 220
        center_w = 520
        right_w = max(560, w - left_w - center_w)
        self._main_splitter.setSizes([left_w, center_w, right_w])

        # Override with saved splitter sizes if available.
        if self._settings.main_splitter_sizes:
            sizes = self._settings.main_splitter_sizes
            w = self.width()
            if len(sizes) == 3 and sum(sizes) > 0 and all(s >= 40 for s in sizes) and sum(sizes) <= 2 * w:
                self._main_splitter.setSizes(sizes)
            # else keep the defaults already set above

        # Keep the sidebar stable and let the workspace receive most extra room.
        self._main_splitter.setStretchFactor(0, 0)  # workspace tree: fixed
        self._main_splitter.setStretchFactor(1, 1)  # chat: stable reading/planning column
        self._main_splitter.setStretchFactor(2, 2)  # workspace: primary work surface
        self._main_splitter.setCollapsible(0, False)   # left pane: keep visible
        self._main_splitter.setCollapsible(1, True)    # center: allow collapse to 0
        self._main_splitter.setCollapsible(2, True)    # playground: allow collapse to 0

        self.setCentralWidget(self._main_splitter)

        # Make the central widget and splitter transparent so the gradient shows through
        self._main_splitter.setStyleSheet("background: transparent;")
        self.centralWidget().setStyleSheet("background: transparent;")
        self.centralWidget().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

        # Edge tab rail — terminal + checkpoint tabs
        self._edge_rail = EdgeTabRail(self)
        self._terminal_tab = self._edge_rail.terminal_tab
        self._terminal_container = self._edge_rail.terminal_container
        self._corner_widget = self._edge_rail.corner_widget
        self._edge_rail.terminalTabToggled.connect(self._terminal_controller._on_terminal_toggle)
        # Wire checkpoint tab click to the existing handler on MainWindow.
        checkpoint_tab = self._edge_rail.checkpoint_tab
        if checkpoint_tab is not None:
            checkpoint_tab.clicked.connect(lambda: self._on_open_checkpoints())
        self._edge_rail.droneBayRequested.connect(self._drone_controller.on_drone_bay_requested)
        self._edge_rail.droneRunFocusRequested.connect(self._drone_controller.on_focus_drone_run)
        self._drone_controller.sync_drone_tab_checked()
        self._edge_rail.companionRequested.connect(self._on_open_companion_popout)

        # Sync companion badge after rail exists (status may have fired before rail was created)
        self._companion_controller.sync_edge_rail_status()

        # Frameless window — no native title bar unless explicitly disabled.
        if not self._use_native_chrome:
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
        self._input.handoff_requested.connect(self._on_handoff_requested)

        self._tree = self._playground.file_tree()
        self._tree.file_activated.connect(self._playground.open_file)
        self._playground.focused_action_requested.connect(self._on_focused_action_requested)
        terminal_window = self._playground.terminal_window()
        terminal_window.terminal_started.connect(self._terminal_controller._on_terminal_started)
        terminal_window.terminal_finished.connect(self._terminal_controller._on_terminal_finished)
        terminal_window.visibility_changed.connect(self._terminal_controller._on_terminal_visibility_changed)
        terminal_window.terminal_cleared.connect(self._terminal_controller._on_terminal_cleared)
        terminal_window.geometry_saved.connect(self._terminal_controller._on_terminal_geometry_saved)

        # Worker signal wiring (delegated to WorkerEventHandler).
        self._worker_handler.connect_bridge_signals()

        # Mermaid diagram detection from chat → playground
        self._chat.mermaid_detected.connect(self._playground.add_mermaid_artifact)

        self._workspace_controller.update_workspace_label()

        # Wire project thread selection from left pane
        self._left_pane.thread_selected.connect(self._on_thread_selected)
        self._persistence.project_thread_updated.connect(self._on_project_thread_updated)
        self._persistence.current_context_changed.connect(self._on_current_context_changed)

        QTimer.singleShot(0, lambda: self._left_pane.refresh_projects(self._workspace_root))
        QTimer.singleShot(0, lambda: self._left_pane.refresh_drones(self._workspace_root))

        self._refresh_status_bar()
        self._balance_controller.refresh(self._settings)
        self._position_edge_tabs()

        logger.debug(
            "layout_diag win_min=(%d,%d) splitter_min=(%d,%d) left_min=(%d,%d) center_min=(%d,%d) playground_min=(%d,%d) chat_min=(%d,%d) input_min=(%d,%d)",
            self.minimumSizeHint().width(), self.minimumSizeHint().height(),
            self._main_splitter.minimumSizeHint().width(), self._main_splitter.minimumSizeHint().height(),
            self._left_pane.minimumSizeHint().width(), self._left_pane.minimumSizeHint().height(),
            self._center_stack.minimumSizeHint().width(), self._center_stack.minimumSizeHint().height(),
            self._playground_aura.minimumSizeHint().width(), self._playground_aura.minimumSizeHint().height(),
            self._chat.minimumSizeHint().width(), self._chat.minimumSizeHint().height(),
            self._input.minimumSizeHint().width(), self._input.minimumSizeHint().height(),
        )

        # Restore most recent conversation if enabled.
        if self._settings.restore_last_conversation:
            # Defer restoration so the UI paints and becomes interactive first.
            initial_root = self._workspace_root
            QTimer.singleShot(100, lambda: self._persistence.restore_last(initial_root))

        # Check for updates in the background.
        self._update_controller.schedule_background_check(2000)

        # Restore saved window geometry/state after construction.
        if self._settings.main_window_geometry:
            QTimer.singleShot(0, self._restore_layout)

    def _restore_layout(self) -> None:
        if self._settings.main_window_geometry:
            geo = QByteArray.fromBase64(self._settings.main_window_geometry.encode("ascii"))
            self.restoreGeometry(geo)
        if self._settings.main_window_state:
            state = QByteArray.fromBase64(self._settings.main_window_state.encode("ascii"))
            self.restoreState(state)
        if self._settings.main_splitter_sizes:
            sizes = self._settings.main_splitter_sizes
            w = self.width()
            if not (len(sizes) == 3 and sum(sizes) > 0 and all(s >= 40 for s in sizes) and sum(sizes) <= 2 * w):
                left_w = max(180, int(w * 0.16))
                center_w = max(320, int(w * 0.40))
                right_w = max(320, int(w * 0.44))
                self._main_splitter.setSizes([left_w, center_w, right_w])

    def closeEvent(self, event) -> None:
        # Save window geometry/state.
        geo = self.saveGeometry()
        self._settings.main_window_geometry = bytes(geo.toBase64()).decode("ascii")
        state = self.saveState()
        self._settings.main_window_state = bytes(state.toBase64()).decode("ascii")
        # Save splitter sizes.
        self._settings.main_splitter_sizes = list(self._main_splitter.sizes())
        save_settings(self._settings)
        self._companion_controller.stop()
        self._balance_controller.shutdown()
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        """Triggered when the window is shown."""
        super().showEvent(event)
        self._position_edge_tabs()
        # Mark first launch done so onboarding never shows on subsequent starts.
        QTimer.singleShot(0, self._mark_first_launch_done)

    def _mark_first_launch_done(self) -> None:
        self._settings.first_launch_done = True
        save_settings(self._settings)

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

    def _show_onboarding(self) -> None:
        dlg = OnboardingDialog(
            self,
            workspace_path=str(self._workspace_root) if self._workspace_root else "",
            on_change_workspace=self._workspace_controller.onboarding_change_workspace,
        )
        result = dlg.exec()
        if dlg.open_settings_requested:
            self._settings.first_launch_done = True
            from aura.config import save_settings
            save_settings(self._settings)
            self._settings_controller.open_settings()
            return
        if result == QDialog.DialogCode.Accepted:
            self._settings.first_launch_done = True
            from aura.config import save_settings
            save_settings(self._settings)
            if dlg.selected_mission_text:
                self._input.set_text(dlg.selected_mission_text)

        # After dialog closes, show launchpad or workspace view
        self._update_center_view()

    def _switch_to_workspace_view(self) -> None:
        """Switch to the chat+input workspace view."""
        self._center_stack.setCurrentIndex(1)

    def _show_launchpad(self) -> None:
        """Show the project launchpad."""
        self._center_stack.setCurrentIndex(0)

    def _update_center_view(self) -> None:
        """Toggle between launchpad and workspace view based on workspace_root state."""
        if self._workspace_root is None:
            self._show_launchpad()
        else:
            self._switch_to_workspace_view()

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
        has_aura_key = bool(get_api_key("aura"))
        has_provider = has_usable_provider_configuration(self._settings.provider)
        self._status_bar.refresh(
            workspace_root=ws,
            model_id=self.current_model(),
            thinking=self.current_thinking(),
            session_usage=self._worker_handler.session_usage,
            has_aura_key=has_aura_key,
            balance_micros=self._balance_controller.balance_micros,
            has_provider=has_provider,
        )

    def _reset_session_usage(self) -> None:
        self._worker_handler.reset_session_usage()

    # ----- handlers -------------------------------------------------------








    def _on_read_only_toggled(self, checked: bool) -> None:
        self._bridge.set_read_only(checked)
        self._toolbar.set_read_only(checked)
        self._playground.set_read_only_mode(checked)

    def _on_focused_action_requested(self, prompt: str) -> None:
        payload = SendPayload(text=prompt, attachments=[])
        self._send_handler.handle_send(payload, self.current_model(), self.current_thinking())

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
        self._companion_controller.set_current_conversation("")

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

    def open_api_settings(self) -> None:
        """Open settings dialog directly to the API Keys tab."""
        self._settings_controller.open_api_settings()

    def open_aura_settings(self) -> None:
        """Open the standalone Aura Credits popout."""
        self._settings_controller.open_aura_settings()


    def _on_open_update(self) -> None:
        dlg = UpdateDialog(self)
        dlg.exec()

    def _open_logs_folder(self) -> None:
        from aura.startup_logging import logs_dir

        path = logs_dir()
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        logger.info("open_logs_folder path=%s", path)

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

    def _on_open_companion_popout(self) -> None:
        from aura.gui.companion_popout import CompanionPopoutDialog
        dlg = CompanionPopoutDialog(
            settings=self._settings,
            manager=self._companion_controller.companion_manager,
            on_apply=self._settings_controller._apply_settings,
            parent=self,
        )
        dlg.exec()

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
        # Switch from Drone Bay to workspace so the user sees the run —
        # but do NOT switch away from the Chain Editor (Workflow Studio).
        if not self._drone_controller.is_workbay_open():
            self._playground.switch_to_workspace()
        self._drone_controller.sync_drone_tab_checked()

    def _on_finished(self) -> None:
        if self._answer_only_ui_guard is not None:
            self._answer_only_ui_guard.stop()
            self._answer_only_ui_guard = None
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
                tc.get("function", {}).get("name") in ("dispatch_to_worker",)
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

        # Check for pending handoff after the response completes (no tool calls)
        if not tool_calls:
            self._handoff_controller.finalize_handoff(full_message)

    def _on_tool_result(self, tool_id: str, name: str, ok: bool, result: str, extras: dict) -> None:
        self._chat.set_tool_result(tool_id, ok, result)

        if ok and name == "summon_drone" and extras.get("summon_drone"):
            run_id = self._drone_controller.handle_summon_drone_result(tool_id, extras)
            if run_id:
                drone_name = str(
                    extras.get("drone_name")
                    or extras.get("drone_id")
                    or "Drone"
                )
                self._chat.add_drone_run_badge(run_id, drone_name)

        # Normal Drone Bay refresh for successful folder registrations.
        if ok and name == "register_drone_folder" and extras.get("drone_saved"):
            self._drone_controller.refresh_drone_context()
            if self._drone_controller.drone_workbay_window is not None and self._drone_controller.drone_workbay_window.isVisible():
                self._drone_controller.drone_workbay_window.chain_editor.refresh_roster()
        if ok and name in ("read_file", "read_files"):
            try:
                import json
                from pathlib import Path
                res_dict = json.loads(result)
                if isinstance(res_dict, dict):
                    if name == "read_file" and "path" in res_dict:
                        self._playground.open_file(Path(self._workspace_root) / res_dict["path"])
                    elif name == "read_files" and "files" in res_dict:
                        for p in res_dict["files"].keys():
                            self._playground.open_file(Path(self._workspace_root) / p)
            except Exception:
                pass

        # Terminal dispatches don't trigger _on_stream_done, so we must auto-save here
        # to ensure the worker's result is persisted before the app is closed.
        if name in ("dispatch_to_worker",):
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
            self._drone_controller.refresh_drone_context()

    def _prepare_answer_only_research_ui(self) -> None:
        """Hide work surfaces before a pure answer-only research turn."""
        blocked_widgets: list[QWidget] = []

        try:
            terminal_window = self._playground.terminal_window()
            blocked_widgets.append(terminal_window)
            terminal_window.clear()
            terminal_window.hide()
        except Exception:
            logger.debug("Failed to hide terminal for answer-only research", exc_info=True)

        try:
            blocked_widgets.append(self._drone_reports_window)
            self._drone_reports_window.hide()
        except Exception:
            logger.debug("Failed to hide Drone Reports for answer-only research", exc_info=True)

        try:
            workbay = self._drone_controller.drone_workbay_window
            if workbay is not None:
                blocked_widgets.append(workbay)
                workbay.hide()
        except Exception:
            logger.debug("Failed to hide Drone Workbay for answer-only research", exc_info=True)

        try:
            if self._checkpoint_dialog is not None:
                blocked_widgets.append(self._checkpoint_dialog)
                self._checkpoint_dialog.hide()
        except Exception:
            logger.debug("Failed to hide checkpoint dialog for answer-only research", exc_info=True)

        if self._answer_only_ui_guard is not None:
            self._answer_only_ui_guard.stop()
        self._answer_only_ui_guard = SilentResearchUiGuard(
            self,
            blocked_widgets=blocked_widgets,
            logger=logger,
        )
        self._answer_only_ui_guard.start()
        for delay_ms in (100, 500, 1500, 3000, 5000):
            QTimer.singleShot(delay_ms, self._suppress_answer_only_research_surfaces)

        try:
            self._terminal_controller._sync_terminal_checked_state()
            self._drone_controller.sync_drone_tab_checked()
        except Exception:
            logger.debug("Failed to sync research UI chrome", exc_info=True)

    def _suppress_answer_only_research_surfaces(self) -> None:
        if self._answer_only_ui_guard is None:
            return
        self._answer_only_ui_guard.suppress_existing_surfaces()
        try:
            self._terminal_controller._sync_terminal_checked_state()
            self._drone_controller.sync_drone_tab_checked()
        except Exception:
            logger.debug("Failed to sync research UI chrome", exc_info=True)

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
        self._handoff_controller.clear_on_error()
        title = f"API Error {status}" if status > 0 else "Error"
        self._chat.add_error(title, message, show_retry=True)
        self._chat.stop_current_aura()

    def _on_handoff_requested(self) -> None:
        """Handle Continue in Fresh Chat button click."""
        self._handoff_controller.request_handoff()

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

    def _on_thread_selected(self, conversation_path: Path) -> None:
        if self._bridge.is_running():
            QMessageBox.information(self, APP_NAME, "Wait for the current response to finish, or click Stop.")
            return
        try:
            self._persistence.load_and_apply(conversation_path)
            self._send_handler.clear_queue()
            self._input.set_queued_messages(0)
            self._reset_session_usage()
        except Exception as _err:
            QMessageBox.warning(self, APP_NAME, f"Could not open conversation:\n{_err}")

    def _on_current_context_changed(self, project_id: str, thread_id: str) -> None:
        """Sync companion with the active project and conversation context."""
        self._companion_controller.sync_context(project_id, thread_id)

    def _on_project_thread_updated(self) -> None:
        # Use a fine-grained update for the current thread row instead of a
        # full project refresh. Full refresh is reserved for project
        # create/delete/rename/workspace changes.
        if hasattr(self._left_pane, "refresh_current_thread") and self._workspace_root is not None:
            self._left_pane.refresh_current_thread(self._workspace_root)
        else:
            self._left_pane.refresh_projects(self._workspace_root)
