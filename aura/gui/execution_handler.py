"""Execution lifecycle event handler — receives bridge execution signals and
forwards them to chat/playground UI components.

The ``execution*`` signal names are compatibility aliases for the production
execution session's workspace projection. This handler owns the session usage
tracking dict and emits signals so that MainWindow can react to state changes
(status bar refresh, input streaming).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal

from aura.config import redact_secrets
from aura.conversation.telemetry import ConversationTelemetry, context_window_for_model

_log = logging.getLogger(__name__)

from aura.gui.execution_finish_presenter import ExecutionFinishPresenter
from aura.gui.execution_tool_event_router import ExecutionToolEventRouter

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from aura.bridge.qt_bridge import ConversationBridge
    from aura.config import AppSettings
    from aura.gui.chat_view import ChatView
    from aura.gui.playground import AuraPlayground


@dataclass(frozen=True)
class _PendingExecutionFinish:
    tool_call_id: str
    ok: bool
    summary: str
    needs_followup: bool | None
    status: str | None
    generation: int


class ExecutionEventHandler(QObject):
    """Owns execution signal wiring and forwards bridge execution events to the
    chat view and playground.

    Attributes:
        usage_updated: Emitted when conversation telemetry changes so that
            MainWindow can refresh the status bar.
        execution_started: Emitted at the end of ``_on_execution_started`` so that
            MainWindow can set input streaming state.
    """

    usage_updated = Signal()
    execution_started = Signal()
    execution_running_changed = Signal(bool)

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
        self._conversation_telemetry = ConversationTelemetry()
        self._active_execution_tool_call_id: str | None = None
        self._pending_execution_finish: _PendingExecutionFinish | None = None
        self._pending_execution_finish_generation = 0
        self._finish_presenter = ExecutionFinishPresenter(chat, playground)
        self._tool_router = ExecutionToolEventRouter(playground=playground, chat=chat)

    # ---- public property -------------------------------------------------------

    @property
    def conversation_usage(self) -> dict[str, dict[str, int]]:
        """Read-only access to the per-model usage accumulator."""
        return self._conversation_telemetry.per_model

    @property
    def conversation_telemetry(self) -> ConversationTelemetry:
        """Read-only access to all durable conversation telemetry."""
        return self._conversation_telemetry

    # ---- public methods --------------------------------------------------------

    def reset_conversation_usage(self) -> None:
        """Clear the usage accumulator and notify listeners."""
        self._conversation_telemetry = ConversationTelemetry()
        self.usage_updated.emit()

    def restore_conversation_telemetry(self, telemetry: ConversationTelemetry) -> None:
        """Replace live telemetry with a normalized persisted snapshot."""
        self._conversation_telemetry = ConversationTelemetry.from_dict(telemetry.to_dict())
        self.usage_updated.emit()

    def update_settings(self, settings: AppSettings) -> None:
        """Use the latest settings object after Settings is accepted."""
        self._settings = settings

    def connect_bridge_signals(self) -> None:
        """Wire all bridge execution signals to the corresponding handler slots.

        Also connects ``bridge.terminalOutput`` for normal terminal output.
        """
        self._bridge.executionStarted.connect(self._on_execution_started)
        self._bridge.executionFinished.connect(self._on_execution_finished)
        self._bridge.executionCancelled.connect(self._on_execution_cancelled)
        self._bridge.executionReasoningDelta.connect(self._on_execution_reasoning)
        self._bridge.executionContentDelta.connect(self._on_execution_content)
        self._bridge.executionToolCallStart.connect(self._tool_router.on_execution_tool_call_start)
        self._bridge.executionToolCallArgs.connect(self._tool_router.on_execution_tool_args)
        self._bridge.executionToolCallEnd.connect(lambda _t, _w: None)
        self._bridge.executionToolResult.connect(self._tool_router.on_execution_tool_result)
        self._bridge.executionDiffDecided.connect(self._tool_router.on_execution_diff_decided)
        self._bridge.executionFileEditLifecycle.connect(
            self._tool_router.on_execution_file_edit_lifecycle
        )
        self._bridge.executionApiError.connect(self._on_execution_api_error)
        self._bridge.executionUsage.connect(self._on_execution_usage)
        self._bridge.executionActivityUpdated.connect(self._on_execution_activity_updated)
        self._bridge.taskChecklistUpdated.connect(self._on_task_checklist_updated)
        self._bridge.executionTerminalCommandStarted.connect(
            self._tool_router.on_execution_terminal_command_started
        )
        self._bridge.executionTerminalOutput.connect(self._tool_router.on_execution_terminal_output)
        self._bridge.executionAgentProcessStarted.connect(self._tool_router.on_execution_agent_process_started)
        self._bridge.executionAgentProcessOutput.connect(self._tool_router.on_execution_agent_process_output)
        self._bridge.executionAgentProcessFinished.connect(self._tool_router.on_execution_agent_process_finished)
        self._bridge.terminalOutput.connect(self._tool_router.on_terminal_output)

    # ---- production-run helpers ------------------------------------------------

    def _is_production_run(self, tool_call_id: str) -> bool:
        """True when *tool_call_id* is the active direct production run id."""
        run_id = getattr(self._bridge, "production_run_id", "")
        return bool(tool_call_id) and tool_call_id == run_id

    def _mark_chat_working_in_workspace(self) -> None:
        """Show a lightweight in-flight hint on the current chat assistant card."""
        try:
            card = self._chat.current_assistant()
        except Exception:
            _log.debug("No assistant card to mark as working", exc_info=True)
            return
        show = getattr(card, "show_thinking_message", None)
        if callable(show):
            show("Working in the workspace")

    # ---- execution lifecycle slots ------------------------------------------------

    def _on_execution_started(self, tool_call_id: str) -> None:
        """Keep the chat aura alive and point the user at the workspace.

        The production execution session emits one executionStarted per run.
        """
        pending_finish = self._pending_execution_finish
        if (
            pending_finish is not None
            and pending_finish.tool_call_id != tool_call_id
        ):
            self._pending_execution_finish = None
            self._present_execution_finish(
                tool_call_id=pending_finish.tool_call_id,
                ok=pending_finish.ok,
                summary=pending_finish.summary,
                needs_followup=pending_finish.needs_followup,
                status=pending_finish.status,
            )

        _log.info(
            "DIAGNOSTIC _on_execution_started tool_call_id=%s active_execution_tool_call_id=%s",
            tool_call_id,
            self._active_execution_tool_call_id,
        )
        if self._active_execution_tool_call_id == tool_call_id:
            if (
                self._pending_execution_finish is not None
                and self._pending_execution_finish.tool_call_id == tool_call_id
            ):
                _log.info(
                    "execution_finish_cancelled_for_continuing_run tool_call_id=%s",
                    tool_call_id,
                )
                self._pending_execution_finish = None
            _log.info(
                "DIAGNOSTIC execution_started_duplicate_ignored — skipping begin_assistant tool_call_id=%s",
                tool_call_id,
            )
            self.execution_running_changed.emit(True)
            return

        _log.info(
            "DIAGNOSTIC execution_started_first_call — calling begin_assistant tool_call_id=%s",
            tool_call_id,
        )
        self._active_execution_tool_call_id = tool_call_id
        # Direct production execution: the run is still in flight, so keep the
        # chat aura alive and point the user at the workspace instead of
        # duplicating the transcript into the chat.
        self._mark_chat_working_in_workspace()
        self._playground.set_glow_state("coding")
        self._playground.begin_assistant()
        self.execution_started.emit()

        self.execution_running_changed.emit(True)

    def _on_execution_finished(
        self,
        tool_call_id: str,
        ok: bool,
        summary: str,
        needs_followup: bool | None = None,
        status: str | None = None,
    ) -> None:
        """Forward execution finished to playground.

        The production execution session emits one executionFinished signal per
        run.
        """
        _log.info(
            "execution_finished tool_call_id=%s status=%s",
            tool_call_id, status,
        )

        if self._active_execution_tool_call_id == tool_call_id:
            self._pending_execution_finish_generation += 1
            generation = self._pending_execution_finish_generation
            self._pending_execution_finish = _PendingExecutionFinish(
                tool_call_id=tool_call_id,
                ok=ok,
                summary=summary,
                needs_followup=needs_followup,
                status=status,
                generation=generation,
            )
            QTimer.singleShot(
                0,
                lambda: self._flush_pending_execution_finish(tool_call_id, generation),
            )
            return

        self._present_execution_finish(
            tool_call_id=tool_call_id,
            ok=ok,
            summary=summary,
            needs_followup=needs_followup,
            status=status,
        )

    def _flush_pending_execution_finish(self, tool_call_id: str, generation: int) -> None:
        pending = self._pending_execution_finish
        if (
            pending is None
            or pending.tool_call_id != tool_call_id
            or pending.generation != generation
        ):
            return
        self._pending_execution_finish = None
        self._present_execution_finish(
            tool_call_id=pending.tool_call_id,
            ok=pending.ok,
            summary=pending.summary,
            needs_followup=pending.needs_followup,
            status=pending.status,
        )

    def _present_execution_finish(
        self,
        *,
        tool_call_id: str,
        ok: bool,
        summary: str,
        needs_followup: bool | None,
        status: str | None,
    ) -> None:
        metadata = self._execution_result_metadata(tool_call_id)
        self._finish_presenter.present(
            tool_call_id=tool_call_id,
            ok=ok,
            summary=summary,
            needs_followup=needs_followup,
            status=status,
            metadata=metadata,
        )
        if self._active_execution_tool_call_id == tool_call_id:
            self._active_execution_tool_call_id = None
        self.execution_running_changed.emit(False)

    def _execution_result_metadata(self, tool_call_id: str) -> dict:
        """Read run metadata through the bridge's role-neutral accessor."""
        getter = getattr(self._bridge, "execution_result_metadata", None)
        if not callable(getter):
            return {}
        metadata = getter(tool_call_id)
        return metadata if isinstance(metadata, dict) else {}

    def _on_execution_cancelled(self, tool_call_id: str) -> None:
        """Stop execution aura and forward cancel to playground."""

        self._clear_pending_execution_finish(tool_call_id)
        self._playground.stop_aura()
        self._playground.execution_cancelled()

        if self._active_execution_tool_call_id == tool_call_id:
            self._active_execution_tool_call_id = None
        self.execution_running_changed.emit(False)

    # ---- execution content slots --------------------------------------------------

    def _on_execution_reasoning(self, tool_call_id: str, text: str) -> None:
        """Forward reasoning delta to playground."""

        self._playground.append_reasoning(text)

    def _on_execution_content(self, tool_call_id: str, text: str) -> None:
        """Forward content delta to playground."""

        self._playground.append_content(text)


    def _on_execution_api_error(self, tool_call_id: str, status: int, message: str) -> None:
        """Forward API error to playground with a formatted title."""
        _log.info(
            "api_error tool_call_id=%s status=%s message_redacted=%s",
            tool_call_id, status, redact_secrets(message)[:200],
        )
        title = f"API Error {status}" if status > 0 else "Execution Error"
        self._playground.add_error(f"{title}: {message}")
        self._playground.stop_aura()
        self._playground.set_execution_running(False)
        self._clear_pending_execution_finish(tool_call_id)
        if self._active_execution_tool_call_id == tool_call_id:
            self._active_execution_tool_call_id = None
        self.execution_running_changed.emit(False)

    def _clear_pending_execution_finish(self, tool_call_id: str) -> None:
        if (
            self._pending_execution_finish is not None
            and self._pending_execution_finish.tool_call_id == tool_call_id
        ):
            self._pending_execution_finish = None

    def _on_execution_usage(
        self,
        _tool_call_id: str,
        model_id: str,
        prompt: int,
        completion: int,
        hit: int,
        miss: int,
    ) -> None:
        """Accumulate per-model token usage and emit update signal."""

        self._conversation_telemetry.record_usage(
            model_id=model_id,
            prompt=prompt,
            completion=completion,
            hit=hit,
            miss=miss,
            context_window_tokens=context_window_for_model(model_id),
        )
        self.usage_updated.emit()

    def _on_execution_activity_updated(self, tool_call_id: str, entries: list) -> None:
        """Route Execution Activity snapshots to playground (append-only heartbeat)."""
        _log.debug(
            "_on_execution_activity_updated tool_call_id=%s entry_count=%d",
            tool_call_id, len(entries),
        )
        self._playground.update_activity(entries, tool_call_id)

    def _on_task_checklist_updated(self, tool_call_id: str, items: list) -> None:
        """Route Task Checklist snapshots to playground (full replacement lens)."""
        _log.debug(
            "_on_task_checklist_updated tool_call_id=%s item_count=%d",
            tool_call_id, len(items),
        )
        self._playground.update_task_checklist(items, tool_call_id)
