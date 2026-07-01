"""Worker lifecycle event handler — receives bridge worker signals and
forwards them to chat/playground UI components.

Owns its own session usage tracking dict and emits signals so that
MainWindow can react to state changes (status bar refresh, input streaming).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from aura.config import redact_secrets

_log = logging.getLogger(__name__)

from aura.conversation.workflow_state import WorkflowState, WorkflowStatus
from aura.gui.dispatch_ui_lifecycle import DispatchUiLifecycle
from aura.gui.worker_finish_presenter import WorkerFinishPresenter

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
    worker_running_changed = Signal(bool)

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
        self._active_workflow: WorkflowState | None = None
        self._dispatch_ui = DispatchUiLifecycle(
            bridge=bridge,
            chat=chat,
            parent_widget=parent,
            active_workflow=lambda: self._active_workflow,
            transition_workflow=self._transition_active_workflow,
        )
        self._finish_presenter = WorkerFinishPresenter(chat, playground)

    # ---- public property -------------------------------------------------------

    @property
    def session_usage(self) -> dict[str, dict[str, int]]:
        """Read-only access to the per-model usage accumulator."""
        return self._session_usage

    @property
    def active_workflow(self) -> WorkflowState | None:
        """Authoritative state for the currently active Worker task."""
        return self._active_workflow

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
        steps: list | None = None,
    ) -> None:
        """Route Planner dispatch requests through auto or manual review."""
        if self._finish_presenter.resolve_active_mismatch():
            self._chat.stop_current_aura()

        file_list = list(files)
        step_list = list(steps or [])
        if self._dispatch_ui.consume_internal_continuation(tool_call_id):
            self._set_active_workflow(
                WorkflowState.intent_captured(
                    tool_call_id, goal, summary=summary,
                ).with_status(
                    WorkflowStatus.dispatched,
                    pending_user_action="",
                )
            )
            self._bridge.user_dispatched(tool_call_id, goal, file_list, spec, acceptance, summary)
            self._chat.scroll_to_bottom(force=True)
            return

        if self._bridge.auto_dispatch:
            _log.info(
                "dispatch_auto_accepted tool_call_id=%s goal=%s",
                tool_call_id, goal[:120],
            )
            self._dispatch_ui.begin_auto_dispatch(tool_call_id)
            self._playground.begin_dispatch_todo_list(tool_call_id, step_list)
            self._set_active_workflow(
                WorkflowState.intent_captured(
                    tool_call_id,
                    goal,
                    summary=summary,
                ).with_status(
                    WorkflowStatus.dispatched,
                    pending_user_action="",
                )
            )
            self._bridge.user_dispatched(tool_call_id, goal, file_list, spec, acceptance, summary)
            self._chat.scroll_to_bottom(force=True)
            return

        _log.info(
            "dispatch_card_shown tool_call_id=%s goal=%s",
            tool_call_id, goal[:120],
        )
        self._dispatch_ui.begin_visible_dispatch(tool_call_id)
        self._playground.begin_dispatch_todo_list(tool_call_id, step_list)
        self._set_active_workflow(
            WorkflowState.intent_captured(
                tool_call_id,
                goal,
                summary=summary,
            ).with_status(
                WorkflowStatus.plan_ready,
                pending_user_action="Dispatch, edit, or cancel the plan.",
            )
        )
        self._dispatch_ui.show_spec_card(
            tool_call_id=tool_call_id,
            goal=goal,
            file_list=file_list,
            spec=spec,
            acceptance=acceptance,
            summary=summary,
            step_list=step_list,
        )

    # ---- worker lifecycle slots ------------------------------------------------

    def _on_worker_started(self, tool_call_id: str) -> None:
        """Stop the planner aura and start the playground's assistant aura."""

        self._chat.stop_current_aura()
        self._playground.set_glow_state("coding")
        self._playground.begin_assistant()
        self._playground.render_dispatch_todo_list(tool_call_id)
        self.worker_started.emit()

        self._dispatch_ui.mark_worker_started(tool_call_id)
        self._transition_active_workflow(
            tool_call_id,
            WorkflowStatus.dispatched,
            pending_user_action="",
        )
        self.worker_running_changed.emit(True)

    def _on_worker_finished(
        self,
        tool_call_id: str,
        ok: bool,
        summary: str,
        needs_followup: bool | None = None,
        status: str | None = None,
    ) -> None:
        """Forward worker finished to playground and update spec card."""
        _log.info(
            "worker_finished tool_call_id=%s status=%s",
            tool_call_id, status,
        )

        metadata = self._worker_result_metadata(tool_call_id)
        presentation = self._finish_presenter.present(
            tool_call_id=tool_call_id,
            ok=ok,
            summary=summary,
            needs_followup=needs_followup,
            status=status,
            metadata=metadata,
            active_workflow=self._active_workflow,
            spec_card=self._dispatch_ui.get_spec_card(tool_call_id),
        )
        outcome = presentation.outcome
        if (
            self._active_workflow is not None
            and self._active_workflow.tool_call_id == tool_call_id
            and not outcome.suppress_main_summary
        ):
            self._set_active_workflow(
                self._active_workflow.finish(
                    ok=ok,
                    summary=summary,
                    needs_followup=bool(needs_followup),
                    status=status,
                    modified_files=outcome.metadata.get("modified_files"),
                    validation=outcome.metadata.get("validation"),
                    extras=outcome.extras,
                )
            )
        if outcome.should_clear_dispatch_card:
            self._dispatch_ui.clear_active_spec_card(tool_call_id)
        if outcome.should_set_pending_internal_retool:
            self._dispatch_ui.mark_pending_internal_retool(tool_call_id)
        self._dispatch_ui.discard_canonical_dispatch(tool_call_id)
        self.worker_running_changed.emit(False)

    def _worker_result_metadata(self, tool_call_id: str) -> dict:
        getter = getattr(self._bridge, "worker_result_metadata", None)
        if not callable(getter):
            return {}
        metadata = getter(tool_call_id)
        return metadata if isinstance(metadata, dict) else {}

    def _on_worker_cancelled(self, tool_call_id: str) -> None:
        """Stop worker aura and forward cancel to playground/spec card."""

        self._playground.stop_aura()
        self._playground.worker_cancelled()

        self._transition_active_workflow(
            tool_call_id,
            WorkflowStatus.cancelled,
            pending_user_action="",
        )
        self._dispatch_ui.mark_worker_cancelled(tool_call_id)
        self.worker_running_changed.emit(False)

    # ---- worker content slots --------------------------------------------------

    def _on_worker_reasoning(self, tool_call_id: str, text: str) -> None:
        """Forward reasoning delta to playground."""

        if self._dispatch_ui.is_canonical_dispatch(tool_call_id):
            return
        self._playground.append_reasoning(text)

    def _on_worker_content(self, tool_call_id: str, text: str) -> None:
        """Forward content delta to playground."""

        if self._dispatch_ui.is_canonical_dispatch(tool_call_id):
            return
        self._playground.append_content(text)

    # ---- worker tool call slots ------------------------------------------------

    def _on_worker_tool_call_start(
        self, tool_call_id: str, worker_tool_id: str, name: str
    ) -> None:
        """Forward tool call start to playground."""

        self._playground.add_tool_call(worker_tool_id, name, parent_tool_id=tool_call_id)
        write_tools = {
            "write_file",
            "apply_edit_transaction",
            "edit_file",
            "edit_symbol",
            "edit_line_range",
            "patch_file",
        }
        if name in write_tools:
            self._transition_active_workflow(
                tool_call_id,
                WorkflowStatus.editing,
                pending_user_action="",
            )
        elif name == "run_terminal_command":
            self._transition_active_workflow(
                tool_call_id,
                WorkflowStatus.validating,
                pending_user_action="",
            )
        elif name == "run_and_watch":
            self._transition_active_workflow(
                tool_call_id,
                WorkflowStatus.validating,
                pending_user_action="",
            )

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
        if self._active_workflow is not None and self._active_workflow.tool_call_id == parent_tool_id:
            self._set_active_workflow(
                self._active_workflow.absorb_worker_tool_result(name, ok, result, extras)
            )

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
        if (
            decision == "approve"
            and self._active_workflow is not None
            and self._active_workflow.tool_call_id == parent_tool_id
        ):
            self._set_active_workflow(self._active_workflow.with_changed_file(rel_path))

    def _on_worker_api_error(self, tool_call_id: str, status: int, message: str) -> None:
        """Forward API error to playground with a formatted title."""
        _log.info(
            "api_error tool_call_id=%s status=%s message_redacted=%s",
            tool_call_id, status, redact_secrets(message)[:200],
        )
        title = f"API Error {status}" if status > 0 else "Worker Error"
        self._playground.add_error(f"{title}: {message}")
        self._transition_active_workflow(
            tool_call_id,
            WorkflowStatus.failed_nonrecoverable,
            failure_reason=message,
            follow_up_required=False,
            pending_user_action="Review the failure before retrying.",
        )
        self._dispatch_ui.clear_active_spec_card(tool_call_id)

    def _set_active_workflow(self, state: WorkflowState) -> None:
        self._active_workflow = state
        card = self._dispatch_ui.get_spec_card(state.tool_call_id)
        if card is not None and hasattr(card, "update_workflow_state"):
            card.update_workflow_state(state)

    def _transition_active_workflow(
        self,
        tool_call_id: str,
        status: WorkflowStatus,
        *,
        pending_user_action: str | None = None,
        blocker_reason: str | None = None,
        failure_reason: str | None = None,
        follow_up_required: bool | None = None,
    ) -> None:
        if self._active_workflow is None or self._active_workflow.tool_call_id != tool_call_id:
            return
        self._set_active_workflow(
            self._active_workflow.with_status(
                status,
                pending_user_action=pending_user_action,
                blocker_reason=blocker_reason,
                failure_reason=failure_reason,
                follow_up_required=follow_up_required,
            )
        )

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

        if self._dispatch_ui.is_canonical_dispatch(tool_call_id):
            return
        self._playground.update_todo_list(tasks, tool_call_id)

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
