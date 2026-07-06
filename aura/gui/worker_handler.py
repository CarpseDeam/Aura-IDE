"""Worker lifecycle event handler — receives bridge worker signals and
forwards them to chat/playground UI components.

Owns its own session usage tracking dict and emits signals so that
MainWindow can react to state changes (status bar refresh, input streaming).

WorkflowState is owned by the backend _DispatchProxy. This handler only
stores and forwards the latest canonical snapshot via _on_workflow_state_changed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal

from aura.config import redact_secrets

_log = logging.getLogger(__name__)

from aura.conversation.workflow_state import WorkflowState, WorkflowStatus
from aura.gui.dispatch_ui_lifecycle import DispatchUiLifecycle
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
        # WorkflowState snapshot — stored from backend emissions only, never
        # constructed or mutated here.
        self._active_workflow: WorkflowState | None = None
        self._dispatch_ui = DispatchUiLifecycle(
            bridge=bridge,
            chat=chat,
            parent_widget=parent,
            active_workflow=lambda: self._active_workflow,
        )
        self._finish_presenter = WorkerFinishPresenter(chat, playground)
        self._tool_router = WorkerToolEventRouter(playground=playground, chat=chat)

    # ---- public property -------------------------------------------------------

    @property
    def session_usage(self) -> dict[str, dict[str, int]]:
        """Read-only access to the per-model usage accumulator."""
        return self._session_usage

    @property
    def active_workflow(self) -> WorkflowState | None:
        """Last canonical snapshot from the backend _DispatchProxy."""
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
        # Backend-owned canonical WorkflowState snapshots.
        self._bridge.workflowStateChanged.connect(self._on_workflow_state_changed)
        # WorkArtifact projection updates.
        self._bridge.artifactProjectionUpdated.connect(self._on_artifact_projection_updated)

    # ---- canonical WorkflowState snapshot from backend -------------------------

    def _on_workflow_state_changed(self, state: WorkflowState) -> None:
        """Store and forward a canonical WorkflowState snapshot from the backend."""
        _log.debug(
            "_on_workflow_state_changed tool_call_id=%s status=%s",
            state.tool_call_id, state.status.value if state.status else "?",
        )
        self._active_workflow = state
        # Forward to the spec card for rendering.
        card = self._dispatch_ui.get_spec_card(state.tool_call_id)
        if card is not None and hasattr(card, "update_workflow_state"):
            card.update_workflow_state(state)
        # Forward to the plan writer card only while the backend says the
        # dispatch is still in the pre-worker review state.
        if state.status == WorkflowStatus.plan_ready:
            plan_card = getattr(self._chat, "get_plan_writer_card", lambda tid: None)(state.tool_call_id)
            if plan_card is not None and hasattr(plan_card, "update_workflow_state"):
                plan_card.update_workflow_state(state)

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
        """Route Planner dispatch requests through auto or manual review."""
        if self._finish_presenter.resolve_active_mismatch():
            self._chat.stop_current_aura()

        file_list = list(files)

        if self._bridge.auto_dispatch:
            _log.info(
                "dispatch_auto_accepted tool_call_id=%s goal=%s",
                tool_call_id, goal[:120],
            )
            self._dispatch_ui.begin_auto_dispatch(tool_call_id)
            # Backend _DispatchProxy owns the plan_ready/dispatched snapshot.
            self._bridge.user_dispatched(tool_call_id, goal, file_list, spec, acceptance, summary)
            self._chat.scroll_to_bottom(force=True)
            return

        _log.info(
            "dispatch_card_shown tool_call_id=%s goal=%s",
            tool_call_id, goal[:120],
        )
        self._dispatch_ui.begin_visible_dispatch(tool_call_id)
        # Backend _DispatchProxy owns the plan_ready snapshot (emitted inside
        # request_dispatch after showSpecCard).  No WorkflowState construction here.
        self._dispatch_ui.show_spec_card(
            tool_call_id=tool_call_id,
            goal=goal,
            file_list=file_list,
            spec=spec,
            acceptance=acceptance,
            summary=summary,
        )

    # ---- worker lifecycle slots ------------------------------------------------

    def _on_worker_started(self, tool_call_id: str) -> None:
        """Stop the planner aura, remove the plan writer card, and start the
        playground's assistant aura.

        DispatchProxy emits one workerStarted signal per dispatch.
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
                    "worker_finish_cancelled_for_continuing_campaign tool_call_id=%s",
                    tool_call_id,
                )
                self._pending_worker_finish = None
            _log.info(
                "DIAGNOSTIC worker_started_duplicate_ignored — skipping begin_assistant tool_call_id=%s",
                tool_call_id,
            )
            self._dispatch_ui.mark_worker_started(tool_call_id)
            self.worker_running_changed.emit(True)
            return

        _log.info(
            "DIAGNOSTIC worker_started_first_call — calling begin_assistant tool_call_id=%s",
            tool_call_id,
        )
        self._active_worker_tool_call_id = tool_call_id
        self._chat.stop_current_aura()
        # Remove any remaining PlanWriterCard — once the Worker is running,
        # the plan-writing UI is replaced by the Worker Log.  This covers
        # both the auto-dispatch path (no spec card) and the visible-dispatch
        # path where the card was already removed by prepare_spec_card
        # (the call is idempotent).
        self._chat._remove_plan_writer_card(tool_call_id)
        self._playground.set_glow_state("coding")
        self._playground.begin_assistant()
        self.worker_started.emit()

        self._dispatch_ui.mark_worker_started(tool_call_id)
        # The backend _DispatchProxy emitted the dispatched status in
        # request_dispatch before WorkArtifactController is used.  No transition needed.
        self.worker_running_changed.emit(True)

    def _on_worker_finished(
        self,
        tool_call_id: str,
        ok: bool,
        summary: str,
        needs_followup: bool | None = None,
        status: str | None = None,
    ) -> None:
        """Forward worker finished to playground and update spec card.

        DispatchProxy emits one workerFinished signal per dispatch.
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
        active_workflow = (
            self._active_workflow
            if self._active_workflow is not None
            and self._active_workflow.tool_call_id == tool_call_id
            else None
        )
        presentation = self._finish_presenter.present(
            tool_call_id=tool_call_id,
            ok=ok,
            summary=summary,
            needs_followup=needs_followup,
            status=status,
            metadata=metadata,
            active_workflow=active_workflow,
            spec_card=self._dispatch_ui.get_spec_card(tool_call_id),
        )
        outcome = presentation.outcome
        # The backend _DispatchProxy emits the finished WorkflowState snapshot
        # in request_dispatch after WorkArtifactController is used.  No finish() call here.
        if outcome.should_clear_dispatch_card:
            self._dispatch_ui.clear_active_spec_card(tool_call_id)
        if self._active_worker_tool_call_id == tool_call_id:
            self._active_worker_tool_call_id = None
        self.worker_running_changed.emit(False)

    def _worker_result_metadata(self, tool_call_id: str) -> dict:
        getter = getattr(self._bridge, "worker_result_metadata", None)
        if not callable(getter):
            return {}
        metadata = getter(tool_call_id)
        return metadata if isinstance(metadata, dict) else {}

    def _on_worker_cancelled(self, tool_call_id: str) -> None:
        """Stop worker aura and forward cancel to playground/spec card."""

        self._clear_pending_worker_finish(tool_call_id)
        self._playground.stop_aura()
        self._playground.worker_cancelled()

        # Backend _DispatchProxy owns the cancelled snapshot.
        self._dispatch_ui.mark_worker_cancelled(tool_call_id)
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

    def _on_artifact_projection_updated(self, projection) -> None:
        """Receive WorkArtifact projection updates and render/update the artifact card."""
        from aura.work_artifact.projection import WorkArtifactProjection

        if not isinstance(projection, WorkArtifactProjection):
            return
        _log.debug(
            "_on_artifact_projection_updated artifact_id=%s items=%d",
            projection.artifact_id, len(projection.items),
        )
        card = self._chat.add_or_update_artifact_card(projection)
