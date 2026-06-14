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
    ApiError,
    AgentProcessFinished,
    AgentProcessOutput,
    AgentProcessStarted,
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
from aura.hooks import hooks
from aura.config import (
    ModelId,
    ProviderId,
    ThinkingMode,
)
from aura.conversation import (
    ConversationManager,
    History,
    WorkerDispatchRequest,
)
from aura.conversation.tools import (
    ToolRegistry,
)
from aura.prompts import (
    PLANNER_SYSTEM_PROMPT,
    SINGLE_SYSTEM_PROMPT,
    build_tier1_context,
    inject_tier1_context,
)


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
            self._manager.send(
                on_event=self._on_event,
                approval_cb=self._approval_proxy.request_approval,
                cancel_event=self._cancel,
                model=self._model,
                thinking=self._thinking,
                dispatch_cb=dispatch_cb,
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
                ev.tool_call_id, ev.goal, list(ev.files), ev.spec, ev.acceptance, ev.summary
            )
        elif isinstance(ev, TerminalOutput):
            self.terminalOutput.emit(ev.tool_call_id, ev.text)
        elif isinstance(ev, AgentProcessStarted):
            self.agentProcessStarted.emit(ev.process_id, ev.label, ev.command)
        elif isinstance(ev, AgentProcessOutput):
            self.agentProcessOutput.emit(ev.process_id, ev.text)
        elif isinstance(ev, AgentProcessFinished):
            self.agentProcessFinished.emit(ev.process_id, ev.exit_code)


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
    workerTodoListUpdated = Signal(str, list)
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
        self._planner_provider = provider
        self._worker_provider = provider
        
        self._planner_backend = APIAgentBackend(provider=provider)
        self._worker_backend = APIAgentBackend(provider=provider)
        
        # Register the backends for planner and worker
        hooks.register('generate_planner_code', self._planner_backend.stream)
        hooks.register('generate_worker_code', self._worker_backend.stream)
        
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
        self._show_planner_reasoning: bool = False
        self._temperature: float = 0.7
        self._single_system_prompt: str = ""
        self._planner_system_prompt: str = ""
        self._tier1_context: str = ""
        self._auto_dispatch: bool = False
        self._pre_worker_sha: str | None = None
        self._active_prompt_mode: str | None = None

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
        self._dispatch_proxy.workerTodoListUpdated.connect(self.workerTodoListUpdated)
        self._dispatch_proxy.workerTerminalOutput.connect(self.workerTerminalOutput)
        self._dispatch_proxy.workerAgentProcessStarted.connect(self.workerAgentProcessStarted)
        self._dispatch_proxy.workerAgentProcessOutput.connect(self.workerAgentProcessOutput)
        self._dispatch_proxy.workerAgentProcessFinished.connect(self.workerAgentProcessFinished)

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

    def set_workspace_root(self, root) -> None:
        self._registry.set_workspace_root(root)
        self._dispatch_proxy.set_workspace_root(root)
        self._manager.set_workspace_root(root)
        self.refresh_tier1_context()

    def set_read_only(self, value: bool) -> None:
        self._registry.set_read_only(value)

    def set_system_prompt(self, prompt: str) -> None:
        # Inject Tier 1 core context (project rules + repo map) into the system prompt.
        workspace_root = self._registry.workspace_root
        if workspace_root is not None:
            self._tier1_context = build_tier1_context(workspace_root)
        enriched = inject_tier1_context(prompt, self._tier1_context)
        self._history.set_system(enriched)

    def _compute_and_cache_tier1(self) -> None:
        """Recompute Tier 1 context from the current workspace root and cache it."""
        workspace_root = self._registry.workspace_root
        if workspace_root is not None:
            self._tier1_context = build_tier1_context(workspace_root)
        self._dispatch_proxy.set_tier1_context(self._tier1_context)

    def refresh_tier1_context(self) -> None:
        """Refresh workspace context and reapply the active system prompt."""
        self._compute_and_cache_tier1()
        if self._planner_worker_mode:
            sys_prompt = self._planner_system_prompt if self._planner_system_prompt else PLANNER_SYSTEM_PROMPT
        else:
            sys_prompt = self._single_system_prompt if self._single_system_prompt else SINGLE_SYSTEM_PROMPT
        self._history.set_system(inject_tier1_context(sys_prompt, self._tier1_context))

    def set_planner_worker_mode(self, enabled: bool) -> None:
        self._planner_worker_mode = enabled
        self._compute_and_cache_tier1()
        mode_key = "planner" if enabled else "single"
        self._registry.set_mode(mode_key)
        if self._active_prompt_mode == mode_key:
            return  # Already set, avoid churn
        if enabled:
            sys_prompt = self._planner_system_prompt if self._planner_system_prompt else PLANNER_SYSTEM_PROMPT
        else:
            sys_prompt = self._single_system_prompt if self._single_system_prompt else SINGLE_SYSTEM_PROMPT
        self._history.set_system(inject_tier1_context(sys_prompt, self._tier1_context))
        self._active_prompt_mode = mode_key

    def set_show_planner_reasoning(self, enabled: bool) -> None:
        self._show_planner_reasoning = enabled

    def set_temperature(self, temperature: float) -> None:
        self._temperature = temperature

    def set_custom_system_prompts(self, single: str, planner: str, worker: str) -> None:
        self._single_system_prompt = single
        self._planner_system_prompt = planner
        self._dispatch_proxy.set_worker_system_prompt(worker)
        self._compute_and_cache_tier1()
        # Apply the prompt for the current mode immediately
        if self._planner_worker_mode:
            sys_prompt = self._planner_system_prompt if self._planner_system_prompt else PLANNER_SYSTEM_PROMPT
        else:
            sys_prompt = self._single_system_prompt if self._single_system_prompt else SINGLE_SYSTEM_PROMPT
        self._history.set_system(inject_tier1_context(sys_prompt, self._tier1_context))

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

    def set_planner_provider(self, provider: ProviderId) -> None:
        """Update the planner provider and its backend hook."""
        self._planner_provider = provider
        self._planner_backend = APIAgentBackend(provider=provider)
        hooks.unregister('generate_planner_code')
        hooks.register('generate_planner_code', self._planner_backend.stream)

    def set_worker_provider(self, provider: ProviderId) -> None:
        """Update the worker provider and its backend hook."""
        self._worker_provider = provider
        self._worker_backend = APIAgentBackend(provider=provider)
        hooks.unregister('generate_worker_code')
        hooks.register('generate_worker_code', self._worker_backend.stream)

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
        root = self._registry.workspace_root
        return True  # 'default_api' is always authenticated


    def reset_history(self) -> None:
        self._history.messages.clear()
        self._index_to_id.clear()
        self._index_to_name.clear()
        self._dispatch_proxy.clear_records()
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

    def dispatch_drone_build(self, req: WorkerDispatchRequest) -> str:
        """Run a Builder Worker dispatch on a background thread.

        The parent project conversation history is never touched.
        Returns the tool_call_id for tracking.
        """
        return self._dispatch_proxy.start_drone_build(req)

    # ---- send / cancel ----------------------------------------------------

    def send(self, model: ModelId, thinking: ThinkingMode, max_tool_rounds: int | None = None) -> None:
        if self.is_running():
            return
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

        if (not self._planner_worker_mode) or self._show_planner_reasoning:
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


def _dummy_root():
    return Path.home()
