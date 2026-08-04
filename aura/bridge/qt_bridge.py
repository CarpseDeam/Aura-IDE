"""Bridge between the (sync) ConversationManager worker thread and Qt's GUI thread.

Normal Aura coding is one continuous production run:

    user request
      → selected production provider and model
      → one ConversationManager over the original conversation
      → inspection → live TODO → edits → terminal → diagnosis → repair
      → validation rerun → one factual completion receipt

- send() spawns a QThread that runs ConversationManager.send against the
  `generate_production_code` hook.
- Every event is projected into the workspace through
  ``ProductionExecutionSession`` under one stable run id.
- The approval callback is bridged via QMetaObject.invokeMethod with
  Qt.BlockingQueuedConnection — the worker thread blocks until the user clicks
  in the modal dialog on the main thread.

There is exactly one backend, one provider, one system prompt, one model, one
thinking choice, and one ``PRODUCTION_STREAM_HOOK``.
"""
from __future__ import annotations

import copy
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QObject,
    QThread,
    Signal,
    Slot,
)

from aura.backends import (
    APIAgentBackend,
)
from aura.bridge.approval_proxy import _ApprovalProxy
from aura.bridge.production_execution import ProductionExecutionSession
from aura.client import (
    ApiError,
    ContentDelta,
    Done,
    Event,
)
from aura.config import (
    ModelId,
    ProviderId,
    ThinkingMode,
)
from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import (
    SINGLE_SYSTEM_PROMPT,
    compose_system_prompt,
    context_gearbox_metadata,
    diagnose_custom_prompt,
    format_custom_prompt_diagnostics,
    format_prompt_composition,
)
from aura.conversation import (
    ConversationManager,
    History,
)
from aura.conversation.task_router import TaskLane, TaskRoute
from aura.conversation.tools import (
    ToolRegistry,
)
from aura.model_streams import (
    PRODUCTION_STREAM_HOOK,
    model_streams,
)
from aura.research.policy import NO_RESEARCH, decide_research_policy
from aura.windows_mcp import WindowsComputerUseManager

_log = logging.getLogger(__name__)


class _Worker(QObject):
    """Lives on the worker thread. Runs the production conversation loop."""

    reasoningDelta = Signal(str)
    contentDelta = Signal(str)
    toolCallStart = Signal(int, str, str)  # index, id, name
    toolCallArgs = Signal(int, str)  # index, fragment
    toolCallEnd = Signal(int)
    apiError = Signal(int, str)
    streamDone = Signal(str, dict)
    toolResultEmitted = Signal(str, str, bool, str, dict)
    terminalOutput = Signal(str, str)  # (tool_call_id, text)
    agentProcessStarted = Signal(str, str, str)  # process_id, label, command
    agentProcessOutput = Signal(str, str)  # process_id, text
    agentProcessFinished = Signal(str, object)  # process_id, exit_code
    finished = Signal()

    def __init__(
        self,
        manager: ConversationManager,
        approval_proxy: "_ApprovalProxy",
        cancel_event: threading.Event,
        model: ModelId,
        thinking: ThinkingMode,
        temperature: float = 0.7,
        workspace_root: Path | None = None,
        production_session: "ProductionExecutionSession | None" = None,
        task_route: TaskRoute | None = None,
    ) -> None:
        super().__init__()
        self._manager = manager
        self._approval_proxy = approval_proxy
        self._cancel = cancel_event
        self._model = model
        self._thinking = thinking
        self._temperature = temperature
        self._workspace_root = workspace_root
        self._production_session = production_session
        self._task_route = task_route
        self._blocked_reason: str = ""
        self._provider_contract_failure: bool = False
        self._already_satisfied: bool = False
        self._bears_production_action: bool = False

    @Slot()
    def run(self) -> None:
        try:
            self._manager.send(
                on_event=self._on_event,
                approval_cb=self._approval_proxy.request_approval,
                cancel_event=self._cancel,
                model=self._model,
                thinking=self._thinking,
                temperature=self._temperature,
                task_route=self._task_route,
            )
            self._blocked_reason = self._manager.last_turn_blocked_reason
            self._provider_contract_failure = (
                self._manager.last_turn_provider_contract_failure
            )
            self._already_satisfied = self._manager.last_turn_already_satisfied
            self._bears_production_action = (
                self._manager.last_turn_bears_production_action
            )
        except Exception as exc:
            from aura.config import redact_secrets
            self.apiError.emit(-1, redact_secrets(f"{type(exc).__name__}: {exc}"))
        finally:
            if self._cancel.is_set():
                self._manager.history.pop_if_empty_assistant_message()
            self.finished.emit()

    def _on_event(self, ev: Event) -> None:
        session = self._production_session
        if session is not None:
            # Production *execution* projects into the workspace; the
            # assistant's conversation prose stays chat-owned and travels the
            # canonical contentDelta path (see _emit_chat_facts).
            session.handle_event(ev)
            self._emit_chat_facts(ev)
            return

    def _emit_chat_facts(self, ev: Event) -> None:
        """Emit the event set the chat and persistence layers own.

        Assistant prose is chat-owned and must reach ChatView through exactly
        one signal path, so ``ContentDelta`` surfaces here and is *not* also
        projected into the workspace Worker Log.  Reasoning, tool calls, usage
        accounting, and every other execution fact stay workspace-owned: usage
        reaches the GUI accumulator once, through the execution ledger's
        ``workerUsage`` path.
        """
        if isinstance(ev, ContentDelta):
            self.contentDelta.emit(ev.text)
        elif isinstance(ev, Done):
            if ev.full_message:
                self.streamDone.emit(ev.finish_reason or "", ev.full_message)
        elif isinstance(ev, ApiError):
            from aura.config import redact_secrets
            self.apiError.emit(
                ev.status_code if ev.status_code is not None else -1,
                redact_secrets(ev.message)
            )


class ConversationBridge(QObject):
    """Public Qt-facing facade for one running conversation."""

    reasoningDelta = Signal(str)
    contentDelta = Signal(str)
    toolCallStart = Signal(str, str)  # tool_call_id, name
    toolCallArgs = Signal(str, str)
    toolCallEnd = Signal(str)
    apiError = Signal(int, str)
    streamDone = Signal(str, dict)
    toolResult = Signal(str, str, bool, str, dict)
    diffApplied = Signal(str, str, str, str, bool)
    diffDecided = Signal(str, str, str, str, str, bool)
    started = Signal()
    finished = Signal()

    # Workspace projection signals (worker* names are compatibility aliases —
    # the production execution session re-emits these so the existing
    # WorkerEventHandler / AuraPlayground projection binds unchanged).
    workerStarted = Signal(str)
    workerFinished = Signal(str, bool, str, bool, str)
    workerCancelled = Signal(str)
    workerReasoningDelta = Signal(str, str)
    workerContentDelta = Signal(str, str)
    workerToolCallStart = Signal(str, str, str)
    workerToolCallArgs = Signal(str, str, str)
    workerToolCallEnd = Signal(str, str)
    workerToolResult = Signal(str, str, str, bool, str, dict)
    workerDiffDecided = Signal(str, str, str, str, str, str, bool)
    workerApiError = Signal(str, int, str)
    workerUsage = Signal(str, str, int, int, int, int)
    workerActivityUpdated = Signal(str, list)  # Activity entries (append-only execution heartbeat)
    workerTodoUpdated = Signal(str, list)  # Full Worker TODO snapshot
    workerTerminalOutput = Signal(str, str, str)  # parent_tool_id, worker_tool_id, text
    workerAgentProcessStarted = Signal(str, str, str, str)
    workerAgentProcessOutput = Signal(str, str, str)
    workerAgentProcessFinished = Signal(str, str, object)

    # Terminal output (single mode)
    terminalOutput = Signal(str, str)  # tool_call_id, text
    agentProcessStarted = Signal(str, str, str)
    agentProcessOutput = Signal(str, str)
    agentProcessFinished = Signal(str, object)

    def __init__(
        self,
        parent_widget,
        provider: ProviderId = "deepseek",
    ) -> None:
        super().__init__()
        self._provider = provider

        # The one active production backend. Normal coding always runs here.
        self._production_backend = APIAgentBackend(provider=provider)
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        model_streams.register(PRODUCTION_STREAM_HOOK, self._production_backend.stream)

        self._history = History()
        self._registry = ToolRegistry(workspace_root=_dummy_root(), mode="single")
        self._manager = ConversationManager(self._history, self._registry)
        # Owned here because this is where the production ToolRegistry lives —
        # the manager drives that registry's MCP seam and adds no table of its
        # own. Constructing it starts nothing: with the setting off it never
        # touches the network, the disk, or a subprocess.
        self._windows_computer_use = WindowsComputerUseManager(self._registry)
        self._parent_widget = parent_widget
        self._approval_proxy = _ApprovalProxy(parent_widget)

        # Production execution session — owns the run identity, the single
        # authoritative execution ledger, and workspace projection.
        self._production_session = ProductionExecutionSession(
            approval_proxy=self._approval_proxy,
            parent=self,
        )

        self._cancel: threading.Event = threading.Event()
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._index_to_id: dict[int, str] = {}
        self._index_to_name: dict[int, str] = {}
        self._last_proposed_tool_call_id: str | None = None
        self._active_model: str = ""

        self._temperature: float = 0.7
        self._single_system_prompt: str = ""
        self._tier1_context: str = ""
        self._context_gearbox_metadata: dict = {}
        self._custom_prompt_diagnostics = diagnose_custom_prompt(RuntimeRole.SINGLE, "")
        self._pre_worker_sha: str | None = None
        self._active_prompt_mode: str | None = None
        self._turn_task_route: TaskRoute | None = None
        self._turn_task_kind: str | None = None
        self._turn_content: str = ""
        self._turn_target_files: tuple[str, ...] = ()

        # Re-emit production session signals on the same bridge signals so the
        # polished workspace projection binds once and stays role-neutral.
        session = self._production_session
        session.workerStarted.connect(self.workerStarted)
        session.workerFinished.connect(self.workerFinished)
        session.workerCancelled.connect(self.workerCancelled)
        session.workerReasoningDelta.connect(self.workerReasoningDelta)
        session.workerContentDelta.connect(self.workerContentDelta)
        session.workerToolCallStart.connect(self.workerToolCallStart)
        session.workerToolCallArgs.connect(self.workerToolCallArgs)
        session.workerToolCallEnd.connect(self.workerToolCallEnd)
        session.workerToolResult.connect(self.workerToolResult)
        session.workerDiffDecided.connect(self.workerDiffDecided)
        session.workerApiError.connect(self.workerApiError)
        session.workerUsage.connect(self.workerUsage)
        session.workerActivityUpdated.connect(self.workerActivityUpdated)
        session.workerTodoUpdated.connect(self.workerTodoUpdated)
        session.workerTerminalOutput.connect(self.workerTerminalOutput)
        session.workerAgentProcessStarted.connect(self.workerAgentProcessStarted)
        session.workerAgentProcessOutput.connect(self.workerAgentProcessOutput)
        session.workerAgentProcessFinished.connect(self.workerAgentProcessFinished)

    # ---- config -----------------------------------------------------------

    @property
    def history(self) -> History:
        return self._history

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def production_session(self) -> ProductionExecutionSession:
        """The active production execution owner (run identity + ledger)."""
        return self._production_session

    @property
    def production_run_id(self) -> str:
        return self._production_session.run_id

    def execution_result_metadata(self, run_id: str) -> dict:
        """Role-neutral result metadata accessor for the active execution."""
        return self._production_session.result_metadata(run_id)

    def worker_result_metadata(self, tool_call_id: str) -> dict:
        """Compatibility alias for ``execution_result_metadata``."""
        return self.execution_result_metadata(tool_call_id)

    def context_gearbox_metadata(self) -> dict:
        return copy.deepcopy(self._context_gearbox_metadata)

    def set_workspace_root(self, root) -> None:
        if root is None:
            self._tier1_context = ""
            self._context_gearbox_metadata = {}
            return
        self._registry.set_workspace_root(root)
        self._manager.set_workspace_root(root)
        self.refresh_tier1_context()

    def set_read_only(self, value: bool) -> None:
        self._registry.set_read_only(value)

    def set_system_prompt(self, prompt: str) -> None:
        """Store the custom production system prompt and reapply composition."""
        self._single_system_prompt = prompt or ""
        composed = self._compose_prompt(self._active_runtime_role(), self._single_system_prompt)
        self._history.set_system(composed.system_prompt)

    def refresh_tier1_context(self, force_repo_map: bool = False) -> None:
        """Refresh workspace context and reapply the active system prompt."""
        role = self._active_runtime_role()
        composed = self._compose_prompt(
            role,
            self._single_system_prompt,
            force_repo_map=force_repo_map,
        )
        self._history.set_system(composed.system_prompt)

    def set_production_mode(self) -> None:
        """Put the bridge in production single-agent mode (the normal product)."""
        mode_key = "single"
        self._registry.set_mode(mode_key)
        role = self._active_runtime_role()
        composed = self._compose_prompt(role, self._single_system_prompt)
        self._history.set_system(composed.system_prompt)
        self._active_prompt_mode = mode_key

    def set_temperature(self, temperature: float) -> None:
        self._temperature = temperature

    @property
    def windows_computer_use(self) -> WindowsComputerUseManager:
        """The Windows Computer Use lifecycle, for the settings page."""
        return self._windows_computer_use

    def apply_windows_computer_use(self, settings) -> None:
        """Bring the Windows MCP connection in line with *settings*.

        Returns immediately; connecting happens on the manager's worker.
        """
        self._windows_computer_use.apply_settings(settings)

    def shutdown_windows_computer_use(self) -> None:
        """Disconnect the Windows MCP server and close its process."""
        self._windows_computer_use.shutdown()

    def set_auto_approve(self, enabled: bool) -> None:
        self._approval_proxy.set_approve_all_session(enabled)

    def _active_runtime_role(self) -> RuntimeRole:
        """Normal coding always runs the production single-agent role."""
        return RuntimeRole.SINGLE

    def _compose_prompt(
        self,
        role: RuntimeRole,
        custom_prompt: str,
        *,
        force_repo_map: bool = False,
    ):
        """The one place a production system prompt is built and cached."""
        composed = compose_system_prompt(
            role,
            custom_prompt,
            self._registry.workspace_root,
            force=force_repo_map,
            model=self._active_model or None,
            task_kind=self._turn_task_kind,
            target_files=self._turn_target_files,
            content=self._turn_content or None,
            active_capabilities=self._registry.active_capabilities(),
        )
        self._context_gearbox_metadata = context_gearbox_metadata(
            composed.ledger, workspace_root=self._registry.workspace_root,
        )
        self._custom_prompt_diagnostics = diagnose_custom_prompt(role, custom_prompt)
        _log.info(
            "prompt_composed role=%s %s",
            role.value,
            format_custom_prompt_diagnostics(
                self._custom_prompt_diagnostics,
                effective_prompt_chars=len(composed.system_prompt),
            ),
        )
        _log.info("%s", format_prompt_composition(composed))
        self._tier1_context = composed.context_text
        return composed

    def set_production_provider(self, provider: ProviderId) -> None:
        """Point the one production backend at *provider*.

        This is the canonical provider entry point for normal coding.
        """
        self._provider = provider
        self._production_backend = APIAgentBackend(provider=provider)
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        model_streams.register(PRODUCTION_STREAM_HOOK, self._production_backend.stream)

    def set_provider(self, provider: ProviderId) -> None:
        """Update the production provider."""
        self.set_production_provider(provider)

    def check_backend_auth(self, backend_name: str) -> bool:
        """Check if the named backend is authenticated.

        Args:
            backend_name: 'default_api'

        Returns:
            True if the backend is authenticated, False otherwise.
        """
        return True  # 'default_api' is always authenticated


    def reset_history(self) -> None:
        self._history.messages.clear()
        self._index_to_id.clear()
        self._index_to_name.clear()
        self._production_session.clear()
        # We do NOT reset _approve_all_session here, as it is managed by the
        # persistent toolbar toggle.

    def is_running(self) -> bool:
        thread = self._thread
        if thread is None:
            return False
        try:
            return thread.isRunning()
        except RuntimeError:
            self._thread = None
            self._worker = None
            return False

    def get_pre_worker_snapshot(self) -> str | None:
        return self._pre_worker_sha

    def clear_pre_worker_snapshot(self) -> None:
        self._pre_worker_sha = None

    # ---- send / cancel ----------------------------------------------------

    def send(
        self,
        model: ModelId,
        thinking: ThinkingMode,
        *,
        route: TaskRoute | None = None,
    ) -> None:
        """Run one production turn over the existing conversation.

        ``route`` is the deterministic ``TaskRoute`` selected at send time and
        is the authority for this turn's task kind. Callers that genuinely do
        not provide one fall back to the previous research-only recomputation
        inside ``_prepare_turn_context``.

        The manager already owns the persisted ``History``, so the model
        receives the actual conversation and the user's latest original
        request — never a SpecCard or a generated implementation capsule.
        """
        if self.is_running():
            return
        # The active model is terrain for skill selection, so it must be known
        # before the turn's system prompt is composed.
        self._active_model = str(model)
        self._turn_task_route = route
        self._prepare_turn_context()
        # Capture pre-run snapshot for reliable /undo.
        if self._registry.workspace_root is not None:
            from aura.git_ops import snapshot
            self._pre_worker_sha = snapshot(self._registry.workspace_root)
        else:
            self._pre_worker_sha = None
        self._cancel = threading.Event()
        self._index_to_id.clear()
        self._index_to_name.clear()
        if self._registry.workspace_root is not None:
            base_prompt = (
                self._single_system_prompt
                if self._single_system_prompt
                else SINGLE_SYSTEM_PROMPT
            )
            self._manager.configure_runtime_context(
                base_prompt=base_prompt,
                workspace_root=self._registry.workspace_root,
                role=RuntimeRole.SINGLE,
                model=self._active_model or None,
                task_kind=self._turn_task_kind,
                content=self._turn_content or None,
                target_files=self._turn_target_files,
            )

        # One stable execution identity for this production turn.
        self._production_session.begin(model=str(model))

        self._thread = QThread()
        self._worker = _Worker(
            manager=self._manager,
            approval_proxy=self._approval_proxy,
            cancel_event=self._cancel,
            model=model,
            thinking=thinking,
            temperature=self._temperature,
            workspace_root=self._registry.workspace_root,
            production_session=self._production_session,
            task_route=self._turn_task_route,
        )
        self._worker.moveToThread(self._thread)

        self._worker.reasoningDelta.connect(self.reasoningDelta)
        self._worker.contentDelta.connect(self.contentDelta)
        self._worker.toolCallStart.connect(self._on_tool_call_start)
        self._worker.toolCallArgs.connect(self._on_tool_call_args)
        self._worker.toolCallEnd.connect(self._on_tool_call_end)
        self._worker.apiError.connect(self.apiError)
        self._worker.streamDone.connect(self.streamDone)
        self._worker.toolResultEmitted.connect(self._on_tool_result)
        self._worker.terminalOutput.connect(self.terminalOutput)
        self._worker.agentProcessStarted.connect(self.agentProcessStarted)
        self._worker.agentProcessOutput.connect(self.agentProcessOutput)
        self._worker.agentProcessFinished.connect(self.agentProcessFinished)
        self._worker.finished.connect(self._on_finished)

        self._thread.started.connect(self._worker.run)
        self.started.emit()
        self._thread.start()



    def request_cancel(self) -> None:
        self._cancel.set()
        self._production_session.note_cancelled()
        self._approval_proxy.cancel_active_dialog()

    # ---- private slots ----------------------------------------------------

    @Slot(int, str, str)
    def _on_tool_call_start(self, index: int, tool_id: str, name: str) -> None:
        self._index_to_id[index] = tool_id
        self._index_to_name[index] = name
        self._last_proposed_tool_call_id = tool_id
        self.toolCallStart.emit(tool_id, name)

    @Slot(int, str)
    def _on_tool_call_args(self, index: int, fragment: str) -> None:
        tool_id = self._index_to_id.get(index, "")
        if tool_id:
            self.toolCallArgs.emit(tool_id, fragment)

    @Slot(int)
    def _on_tool_call_end(self, index: int) -> None:
        tool_id = self._index_to_id.get(index, "")
        if tool_id:
            self.toolCallEnd.emit(tool_id)

    @Slot(str, str, bool, str, dict)
    def _on_tool_result(
        self, tool_id: str, name: str, ok: bool, result: str, extras: dict
    ) -> None:
        approval = extras.get("approval")
        if approval:
            ev = self._approval_proxy.consume_last_event()
            if ev is not None:
                self.diffDecided.emit(
                    tool_id,
                    str(approval),
                    str(ev["rel_path"]),
                    str(ev["old_content"]),
                    str(ev["new_content"]),
                    bool(ev["is_new_file"]),
                )
        self.toolResult.emit(tool_id, name, ok, result, extras)

    @Slot()
    def _on_finished(self) -> None:
        thread = self._thread
        worker = self._worker
        self._thread = None
        self._worker = None

        # Exactly one completion receipt per production turn, built from the
        # run's structured execution evidence, then back to idle. A successful
        # ``report_blocker`` names the reason so the receipt reports the turn as
        # blocked, never as completed; a provider-contract failure is reported
        # as its own terminal status rather than as a completed turn.
        # Structured ``report_already_satisfied`` evidence and the turn's
        # production-action route are carried so the completion contract can
        # report truthfully.
        blocked_reason = worker._blocked_reason if worker is not None else ""
        provider_contract_failure = (
            worker._provider_contract_failure if worker is not None else False
        )
        already_satisfied = worker._already_satisfied if worker is not None else False
        bears_production_action = (
            worker._bears_production_action if worker is not None else False
        )
        try:
            self._production_session.finish(
                blocked_reason=blocked_reason,
                provider_contract_failure=provider_contract_failure,
                already_satisfied=already_satisfied,
                bears_production_action=bears_production_action,
            )
        except Exception:
            _log.exception("Failed to build production completion receipt")

        # Surface the turn's skill activation ledger on the same metadata the
        # Context Gearbox exposes, so activations that happened *during* the
        # turn join the composition-time candidate/guard/skipped records.
        try:
            activations = self._manager.skill_activation_log()
            if activations:
                metadata = dict(self._context_gearbox_metadata or {})
                metadata["skill_activations"] = list(activations)
                self._context_gearbox_metadata = metadata
        except Exception:
            _log.exception("Failed to surface skill activation ledger")

        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.quit()
            thread.wait(2000)
            thread.deleteLater()
        self.finished.emit()

    def _prepare_turn_context(self) -> None:
        """Recompose the system prompt against this turn's live terrain.

        Runs on every turn: skill selection uses the current user message,
        task kind, active model, and any known target files, never a cached
        workspace-startup composition.
        """
        self._turn_content = _latest_user_text(self._history)
        route = self._turn_task_route
        if route is not None:
            # The route selected at send time is the authority for this turn.
            self._turn_task_kind = _task_kind_from_route(route)
        else:
            # Fallback for callers that genuinely do not supply a route:
            # keep the research-only recomputation from before.
            self._turn_task_kind = _research_task_kind_for_text(self._turn_content)
        if self._registry.workspace_root is None:
            return
        role = self._active_runtime_role()
        composed = self._compose_prompt(role, self._single_system_prompt)
        self._history.set_system(composed.system_prompt)
        _log.info(
            "context_gearbox_turn_summary %s",
            self._context_gearbox_metadata.get("summary", {}).get("display", ""),
        )

    def set_turn_target_files(self, target_files: list[str] | tuple[str, ...]) -> None:
        """Declare the files this turn is known to target.

        The send layer calls this before every send with the paths the user
        actually named — and with an empty tuple when they named none, so the
        previous turn's scope does not carry over.
        """
        self._turn_target_files = tuple(str(path) for path in target_files if str(path).strip())


def _dummy_root():
    return Path.home()


def _task_kind_from_route(route: TaskRoute) -> str | None:
    """Project a deterministic ``TaskRoute`` onto the turn's task kind.

    The route is a lossless, first-class value from the existing router — no
    second classifier here. Every lane that has a production task kind simply
    surfaces the router's own action: research keeps ``web_research`` /
    ``research_then_worker``, implementation keeps the shape it was routed as
    (``bugfix``, ``refactor``, ``cleanup``, ``implementation``), and validation
    keeps ``validation``. Chat and built-in actions have no production task
    kind and yield ``None``.
    """
    if route.lane in (
        TaskLane.research,
        TaskLane.implementation,
        TaskLane.validation,
    ):
        return route.action
    return None


def _research_task_kind_for_text(text: str) -> str | None:
    decision = decide_research_policy(text)
    if decision.route == NO_RESEARCH:
        return None
    return decision.route


def _latest_user_text(history: History) -> str:
    """This turn's content terrain: the real user request.

    Aura's own ``aura_internal`` steering is ``role="user"`` too, and letting a
    nudge stand in as terrain would drive skill selection off Aura's words
    rather than the user's. ``History`` owns that distinction.
    """
    return history.latest_real_user_text() or ""
