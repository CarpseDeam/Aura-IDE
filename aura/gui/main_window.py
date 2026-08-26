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
from aura.config import (
    APP_NAME,
    PROVIDERS,
    AppSettings,
    ThinkingMode,
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
from aura.gui.drones.drone_reports_window import DroneReportsWindow
from aura.gui.edge_rail_host import ExternalEdgeRailHost
from aura.gui.execution_handler import ExecutionEventHandler
from aura.gui.input_panel import InputPanel, SendPayload
from aura.gui.left_pane import LeftPane
from aura.gui.main_window_companion import MainWindowCompanionController
from aura.gui.main_window_drones import MainWindowDroneController
from aura.gui.main_window_handoff import MainWindowHandoffController
from aura.gui.main_window_pricing import MainWindowPricingController
from aura.gui.main_window_settings import MainWindowSettingsController
from aura.gui.main_window_signal_wiring import MainWindowSignalWiring
from aura.gui.main_window_terminal import MainWindowTerminalController
from aura.gui.main_window_toolbar import MainWindowToolbar
from aura.gui.main_window_update import MainWindowUpdateController
from aura.gui.main_window_workspace import MainWindowWorkspaceController
from aura.gui.onboarding_dialog import OnboardingDialog
from aura.gui.plan_review_controller import PlanReviewController
from aura.gui.playground import AuraPlayground
from aura.gui.send_handler import SendHandler
from aura.gui.skills_manager import SkillsManagerController
from aura.gui.status_bar import AuraStatusBar
from aura.gui.update_dialog import UpdateDialog
from aura.gui.widgets.aura_glow import AuraWidget
from aura.gui.window_chrome import WindowChromeMixin


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
        # Final assistant message of the current turn, held until the turn is
        # saved so a pending handoff can run against it afterwards.
        self._final_stream_message: dict = {}
        self._use_native_chrome = os.environ.get("AURA_NATIVE_CHROME") == "1"
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(icon_path())))
        clamp_to_screen(self, 1500, 920)

        # Settings.
        self._settings: AppSettings = load_settings()

        # Workspace.
        self._workspace_root: Path | None = load_workspace_root()

        # Bridge — one production provider owns normal coding.
        self._bridge = ConversationBridge(
            parent_widget=self,
            provider=self._settings.provider,
        )
        self._bridge.set_production_provider(self._settings.provider)
        self._bridge.set_workspace_root(self._workspace_root)
        self._enter_production_mode()
        self._bridge.set_temperature(self._settings.temperature)
        self._bridge.set_auto_approve(self._settings.auto_approve)
        self._bridge.set_review_plan_before_changes(self._settings.review_plan_before_changes)
        # Restores a connection the user already turned on. Disabled is the
        # default and does nothing at all — no process, no download, no
        # network — so launch cost is unchanged for everyone else.
        self._bridge.apply_windows_computer_use(self._settings)

        # ----- toolbar ----
        self._toolbar = MainWindowToolbar(self._settings, self)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._toolbar)
        self._settings_controller = MainWindowSettingsController(self)
        self._update_controller = MainWindowUpdateController(self._toolbar, parent=self)
        self._pricing_controller = MainWindowPricingController(
            self._settings.provider, parent=self
        )

        # ----- status bar -----
        self._status_bar = AuraStatusBar(
            self,
            show_resize_grip=not self._use_native_chrome,
        )
        self.setStatusBar(self._status_bar)

        self._drone_controller = MainWindowDroneController(self)
        self._terminal_controller = MainWindowTerminalController(self)

        # ----- splitter ----
        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setHandleWidth(3)

        # Left pane: workspace label + change root + tree + model config.
        self._left_pane = LeftPane(self._workspace_root, parent=self)
        self._left_pane.populate_models(self._settings.provider)
        self._workspace_controller = MainWindowWorkspaceController(self)
        self._main_splitter.addWidget(self._left_pane)

        # Center column: stacked launchpad / workspace view
        self._center_stack = _ShrinkableStack(self)
        self._center_stack.setMinimumWidth(0)
        self._center_stack.setStyleSheet("background: transparent;")

        # Page 0: Project Launchpad (shown when no workspace)
        from aura.gui.project_launchpad import ProjectLaunchpad
        self._launchpad = ProjectLaunchpad(self)
        self._center_stack.addWidget(self._launchpad)

        # Page 1: Chat + Input (normal workspace view)
        center = QWidget()
        center.setMinimumWidth(280)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(20, 0, 20, 16)
        center_layout.setSpacing(0)

        self._chat = ChatView()
        self._chat.setParent(self)
        # Production execution projects into the workspace, so the chat keeps
        # its compact tool presentation.
        self._chat.set_compact_tools(True)
        center_layout.addWidget(self._chat, 1)

        # Plan Review — renders the inline Plan Ready card and forwards the
        # user's Implement/Edit/Cancel decision back through the bridge's
        # proxy so the blocked conversation thread resumes.
        self._plan_review_controller = PlanReviewController(
            proxy=self._bridge.plan_review_proxy,
            chat=self._chat,
            parent_widget=self,
        )

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
        # Skills manager — the only GUI owner of SkillLibrary access. Both the
        # composer's Skills button and /skills reach this one controller.
        self._skills_controller = SkillsManagerController(
            input_panel=self._input,
            parent_widget=self,
            workspace_root=self._workspace_root,
            parent=self,
        )
        # Companion (mobile control plane)
        self._companion_controller = MainWindowCompanionController(self)

        # Right pane: execution activity (embedded, not a separate window)
        self._playground = AuraPlayground(
            parent=self,
            terminal_window_geometry=self._settings.terminal_window_geometry,
            outer_splitter_sizes=self._settings.playground_outer_splitter_sizes or None,
            vertical_splitter_sizes=self._settings.playground_vertical_splitter_sizes or None,
        )
        self._playground_aura = AuraWidget(
            self._playground,
            glow_color="#00e5ff",
            glow_spread=16,
            parent=self,
        )
        self._playground.set_aura_wrapper(self._playground_aura)
        self._playground.set_workspace_root(self._workspace_root)
        self._playground.set_read_only_mode(False)


        # Floating Drone Reports window. Active run cards live here instead of
        # consuming space in the production/workspace area.
        self._drone_reports_window = DroneReportsWindow(
            self,
            initial_geometry=self._settings.drone_reports_window_geometry,
        )
        # Execution event handler — owns session usage, forwards bridge signals
        # to chat / playground UI components.
        self._execution_handler = ExecutionEventHandler(
            bridge=self._bridge,
            chat=self._chat,
            playground=self._playground,
            settings=self._settings,
            parent=self,
        )
        # Conversation persistence (auto-save, load, restore, replay).
        self._persistence = ConversationPersistence(
            bridge=self._bridge,
            chat=self._chat,
            playground=self._playground,
            input_panel=self._input,
            left_pane=self._left_pane,
            settings=self._settings,
            get_conversation_telemetry=lambda: self._execution_handler.conversation_telemetry,
            restore_conversation_telemetry=self._execution_handler.restore_conversation_telemetry,
            reset_conversation_usage=self._execution_handler.reset_conversation_usage,
            parent=self,
        )
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
            parent_widget=self,
            parent=self,
        )

        # Apply the production model / thinking from settings.
        self.set_model(self._settings.default_model)
        self.set_thinking(self._settings.default_thinking)
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

        # Edge tab rail — terminal + checkpoint tabs. Hosted in a separate
        # frameless tool window that clings to the outside of the main
        # window's right edge, so it consumes zero pixels of the main
        # window's own layout. ExternalEdgeRailHost is the single owner of
        # that window's geometry.
        self._edge_rail_host = ExternalEdgeRailHost(self)
        self._edge_rail = self._edge_rail_host.rail
        self._terminal_tab = self._edge_rail.terminal_tab
        self._terminal_container = self._edge_rail.terminal_container
        self._corner_widget = self._edge_rail.corner_widget
        self._drone_controller.sync_drone_tab_checked()

        # Sync companion badge after rail exists (status may have fired before rail was created)
        self._companion_controller.sync_edge_rail_status()

        # Frameless window — no native title bar unless explicitly disabled.
        if not self._use_native_chrome:
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)

        self._tree = self._playground.file_tree()

        # Execution signal wiring (delegated to ExecutionEventHandler).
        self._execution_handler.connect_bridge_signals()

        self._workspace_controller.update_workspace_label()

        # Signal wiring — extracted for clarity.
        self._signal_wiring = MainWindowSignalWiring(self)
        self._signal_wiring.wire()

        QTimer.singleShot(0, lambda: self._left_pane.refresh_projects(self._workspace_root))
        QTimer.singleShot(0, lambda: self._left_pane.refresh_drones(self._workspace_root))

        self._refresh_status_bar()

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

        # Hydrate official provider pricing once per launch, off the UI
        # thread. Settings/Models discovery is the only other refresh, and
        # a normal launch never opens it.
        self._pricing_controller.schedule_startup_refresh()

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
        playground_outer, playground_vert = self._playground.splitter_sizes()
        self._settings.playground_outer_splitter_sizes = playground_outer
        self._settings.playground_vertical_splitter_sizes = playground_vert
        save_settings(self._settings)
        self._companion_controller.stop()
        self._pricing_controller.shutdown()
        self._bridge.shutdown()
        # Closes the Windows MCP subprocess. Without this the server outlives
        # the app that launched it.
        self._bridge.shutdown_windows_computer_use()
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        """Triggered when the window is shown."""
        super().showEvent(event)
        # Mark first launch done so onboarding never shows on subsequent starts.
        QTimer.singleShot(0, self._mark_first_launch_done)

    def _mark_first_launch_done(self) -> None:
        self._settings.first_launch_done = True
        save_settings(self._settings)

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
        return self._left_pane.current_production_model()

    def current_thinking(self) -> ThinkingMode:
        return self._left_pane.current_production_thinking()

    def set_model(self, model: str) -> None:
        self._left_pane.set_production_model(model)

    def set_thinking(self, thinking: ThinkingMode) -> None:
        self._left_pane.set_production_thinking(thinking)

    # ----- status bar -----------------------------------------------------

    def _refresh_status_bar(self) -> None:
        ws = str(self._workspace_root) if self._workspace_root else "(none)"
        has_provider = has_usable_provider_configuration(self._settings.provider)
        self._status_bar.refresh(
            workspace_root=ws,
            model_id=self.current_model(),
            thinking=self.current_thinking(),
            conversation_usage=self._execution_handler.conversation_usage,
            latest_context=self._execution_handler.conversation_telemetry.latest_context,
            has_provider=has_provider,
            telemetry=self._execution_handler.conversation_telemetry,
        )

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

    def open_api_settings(self) -> None:
        """Open settings dialog directly to the API Keys tab."""
        self._settings_controller.open_api_settings()

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

    def _enter_production_mode(self) -> None:
        """Configure the bridge for the normal production conversation.

        One continuous production model owns the user's request end to end and
        projects its execution into the workspace.
        """
        self._bridge.refresh_production_prompt()
        if hasattr(self, "_chat"):
            self._chat.set_compact_tools(True)

    def _on_started(self) -> None:
        self._final_stream_message = {}
        self._input.set_execution_active(True)
        self._skills_controller.set_execution_active(True)
        # Handoff follows the main conversation lifecycle, including
        # conversation-first Read Only turns that have no production session.
        self._status_bar.set_execution_active(True)
        # Production turns switch from Drone Bay to workspace so the user sees
        # the run — but collaborative Read Only turns keep the current chat /
        # playground presentation, including Workflow Studio if it is open.
        if (
            not self._bridge.active_turn_read_only
            and not self._drone_controller.is_workbay_open()
        ):
            self._playground.switch_to_workspace()
        self._drone_controller.sync_drone_tab_checked()

    def _on_finished(self) -> None:
        self._input.set_execution_active(False)
        self._skills_controller.set_execution_active(False)
        self._status_bar.set_execution_active(False)
        # Closes the assistant card and records its transcript. By this point
        # ConversationManager has committed the final assistant message to
        # History, so chat and model history agree.
        self._chat.assistant_done()
        self._chat.stop_current_aura()
        self._input.focus_editor()
        # Settle the turn, then drain one queued item. Both are deferred so the
        # run's own completion presentation (the receipt flush scheduled by the
        # execution handler) lands first; the FIFO order guarantees the snapshot is
        # taken after this turn is fully finalized and before the next queued
        # turn touches the conversation.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._settle_finished_turn)
        QTimer.singleShot(0, lambda: self._send_handler.process_message_queue(
            self.current_model(), self.current_thinking()
        ))

    def _settle_finished_turn(self) -> None:
        """Save the completed turn, then run any pending handoff."""
        self._auto_save_conversation()
        final_message = self._final_stream_message
        self._final_stream_message = {}
        if final_message:
            # Handoff abandons the conversation for a fresh one, so it only
            # runs once the finished turn is safely on disk.
            self._handoff_controller.finalize_handoff(final_message)

    def _auto_save_conversation(self) -> None:
        """Persist the conversation as it currently stands."""
        self._persistence.auto_save(
            workspace_root=self._workspace_root,
            model=self.current_model(),
            thinking=self.current_thinking(),
            provider=self._settings.provider,
        )

    def _on_stream_done(self, finish_reason: str, full_message: dict) -> None:
        # If the model produced tool calls, it's not actually done — the bridge
        # will execute them and loop back. Keep the aura alive.
        tool_calls = full_message.get("tool_calls") or []
        if tool_calls:
            # Finalize markdown but keep the aura pulsing.
            self._chat.finalize_markdown_only()
            # Note: we keep the current aura state (which is usually already
            # "coding" if a tool call was emitted).
        else:
            # No tool calls — this is the final turn.
            self._chat.assistant_done()
        # No auto-save here. A stream can end mid-turn (tool-call rounds), and
        # ConversationManager only appends the assistant message to History
        # *after* this event, so saving now would persist an incomplete turn.
        # The turn is saved from _on_finished instead.

        # A pending handoff starts a fresh conversation, so it must run after
        # the completed one has been saved — remember the final message and
        # finalize from _on_finished.
        if not tool_calls:
            self._final_stream_message = dict(full_message)

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
        if ok and name == "read_file":
            try:
                import json
                from pathlib import Path
                res_dict = json.loads(result)
                if isinstance(res_dict, dict):
                    if "files" in res_dict and isinstance(res_dict["files"], dict):
                        for p in res_dict["files"].keys():
                            self._playground.open_file(Path(self._workspace_root) / p)
                    elif "path" in res_dict:
                        self._playground.open_file(Path(self._workspace_root) / res_dict["path"])
            except Exception:
                pass

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

# ----- persistence (delegated to ConversationPersistence) --------------

    def _on_thread_selected(self, conversation_path: Path) -> None:
        if self._bridge.is_running():
            QMessageBox.information(self, APP_NAME, "Wait for the current response to finish, or click Stop.")
            return
        try:
            self._persistence.load_and_apply(conversation_path)
            self._send_handler.clear_queue()
            self._input.set_queued_messages(0)
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
