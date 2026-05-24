"""Worker lifecycle event handler — receives bridge worker signals and
forwards them to chat/playground UI components.

Owns its own session usage tracking dict and emits signals so that
MainWindow can react to state changes (status bar refresh, input streaming).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from aura.bridge.qt_bridge import ConversationBridge
    from aura.config import AppSettings
    from aura.gui.chat_view import ChatView
    from aura.gui.playground import AuraPlayground


class WorkerEventHandler(QObject):
    """Owns worker signal wiring and forwards bridge worker events to the
    chat view and playground.

    Attributes:
        usage_updated: Emitted when ``_session_usage`` changes so that
            MainWindow can refresh the status bar.
        worker_started: Emitted at the end of ``_on_worker_started`` so that
            MainWindow can set input streaming state.
    """

    usage_updated = Signal()
    worker_started = Signal()

    def __init__(
        self,
        bridge: ConversationBridge,
        chat: ChatView,
        playground: AuraPlayground,
        settings: AppSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._chat = chat
        self._playground = playground
        self._settings = settings
        self._session_usage: dict[str, dict[str, int]] = {}

    # ---- public property -------------------------------------------------------

    @property
    def session_usage(self) -> dict[str, dict[str, int]]:
        """Read-only access to the per-model usage accumulator."""
        return self._session_usage

    # ---- public methods --------------------------------------------------------

    def reset_session_usage(self) -> None:
        """Clear the usage accumulator and notify listeners."""
        self._session_usage.clear()
        self.usage_updated.emit()

    def update_settings(self, settings: AppSettings) -> None:
        """Use the latest settings object after Settings is accepted."""
        self._settings = settings

    def connect_bridge_signals(self) -> None:
        """Wire all bridge worker signals to the corresponding handler slots.

        Also connects ``bridge.terminalOutput`` for single-mode terminal output.
        """
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
        self._bridge.workerAgentProcessStarted.connect(self._on_worker_agent_process_started)
        self._bridge.workerAgentProcessOutput.connect(self._on_worker_agent_process_output)
        self._bridge.workerAgentProcessFinished.connect(self._on_worker_agent_process_finished)
        self._bridge.terminalOutput.connect(self._on_terminal_output)

    # ---- dispatch slots --------------------------------------------------------

    def _on_worker_dispatch_requested(
        self,
        tool_call_id: str,
        goal: str,
        files: list,
        spec: str,
        acceptance: str,
        summary: str,
    ) -> None:
        """Always show the SpecCard; auto-dispatch or wait for card interaction."""
        file_list = list(files)
        card = self._chat.add_spec_card(tool_call_id, goal, file_list, spec, acceptance, summary)
        card.dispatch_clicked.connect(self._on_dispatch_clicked)
        card.edit_clicked.connect(self._on_edit_spec_clicked)
        card.cancel_clicked.connect(self._on_cancel_dispatch_clicked)
        card.view_worker_clicked.connect(self._on_view_worker_clicked)

        if self._bridge.auto_dispatch:
            card.mark_dispatched()
            self._bridge.user_dispatched(tool_call_id, goal, file_list, spec, acceptance, summary)
            return

    def _on_dispatch_clicked(self, tool_call_id: str) -> None:
        """Dispatch the spec card's current values directly."""
        card = self._chat.get_spec_card(tool_call_id)
        if card is None:
            return
        goal, files, spec, acceptance, summary = card.current_spec()
        accepted = self._bridge.user_dispatched(tool_call_id, goal, files, spec, acceptance, summary)
        if not accepted:
            card.mark_stale()

    def _on_edit_spec_clicked(self, tool_call_id: str) -> None:
        """Open the SpecEditDialog pre-populated with the spec card's values."""
        from aura.gui.spec_edit_dialog import SpecEditDialog

        card = self._chat.get_spec_card(tool_call_id)
        if card is None:
            return
        goal, files, spec, acceptance, summary = card.current_spec()
        dlg = SpecEditDialog(goal, files, spec, acceptance, summary, parent=self.parent())
        if dlg.exec() == SpecEditDialog.DialogCode.Accepted:
            card.update_spec(dlg.goal(), dlg.files(), dlg.spec(), dlg.acceptance(), dlg.summary())

    def _on_cancel_dispatch_clicked(self, tool_call_id: str) -> None:
        """Cancel the pending dispatch."""
        accepted = self._bridge.user_cancelled_dispatch(tool_call_id)
        if not accepted:
            card = self._chat.get_spec_card(tool_call_id)
            if card:
                card.mark_stale()

    # ---- worker lifecycle slots ------------------------------------------------

    def _on_worker_started(self, tool_call_id: str) -> None:
        """Stop the planner aura and start the playground's assistant aura."""
        self._chat.stop_current_aura()
        self._playground.set_glow_state("coding")
        self._playground.begin_assistant()
        self.worker_started.emit()

        card = self._chat.get_spec_card(tool_call_id)
        if card:
            card.mark_worker_running()

    def _on_worker_finished(self, tool_call_id: str, ok: bool, summary: str) -> None:
        """Forward worker finished to playground and update spec card."""
        self._playground.stop_aura()
        self._playground.worker_finished(ok, summary)
        
        card = self._chat.get_spec_card(tool_call_id)
        if card:
            card.worker_finished(ok, summary)

    def _on_worker_cancelled(self, tool_call_id: str) -> None:
        """Stop worker aura and forward cancel to playground/spec card."""
        self._playground.stop_aura()
        self._playground.worker_cancelled()

        card = self._chat.get_spec_card(tool_call_id)
        if card:
            card.worker_cancelled()

    # ---- worker content slots --------------------------------------------------

    def _on_worker_reasoning(self, tool_call_id: str, text: str) -> None:
        """Forward reasoning delta to playground."""
        self._playground.append_reasoning(text)

    def _on_worker_content(self, tool_call_id: str, text: str) -> None:
        """Forward content delta to playground."""
        self._playground.append_content(text)

    # ---- worker tool call slots ------------------------------------------------

    def _on_worker_tool_call_start(
        self, tool_call_id: str, worker_tool_id: str, name: str
    ) -> None:
        """Forward tool call start to playground."""
        self._playground.add_tool_call(worker_tool_id, name)

    def _on_worker_tool_args(
        self, tool_call_id: str, worker_tool_id: str, fragment: str
    ) -> None:
        """Forward tool call args delta to playground."""
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
        """Forward tool result to playground."""
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
        """Forward diff decision to playground."""
        self._playground.show_code_diff(worker_tool_id, rel_path, old, new, decision)

    def _on_worker_api_error(self, tool_call_id: str, status: int, message: str) -> None:
        """Forward API error to playground with a formatted title."""
        title = f"API Error {status}" if status > 0 else "Worker Error"
        self._playground.add_error(f"{title}: {message}")

    def _on_view_worker_clicked(self, tool_call_id: str) -> None:
        """No-op placeholder for view-worker button."""

    def _on_worker_usage(
        self,
        _tool_call_id: str,
        model_id: str,
        prompt: int,
        completion: int,
        hit: int,
        miss: int,
    ) -> None:
        """Accumulate per-model token usage and emit update signal."""
        if hit == 0 and miss == 0:
            miss = prompt
        bucket = self._session_usage.setdefault(
            model_id, {"hit": 0, "miss": 0, "out": 0}
        )
        bucket["hit"] += hit
        bucket["miss"] += miss
        bucket["out"] += completion
        self.usage_updated.emit()

    def _on_worker_todo_list_updated(self, tool_call_id: str, tasks: list) -> None:
        """Route the worker's TODO list update to the playground."""
        self._playground.update_todo_list(tasks)

    def _on_worker_terminal_output(
        self, parent_tool_id: str, worker_tool_id: str, text: str
    ) -> None:
        """Route terminal output (worker mode) to the playground."""
        self._playground.append_terminal_output(worker_tool_id, text)

    def _on_worker_agent_process_started(
        self, parent_tool_id: str, process_id: str, label: str, command: str
    ) -> None:
        """Route CLI backend process start to the playground terminal."""
        self._playground.start_terminal_process(process_id, command)

    def _on_worker_agent_process_output(
        self, parent_tool_id: str, process_id: str, text: str
    ) -> None:
        """Route CLI backend process output to the playground terminal."""
        self._playground.append_terminal_output(process_id, text)

    def _on_worker_agent_process_finished(
        self, parent_tool_id: str, process_id: str, exit_code: int
    ) -> None:
        """Route CLI backend process completion to the playground terminal."""
        self._playground.finish_terminal_process(process_id, exit_code)

    def _on_terminal_output(self, tool_call_id: str, text: str) -> None:
        """Route terminal output (single mode) to the chat view."""
        self._chat.append_terminal_output(tool_call_id, text)
