"""Main application window: three-pane splitter, toolbar, chat + input."""
from __future__ import annotations

import difflib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

from PySide6.QtCore import QByteArray, QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from aura.bridge import ConversationBridge
from aura.bridge.qt_bridge import PLANNER_SYSTEM_PROMPT
from aura.companion import CompanionManager
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
from aura.conversation.tools._types import ApprovalDecision, ApprovalRequest
from aura.drones.chain_runner import classify_consequential_nodes, run_chain
from aura.drones.chain_store import ChainStore
from aura.drones.definition import DroneDefinition
from aura.drones.receipt import DroneReceipt
from aura.drones.runner import DroneRunner
from aura.drones.store import DroneStore, RunHistoryStore
from aura.drones.construction_context import enter_drone_construction, clear_drone_construction
from aura.git_ops import git_init, is_git_repo
from aura.gui.chat_view import ChatView
from aura.gui.checkpoint_dialog import CheckpointDialog
from aura.gui.conv_persistence import ConversationPersistence
from aura.gui.drones.drone_reports_window import DroneReportsWindow
from aura.gui.drones.drone_run_card import DroneRunCard
from aura.gui.drones.drone_summon_card import DroneSummonCard
from aura.gui.drones.drone_workbay_window import DroneWorkbayWindow
from aura.gui.drones.chain_loop_controller import ChainLoopController
from aura.gui.edge_rails import EdgeTabRail
from aura.gui.input_panel import InputPanel, SendPayload
from aura.gui.left_pane import LeftPane
from aura.gui.main_window_toolbar import MainWindowToolbar
from aura.gui.onboarding_dialog import OnboardingDialog
from aura.gui.playground import AuraPlayground
from aura.gui.send_handler import SendHandler
from aura.gui.settings_dialog import SettingsDialog
from aura.gui.status_bar import AuraStatusBar
from aura.gui.update_dialog import UpdateDialog, UpdateWorker
from aura.gui.widgets.aura_glow import AuraWidget
from aura.gui.window_chrome import WindowChromeMixin
from aura.gui.worker_handler import WorkerEventHandler
from aura.handoff import extract_handoff_text, generate_handoff_prompt, save_handoff
from aura.hooks import hooks
from aura.prompts import SINGLE_SYSTEM_PROMPT
from aura.updater import UpdateStatus

MAX_PARALLEL_READ_ONLY_DRONES = 3





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
        self._toolbar.new_conversation_requested.connect(self._on_new_conversation)
        self._toolbar.open_conversation_requested.connect(self._on_open_conversation)
        self._toolbar.read_only_toggled.connect(self._on_read_only_toggled)
        self._toolbar.auto_dispatch_toggled.connect(self._on_auto_dispatch_toggled)
        self._toolbar.auto_approve_toggled.connect(self._on_auto_approve_toggled)
        self._toolbar.auto_summon_drones_toggled.connect(self._on_auto_summon_drones_toggled)
        self._toolbar.update_requested.connect(self._on_open_update)
        self._toolbar.settings_requested.connect(self._on_open_settings)
        self._toolbar.minimize_requested.connect(self.showMinimized)
        self._toolbar.maximize_requested.connect(self._toggle_maximize)
        self._toolbar.close_requested.connect(self.close)

        # ----- status bar -----
        self._status_bar = AuraStatusBar(self)
        self.setStatusBar(self._status_bar)

        # ----- splitter ----
        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setHandleWidth(3)

        # Left pane: workspace label + change root + tree + model config.
        self._left_pane = LeftPane(self._workspace_root, parent=self)
        self._left_pane.populate_models(self._settings.planner_provider, self._settings.worker_provider)
        self._left_pane.change_root_requested.connect(self._on_change_root)
        self._left_pane.project_selected.connect(self._on_project_selected)
        self._left_pane.new_project_requested.connect(self._on_new_project)
        self._left_pane.planner_model_changed.connect(lambda: self._refresh_status_bar())
        self._left_pane.planner_thinking_changed.connect(lambda: self._refresh_status_bar())
        self._left_pane.worker_model_changed.connect(self._on_sidebar_worker_model_changed)
        self._left_pane.worker_thinking_changed.connect(self._on_sidebar_worker_thinking_changed)
        self._left_pane.drone_selected.connect(self._on_drone_folder_selected)
        self._left_pane.new_drone_requested.connect(self._on_create_drone)
        self._main_splitter.addWidget(self._left_pane)

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
        self._send_handler.drone_bay_requested.connect(self._on_drone_bay_requested)

        # Companion (mobile control plane)
        self._companion = CompanionManager(self._settings)
        self._companion.connection_status_changed.connect(self._on_companion_status)
        self._companion.message_received.connect(self._on_companion_message)
        self._companion.conversation_selected_by_companion.connect(self._on_companion_thread_selected)
        self._companion.set_bridge(self._bridge)
        self._companion.set_workspace_root(str(self._workspace_root))
        self._companion.start()

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


        self._drone_workbay_window: DroneWorkbayWindow | None = None

        # Floating Drone Reports window. Active run cards live here instead of
        # consuming space in the Worker/workspace area.
        self._drone_reports_window = DroneReportsWindow(
            self,
            initial_geometry=self._settings.drone_reports_window_geometry,
        )
        self._drone_reports_window.geometry_saved.connect(
            self._on_drone_reports_geometry_saved
        )

        # Chain execution loop controller
        self._chain_controller = ChainLoopController(
            chain_provider=self._resolve_current_chain,
            parent=self,
        )
        self._chain_controller.lap_finished.connect(self._on_lap_finished)
        self._chain_controller.lap_error.connect(self._on_lap_error)
        self._chain_controller.loop_finished.connect(self._on_loop_finished)


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

        self._main_splitter.addWidget(center)
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
            self._main_splitter.setSizes(self._settings.main_splitter_sizes)

        # Keep the sidebar stable and let the workspace receive most extra room.
        self._main_splitter.setStretchFactor(0, 0)  # workspace tree: fixed
        self._main_splitter.setStretchFactor(1, 1)  # chat: stable reading/planning column
        self._main_splitter.setStretchFactor(2, 2)  # workspace: primary work surface

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
        self._edge_rail.terminalTabToggled.connect(self._on_terminal_toggle)
        # Wire checkpoint tab click to the existing handler on MainWindow.
        checkpoint_tab = self._edge_rail.checkpoint_tab
        if checkpoint_tab is not None:
            checkpoint_tab.clicked.connect(lambda: self._on_open_checkpoints())
        self._edge_rail.droneBayRequested.connect(self._on_drone_bay_requested)
        self._edge_rail.droneRunFocusRequested.connect(self._on_focus_drone_run)
        self._sync_drone_tab_checked()

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
        self._input.handoff_requested.connect(self._on_handoff_requested)
        # Drone runner state. Read-only Drones can run in parallel; write-capable
        # Drones remain exclusive because they share the write approval lane.
        self._drone_runner: DroneRunner | None = None
        self._drone_runner_thread: QThread | None = None
        self._active_run_card: QWidget | None = None
        self._drone_runs: dict[str, dict] = {}
        self._write_drone_run_id: str | None = None
        self._drone_receipt: DroneReceipt | None = None
        self._pending_drone_summons: dict[str, dict[str, str]] = {}

        self._pending_handoff: bool = False
        self._tree = self._playground.file_tree()
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

        # Wire project thread selection from left pane
        self._left_pane.thread_selected.connect(self._on_thread_selected)
        self._persistence.project_thread_updated.connect(self._on_project_thread_updated)
        self._persistence.current_context_changed.connect(self._on_current_context_changed)

        self._left_pane.refresh_projects(self._workspace_root)
        self._left_pane.refresh_drones(self._workspace_root)

        self._refresh_status_bar()
        self._position_edge_tabs()

        # Restore most recent conversation if enabled.
        if self._settings.restore_last_conversation:
            # Defer restoration so the UI paints and becomes interactive first.
            initial_root = self._workspace_root
            QTimer.singleShot(100, lambda: self._persistence.restore_last(initial_root))

        # Check for updates in the background.
        QTimer.singleShot(2000, self._check_for_updates)

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

    def closeEvent(self, event) -> None:
        # Save window geometry/state.
        geo = self.saveGeometry()
        self._settings.main_window_geometry = bytes(geo.toBase64()).decode("ascii")
        state = self.saveState()
        self._settings.main_window_state = bytes(state.toBase64()).decode("ascii")
        # Save splitter sizes.
        self._settings.main_splitter_sizes = list(self._main_splitter.sizes())
        save_settings(self._settings)
        self._companion.stop()
        super().closeEvent(event)

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

    def _on_drone_reports_geometry_saved(self, geometry: str) -> None:
        if self._settings.drone_reports_window_geometry == geometry:
            return
        self._settings.drone_reports_window_geometry = geometry
        save_settings(self._settings)

    def _on_drone_workbay_geometry_saved(self, geometry: str) -> None:
        if self._settings.drone_workbay_window_geometry == geometry:
            return
        self._settings.drone_workbay_window_geometry = geometry
        save_settings(self._settings)

    def _query_workbay_state(self) -> dict | None:
        """Hook handler: return the active Workbay tab's canvas state as plain dict."""
        workbay = self._drone_workbay_window
        if workbay is None or not workbay.is_open():
            return None
        editor = workbay.chain_editor
        if not editor:
            return None
        chain_id = editor._current_chain_id
        if not chain_id:
            return None
        name = editor._chain_name
        description = editor._chain_desc

        nodes, edges, mission_core = editor._canvas.to_chain_nodes_and_edges()

        # Build drone lookup from canvas nodes
        drone_lookup: dict[str, dict] = {}
        for node_dict in nodes:
            drone_id = node_dict.get("drone_id", "")
            if not drone_id:
                continue
            # Find the ChainNodeItem with this drone_id to access its .drone attribute
            for node_item in editor._canvas._nodes.values():
                if node_item.drone_id == drone_id:
                    drone_def = node_item.drone
                    if drone_def is not None:
                        drone_lookup[drone_id] = {
                            "id": drone_def.id,
                            "name": drone_def.name,
                            "write_policy": drone_def.write_policy,
                        }
                    break

        return {
            "chain_id": chain_id,
            "name": name,
            "description": description,
            "auto_route": editor._auto_route,
            "dirty": editor._dirty,
            "nodes": nodes,
            "edges": edges,
            "mission_core": mission_core or {},
            "drone_lookup": drone_lookup,
        }

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
        self._companion.set_workspace_root(str(self._workspace_root))
        self._tree.set_root(path)
        save_workspace_root(path)
        from aura.projects.store import ProjectStore
        _project = ProjectStore().create_or_update_project(path)
        self._companion.set_current_project(_project.id, _project.name)
        self._update_workspace_label()
        self._left_pane.refresh_projects(path)
        self._left_pane.refresh_drones(path)
        # Close chain editor when workspace root changes
        if self._drone_workbay_window is not None:
            hooks.unregister('query_mission_workbay_state')
            self._drone_workbay_window.hide()
            self._drone_workbay_window.deleteLater()
            self._drone_workbay_window = None
        clear_drone_construction()
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
        self._on_project_selected(path)

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

    def _retarget_workspace(self, root_path: Path, *, restore_last: bool = True) -> None:
        clear_drone_construction()
        if self._workspace_root is not None and self._workspace_root.resolve() != root_path.resolve():
            self._persistence.new_conversation()

        self._workspace_root = root_path
        self._checkpoint_dialog = None
        self._bridge.set_workspace_root(root_path)
        self._input.set_workspace_root(root_path)
        self._send_handler.set_workspace_root(root_path)
        self._playground.set_workspace_root(root_path)
        self._companion.set_workspace_root(str(self._workspace_root))
        self._tree.set_root(root_path)

        self._update_workspace_label()
        self._refresh_status_bar()

        if self._settings.restore_last_conversation and restore_last:
            QTimer.singleShot(0, lambda: self._persistence.restore_last(root_path))

        # One-time recovery for broken nested drone folders
        self._recover_nested_drone_folders()

    def _on_project_selected(self, root_path: Path, *, restore_last: bool = True) -> None:
        from aura.projects.store import ProjectStore
        project = ProjectStore().create_or_update_project(root_path)
        self._companion.set_current_project(project.id, project.name)
        save_workspace_root(root_path)

        self._retarget_workspace(root_path, restore_last=restore_last)
        self._left_pane.refresh_projects(self._workspace_root)
        self._left_pane.refresh_drones(self._workspace_root)

    def _on_drone_folder_selected(self, folder: Path) -> None:
        # Resolve the owning project root — if the folder is inside .aura/drones,
        # this returns the project root above it; otherwise returns the folder itself.
        from aura.drones.store import _project_root_for_drone_storage
        project_root = _project_root_for_drone_storage(folder)
        project_resolved = project_root.resolve()

        # If Aura is currently rooted inside .aura/drones/<id>, retarget once back
        current = self._workspace_root.resolve() if self._workspace_root else None
        if current is None or current != project_resolved:
            self._retarget_workspace(project_root, restore_last=False)

        # Refresh drone sidebar (pass the drone folder for highlight)
        self._left_pane.refresh_drones(folder)

        # Mark construction context
        enter_drone_construction("existing", folder.name)

    def _on_create_drone(self) -> None:
        import json
        from datetime import datetime, timezone
        from uuid import uuid4

        from aura.drones.definition import DroneBudget, DroneDefinition
        from aura.drones.store import (
            _global_drones_root,
            _is_safe_drone_id,
            _project_root_for_drone_storage,
            DroneStore,
        )

        # One-time recovery: promote nested drone folders from a previous broken state
        self._recover_nested_drone_folders()

        # Ensure workspace is rooted at the project root
        project_root = _project_root_for_drone_storage(self._workspace_root)
        project_resolved = project_root.resolve()
        current = self._workspace_root.resolve() if self._workspace_root else None
        if current is None or current != project_resolved:
            self._retarget_workspace(project_root, restore_last=False)

        drone_id = f"drone-{uuid4().hex[:8]}"
        drone_dir = _global_drones_root(self._workspace_root) / drone_id
        drone_dir.mkdir(parents=True, exist_ok=True)

        # ── Write valid drone.json ──
        now = datetime.now(timezone.utc).isoformat()
        drone = DroneDefinition(
            id=drone_id,
            name="New Drone",
            description="A new Drone scaffold",
            instructions="You are a helpful Drone. Follow the user's instructions to complete the task.",
            write_policy="read_only",
            runtime="python",
            entrypoint={
                "kind": "command",
                "command": ["python", "main.py"],
                "protocol": "json-stdio",
            },
            budget=DroneBudget(timeout_seconds=60),
            scope="global",
            manifest_version="1",
            input_contract={
                "type": "object",
                "description": "Standard drone input: goal plus workspace context",
                "schema": {"goal": "string", "workspace_root": "string"},
            },
            cargo_contract={
                "type": "object",
                "description": "Standard drone cargo: structured result data",
                "schema": {"drone_id": "string", "ready": "bool"},
            },
            output_contract={
                "description": "Standard drone output",
                "properties": {
                    "ok": {"type": "boolean"},
                    "summary": {"type": "string"},
                },
                "required": ["ok", "summary"],
            },
            created_at=now,
            updated_at=now,
            created_by="user",
        )
        DroneStore._write_manifest(drone_dir, drone)

        # ── Write scaffold main.py ──
        main_py = (
            '"""Scaffold Drone: ' + drone_id + '"""\n'
            'import json\n'
            'import sys\n\n'
            'def main() -> None:\n'
            '    raw = sys.stdin.read()\n'
            '    payload = json.loads(raw) if raw.strip() else {}\n'
            '    goal = payload.get("goal", "")\n'
            '    cargo = payload.get("cargo", {})\n'
            '    result = {\n'
            '        "ok": True,\n'
            '        "summary": "New Drone scaffold is ready.",\n'
            '        "cargo": {\n'
            '            "drone_id": "' + drone_id + '",\n'
            '            "ready": True,\n'
            '        },\n'
            '    }\n'
            '    sys.stdout.write(json.dumps(result))\n\n'
            'if __name__ == "__main__":\n'
            '    main()\n'
        )
        (drone_dir / "main.py").write_text(main_py, encoding="utf-8")

        # Keep Aura rooted at the project root — do NOT call _on_drone_folder_selected.
        # Refresh left Drone list with the new drone highlighted.
        self._left_pane.refresh_drones(drone_dir)

        # Enter Drone construction mode
        enter_drone_construction("new", drone_id)

        # Refresh Workbay roster when Workbay is open
        if self._drone_workbay_window is not None and self._drone_workbay_window.is_open():
            self._drone_workbay_window.chain_editor.refresh_roster()

    def _recover_nested_drone_folders(self) -> None:
        """One-time recovery: promote nested drone folders from a previous broken state.

        If a valid Drone folder exists nested under .aura/drones/<temp>/<real>/,
        promote <real> to .aura/drones/<real>/. After promotion, refresh the
        left sidebar and Workbay roster.
        """
        if getattr(self, "_drone_recovery_done", False):
            return
        self._drone_recovery_done = True

        import json
        import shutil

        from aura.drones.store import _global_drones_root, _is_safe_drone_id

        drones_root = _global_drones_root(self._workspace_root)
        if not drones_root.exists():
            return

        promoted = False
        for subdir in list(drones_root.iterdir()):
            if not subdir.is_dir():
                continue
            # Check for nested drone folders one level deep
            for nested in list(subdir.iterdir()):
                if not nested.is_dir():
                    continue
                drone_json = nested / "drone.json"
                if not drone_json.exists():
                    continue
                try:
                    data = json.loads(drone_json.read_text(encoding="utf-8"))
                    real_id = data.get("id", nested.name)
                    if not _is_safe_drone_id(real_id):
                        continue
                    target = drones_root / real_id
                    if target.exists():
                        logger.warning(
                            "Skipping nested drone promotion: target %s already exists",
                            target,
                        )
                        continue
                    shutil.copytree(nested, target, dirs_exist_ok=True)
                    shutil.rmtree(nested, ignore_errors=True)
                    logger.info("Promoted nested drone %s -> %s", nested, target)
                    promoted = True
                except Exception as exc:
                    logger.warning("Failed to promote nested drone %s: %s", nested, exc)
                    continue
            # Remove the now-empty temporary parent folder
            try:
                if subdir.exists() and not any(subdir.iterdir()):
                    shutil.rmtree(subdir, ignore_errors=True)
            except Exception:
                logger.warning("Failed to remove empty temp folder %s", subdir)

        if promoted:
            self._left_pane.refresh_drones(self._workspace_root)
            if self._drone_workbay_window is not None and self._drone_workbay_window.is_open():
                self._drone_workbay_window.chain_editor.refresh_roster()

    def _on_new_project(self) -> None:
        start = str(self._workspace_root) if self._workspace_root else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose or Create Workspace Directory", start)
        if not chosen:
            return
        chosen_path = Path(chosen)
        from aura.projects.store import ProjectStore
        ProjectStore().create_or_update_project(chosen_path)
        self._on_project_selected(chosen_path)

    def _update_workspace_label(self) -> None:
        self._left_pane.update_workspace_label(self._workspace_root)

    def _on_read_only_toggled(self, checked: bool) -> None:
        self._bridge.set_read_only(checked)
        self._toolbar.set_read_only(checked)
        self._playground.set_read_only_mode(checked)

    def _on_focused_action_requested(self, prompt: str) -> None:
        payload = SendPayload(text=prompt, attachments=[])
        self._send_handler.handle_send(payload, self.current_model(), self.current_thinking())

    # ----- Drone Bay handlers --------------------------------------------

    def _on_drone_bay_requested(self) -> None:
        self._open_or_toggle_drone_workbay()
        self._sync_drone_tab_checked()
        self._position_edge_tabs()

    def _open_or_toggle_drone_workbay(self) -> None:
        """Open the Drone Workbay as a standalone window or focus it."""
        print(f"[DRONE] clicked. workspace_root={self._workspace_root!r} existing_window={self._drone_workbay_window!r}")
        if self._workspace_root is None:
            print("[DRONE] BAILING — workspace_root is None")
            return
        if self._drone_workbay_window is not None:
            if self._drone_workbay_window.is_open():
                self._drone_workbay_window.raise_()
                self._drone_workbay_window.activateWindow()
            else:
                self._drone_workbay_window.show_and_raise()
            return

        try:
            self._drone_workbay_window = DroneWorkbayWindow(
                workspace_root=self._workspace_root,
                chain_id=None,
                provider_id=self._settings.planner_provider,
                model=self.current_model(),
                thinking=self.current_thinking(),
                temperature=self._settings.temperature,
                initial_geometry=self._settings.drone_workbay_window_geometry,
                parent=None,
            )
            print("[DRONE] window constructed OK")
        except Exception:
            import traceback
            traceback.print_exc()
            raise
        workbay = self._drone_workbay_window
        editor = workbay.chain_editor
        editor.goBackRequested.connect(workbay.hide)
        editor.closeRequested.connect(workbay.hide)
        editor.runChainRequested.connect(lambda cid: self._on_run_workflow(cid))
        editor.runDroneRequested.connect(self._on_launch_drone)
        editor.deleteDroneRequested.connect(self._on_delete_drone)
        editor.loopToggled.connect(self._on_loop_toggled)
        workbay.geometry_saved.connect(self._on_drone_workbay_geometry_saved)
        if hooks.is_registered('query_mission_workbay_state'):
            hooks.unregister('query_mission_workbay_state')
        hooks.register('query_mission_workbay_state', self._query_workbay_state)
        workbay.show_and_raise()

    def _sync_drone_tab_checked(self) -> None:
        if self._edge_rail.drone_tab is not None:
            workbay_open = self._drone_workbay_window.is_open() if self._drone_workbay_window else False
            is_open = workbay_open or self._drone_reports_window.is_open()
            self._edge_rail.drone_tab.setChecked(is_open)




    def _on_delete_drone(self, drone_id: str) -> None:
        reply = QMessageBox.question(
            self,
            "Delete Drone",
            "Are you sure you want to delete this drone?\n\nAny workflow that references this drone will show a missing node.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            DroneStore.delete_drone(self._workspace_root, drone_id)
            self._refresh_drone_context()
            if self._drone_workbay_window is not None and self._drone_workbay_window.isVisible():
                self._drone_workbay_window.chain_editor.refresh_roster()

    def _refresh_drone_context(self) -> None:
        refresher = getattr(self._bridge, "refresh_tier1_context", None)
        if callable(refresher):
            refresher()

    # ----- Workflow handlers -----------------------------------------------

    def _on_run_workflow(self, chain_id: str) -> None:
        """Run a workflow chain with upfront approval and background execution."""
        # ── Load and validate ──
        chain = ChainStore.load_chain(self._workspace_root, chain_id)
        if chain is None:
            QMessageBox.warning(
                self, "Workflow Not Found",
                f"Chain '{chain_id}' could not be loaded."
            )
            return

        drones = DroneStore.list_drones(self._workspace_root)
        drone_lookup: dict[str, object] = {d.id: d for d in drones}

        missing = [
            n.drone_id for n in chain.nodes
            if n.drone_id not in drone_lookup
        ]
        if missing:
            QMessageBox.warning(
                self,
                "Missing Drones",
                f"The following Drones are not Ready for workflow "
                f"'{chain.name}': {', '.join(missing)}",
            )
            return

        # ── Classify and approve (if needed) ──
        consequential = classify_consequential_nodes(chain, drone_lookup)
        if consequential:
            lines = [
                f"<b>{chain.name}</b> contains write-capable nodes:",
                "<br>",
            ]
            for cn in consequential:
                tools = cn["consequential_tools"][:5]
                tool_list = ", ".join(tools) if tools else "all tools"
                drone_def = drone_lookup.get(cn["drone_id"])
                drone_name = (
                    drone_def.name
                    if drone_def
                    else cn["drone_id"]
                )
                lines.append(
                    f"• <b>{drone_name}</b> ({cn['drone_id']})"
                    f" — {cn['write_policy']}: {tool_list}"
                )
            lines.append("<br>Run anyway?")
            msg = "<br>".join(lines)

            answer = QMessageBox.question(
                self,
                "Confirm Workflow Run",
                msg,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        # ── Run via controller ──
        self._chain_controller.start()

    def _on_lap_finished(self, result: dict) -> None:
        """Post one lap's run report to the chat."""
        chain_name = result.get("chain_name", "Unknown")
        nodes_data = result.get("node_runs", {})
        status = result.get("status", "unknown")
        elapsed = result.get("elapsed", "")
        failed_at = result.get("failed_at", "")

        # Build the run report.
        lines = [
            f"━━━ Workflow Complete: {chain_name} ━━━",
            "",
        ]
        for node_id, nr in nodes_data.items():
            node_status = nr.get("status", "unknown")
            drone_id = nr.get("drone_id", node_id)
            met = nr.get("met", "")
            evidence = nr.get("evidence")

            if node_status == "completed":
                icon = "✓"
                ev_type = "result"
                if isinstance(evidence, dict):
                    ev_type = evidence.get("type", "result")
                lines.append(
                    f"  {icon} {node_id} ({drone_id}) "
                    f"— Produced valid {ev_type}"
                )
            elif node_status == "failed":
                icon = "✗"
                evidence_str = ""
                if isinstance(evidence, dict):
                    evidence_str = evidence.get("error", "")
                elif isinstance(evidence, str):
                    evidence_str = evidence
                lines.append(
                    f"  {icon} {node_id} ({drone_id}) "
                    f"— Failed: {evidence_str}"
                )
            elif node_status == "skipped":
                icon = "○"
                reason = met or "upstream failed"
                lines.append(
                    f"  {icon} {node_id} ({drone_id}) "
                    f"— Skipped ({reason})"
                )
            else:
                icon = "·"
                lines.append(
                    f"  {icon} {node_id} ({drone_id}) "
                    f"— {node_status}"
                )

        lines.append("")
        if failed_at:
            lines.append(f'Status: Failed at node "{failed_at}"')
        else:
            lines.append(f"Status: {status.capitalize()}")
        if elapsed:
            lines.append(f"Time: {elapsed}")

        report = "\n".join(lines)
        self._chat.begin_assistant()
        self._chat.append_content(report)
        self._chat.assistant_done()

        # Refresh chain editor run state (post-run stats, status dots)
        if self._drone_workbay_window and self._drone_workbay_window.is_open():
            self._drone_workbay_window.chain_editor.refresh_run_state()


    def _on_lap_error(self, msg: str) -> None:
        """Handle a lap-level error — post to chat if chat is available."""
        logger.error("Chain lap error: %s", msg)
        try:
            self._chat.begin_assistant()
            self._chat.append_content(f"\u26a0 Lap error: {msg}")
            self._chat.assistant_done()
        except Exception as exc:
            logger.warning("Failed to report lap error to chat: %s", exc)

    def _on_loop_finished(self) -> None:
        """Loop fully stopped (no more laps to run)."""

    def _resolve_current_chain(self) -> tuple:
        """Return (workspace_root, chain, drone_lookup) for the current editor."""
        ws = self._workspace_root
        if ws is None:
            return None, None, None
        if self._drone_workbay_window is None:
            return None, None, None
        editor = self._drone_workbay_window.chain_editor
        chain_id = editor._current_chain_id
        if not chain_id:
            return None, None, None
        chain = ChainStore.load_chain(ws, chain_id)
        if chain is None:
            return None, None, None
        drones = DroneStore.list_drones(ws)
        drone_lookup = {d.id: d for d in drones}
        return ws, chain, drone_lookup

    def _on_loop_toggled(self, enabled: bool) -> None:
        """Handle loop toggle from Mission Control."""
        ws, chain, _ = self._resolve_current_chain()
        if ws is None or chain is None:
            return
        from dataclasses import replace
        updated = replace(chain, loop=enabled)
        ChainStore.save_chain(ws, updated)

        if enabled:
            self._chain_controller.start()
        else:
            self._chain_controller.stop()

    def _on_edit_workflow(self, chain_id: str) -> None:
        """Open the chain editor for an existing workflow."""
        if self._workspace_root is None:
            return
        self._open_chain_editor(chain_id=chain_id)

    def _on_delete_workflow(self, chain_id: str) -> None:
        """Delete a workflow chain."""
        
        ChainStore.delete_chain(self._workspace_root, chain_id)
        logger.info("Deleted workflow: %s", chain_id)

    def _on_new_workflow(self) -> None:
        """Create a new workflow and open the chain editor."""
        if self._workspace_root is None:
            return
        self._open_chain_editor(chain_id=None)

    def _open_chain_editor(self, chain_id: str | None) -> None:
        """Open the workbay (creating/toggling as needed) and open a chain in it."""
        if self._workspace_root is None:
            return
        self._open_or_toggle_drone_workbay()
        if self._drone_workbay_window is not None:
            self._drone_workbay_window.chain_editor.open_chain(chain_id)
    # ----- Drone Run lifecycle (Phase 2) --------------------------------

    def _on_launch_drone(self, drone_id: str, folder: str = "") -> None:
        """Launch a Drone (read-only or write-capable)."""
        if self._workspace_root is None:
            return

        if folder and Path(folder).is_dir():
            drone = DroneStore.load_drone_from_folder(Path(folder))
        else:
            drone = DroneStore.load_drone(self._workspace_root, drone_id)
        if drone is None:
            return
        self._start_drone_run(drone)

    def _start_drone_run(self, drone: DroneDefinition, summon_goal: str = "") -> None:
        """Start a Drone run from a saved Drone or an Aura-summoned goal."""
        if self._workspace_root is None:
            return
        run_drone = self._drone_for_summoned_goal(drone, summon_goal) if summon_goal else drone
        if not self._can_start_drone(run_drone):
            return

        run_card = DroneRunCard(run_drone, parent=self._drone_reports_window)
        thread = QThread(self)
        runner = DroneRunner(
            workspace_root=self._workspace_root,
            drone=run_drone,
            provider_id=self._settings.worker_provider,
            model=self.current_worker_model(),
            auto_approve=self._settings.auto_approve,
            parent=None,
        )
        runner.moveToThread(thread)
        run_id = runner.run_state.run_id
        self._drone_runs[run_id] = {
            "runner": runner,
            "thread": thread,
            "card": run_card,
            "drone": run_drone,
        }
        if run_drone.write_policy != "read_only":
            self._write_drone_run_id = run_id
        self._drone_runner = runner
        self._companion.set_drone_runner(self._drone_runner)
        self._drone_runner_thread = thread
        self._active_run_card = run_card
        self._drone_reports_window.add_run_card(run_id, run_card)

        # Connect signals.
        runner.statusChanged.connect(run_card.on_status_changed)
        runner.statusChanged.connect(
            lambda status, rid=run_id, name=run_drone.name: self._on_drone_status_changed(rid, name, status)
        )
        runner.contentDelta.connect(run_card.on_content_delta)
        runner.toolCallStart.connect(run_card.on_tool_call_start)
        runner.toolCallArgsDelta.connect(run_card.on_tool_call_args)
        runner.toolResult.connect(run_card.on_tool_result)
        runner.apiError.connect(run_card.on_api_error)
        runner.receiptReady.connect(run_card.on_receipt_ready)
        runner.receiptReady.connect(self._on_drone_receipt)
        runner.finished.connect(lambda rid=run_id: self._on_drone_finished(rid))

        # Standard Qt worker lifetime cleanup — no blocking wait on GUI thread.
        # runner.finished fires from the worker thread:
        #   • thread.quit is queued to the main-thread QThread object → tells the
        #     worker event loop to exit cleanly.
        #   • runner.deleteLater is a direct connection (runner lives on the worker
        #     thread, signal emitted from the worker thread) → DeferredDelete posted
        #     to the worker event queue, processed before the thread exits.
        # thread.finished fires after the worker event loop exits → thread.deleteLater
        # is queued to the main thread and handled safely.
        runner.finished.connect(thread.quit)
        runner.finished.connect(runner.deleteLater)
        thread.finished.connect(thread.deleteLater)

        # Wire approval for write-capable drones.
        if run_drone.write_policy != "read_only":
            runner.approval_requested.connect(
                lambda request, r=runner, rid=run_id, name=run_drone.name: (
                    self._on_drone_approval_requested(request, r, rid, name)
                )
            )

        # Wire cancel button.
        run_card.cancelRequested.connect(lambda rid=run_id: self._on_cancel_drone_run(rid))

        self._edge_rail.add_drone_run_pip(run_id, run_drone.name)
        self._position_edge_tabs()

        # Start the thread.
        thread.started.connect(runner.run)
        thread.start()

    def _can_start_drone(self, drone: DroneDefinition) -> bool:
        active_runs = []
        for record in self._drone_runs.values():
            try:
                if record["runner"].run_state.is_active:
                    active_runs.append(record)
            except RuntimeError:
                # C++ QObject already freed (deleteLater on worker thread fired
                # before _on_drone_finished ran on main thread).  Treat as finished.
                logger.debug("[DroneRun] _can_start_drone: runner C++ object already deleted")

        has_write_active = any(
            record["drone"].write_policy != "read_only" for record in active_runs
        )
        if drone.write_policy != "read_only":
            if active_runs:
                QMessageBox.information(
                    self,
                    "Drone Bay",
                    "Write-capable Drones use the shared write lane. Wait for active Drone runs to finish first.",
                )
                return False
            return True
        if has_write_active:
            QMessageBox.information(
                self,
                "Drone Bay",
                "A write-capable Drone is active. Read-only parallel Drones can start after it finishes.",
            )
            return False
        if len(active_runs) >= MAX_PARALLEL_READ_ONLY_DRONES:
            QMessageBox.information(
                self,
                "Drone Bay",
                f"Up to {MAX_PARALLEL_READ_ONLY_DRONES} read-only Drones can run at once.",
            )
            return False
        return True

    @staticmethod
    def _drone_for_summoned_goal(drone: DroneDefinition, goal: str) -> DroneDefinition:
        instructions = (
            f"{drone.instructions.rstrip()}\n\n"
            "This run was summoned by Aura for a specific goal:\n"
            f"{goal.strip()}"
        )
        return DroneDefinition(
            id=drone.id,
            name=drone.name,
            description=goal.strip() or drone.description,
            instructions=instructions,
            write_policy=drone.write_policy,
            output_contract=drone.output_contract,
            input_contract=drone.input_contract,
            cargo_contract=drone.cargo_contract,
            budget=drone.budget,
            scope=drone.scope,
            enabled=drone.enabled,
            created_by=drone.created_by,
            created_at=drone.created_at,
            updated_at=drone.updated_at,
            runtime=drone.runtime,
            entrypoint=drone.entrypoint,
            permissions=drone.permissions,
            secrets=drone.secrets,
            dependencies=drone.dependencies,
            manifest_version=drone.manifest_version,
        )

    def _handle_summon_drone_result(self, tool_id: str, extras: dict) -> None:
        if self._workspace_root is None:
            return
        drone_id = str(extras.get("drone_id") or "").strip()
        goal = str(extras.get("goal") or "").strip()
        reason = str(extras.get("reason") or "").strip()
        if not drone_id:
            return
        drone = DroneStore.load_drone(self._workspace_root, drone_id)
        if drone is None:
            return

        if self._settings.auto_summon_drones:
            self._start_drone_run(drone, summon_goal=goal)
            return

        request_id = tool_id or drone_id
        self._pending_drone_summons[request_id] = {
            "drone_id": drone_id,
            "goal": goal,
            "reason": reason,
        }

        card = DroneSummonCard(
            request_id=request_id,
            drone=drone,
            goal=goal or drone.description,
            reason=reason,
            parent=self._playground,
        )
        card.summonRequested.connect(self._on_confirm_summon_drone)
        card.cancelRequested.connect(self._on_cancel_summon_drone)
        self._active_run_card = card
        self._playground.switch_to_workspace()
        self._sync_drone_tab_checked()
        self._playground.add_run_card(f"summon:{request_id}", card)

    def _on_confirm_summon_drone(self, request_id: str) -> None:
        if self._workspace_root is None:
            return
        request = self._pending_drone_summons.pop(request_id, None)
        if request is None:
            return
        self._playground.remove_run_card(f"summon:{request_id}")
        drone = DroneStore.load_drone(self._workspace_root, request["drone_id"])
        if drone is None:
            return
        self._start_drone_run(drone, summon_goal=request.get("goal", ""))

    def _on_cancel_summon_drone(self, request_id: str) -> None:
        self._pending_drone_summons.pop(request_id, None)
        self._playground.remove_run_card(f"summon:{request_id}")
        self._active_run_card = None

    def _on_cancel_drone(self) -> None:
        """Request cancellation of the active drone run."""
        if self._drone_runner is not None:
            self._drone_runner.cancel()

    def _on_cancel_drone_run(self, run_id: str) -> None:
        """Cancel a running Drone — idempotent, does not delete any state.

        Only sets the cancel event and updates the card UI.
        Cleanup happens later when runner emits finished.
        """
        record = self._drone_runs.get(run_id)
        if record is None:
            return
        record["runner"].cancel()
        self._drone_reports_window.mark_cancelling(run_id)

    def _on_drone_status_changed(self, run_id: str, drone_name: str, status: str) -> None:
        self._edge_rail.set_drone_run_pip_state(run_id, drone_name, status)

    def _remove_drone_run_pip(self, run_id: str) -> None:
        logger.debug("[DroneRun] remove_drone_run_pip run_id=%s", run_id)
        self._edge_rail.remove_drone_run_pip(run_id)
        self._position_edge_tabs()

    def _on_drone_finished(self, run_id: str) -> None:
        """UI/bookkeeping cleanup after a drone run.

        Thread and object lifetime are managed entirely by the signal connections
        wired in _start_drone_run (runner.finished→thread.quit, runner.finished→
        runner.deleteLater, thread.finished→thread.deleteLater).  Do NOT touch
        thread or runner here — the runner C++ object may already have been freed
        by its direct deleteLater connection on the worker thread before this slot
        runs on the main thread.
        """
        record = self._drone_runs.pop(run_id, None)
        if record is None:
            logger.debug("[DroneRun] _on_drone_finished: unknown run_id=%s (already cleaned up?)", run_id)
            return
        runner = record["runner"]
        drone = record["drone"]
        logger.debug("[DroneRun] _on_drone_finished start run_id=%s", run_id)
        logger.debug("[DroneRun] finished  run_id=%s  drone=%s", run_id, drone.name)
        # Pip state already reflects the final status via statusChanged signal;
        # schedule timed removal so the user can see the final badge briefly.
        QTimer.singleShot(15000, lambda rid=run_id: self._remove_drone_run_pip(rid))
        if self._write_drone_run_id == run_id:
            self._write_drone_run_id = None
        if self._drone_runner is runner:
            self._drone_runner = None
            self._companion.set_drone_runner(None)
            self._drone_runner_thread = None
        logger.debug("[DroneRun] _on_drone_finished end run_id=%s", run_id)

    def _on_drone_receipt(self, receipt: object) -> None:
        """Handle completed drone receipt — save to disk."""
        if not isinstance(receipt, DroneReceipt):
            return
        self._drone_receipt = receipt
        if self._workspace_root is not None:
            RunHistoryStore.save_run(self._workspace_root, receipt)

    def _on_view_drone_receipt(self, run_id: str) -> None:
        """Open a read-only run card for a saved receipt."""
        workspace_root = self._workspace_root
        if workspace_root is None:
            return

        receipt = RunHistoryStore.load_run(workspace_root, run_id)
        if not receipt:
            return

        # Build a minimal DroneDefinition from the receipt
        minimal_drone = DroneDefinition(
            id="history:" + run_id,
            name=receipt.drone_name,
            description="",
            instructions="",
            write_policy="read_only",
            allowed_tools=(),
            output_contract={},
        )

        run_card = DroneRunCard(minimal_drone, parent=self._drone_reports_window, readonly=True)
        run_card.populate_from_receipt(receipt)

        self._active_run_card = run_card
        card_id = f"receipt:{run_id}"
        self._drone_reports_window.add_run_card(card_id, run_card)

        self._drone_reports_window.show_and_focus(card_id)

    def _on_focus_drone_run(self, run_id: str = "") -> None:
        """Open Drone Reports and focus the requested run card."""
        if run_id:
            self._drone_reports_window.show_and_focus(run_id)
            return
        if self._drone_runs:
            self._drone_reports_window.show_and_raise()

    def _on_drone_approval_requested(
        self,
        request: ApprovalRequest,
        runner: DroneRunner | None = None,
        run_id: str = "",
        drone_name: str = "",
    ) -> None:
        """Show approval dialog for a write operation requested by a Drone."""
        runner = runner or self._drone_runner
        if runner is None:
            return
        record = self._drone_runs.get(run_id) if run_id else None
        run_card = record.get("card") if record else None
        if isinstance(run_card, DroneRunCard):
            run_card.on_status_changed("waiting for approval")
        if run_id and drone_name:
            self._edge_rail.set_drone_run_pip_state(run_id, drone_name, "waiting for approval")
            self._drone_reports_window.show_and_focus(run_id)
        approval_id = request.approval_id or None

        # Build the diff text.
        if request.is_new_file:
            diff_text = f"[New file] {request.rel_path}\n\n{request.new_content}"
        else:
            diff_lines = list(difflib.unified_diff(
                request.old_content.splitlines(keepends=True),
                request.new_content.splitlines(keepends=True),
                fromfile=request.rel_path,
                tofile=request.rel_path,
            ))
            diff_text = "".join(diff_lines) if diff_lines else "(no changes)"

        dialog = QDialog(self._playground)
        dialog.setWindowTitle(f"Drone: {request.tool_name}")
        dialog.resize(600, 400)

        layout = QVBoxLayout(dialog)

        info = QLabel(f"<b>Tool:</b> {request.tool_name} | <b>File:</b> {request.rel_path}")
        info.setWordWrap(True)
        layout.addWidget(info)

        diff_view = QPlainTextEdit()
        diff_view.setPlainText(diff_text)
        diff_view.setReadOnly(True)
        layout.addWidget(diff_view, stretch=1)

        button_box = QDialogButtonBox(dialog)
        approve_btn = button_box.addButton("Approve", QDialogButtonBox.ButtonRole.AcceptRole)
        reject_btn = button_box.addButton("Reject", QDialogButtonBox.ButtonRole.RejectRole)
        approve_all_btn = button_box.addButton("Approve All", QDialogButtonBox.ButtonRole.AcceptRole)
        reject_all_btn = button_box.addButton("Reject All", QDialogButtonBox.ButtonRole.RejectRole)

        button_box.clicked.connect(lambda btn: self._on_drone_approval_button_clicked(
            dialog, runner, btn, approval_id, approve_btn, reject_btn, approve_all_btn, reject_all_btn
        ))

        layout.addWidget(button_box)

        # Ensure worker thread unblocks even if dialog is closed via X.
        dialog.rejected.connect(lambda: runner.set_approval_result(
            ApprovalDecision(action="reject"),
            approval_id=approval_id,
        ))

        dialog.exec()
        if (
            run_id
            and drone_name
            and runner.run_state.is_active
            and not runner.run_state.cancel_event.is_set()
        ):
            if isinstance(run_card, DroneRunCard):
                run_card.on_status_changed("running")
            self._edge_rail.set_drone_run_pip_state(run_id, drone_name, "running")

    def _on_drone_approval_button_clicked(
        self, dialog: QDialog, runner, btn, approval_id,
        approve_btn, reject_btn, approve_all_btn, reject_all_btn
    ) -> None:
        if btn == approve_btn:
            decision = ApprovalDecision(action="approve")
        elif btn == reject_btn:
            decision = ApprovalDecision(action="reject")
        elif btn == approve_all_btn:
            decision = ApprovalDecision(action="approve_all")
        elif btn == reject_all_btn:
            decision = ApprovalDecision(action="reject_all")
        else:
            decision = ApprovalDecision(action="reject")

        runner.set_approval_result(decision, approval_id=approval_id)
        dialog.accept()

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

    def _on_auto_summon_drones_toggled(self, checked: bool) -> None:
        self._settings.auto_summon_drones = checked
        self._toolbar.set_auto_summon_drones(checked)
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
        self._companion.set_current_conversation("")

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

    def _apply_settings(self, settings: AppSettings) -> None:
        self._settings = settings
        self._send_handler.update_settings(settings)
        self._companion.update_settings(settings)
        self._persistence.update_settings(settings)
        self._worker_handler.update_settings(settings)
        self._toolbar.update_settings(settings)

        self._left_pane.populate_models(
            settings.planner_provider,
            settings.worker_provider,
        )
        self._bridge.set_planner_provider(settings.planner_provider)
        self._bridge.set_worker_provider(settings.worker_provider)

        if settings.planner_worker_mode:
            self.set_model(settings.default_planner_model)
            self.set_thinking(settings.default_planner_thinking)
        else:
            self.set_model(settings.default_model)
            self.set_thinking(settings.default_thinking)
        self.set_worker_model(settings.default_worker_model)
        self.set_worker_thinking(settings.default_worker_thinking)
        self._set_sidebar_planner_worker_mode(settings.planner_worker_mode)
        self._apply_planner_worker_mode_to_bridge(settings.planner_worker_mode)
        self._bridge.set_worker_model(settings.default_worker_model)
        self._bridge.set_worker_thinking(settings.default_worker_thinking)
        self._bridge.set_temperature(settings.temperature)
        self._bridge.set_worker_temperature(settings.worker_temperature)
        self._bridge.set_custom_system_prompts(
            settings.system_prompt,
            settings.planner_system_prompt,
            settings.worker_system_prompt,
        )
        self._bridge.set_auto_dispatch(settings.auto_dispatch)
        self._bridge.set_auto_approve(settings.auto_approve)
        self._toolbar.set_auto_dispatch(settings.auto_dispatch)
        self._toolbar.set_auto_approve(settings.auto_approve)
        self._toolbar.set_auto_summon_drones(settings.auto_summon_drones)
        self._refresh_status_bar()

    def _on_open_settings(self) -> None:
        dlg = SettingsDialog(
            settings=self._settings,
            workspace_root=self._workspace_root,
            on_change_root=self._on_change_root,
            parent=self,
            on_live_settings_applied=self._apply_settings,
        )
        dlg.set_companion_manager(self._companion)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            self._apply_settings(dlg.result_settings())

    def open_api_settings(self) -> None:
        """Open settings dialog directly to the API Keys tab."""
        dlg = SettingsDialog(
            settings=self._settings,
            workspace_root=self._workspace_root,
            on_change_root=self._on_change_root,
            parent=self,
            open_api_keys_tab=True,
            on_live_settings_applied=self._apply_settings,
        )
        dlg.set_companion_manager(self._companion)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            self._apply_settings(dlg.result_settings())

    def _on_companion_status(self, status: str) -> None:
        """Handle companion connection status changes."""
        logger.info("[MainWindow] Companion status: %s", status)
        # Phase 2: update status bar indicator

    def _on_companion_message(self, msg: dict) -> None:
        """Handle an incoming companion message."""
        logger.debug("[MainWindow] Companion msg: %s", msg.get("type"))

    def _on_companion_thread_selected(self, project_root: Path, conversation_path: Path) -> None:
        if self._bridge.is_running():
            self._companion.complete_conversation_select(
                False, "Desktop is busy — wait for the current response to finish, or click Stop."
            )
            return
        if self._workspace_root is not None and self._workspace_root.resolve() != project_root.resolve():
            self._on_project_selected(project_root, restore_last=False)
        try:
            self._persistence.load_and_apply(conversation_path)
            self._send_handler.clear_queue()
            self._input.set_queued_messages(0)
            self._reset_session_usage()
            self._companion.complete_conversation_select(True)
        except Exception as _err:
            QMessageBox.warning(self, APP_NAME, f"Could not open conversation:\n{_err}")
            self._companion.complete_conversation_select(False, str(_err))

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
        # Switch from Drone Bay to workspace so the user sees the run —
        # but do NOT switch away from the Chain Editor (Workflow Studio).
        if not (self._drone_workbay_window and self._drone_workbay_window.is_open()):
            self._playground.switch_to_workspace()
        self._sync_drone_tab_checked()

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

        # Check for pending handoff after the response completes (no tool calls)
        if self._pending_handoff and not tool_calls:
            self._pending_handoff = False
            handoff_text = extract_handoff_text(full_message)
            if not handoff_text.strip():
                self._chat.add_error(
                    "Handoff",
                    "Handoff response was empty. Please try again.",
                )
                return
            if self._workspace_root is None:
                self._chat.add_error(
                    "Handoff",
                    "No workspace root set. Cannot save handoff.",
                )
                return
            try:
                save_handoff(self._workspace_root, handoff_text)
            except Exception as exc:
                self._chat.add_error(
                    "Handoff",
                    f"Could not save handoff: {exc}",
                )
                return
            # Start a fresh conversation
            self._persistence.new_conversation()
            self._send_handler.clear_queue()
            self._input.set_queued_messages(0)
            self._reset_session_usage()
            # Add handoff to bridge history as prior context (no API call)
            self._bridge.history.append_user_text(
                f"[Handoff from previous conversation — use as context for the next user request]\n\n{handoff_text}"
            )
            # Show local-only assistant message
            self._chat.begin_assistant()
            self._chat.append_content("Context loaded. What do you need?")
            self._chat.assistant_done()

    def _on_tool_result(self, tool_id: str, name: str, ok: bool, result: str, extras: dict) -> None:
        self._chat.set_tool_result(tool_id, ok, result)

        if ok and name == "summon_drone" and extras.get("summon_drone"):
            self._handle_summon_drone_result(tool_id, extras)

        # Normal Drone Bay refresh for successful folder registrations.
        if ok and name == "register_drone_folder" and extras.get("drone_saved"):
            self._refresh_drone_context()
            if self._drone_workbay_window is not None and self._drone_workbay_window.isVisible():
                self._drone_workbay_window.chain_editor.refresh_roster()
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
            self._refresh_drone_context()

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
        self._pending_handoff = False
        title = f"API Error {status}" if status > 0 else "Error"
        self._chat.add_error(title, message, show_retry=True)
        self._chat.stop_current_aura()

    def _on_handoff_requested(self) -> None:
        """Handle Continue in Fresh Chat button click."""
        if self._bridge.is_running():
            QMessageBox.information(
                self,
                APP_NAME,
                "Please wait for the current response to finish, or click Stop before generating a handoff.",
            )
            return
        if not self._workspace_root or not self._workspace_root.exists():
            QMessageBox.information(
                self,
                APP_NAME,
                "Set a workspace root before generating a handoff.",
            )
            return

        # Build the handoff generation prompt
        prompt_text = generate_handoff_prompt()
        payload = SendPayload(text=prompt_text, attachments=[])
        self._pending_handoff = True
        self._send_handler.handle_send(payload, self.current_model(), self.current_thinking())

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
        if project_id:
            self._companion.set_current_project(project_id)
        self._companion.set_current_conversation(thread_id or "")

    def _on_project_thread_updated(self) -> None:
        self._left_pane.refresh_projects(self._workspace_root)
