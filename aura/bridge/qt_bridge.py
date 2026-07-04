"""Bridge between the (sync) ConversationManager worker thread and Qt's GUI thread.

- send() spawns a QThread that runs ConversationManager.send for the planner.
- Each event becomes a Qt signal on the GUI thread.
- The approval callback is bridged via QMetaObject.invokeMethod with
  Qt.BlockingQueuedConnection — the worker thread blocks until the user clicks
  in the modal dialog on the main thread.

Planner / worker mode:
- The planner runs as the long-lived manager. When it calls dispatch_to_worker,
  the dispatch callback (`_DispatchProxy`) marshals the spec to the GUI thread,
  blocks until the user dispatches or cancels, and (on dispatch) runs a worker
  ConversationManager synchronously on the same background thread, forwarding
  worker-prefixed signals up to the GUI for nested rendering.
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

if TYPE_CHECKING:
    from aura.conversation.persistence import WorkerDispatchRecord

from aura.backends import (
    APIAgentBackend,
)
from aura.bridge.approval_proxy import _ApprovalProxy
from aura.bridge.dispatch import _DispatchProxy
from aura.client import (
    AgentProcessFinished,
    AgentProcessOutput,
    AgentProcessStarted,
    ApiError,
    ContentDelta,
    Done,
    Event,
    ReasoningDelta,
    TerminalOutput,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResult,
    Usage,
    WorkerDispatchRequested,
)
from aura.config import (
    ModelId,
    ProviderId,
    ThinkingMode,
)
from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import (
    PLANNER_SYSTEM_PROMPT,
    SINGLE_SYSTEM_PROMPT,
    build_context_text,
    compose_system_prompt,
    context_gearbox_metadata,
)
from aura.conversation import (
    ConversationManager,
    History,
)
from aura.conversation.tools import (
    ToolRegistry,
)
from aura.model_streams import model_streams
from aura.research.policy import NO_RESEARCH, decide_research_policy

_log = logging.getLogger(__name__)


class _Worker(QObject):
    """Lives on the worker thread. Runs the planner conversation loop."""

    reasoningDelta = Signal(str)
    contentDelta = Signal(str)
    toolCallStart = Signal(int, str, str)  # index, id, name
    toolCallArgs = Signal(int, str)  # index, fragment
    toolCallEnd = Signal(int)
    usageEmitted = Signal(int, int, int, int)
    apiError = Signal(int, str)
    streamDone = Signal(str, dict)
    toolResultEmitted = Signal(str, str, bool, str, dict)
    workerDispatchRequested = Signal(str, str, list, str, str, str)
    terminalOutput = Signal(str, str)  # (tool_call_id, text)
    agentProcessStarted = Signal(str, str, str)  # process_id, label, command
    agentProcessOutput = Signal(str, str)  # process_id, text
    agentProcessFinished = Signal(str, object)  # process_id, exit_code
    finished = Signal()

    def __init__(
        self,
        manager: ConversationManager,
        approval_proxy: "_ApprovalProxy",
        dispatch_proxy: "_DispatchProxy | None",
        cancel_event: threading.Event,
        model: ModelId,
        thinking: ThinkingMode,
        temperature: float = 0.7,
        workspace_root: Path | None = None,
        max_tool_rounds: int | None = None,
    ) -> None:
        super().__init__()
        self._manager = manager
        self._approval_proxy = approval_proxy
        self._dispatch_proxy = dispatch_proxy
        self._cancel = cancel_event
        self._model = model
        self._thinking = thinking
        self._temperature = temperature
        self._workspace_root = workspace_root
        self._max_tool_rounds = max_tool_rounds

    @Slot()
    def run(self) -> None:
        try:
            dispatch_cb = (
                self._dispatch_proxy.request_dispatch
                if self._dispatch_proxy is not None
                else None
            )
            workflow_state_cb = (
                self._dispatch_proxy._workflow_state_callback
                if self._dispatch_proxy is not None
                else None
            )
            self._manager.send(
                on_event=self._on_event,
                approval_cb=self._approval_proxy.request_approval,
                cancel_event=self._cancel,
                model=self._model,
                thinking=self._thinking,
                dispatch_cb=dispatch_cb,
                workflow_state_cb=workflow_state_cb,
                temperature=self._temperature,
                hook_name='generate_planner_code',
                max_tool_rounds=self._max_tool_rounds,
            )
        except Exception as exc:
            from aura.config import redact_secrets
            self.apiError.emit(-1, redact_secrets(f"{type(exc).__name__}: {exc}"))
        finally:
            if self._cancel.is_set():
                self._manager.history.pop_if_empty_assistant_message()
            self.finished.emit()

    def _on_event(self, ev: Event) -> None:
        if isinstance(ev, ReasoningDelta):
            self.reasoningDelta.emit(ev.text)
        elif isinstance(ev, ContentDelta):
            self.contentDelta.emit(ev.text)
        elif isinstance(ev, ToolCallStart):
            self.toolCallStart.emit(ev.index, ev.id, ev.name)
        elif isinstance(ev, ToolCallArgsDelta):
            self.toolCallArgs.emit(ev.index, ev.args_chunk)
        elif isinstance(ev, ToolCallEnd):
            self.toolCallEnd.emit(ev.index)
        elif isinstance(ev, Usage):
            self.usageEmitted.emit(
                ev.prompt_tokens, ev.completion_tokens, ev.cache_hit_tokens, ev.cache_miss_tokens
            )
        elif isinstance(ev, ApiError):
            from aura.config import redact_secrets
            self.apiError.emit(
                ev.status_code if ev.status_code is not None else -1,
                redact_secrets(ev.message)
            )
        elif isinstance(ev, Done):
            if ev.full_message:
                self.streamDone.emit(ev.finish_reason or "", ev.full_message)
        elif isinstance(ev, ToolResult):
            self.toolResultEmitted.emit(ev.tool_call_id, ev.name, ev.ok, ev.result, ev.extras or {})
        elif isinstance(ev, WorkerDispatchRequested):
            self.workerDispatchRequested.emit(
                ev.tool_call_id,
                ev.goal,
                list(ev.files),
                ev.spec,
                ev.acceptance,
                ev.summary,
            )
        elif isinstance(ev, TerminalOutput):
            self.terminalOutput.emit(ev.tool_call_id, ev.text)
        elif isinstance(ev, AgentProcessStarted):
            self.agentProcessStarted.emit(ev.process_id, ev.label, ev.command)
        elif isinstance(ev, AgentProcessOutput):
            self.agentProcessOutput.emit(ev.process_id, ev.text)
        elif isinstance(ev, AgentProcessFinished):
            self.agentProcessFinished.emit(ev.process_id, ev.exit_code)


class _ArtifactItemDispatchWorker(QObject):
    """Short-lived worker that dispatches the next artifact item on a background thread."""

    def __init__(
        self,
        dispatch_proxy: "_DispatchProxy",
        tool_call_id: str,
    ) -> None:
        super().__init__()
        self._dispatch_proxy = dispatch_proxy
        self._tool_call_id = tool_call_id

    def run(self) -> None:
        result = self._dispatch_proxy.dispatch_next_item(self._tool_call_id)
        _log.info(
            "dispatch_next_item completed tool_call_id=%s result_ok=%s",
            self._tool_call_id, result.ok if result is not None else "None",
        )
        self.thread().quit()


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
    usageEmitted = Signal(int, int, int, int)
    usageWithModel = Signal(str, int, int, int, int)

    # Planner / worker signals (re-exposed from the dispatch proxy so the GUI
    # binds to a single object).
    workerDispatchRequested = Signal(str, str, list, str, str, str)
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
    workflowStateChanged = Signal(object)  # WorkflowState snapshot
    workerTerminalOutput = Signal(str, str, str)  # parent_tool_id, worker_tool_id, text
    workerAgentProcessStarted = Signal(str, str, str, str)
    workerAgentProcessOutput = Signal(str, str, str)
    workerAgentProcessFinished = Signal(str, str, object)
    artifactProjectionUpdated = Signal(object)  # WorkArtifactProjection

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
        self._planner_provider = provider
        self._worker_provider = provider
        
        self._planner_backend = APIAgentBackend(provider=provider)
        self._worker_backend = APIAgentBackend(provider=provider)
        
        # Register the backends for planner and worker
        model_streams.unregister('generate_planner_code')
        model_streams.register('generate_planner_code', self._planner_backend.stream)
        model_streams.unregister('generate_worker_code')
        model_streams.register('generate_worker_code', self._worker_backend.stream)
        
        self._history = History()
        self._registry = ToolRegistry(workspace_root=_dummy_root(), mode="single")
        self._manager = ConversationManager(self._history, self._registry)
        self._parent_widget = parent_widget
        self._approval_proxy = _ApprovalProxy(parent_widget)

        # Dispatch proxy (used only when planner_worker_mode is on).
        self._dispatch_proxy = _DispatchProxy(
            parent_widget=parent_widget,
            registry_factory=self._make_worker_registry,
            approval_proxy=self._approval_proxy,
            workspace_root=self._registry.workspace_root,
            provider=provider,
        )

        self._cancel: threading.Event = threading.Event()
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._index_to_id: dict[int, str] = {}
        self._index_to_name: dict[int, str] = {}
        self._last_proposed_tool_call_id: str | None = None
        self._active_model: str = ""

        self._planner_worker_mode: bool = False  # configured by main_window
        self._temperature: float = 0.7
        self._single_system_prompt: str = ""
        self._planner_system_prompt: str = ""
        self._tier1_context: str = ""
        self._context_gearbox_metadata: dict = {}
        self._auto_dispatch: bool = False
        self._pre_worker_sha: str | None = None
        self._active_prompt_mode: str | None = None
        self._turn_task_kind: str | None = None

        # Re-emit dispatch proxy signals on the bridge so the GUI binds once.
        self._dispatch_proxy.showSpecCard.connect(self.workerDispatchRequested)
        self._dispatch_proxy.workerStarted.connect(self.workerStarted)
        self._dispatch_proxy.workerFinished.connect(self.workerFinished)
        self._dispatch_proxy.workerCancelled.connect(self.workerCancelled)
        self._dispatch_proxy.workerReasoningDelta.connect(self.workerReasoningDelta)
        self._dispatch_proxy.workerContentDelta.connect(self.workerContentDelta)
        self._dispatch_proxy.workerToolCallStart.connect(self.workerToolCallStart)
        self._dispatch_proxy.workerToolCallArgs.connect(self.workerToolCallArgs)
        self._dispatch_proxy.workerToolCallEnd.connect(self.workerToolCallEnd)
        self._dispatch_proxy.workerToolResult.connect(self.workerToolResult)
        self._dispatch_proxy.workerDiffDecided.connect(self.workerDiffDecided)
        self._dispatch_proxy.workerApiError.connect(self.workerApiError)
        self._dispatch_proxy.workerUsage.connect(self.workerUsage)
        self._dispatch_proxy.workerActivityUpdated.connect(self.workerActivityUpdated)
        self._dispatch_proxy.workerTodoUpdated.connect(self.workerTodoUpdated)
        self._dispatch_proxy.workflowStateChanged.connect(self.workflowStateChanged)
        self._dispatch_proxy.workerTerminalOutput.connect(self.workerTerminalOutput)
        self._dispatch_proxy.workerAgentProcessStarted.connect(self.workerAgentProcessStarted)
        self._dispatch_proxy.workerAgentProcessOutput.connect(self.workerAgentProcessOutput)
        self._dispatch_proxy.workerAgentProcessFinished.connect(self.workerAgentProcessFinished)
        self._dispatch_proxy.artifactProjectionUpdated.connect(self.artifactProjectionUpdated)

    # ---- config -----------------------------------------------------------

    @property
    def history(self) -> History:
        return self._history

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def planner_worker_mode(self) -> bool:
        return self._planner_worker_mode

    @property
    def auto_dispatch(self) -> bool:
        return self._auto_dispatch

    @property
    def dispatch_records(self) -> "list[WorkerDispatchRecord]":
        return self._dispatch_proxy.records()

    def set_dispatch_records(self, records: list[WorkerDispatchRecord]) -> None:
        self._dispatch_proxy.set_records(records)

    def clear_dispatch_records(self) -> None:
        self._dispatch_proxy.clear_records()

    def worker_result_metadata(self, tool_call_id: str) -> dict:
        return self._dispatch_proxy.result_metadata(tool_call_id)

    def context_gearbox_metadata(self) -> dict:
        return copy.deepcopy(self._context_gearbox_metadata)

    def set_workspace_root(self, root) -> None:
        if root is None:
            self._tier1_context = ""
            self._context_gearbox_metadata = {}
            return
        self._registry.set_workspace_root(root)
        self._dispatch_proxy.set_workspace_root(root)
        self._manager.set_workspace_root(root)
        self.refresh_tier1_context()

    def set_read_only(self, value: bool) -> None:
        self._registry.set_read_only(value)

    def set_system_prompt(self, prompt: str) -> None:
        role = self._active_runtime_role()
        composed = self._compose_prompt(role, prompt)
        self._history.set_system(composed.system_prompt)

    def _compute_and_cache_tier1(self, force_repo_map: bool = False) -> None:
        """Recompute runtime context from the current workspace root and cache it."""
        context = build_context_text(
            self._active_runtime_role(),
            self._registry.workspace_root,
            force=force_repo_map,
            task_kind=self._turn_task_kind,
        )
        self._context_gearbox_metadata = context_gearbox_metadata(
            context.ledger, workspace_root=self._registry.workspace_root,
        )
        self._tier1_context = context.context_text
        self._dispatch_proxy.set_tier1_context(self._tier1_context)

    def refresh_tier1_context(self, force_repo_map: bool = False) -> None:
        """Refresh workspace context and reapply the active system prompt."""
        role = self._active_runtime_role()
        composed = self._compose_prompt(
            role,
            self._custom_prompt_for_role(role),
            force_repo_map=force_repo_map,
        )
        self._history.set_system(composed.system_prompt)

    def set_planner_worker_mode(self, enabled: bool) -> None:
        self._planner_worker_mode = enabled
        self._compute_and_cache_tier1()
        mode_key = "planner" if enabled else "single"
        self._registry.set_mode(mode_key)
        if self._active_prompt_mode == mode_key:
            return  # Already set, avoid churn
        role = self._active_runtime_role()
        composed = self._compose_prompt(role, self._custom_prompt_for_role(role))
        self._history.set_system(composed.system_prompt)
        self._active_prompt_mode = mode_key

    def set_temperature(self, temperature: float) -> None:
        self._temperature = temperature

    def set_custom_system_prompts(self, single: str, planner: str, worker: str) -> None:
        self._single_system_prompt = single
        self._planner_system_prompt = planner
        self._dispatch_proxy.set_worker_system_prompt(worker)
        role = self._active_runtime_role()
        composed = self._compose_prompt(role, self._custom_prompt_for_role(role))
        self._history.set_system(composed.system_prompt)

    def set_worker_model(self, model: ModelId) -> None:
        self._dispatch_proxy.set_worker_model(model)

    def set_worker_thinking(self, thinking: ThinkingMode) -> None:
        self._dispatch_proxy.set_worker_thinking(thinking)

    def set_worker_temperature(self, temperature: float) -> None:
        self._dispatch_proxy.set_worker_temperature(temperature)

    def set_auto_dispatch(self, enabled: bool) -> None:
        self._auto_dispatch = enabled

    def set_auto_approve(self, enabled: bool) -> None:
        self._approval_proxy.set_approve_all_session(enabled)
        self._dispatch_proxy.set_auto_approve(enabled)

    def _active_runtime_role(self) -> RuntimeRole:
        return RuntimeRole.PLANNER if self._planner_worker_mode else RuntimeRole.SINGLE

    def _custom_prompt_for_role(self, role: RuntimeRole) -> str:
        if role == RuntimeRole.PLANNER:
            return self._planner_system_prompt
        if role == RuntimeRole.SINGLE:
            return self._single_system_prompt
        return ""

    def _compose_prompt(
        self,
        role: RuntimeRole,
        custom_prompt: str,
        *,
        force_repo_map: bool = False,
    ):
        composed = compose_system_prompt(
            role,
            custom_prompt,
            self._registry.workspace_root,
            force=force_repo_map,
            task_kind=self._turn_task_kind,
        )
        self._context_gearbox_metadata = context_gearbox_metadata(
            composed.ledger, workspace_root=self._registry.workspace_root,
        )
        self._tier1_context = composed.context_text
        self._dispatch_proxy.set_tier1_context(self._tier1_context)
        return composed

    def set_planner_provider(self, provider: ProviderId) -> None:
        """Update the planner provider and its backend hook."""
        self._planner_provider = provider
        self._planner_backend = APIAgentBackend(provider=provider)
        model_streams.unregister('generate_planner_code')
        model_streams.register('generate_planner_code', self._planner_backend.stream)

    def set_worker_provider(self, provider: ProviderId) -> None:
        """Update the worker provider and its backend hook."""
        self._worker_provider = provider
        self._worker_backend = APIAgentBackend(provider=provider)
        model_streams.unregister('generate_worker_code')
        model_streams.register('generate_worker_code', self._worker_backend.stream)

    def set_provider(self, provider: ProviderId) -> None:
        """Update both planner and worker to the same provider."""
        self.set_planner_provider(provider)
        self.set_worker_provider(provider)

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
        self._dispatch_proxy.clear_records()
        self._registry.reset_drone_budget()
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

    # ---- worker registry factory -----------------------------------------

    def _make_worker_registry(self, mode: str) -> ToolRegistry:
        worker_reg = ToolRegistry(
            workspace_root=self._registry.workspace_root,
            read_only=self._registry.read_only,
            mode="worker" if mode == "worker" else "single",
        )
        return worker_reg

    # ---- dispatch button-pressed handlers (GUI -> bridge) -----------------

    def user_dispatched(
        self,
        tool_call_id: str,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
        summary: str,
    ) -> bool:
        return self._dispatch_proxy.user_dispatched(tool_call_id, goal, files, spec, acceptance, summary)

    def user_cancelled_dispatch(self, tool_call_id: str) -> bool:
        return self._dispatch_proxy.user_cancelled(tool_call_id)

    def dispatch_next_artifact_item(self, tool_call_id: str) -> None:
        """Review and dispatch the next artifact item on a background thread.

        Called from the GUI thread when the user clicks "Review current item"
        on the WorkArtifactCard.  Spawns a short-lived QThread that calls
        DispatchProxy.dispatch_next_item() (which blocks on user decision).
        """
        thread = QThread(self)
        worker = _ArtifactItemDispatchWorker(
            self._dispatch_proxy, tool_call_id,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    # ---- send / cancel ----------------------------------------------------

    def send(self, model: ModelId, thinking: ThinkingMode, max_tool_rounds: int | None = None) -> None:
        if self.is_running():
            return
        self._registry.reset_drone_budget()
        self._prepare_turn_context()
        # Capture pre-worker snapshot for reliable /undo
        if self._registry.workspace_root is not None:
            from aura.git_ops import snapshot
            self._pre_worker_sha = snapshot(self._registry.workspace_root)
        else:
            self._pre_worker_sha = None
        self._cancel = threading.Event()
        self._index_to_id.clear()
        self._index_to_name.clear()
        self._active_model = str(model)
        self._dispatch_proxy.set_max_tool_rounds(max_tool_rounds)
        if self._registry.workspace_root is not None:
            if self._planner_worker_mode:
                base_prompt = self._planner_system_prompt if self._planner_system_prompt else PLANNER_SYSTEM_PROMPT
            else:
                base_prompt = self._single_system_prompt if self._single_system_prompt else SINGLE_SYSTEM_PROMPT
            self._manager.configure_for_planner(
                base_prompt=base_prompt,
                workspace_root=self._registry.workspace_root,
            )
        self._thread = QThread()
        self._worker = _Worker(
            manager=self._manager,
            approval_proxy=self._approval_proxy,
            dispatch_proxy=self._dispatch_proxy if self._planner_worker_mode else None,
            cancel_event=self._cancel,
            model=model,
            thinking=thinking,
            temperature=self._temperature,
            workspace_root=self._registry.workspace_root,
            max_tool_rounds=max_tool_rounds,
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
        self._worker.workerDispatchRequested.connect(self._on_worker_dispatch_requested)
        self._worker.usageEmitted.connect(self.usageEmitted)
        self._worker.usageEmitted.connect(self._forward_usage_with_model)
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
        self._dispatch_proxy.cancel_all_pending()

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

    @Slot(str, str, list, str, str, str)
    def _on_worker_dispatch_requested(
        self,
        tool_call_id: str,
        goal: str,
        files: list,
        spec: str,
        acceptance: str,
        summary: str,
    ) -> None:
        # The proxy's showSpecCard is the GUI's source of truth for spec
        # cards — the manager event arrives milliseconds earlier on the same
        # thread, so we just no-op here.
        return

    @Slot(int, int, int, int)
    def _forward_usage_with_model(
        self, prompt: int, completion: int, hit: int, miss: int
    ) -> None:
        self.usageWithModel.emit(self._active_model, prompt, completion, hit, miss)

    @Slot()
    def _on_finished(self) -> None:
        thread = self._thread
        worker = self._worker
        self._thread = None
        self._worker = None

        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.quit()
            thread.wait(2000)
            thread.deleteLater()
        self.finished.emit()

    def _prepare_turn_context(self) -> None:
        task_kind = _research_task_kind_for_text(_latest_user_text(self._history))
        if task_kind == self._turn_task_kind:
            return
        self._turn_task_kind = task_kind
        if self._registry.workspace_root is None:
            return
        role = self._active_runtime_role()
        composed = self._compose_prompt(role, self._custom_prompt_for_role(role))
        self._history.set_system(composed.system_prompt)


def _dummy_root():
    return Path.home()


def _research_task_kind_for_text(text: str) -> str | None:
    decision = decide_research_policy(text)
    if decision.route == NO_RESEARCH:
        return None
    return decision.route


def _latest_user_text(history: History) -> str:
    for message in reversed(history.messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            return "\n".join(part for part in parts if part)
    return ""
