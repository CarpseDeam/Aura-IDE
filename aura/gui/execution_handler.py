"""Execution lifecycle event handler — receives bridge execution signals and
forwards them to chat/playground UI components.

The ``execution*`` signal names are compatibility aliases for the production
execution session's workspace projection. This handler owns the session usage
tracking dict and emits signals so that MainWindow can react to state changes
(status bar refresh, input streaming).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Final

from PySide6.QtCore import QObject, Signal

from aura.config import redact_secrets
from aura.conversation.telemetry import ConversationTelemetry, context_window_for_model

_log = logging.getLogger(__name__)

from aura.gui.execution_tool_event_router import ExecutionToolEventRouter

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from aura.bridge.qt_bridge import ConversationBridge
    from aura.config import AppSettings
    from aura.gui.chat_view import ChatView
    from aura.gui.playground import AuraPlayground


class ExecutionEventHandler(QObject):
    """Owns execution signal wiring and forwards bridge execution events to the
    chat view and playground.

    A normal production turn's terminal outcome is owned by the Progress
    pane's status chip alone: reaching this handler's finish/cancel/API-error
    slots never adds a chat card. The one exception is a genuine API/harness
    error, which chat already shows exactly once through the canonical
    ``ConversationBridge.apiError`` -> ``MainWindow._on_api_error`` path; this
    handler only marks the pane's status for that case.

    This handler also owns the workspace's lifecycle decision. The production
    execution session emits ``executionStarted`` for *every* turn, including a
    text-only answer such as "Hi Aura", so that run-level signal owns only
    run/input/status state — it never activates the workspace. The workspace
    is activated lazily, exactly once per run, by the first event that
    truthfully proves concrete tool execution has begun; a run that never
    reaches one leaves the workspace and its Aura entirely untouched.

    Attributes:
        usage_updated: Emitted when conversation telemetry changes so that
            MainWindow can refresh the status bar.
        execution_started: Emitted at the end of ``_on_execution_started`` so that
            MainWindow can set input streaming state.
    """

    usage_updated = Signal()
    execution_started = Signal()
    execution_running_changed = Signal(bool)

    #: ``ExecutionToolEventRouter`` forwards whose arrival proves that concrete
    #: workspace/tool execution is under way. Every one of them is wired
    #: through :meth:`_activating`, so a genuine terminal, file-edit, or
    #: CLI-backed path still opens the workspace even when it bypasses the
    #: ordinary ``executionToolCallStart`` signal.
    WORKSPACE_ACTIVITY_ROUTES: Final[frozenset[str]] = frozenset({
        "on_execution_tool_result",
        "on_execution_file_edit_lifecycle",
        "on_execution_workspace_reconcile_requested",
        "on_execution_terminal_command_started",
        "on_execution_terminal_output",
        "on_execution_agent_process_output",
        "on_execution_agent_process_finished",
    })

    #: Router forwards that describe only a provider/agent process coming up.
    #: Spawning a backend process is startup, not workspace work, so these are
    #: wired straight through and never activate the workspace on their own.
    PROVIDER_STARTUP_ROUTES: Final[frozenset[str]] = frozenset({
        "on_execution_agent_process_started",
    })

    #: Bridge signal backing each router forward. Together with the two sets
    #: above this is the complete routed surface: a new router method that is
    #: not classified here is wired by no branch at all.
    _BRIDGE_SIGNAL_BY_ROUTE: Final[dict[str, str]] = {
        "on_execution_tool_result": "executionToolResult",
        "on_execution_file_edit_lifecycle": "executionFileEditLifecycle",
        "on_execution_workspace_reconcile_requested": "executionWorkspaceReconcileRequested",
        "on_execution_terminal_command_started": "executionTerminalCommandStarted",
        "on_execution_terminal_output": "executionTerminalOutput",
        "on_execution_agent_process_started": "executionAgentProcessStarted",
        "on_execution_agent_process_output": "executionAgentProcessOutput",
        "on_execution_agent_process_finished": "executionAgentProcessFinished",
    }

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
        self._tool_router = ExecutionToolEventRouter(playground=playground)
        self._workspace_activated = False
        self._activity_slots: list[Callable[..., None]] = []

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
        """Wire all bridge execution signals to the corresponding handler slots."""
        self._bridge.executionStarted.connect(self._on_execution_started)
        self._bridge.executionFinished.connect(self._on_execution_finished)
        self._bridge.executionCancelled.connect(self._on_execution_cancelled)
        # Earliest truthful proof that a run is doing real work: the model has
        # begun emitting a concrete tool call. Nothing is projected from it —
        # it exists here purely to open the workspace lifecycle before the
        # first workspace event lands.
        self._bridge.executionToolCallStart.connect(self._on_execution_tool_call_start)
        self._bridge.executionApiError.connect(self._on_execution_api_error)
        self._bridge.executionUsage.connect(self._on_execution_usage)
        self._bridge.executionDelegationUsage.connect(self._on_delegation_usage)
        self._bridge.taskChecklistUpdated.connect(self._on_task_checklist_updated)
        self._connect_tool_router_signals()

    def _connect_tool_router_signals(self) -> None:
        """Wire every router forward, gating the workspace-activity ones.

        Driving the connections from :attr:`_BRIDGE_SIGNAL_BY_ROUTE` keeps the
        classification and the wiring the same fact, so no workspace-activity
        route can be connected while bypassing lazy activation.
        """
        for route, signal_name in self._BRIDGE_SIGNAL_BY_ROUTE.items():
            forward = getattr(self._tool_router, route)
            if route in self.WORKSPACE_ACTIVITY_ROUTES:
                slot = self._activating(forward)
            elif route in self.PROVIDER_STARTUP_ROUTES:
                slot = forward
            else:  # pragma: no cover - guarded by test_execution_workspace_lifecycle
                raise AssertionError(f"unclassified execution route: {route}")
            self._activity_slots.append(slot)
            getattr(self._bridge, signal_name).connect(slot)

    def _activating(self, forward: Callable[..., None]) -> Callable[..., None]:
        """Wrap a router forward so it activates the workspace before projecting."""

        def _slot(*args: object) -> None:
            self._ensure_workspace_activated()
            forward(*args)

        return _slot

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

    # ---- lazy workspace activation ------------------------------------------------

    def _ensure_workspace_activated(self) -> None:
        """Open the workspace lifecycle on the first proof of real work.

        Idempotent for the rest of the run: repeated tool, terminal, or
        file-edit events must not re-prepare the playground or restart the
        Aura's fade-in. The chat hint is shown, the playground is prepared,
        and the workspace Aura starts *before* the caller projects its event.

        Preparing the pane resets its status chip to Idle. Because that now
        happens mid-run rather than at run start, the Live claim the
        run-level start already made is re-asserted immediately after — the
        run is still in flight, so Idle would be a lie.
        """
        if self._workspace_activated:
            return
        self._workspace_activated = True
        self._mark_chat_working_in_workspace()
        self._playground.begin_assistant()
        self._playground.set_execution_running(True)
        self._playground.start_aura()

    def _deactivate_workspace(self) -> None:
        """Fade the workspace Aura out and clear the activation state."""
        self._workspace_activated = False
        self._playground.stop_aura()

    # ---- execution lifecycle slots ------------------------------------------------

    def _on_execution_started(self, tool_call_id: str) -> None:
        """Own run/input/status state for a new production run.

        The production execution session emits one executionStarted per run —
        including a text-only answer that never touches the workspace — so
        this slot deliberately leaves the workspace and its Aura alone. Real
        work opens them later through :meth:`_ensure_workspace_activated`.
        """
        if self._active_execution_tool_call_id == tool_call_id:
            self.execution_running_changed.emit(True)
            return

        self._active_execution_tool_call_id = tool_call_id
        self._workspace_activated = False
        self.execution_started.emit()

        self.execution_running_changed.emit(True)

    def _on_execution_tool_call_start(
        self, run_id: str, execution_tool_id: str, name: str
    ) -> None:
        """Activate the workspace: the model has begun a concrete tool call."""
        self._ensure_workspace_activated()

    def _on_execution_finished(
        self,
        tool_call_id: str,
        ok: bool,
        status: str | None = None,
    ) -> None:
        """Finish the Progress pane for one production turn.

        The production execution session emits one executionFinished signal
        per run, carrying only the run identity and truthful terminal
        status — never a summary/report string. A normal completed turn adds
        no chat card; the model's own final response is chat's sole
        conclusion for the turn.

        A run that never activated the workspace (a text-only answer) is left
        entirely untouched here: no tabs are closed, no status chip is
        written, and no Aura is stopped that was never started.
        """
        _log.info(
            "execution_finished tool_call_id=%s status=%s",
            tool_call_id, status,
        )
        if self._workspace_activated:
            self._playground.execution_finished(ok, status=status)
            self._deactivate_workspace()
        if self._active_execution_tool_call_id == tool_call_id:
            self._active_execution_tool_call_id = None
        self.execution_running_changed.emit(False)

    def _on_execution_cancelled(self, tool_call_id: str) -> None:
        """Stop execution aura, surface the stop in chat, and set the pane status."""

        self._deactivate_workspace()
        self._chat.add_info("Execution", "Stopped by user.")
        self._playground.execution_cancelled()

        if self._active_execution_tool_call_id == tool_call_id:
            self._active_execution_tool_call_id = None
        self.execution_running_changed.emit(False)

    # ---- execution error slots --------------------------------------------------

    def _on_execution_api_error(self, tool_call_id: str, status: int, message: str) -> None:
        """Mark the Progress pane's status for an API/harness error.

        Chat presentation for a genuine API/transport/harness error happens
        exactly once, through the canonical ``ConversationBridge.apiError`` ->
        ``MainWindow._on_api_error`` path — this handler must never add a
        second chat error card for the same failure.
        """
        _log.info(
            "api_error tool_call_id=%s status=%s message_redacted=%s",
            tool_call_id, status, redact_secrets(message)[:200],
        )
        self._playground.mark_execution_error()
        self._deactivate_workspace()
        self._playground.set_execution_running(False)
        if self._active_execution_tool_call_id == tool_call_id:
            self._active_execution_tool_call_id = None
        self.execution_running_changed.emit(False)

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

    def _on_delegation_usage(
        self,
        _tool_call_id: str,
        provider_id: str,
        model_id: str,
        prompt: int,
        completion: int,
        hit: int,
        miss: int,
    ) -> None:
        """Accumulate child usage without replacing the root context snapshot."""
        self._conversation_telemetry.record_usage(
            model_id=model_id,
            prompt=prompt,
            completion=completion,
            hit=hit,
            miss=miss,
            context_window_tokens=0,
            provider_id=provider_id,
            update_latest_context=False,
        )
        self.usage_updated.emit()

    def _on_task_checklist_updated(self, tool_call_id: str, items: list) -> None:
        """Route Task Checklist snapshots to playground (full replacement lens)."""
        _log.debug(
            "_on_task_checklist_updated tool_call_id=%s item_count=%d",
            tool_call_id, len(items),
        )
        self._playground.update_task_checklist(items, tool_call_id)
