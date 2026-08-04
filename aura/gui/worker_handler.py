"""Worker lifecycle event handler — receives bridge worker signals and
forwards them to chat/playground UI components.

The ``worker*`` signal names are compatibility aliases for the production
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

_log = logging.getLogger(__name__)

from aura.gui.worker_finish_presenter import WorkerFinishPresenter
from aura.gui.worker_tool_event_router import WorkerToolEventRouter

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from aura.bridge.qt_bridge import ConversationBridge
    from aura.config import AppSettings
    from aura.gui.chat_view import ChatView
    from aura.gui.playground import AuraPlayground


@dataclass(frozen=True)
class _PendingWorkerFinish:
    tool_call_id: str
    ok: bool
    summary: str
    needs_followup: bool | None
    status: str | None
    generation: int


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
        self._active_worker_tool_call_id: str | None = None
        self._pending_worker_finish: _PendingWorkerFinish | None = None
        self._pending_worker_finish_generation = 0
        self._finish_presenter = WorkerFinishPresenter(chat, playground)
        self._tool_router = WorkerToolEventRouter(playground=playground, chat=chat)

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
        self._bridge.workerStarted.connect(self._on_worker_started)
        self._bridge.workerFinished.connect(self._on_worker_finished)
        self._bridge.workerCancelled.connect(self._on_worker_cancelled)
        self._bridge.workerReasoningDelta.connect(self._on_worker_reasoning)
        self._bridge.workerContentDelta.connect(self._on_worker_content)
        self._bridge.workerToolCallStart.connect(self._tool_router.on_worker_tool_call_start)
        self._bridge.workerToolCallArgs.connect(self._tool_router.on_worker_tool_args)
        self._bridge.workerToolCallEnd.connect(lambda _t, _w: None)
        self._bridge.workerToolResult.connect(self._tool_router.on_worker_tool_result)
        self._bridge.workerDiffDecided.connect(self._tool_router.on_worker_diff_decided)
        self._bridge.workerApiError.connect(self._on_worker_api_error)
        self._bridge.workerUsage.connect(self._on_worker_usage)
        self._bridge.workerActivityUpdated.connect(self._on_worker_activity_updated)
        self._bridge.workerTodoUpdated.connect(self._on_worker_todo_updated)
        self._bridge.workerTerminalOutput.connect(self._tool_router.on_worker_terminal_output)
        self._bridge.workerAgentProcessStarted.connect(self._tool_router.on_worker_agent_process_started)
        self._bridge.workerAgentProcessOutput.connect(self._tool_router.on_worker_agent_process_output)
        self._bridge.workerAgentProcessFinished.connect(self._tool_router.on_worker_agent_process_finished)
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

    # ---- worker lifecycle slots ------------------------------------------------

    def _on_worker_started(self, tool_call_id: str) -> None:
        """Keep the chat aura alive and point the user at the workspace.

        The production execution session emits one workerStarted per run.
        """
        pending_finish = self._pending_worker_finish
        if (
            pending_finish is not None
            and pending_finish.tool_call_id != tool_call_id
        ):
            self._pending_worker_finish = None
            self._present_worker_finish(
                tool_call_id=pending_finish.tool_call_id,
                ok=pending_finish.ok,
                summary=pending_finish.summary,
                needs_followup=pending_finish.needs_followup,
                status=pending_finish.status,
            )

        _log.info(
            "DIAGNOSTIC _on_worker_started tool_call_id=%s active_worker_tool_call_id=%s",
            tool_call_id,
            self._active_worker_tool_call_id,
        )
        if self._active_worker_tool_call_id == tool_call_id:
            if (
                self._pending_worker_finish is not None
                and self._pending_worker_finish.tool_call_id == tool_call_id
            ):
                _log.info(
                    "worker_finish_cancelled_for_continuing_run tool_call_id=%s",
                    tool_call_id,
                )
                self._pending_worker_finish = None
            _log.info(
                "DIAGNOSTIC worker_started_duplicate_ignored — skipping begin_assistant tool_call_id=%s",
                tool_call_id,
            )
            self.worker_running_changed.emit(True)
            return

        _log.info(
            "DIAGNOSTIC worker_started_first_call — calling begin_assistant tool_call_id=%s",
            tool_call_id,
        )
        self._active_worker_tool_call_id = tool_call_id
        # Direct production execution: the run is still in flight, so keep the
        # chat aura alive and point the user at the workspace instead of
        # duplicating the transcript into the chat.
        self._mark_chat_working_in_workspace()
        self._playground.set_glow_state("coding")
        self._playground.begin_assistant()
        self.worker_started.emit()

        self.worker_running_changed.emit(True)

    def _on_worker_finished(
        self,
        tool_call_id: str,
        ok: bool,
        summary: str,
        needs_followup: bool | None = None,
        status: str | None = None,
    ) -> None:
        """Forward worker finished to playground.

        The production execution session emits one workerFinished signal per
        run.
        """
        _log.info(
            "worker_finished tool_call_id=%s status=%s",
            tool_call_id, status,
        )

        if self._active_worker_tool_call_id == tool_call_id:
            self._pending_worker_finish_generation += 1
            generation = self._pending_worker_finish_generation
            self._pending_worker_finish = _PendingWorkerFinish(
                tool_call_id=tool_call_id,
                ok=ok,
                summary=summary,
                needs_followup=needs_followup,
                status=status,
                generation=generation,
            )
            QTimer.singleShot(
                0,
                lambda: self._flush_pending_worker_finish(tool_call_id, generation),
            )
            return

        self._present_worker_finish(
            tool_call_id=tool_call_id,
            ok=ok,
            summary=summary,
            needs_followup=needs_followup,
            status=status,
        )

    def _flush_pending_worker_finish(self, tool_call_id: str, generation: int) -> None:
        pending = self._pending_worker_finish
        if (
            pending is None
            or pending.tool_call_id != tool_call_id
            or pending.generation != generation
        ):
            return
        self._pending_worker_finish = None
        self._present_worker_finish(
            tool_call_id=pending.tool_call_id,
            ok=pending.ok,
            summary=pending.summary,
            needs_followup=pending.needs_followup,
            status=pending.status,
        )

    def _present_worker_finish(
        self,
        *,
        tool_call_id: str,
        ok: bool,
        summary: str,
        needs_followup: bool | None,
        status: str | None,
    ) -> None:
        metadata = self._worker_result_metadata(tool_call_id)
        self._finish_presenter.present(
            tool_call_id=tool_call_id,
            ok=ok,
            summary=summary,
            needs_followup=needs_followup,
            status=status,
            metadata=metadata,
        )
        if self._active_worker_tool_call_id == tool_call_id:
            self._active_worker_tool_call_id = None
        self.worker_running_changed.emit(False)

    def _worker_result_metadata(self, tool_call_id: str) -> dict:
        """Read run metadata through the bridge's role-neutral accessor."""
        getter = getattr(self._bridge, "execution_result_metadata", None)
        if not callable(getter):
            return {}
        metadata = getter(tool_call_id)
        return metadata if isinstance(metadata, dict) else {}

    def _on_worker_cancelled(self, tool_call_id: str) -> None:
        """Stop worker aura and forward cancel to playground."""

        self._clear_pending_worker_finish(tool_call_id)
        self._playground.stop_aura()
        self._playground.worker_cancelled()

        if self._active_worker_tool_call_id == tool_call_id:
            self._active_worker_tool_call_id = None
        self.worker_running_changed.emit(False)

    # ---- worker content slots --------------------------------------------------

    def _on_worker_reasoning(self, tool_call_id: str, text: str) -> None:
        """Forward reasoning delta to playground."""

        self._playground.append_reasoning(text)

    def _on_worker_content(self, tool_call_id: str, text: str) -> None:
        """Forward content delta to playground."""

        self._playground.append_content(text)


    def _on_worker_api_error(self, tool_call_id: str, status: int, message: str) -> None:
        """Forward API error to playground with a formatted title."""
        _log.info(
            "api_error tool_call_id=%s status=%s message_redacted=%s",
            tool_call_id, status, redact_secrets(message)[:200],
        )
        title = f"API Error {status}" if status > 0 else "Worker Error"
        self._playground.add_error(f"{title}: {message}")
        self._playground.stop_aura()
        self._playground.set_worker_running(False)
        self._clear_pending_worker_finish(tool_call_id)
        if self._active_worker_tool_call_id == tool_call_id:
            self._active_worker_tool_call_id = None
        self.worker_running_changed.emit(False)

    def _clear_pending_worker_finish(self, tool_call_id: str) -> None:
        if (
            self._pending_worker_finish is not None
            and self._pending_worker_finish.tool_call_id == tool_call_id
        ):
            self._pending_worker_finish = None

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

    def _on_worker_activity_updated(self, tool_call_id: str, entries: list) -> None:
        """Route Worker Activity snapshots to playground (append-only heartbeat)."""
        _log.debug(
            "_on_worker_activity_updated tool_call_id=%s entry_count=%d",
            tool_call_id, len(entries),
        )
        self._playground.update_activity(entries, tool_call_id)

    def _on_worker_todo_updated(self, tool_call_id: str, items: list) -> None:
        """Route Worker TODO snapshots to playground (full replacement lens)."""
        _log.debug(
            "_on_worker_todo_updated tool_call_id=%s item_count=%d",
            tool_call_id, len(items),
        )
        self._playground.update_worker_todo(items, tool_call_id)
