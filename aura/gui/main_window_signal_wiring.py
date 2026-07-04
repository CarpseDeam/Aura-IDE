"""Signal wiring for MainWindow — extracted to reduce __init__ density."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt

if TYPE_CHECKING:
    from aura.gui.main_window import MainWindow


class MainWindowSignalWiring:
    """Receives an already-constructed MainWindow and wires signal connections.

    Responsibilities (and only these):
    * One public method: wire()
    * Performs the exact same sender-to-receiver .connect() setup as before
    * Preserves connection order within each group (groups listed below)

    Non-responsibilities:
    * Does NOT create widgets
    * Does NOT own workflow state
    * Does NOT subscribe to EventBus
    * Does NOT dispatch Worker/Planner work
    * Does NOT add lifecycle hooks
    * Does NOT change runtime behavior
    """

    def __init__(self, window: MainWindow) -> None:
        self._w = window

    def wire(self) -> None:
        w = self._w

        # ---- group 1: toolbar -> controllers/handlers ----
        w._toolbar.new_conversation_requested.connect(w._on_new_conversation)
        w._toolbar.open_conversation_requested.connect(w._on_open_conversation)
        w._toolbar.read_only_toggled.connect(w._on_read_only_toggled)
        w._toolbar.auto_dispatch_toggled.connect(w._settings_controller.on_auto_dispatch_toggled)
        w._toolbar.auto_approve_toggled.connect(w._settings_controller.on_auto_approve_toggled)
        w._toolbar.auto_summon_drones_toggled.connect(w._settings_controller.on_auto_summon_drones_toggled)
        w._toolbar.update_requested.connect(w._on_open_update)
        w._toolbar.settings_requested.connect(w._settings_controller.open_settings)
        w._toolbar.logs_requested.connect(w._open_logs_folder)
        w._toolbar.debug_report_requested.connect(w._debug_report_handler.on_send_debug_report)
        w._toolbar.minimize_requested.connect(w.showMinimized)
        w._toolbar.maximize_requested.connect(w._toggle_maximize)
        w._toolbar.close_requested.connect(w.close)

        # ---- group 2: balance + status bar ----
        w._balance_controller.balance_changed.connect(w._refresh_status_bar)
        w._status_bar.credits_chip_clicked.connect(w._settings_controller.open_credits_popout)

        # ---- group 3: left pane ----
        w._left_pane.change_root_requested.connect(w._workspace_controller.on_change_root)
        w._left_pane.project_selected.connect(w._workspace_controller._on_project_selected)
        w._left_pane.new_project_requested.connect(w._workspace_controller.on_create_new_project)
        w._left_pane.planner_model_changed.connect(lambda: w._refresh_status_bar())
        w._left_pane.planner_thinking_changed.connect(lambda: w._refresh_status_bar())
        w._left_pane.worker_model_changed.connect(w._on_sidebar_worker_model_changed)
        w._left_pane.worker_thinking_changed.connect(w._on_sidebar_worker_thinking_changed)
        w._left_pane.drone_selected.connect(lambda folder: w._drone_controller.on_drone_folder_selected(folder.name))
        w._left_pane.new_drone_requested.connect(w._drone_controller.on_create_drone)

        # ---- group 4: launchpad ----
        w._launchpad.open_existing_requested.connect(w._workspace_controller.on_open_existing)
        w._launchpad.create_new_requested.connect(w._workspace_controller.on_create_new_project)
        w._launchpad.create_demo_requested.connect(w._workspace_controller.on_create_demo_project)

        # ---- group 5: chat ----
        w._chat.droneRunFocusRequested.connect(w._drone_controller.on_focus_drone_run)

        # ---- group 6: send handler ----
        w._send_handler.drone_bay_requested.connect(w._drone_controller.on_drone_bay_requested)

        # ---- group 7: drone reports window ----
        w._drone_reports_window.geometry_saved.connect(w._terminal_controller._on_drone_reports_geometry_saved)
        w._drone_reports_window.visibility_changed.connect(lambda _visible: w._drone_controller.sync_drone_tab_checked())
        w._send_handler.answer_only_research_started.connect(w._prepare_answer_only_research_ui)

        # ---- group 8: MainWindow drone signals ----
        w.droneRunFinishedOnUiThread.connect(w._drone_controller.on_drone_finished, Qt.ConnectionType.QueuedConnection)
        w.droneStatusChangedOnUiThread.connect(w._drone_controller.on_drone_status_changed)
        w.droneReceiptReadyOnUiThread.connect(w._drone_controller.on_drone_receipt)

        # ---- group 9: worker handler + playground ----
        w._worker_handler.usage_updated.connect(w._refresh_status_bar)
        w._worker_handler.usage_updated.connect(lambda: w._balance_controller.refresh(w._settings))
        w._worker_handler.worker_started.connect(lambda: w._input.set_streaming(False))
        w._playground.stop_worker_requested.connect(w._bridge.request_cancel)
        w._worker_handler.worker_running_changed.connect(w._playground.set_worker_running)

        # ---- group 10: persistence ----
        w._persistence.needs_status_refresh.connect(w._refresh_status_bar)

        # ---- group 11: edge rail ----
        w._edge_rail.terminalTabToggled.connect(w._terminal_controller._on_terminal_toggle)
        checkpoint_tab = w._edge_rail.checkpoint_tab
        if checkpoint_tab is not None:
            checkpoint_tab.clicked.connect(lambda: w._on_open_checkpoints())
        w._edge_rail.droneBayRequested.connect(w._drone_controller.on_drone_bay_requested)
        w._edge_rail.droneRunFocusRequested.connect(w._drone_controller.on_focus_drone_run)
        w._edge_rail.companionRequested.connect(w._on_open_companion_popout)

        # ---- group 12: bridge ↔ view ----
        w._bridge.started.connect(w._on_started)
        w._bridge.finished.connect(w._on_finished)
        w._bridge.reasoningDelta.connect(w._chat.append_reasoning)
        w._bridge.contentDelta.connect(w._chat.append_content)
        w._bridge.toolCallStart.connect(w._chat.add_tool_call)
        w._bridge.toolCallArgs.connect(w._chat.append_tool_args)
        w._bridge.toolCallEnd.connect(lambda _id: None)
        w._bridge.toolResult.connect(w._on_tool_result)
        w._bridge.diffDecided.connect(w._on_diff_decided)
        w._bridge.streamDone.connect(w._on_stream_done)
        w._bridge.apiError.connect(w._on_api_error)
        w._bridge.usageWithModel.connect(w._on_usage)
        w._chat.retry_requested.connect(w._on_retry)

        # ---- group 13: input panel ----
        w._input.sent.connect(lambda p: w._send_handler.handle_send(p, w.current_model(), w.current_thinking()))
        w._input.stop_requested.connect(w._send_handler.handle_stop)
        w._input.handoff_requested.connect(w._on_handoff_requested)

        # ---- group 14: tree + playground ----
        w._tree.file_activated.connect(w._playground.open_file)
        w._playground.focused_action_requested.connect(w._on_focused_action_requested)

        # ---- group 15: terminal window ----
        terminal_window = w._playground.terminal_window()
        terminal_window.terminal_started.connect(w._terminal_controller._on_terminal_started)
        terminal_window.terminal_finished.connect(w._terminal_controller._on_terminal_finished)
        terminal_window.visibility_changed.connect(w._terminal_controller._on_terminal_visibility_changed)
        terminal_window.terminal_cleared.connect(w._terminal_controller._on_terminal_cleared)
        terminal_window.geometry_saved.connect(w._terminal_controller._on_terminal_geometry_saved)

        # ---- group 16: mermaid detection ----
        w._chat.mermaid_detected.connect(w._playground.add_mermaid_artifact)

        # ---- group 17: left pane thread + persistence ----
        w._left_pane.thread_selected.connect(w._on_thread_selected)
        w._persistence.project_thread_updated.connect(w._on_project_thread_updated)
        w._persistence.current_context_changed.connect(w._on_current_context_changed)
