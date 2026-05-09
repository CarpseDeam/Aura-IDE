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

import json
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QMetaObject,
    QObject,
    QThread,
    Qt,
    Signal,
    Slot,
)

from aura.client import (
    ApiError,
    ContentDelta,
    DeepSeekClient,
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
    DEFAULT_WORKER_MODEL,
    DEFAULT_WORKER_THINKING,
    ModelId,
    ProviderId,
    ThinkingMode,
)
from aura.conversation import (
    ConversationManager,
    History,
    WorkerDispatchRequest,
    WorkerDispatchResult,
)
from aura.conversation.persistence import WorkerDispatchRecord
from aura.conversation.tools import (
    ApprovalDecision,
    ApprovalRequest,
    ToolRegistry,
)
from aura.gui.diff_dialog import DiffApprovalDialog
from aura.prompts import PLANNER_SYSTEM_PROMPT, WORKER_SYSTEM_PROMPT


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
    workerDispatchRequested = Signal(str, str, list, str, str)
    terminalOutput = Signal(str, str)  # (tool_call_id, text)
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
        auto_commit_enabled: bool = True,
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
        self._auto_commit_enabled = auto_commit_enabled
        self._write_paths: list[str] = []

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
            )
            # Auto-commit writes in single mode
            if self._auto_commit_enabled and self._write_paths and self._workspace_root is not None:
                try:
                    from aura.git_ops import auto_commit
                    goal_msg = f"AI-assisted edit: {', '.join(self._write_paths)}"
                    summary_msg = f"Modified {len(self._write_paths)} file(s)"
                    threading.Thread(
                        target=auto_commit,
                        args=(self._workspace_root, goal_msg, self._write_paths, summary_msg),
                        daemon=True,
                    ).start()
                except Exception:
                    pass  # Never block the chat on git failures
        except Exception as exc:
            self.apiError.emit(-1, f"{type(exc).__name__}: {exc}")
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
            self.apiError.emit(ev.status_code if ev.status_code is not None else -1, ev.message)
        elif isinstance(ev, Done):
            if ev.full_message:
                self.streamDone.emit(ev.finish_reason or "", ev.full_message)
        elif isinstance(ev, ToolResult):
            self.toolResultEmitted.emit(ev.tool_call_id, ev.name, ev.ok, ev.result, ev.extras or {})
            # Track successful writes for auto-commit in single mode
            if ev.name in ("write_file", "edit_file") and ev.ok:
                try:
                    import json
                    parsed = json.loads(ev.result)
                    path = parsed.get("path")
                    if isinstance(path, str) and path:
                        self._write_paths.append(path)
                except Exception:
                    pass
        elif isinstance(ev, WorkerDispatchRequested):
            self.workerDispatchRequested.emit(
                ev.tool_call_id, ev.goal, list(ev.files), ev.spec, ev.acceptance
            )
        elif isinstance(ev, TerminalOutput):
            self.terminalOutput.emit(ev.tool_call_id, ev.text)


class _ApprovalProxy(QObject):
    """Marshals approval requests from any worker thread onto the GUI thread."""

    def __init__(self, parent_widget) -> None:
        super().__init__()
        self._parent_widget = parent_widget
        self._lock = threading.Lock()
        self._last_decision: ApprovalDecision = ApprovalDecision(action="reject")
        self._last_request: ApprovalRequest | None = None
        self.last_event: dict[str, Any] | None = None
        self._approve_all_session: bool = False

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        if self._approve_all_session:
            return ApprovalDecision(action="approve")
        with self._lock:
            self._last_request = request
            QMetaObject.invokeMethod(
                self,
                "_open_dialog",
                Qt.ConnectionType.BlockingQueuedConnection,
            )
            return self._last_decision

    @Slot()
    def _open_dialog(self) -> None:
        req = self._last_request
        if req is None:
            self._last_decision = ApprovalDecision(action="reject")
            return
        dlg = DiffApprovalDialog(req, parent=self._parent_widget)
        dlg.exec()
        self._last_decision = dlg.decision()
        if self._last_decision.action == "approve_all":
            self._approve_all_session = True
            self._last_decision = ApprovalDecision(action="approve")
        self.last_event = {
            "rel_path": req.rel_path,
            "old_content": req.old_content,
            "new_content": req.new_content,
            "is_new_file": req.is_new_file,
            "decision": self._last_decision.action,
        }

    def reset_approve_all(self) -> None:
        self._approve_all_session = False


class _DispatchProxy(QObject):
    """Routes dispatch_to_worker calls through the GUI (SpecCard) and runs
    the worker manager when the user clicks Dispatch.

    The planner thread calls request_dispatch(); we marshal a "show card"
    signal to the GUI thread, then block on a threading.Event until the user
    clicks Dispatch (after which we run the worker on this same thread, then
    signal back) or Cancel (we just return immediately).
    """

    showSpecCard = Signal(str, str, list, str, str)  # tool_id, goal, files, spec, acceptance
    workerStarted = Signal(str)  # tool_id
    workerFinished = Signal(str, bool, str)  # tool_id, ok, summary
    workerCancelled = Signal(str)
    workerReasoningDelta = Signal(str, str)
    workerContentDelta = Signal(str, str)
    workerToolCallStart = Signal(str, str, str)  # parent_id, worker_tool_id, name
    workerToolCallArgs = Signal(str, str, str)
    workerToolCallEnd = Signal(str, str)
    workerToolResult = Signal(str, str, str, bool, str, dict)
    workerDiffDecided = Signal(str, str, str, str, str, str, bool)
    workerStreamDone = Signal(str, str, dict)
    workerApiError = Signal(str, int, str)
    workerUsage = Signal(str, str, int, int, int, int)  # tool_id, model, prompt, comp, hit, miss
    workerTodoListUpdated = Signal(str, list)  # tool_call_id, tasks
    workerTerminalOutput = Signal(str, str, str)  # parent_tool_id, worker_tool_id, text

    def __init__(
        self,
        parent_widget,
        client: DeepSeekClient,
        registry_factory,
        approval_proxy: _ApprovalProxy,
        workspace_root: Path | None = None,
        provider: ProviderId = "deepseek",
    ) -> None:
        super().__init__()
        self._parent_widget = parent_widget
        self._client = client
        self._registry_factory = registry_factory
        self._approval_proxy = approval_proxy
        self._workspace_root = workspace_root
        self._provider = provider

        self._worker_model: ModelId = DEFAULT_WORKER_MODEL
        self._worker_thinking: ThinkingMode = DEFAULT_WORKER_THINKING
        self._worker_temperature: float = 0.7
        self._worker_system_prompt: str = ""
        self._auto_commit_enabled: bool = True

        # Per-call state — guarded by a lock so concurrent dispatches (which
        # shouldn't happen, but be safe) don't trample each other.
        self._lock = threading.Lock()
        self._pending: dict[str, _DispatchPending] = {}
        # Records of each completed dispatch for persistence.
        self._records: list[WorkerDispatchRecord] = []

    # ---- config -----------------------------------------------------------

    def set_workspace_root(self, root: Path) -> None:
        self._workspace_root = root

    def set_worker_model(self, model: ModelId) -> None:
        self._worker_model = model

    def set_worker_thinking(self, thinking: ThinkingMode) -> None:
        self._worker_thinking = thinking

    def set_worker_temperature(self, temperature: float) -> None:
        self._worker_temperature = temperature

    def set_worker_system_prompt(self, prompt: str) -> None:
        self._worker_system_prompt = prompt

    def set_auto_commit_enabled(self, enabled: bool) -> None:
        self._auto_commit_enabled = enabled

    def records(self) -> list[WorkerDispatchRecord]:
        return list(self._records)

    def clear_records(self) -> None:
        self._records.clear()

    # ---- planner-thread side ---------------------------------------------

    def request_dispatch(
        self, tool_call_id: str, req: WorkerDispatchRequest
    ) -> WorkerDispatchResult:
        """Called from the planner's worker thread. Blocks."""
        pending = _DispatchPending(request=req)
        with self._lock:
            self._pending[tool_call_id] = pending

        # Tell GUI thread to render the spec card; user will call user_dispatched
        # or user_cancelled, which will set decision_event.
        self.showSpecCard.emit(
            tool_call_id, req.goal, list(req.files), req.spec, req.acceptance
        )

        pending.decision_event.wait()
        if pending.cancelled:
            with self._lock:
                self._pending.pop(tool_call_id, None)
            return WorkerDispatchResult(
                ok=False,
                summary="user cancelled dispatch",
                cancelled=True,
            )

        edited = pending.edited_request or req
        result = self._run_worker(tool_call_id, edited, pending)
        with self._lock:
            self._pending.pop(tool_call_id, None)
        return result

    # ---- GUI-thread side --------------------------------------------------

    def user_dispatched(
        self,
        tool_call_id: str,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
    ) -> None:
        with self._lock:
            pending = self._pending.get(tool_call_id)
        if pending is None:
            return
        pending.edited_request = WorkerDispatchRequest(
            goal=goal, files=list(files), spec=spec, acceptance=acceptance
        )
        pending.cancelled = False
        pending.decision_event.set()

    def user_cancelled(self, tool_call_id: str) -> None:
        with self._lock:
            pending = self._pending.get(tool_call_id)
        if pending is None:
            return
        pending.cancelled = True
        pending.decision_event.set()

    def cancel_all_pending(self) -> None:
        """Called when the user hits Stop. Unblocks any planner waiting for a
        dispatch decision AND signals any running worker to cancel."""
        with self._lock:
            for tool_id, pending in list(self._pending.items()):
                # Unblock dispatch decision wait (if planner is waiting on SpecCard)
                if not pending.decision_event.is_set():
                    pending.cancelled = True
                    pending.decision_event.set()
                # Signal the worker's cancel event (if worker is running)
                if pending.cancel_event is not None:
                    pending.cancel_event.set()

    # ---- worker run -------------------------------------------------------

    def _run_worker(
        self,
        tool_call_id: str,
        req: WorkerDispatchRequest,
        pending: "_DispatchPending",
    ) -> WorkerDispatchResult:
        worker_history = History()
        worker_history.set_system(self._worker_system_prompt if self._worker_system_prompt else WORKER_SYSTEM_PROMPT)
        worker_history.append_user_text(_format_spec_as_user_message(req))

        worker_registry = self._registry_factory("worker")
        worker_manager = ConversationManager(self._client, worker_history, worker_registry)

        self.workerStarted.emit(tool_call_id)
        cancel_event = threading.Event()
        pending.cancel_event = cancel_event

        # Track worker tool calls for the structured report and to map
        # streaming index -> id for arg/end signals.
        index_to_id: dict[int, str] = {}
        write_results: list[dict[str, Any]] = []
        api_errors: list[str] = []

        def on_event(ev: Event) -> None:
            if isinstance(ev, ReasoningDelta):
                self.workerReasoningDelta.emit(tool_call_id, ev.text)
            elif isinstance(ev, ContentDelta):
                self.workerContentDelta.emit(tool_call_id, ev.text)
            elif isinstance(ev, ToolCallStart):
                index_to_id[ev.index] = ev.id
                self.workerToolCallStart.emit(tool_call_id, ev.id, ev.name)
            elif isinstance(ev, ToolCallArgsDelta):
                wid = index_to_id.get(ev.index, "")
                if wid:
                    self.workerToolCallArgs.emit(tool_call_id, wid, ev.args_chunk)
            elif isinstance(ev, ToolCallEnd):
                wid = index_to_id.get(ev.index, "")
                if wid:
                    self.workerToolCallEnd.emit(tool_call_id, wid)
            elif isinstance(ev, Usage):
                self.workerUsage.emit(
                    tool_call_id,
                    str(self._worker_model),
                    ev.prompt_tokens,
                    ev.completion_tokens,
                    ev.cache_hit_tokens,
                    ev.cache_miss_tokens,
                )
            elif isinstance(ev, Done):
                if ev.full_message:
                    self.workerStreamDone.emit(tool_call_id, ev.finish_reason or "", ev.full_message)
            elif isinstance(ev, ApiError):
                msg = f"{ev.status_code}: {ev.message}" if ev.status_code is not None else ev.message
                api_errors.append(msg)
                self.workerApiError.emit(
                    tool_call_id,
                    ev.status_code if ev.status_code is not None else -1,
                    ev.message,
                )
            elif isinstance(ev, ToolResult):
                approval = (ev.extras or {}).get("approval")
                if approval and self._approval_proxy.last_event is not None:
                    last = self._approval_proxy.last_event
                    self.workerDiffDecided.emit(
                        tool_call_id,
                        ev.tool_call_id,
                        str(approval),
                        str(last["rel_path"]),
                        str(last["old_content"]),
                        str(last["new_content"]),
                        bool(last["is_new_file"]),
                    )
                    self._approval_proxy.last_event = None
                self.workerToolResult.emit(
                    tool_call_id, ev.tool_call_id, ev.name, ev.ok, ev.result, ev.extras or {}
                )
                # If this is a todo list update, emit the dedicated signal for the pinned UI.
                if ev.name == "update_todo_list":
                    tasks = (ev.extras or {}).get("tasks", [])
                    self.workerTodoListUpdated.emit(tool_call_id, tasks)
                # Track writes for the summary back to the planner.
                try:
                    parsed = json.loads(ev.result)
                except (json.JSONDecodeError, TypeError):
                    parsed = {}
                if (
                    ev.name in ("write_file", "edit_file")
                    and isinstance(parsed, dict)
                    and parsed.get("ok")
                ):
                    write_results.append(
                        {
                            "tool": ev.name,
                            "path": parsed.get("path"),
                            "is_new_file": parsed.get("is_new_file", False),
                        }
                    )
            elif isinstance(ev, TerminalOutput):
                self.workerTerminalOutput.emit(tool_call_id, ev.tool_call_id, ev.text)

        try:
            worker_manager.send(
                on_event=on_event,
                approval_cb=self._approval_proxy.request_approval,
                cancel_event=cancel_event,
                model=self._worker_model,
                thinking=self._worker_thinking,
                dispatch_cb=None,
                temperature=self._worker_temperature,
            )
        except Exception as exc:
            api_errors.append(f"{type(exc).__name__}: {exc}")

        if cancel_event.is_set():
            worker_history.pop_if_empty_assistant_message()

        summary = _build_worker_summary(req, worker_history, write_results, api_errors)
        ok = not api_errors and bool(write_results or _last_assistant_content(worker_history))

        record = WorkerDispatchRecord(
            after_message_index=-1,
            tool_call_id=tool_call_id,
            spec=req.to_dict(),
            worker_history=list(worker_history.messages),
            result_summary=summary,
        )
        self._records.append(record)

        # Auto-commit if worker made changes — fire in background so dispatch isn't blocked.
        if self._auto_commit_enabled and self._workspace_root is not None and write_results:
            try:
                from aura.git_ops import auto_commit

                written_files = [w["path"] for w in write_results if isinstance(w.get("path"), str) and w.get("path")]
                if written_files:
                    def _do_commit(root, goal, files, summary):
                        auto_commit(root, goal, files, summary)
                    threading.Thread(
                        target=_do_commit,
                        args=(self._workspace_root, req.goal, written_files, summary),
                        daemon=True,
                    ).start()
            except Exception:
                pass  # Never block the chat on git failures

        self.workerFinished.emit(tool_call_id, ok, summary)
        return WorkerDispatchResult(
            ok=ok,
            summary=summary,
            cancelled=False,
            extras={"writes": write_results, "errors": api_errors},
        )


class _DispatchPending:
    """Per-dispatch state on the bridge."""

    def __init__(self, request: WorkerDispatchRequest) -> None:
        self.request = request
        self.edited_request: WorkerDispatchRequest | None = None
        self.cancelled: bool = False
        self.decision_event: threading.Event = threading.Event()
        self.cancel_event: threading.Event | None = None


def _format_spec_as_user_message(req: WorkerDispatchRequest) -> str:
    files_block = "\n".join(f"- {p}" for p in req.files) if req.files else "(none listed)"
    return (
        f"Goal: {req.goal}\n\n"
        f"Files involved:\n{files_block}\n\n"
        f"Spec:\n{req.spec}\n\n"
        f"Acceptance criteria:\n{req.acceptance}\n\n"
        "Begin. Read the listed files first, then make the change(s). When done, "
        "respond with a concise summary of what you changed and which files were touched."
    )


def _last_assistant_content(history: History) -> str:
    for msg in reversed(history.messages):
        if msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""


def _build_worker_summary(
    req: WorkerDispatchRequest,
    history: History,
    writes: list[dict[str, Any]],
    errors: list[str],
) -> str:
    lines: list[str] = []
    if errors:
        lines.append("Worker encountered errors:")
        for err in errors:
            lines.append(f"  - {err}")
    if writes:
        lines.append("Files modified:")
        for w in writes:
            tag = "(new)" if w.get("is_new_file") else f"({w.get('tool')})"
            lines.append(f"  - {w.get('path')} {tag}")
    final = _last_assistant_content(history)
    if final:
        lines.append("")
        lines.append("Worker's final report:")
        lines.append(final.strip())
    if not lines:
        lines.append("Worker finished with no changes and no final report.")
    return "\n".join(lines).strip()


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
    workerDispatchRequested = Signal(str, str, list, str, str)
    workerStarted = Signal(str)
    workerFinished = Signal(str, bool, str)
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

    # Terminal output (single mode)
    terminalOutput = Signal(str, str)  # tool_call_id, text

    def __init__(
        self,
        parent_widget,
        provider: ProviderId = "deepseek",
    ) -> None:
        super().__init__()
        self._provider = provider
        self._client = DeepSeekClient(provider=provider)
        self._history = History()
        self._registry = ToolRegistry(workspace_root=_dummy_root(), mode="single")
        self._manager = ConversationManager(self._client, self._history, self._registry)
        self._parent_widget = parent_widget
        self._approval_proxy = _ApprovalProxy(parent_widget)

        # Dispatch proxy (used only when planner_worker_mode is on).
        self._dispatch_proxy = _DispatchProxy(
            parent_widget=parent_widget,
            client=self._client,
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
        self._auto_commit_enabled: bool = True
        self._pre_worker_sha: str | None = None

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
    def dispatch_records(self) -> list[WorkerDispatchRecord]:
        return self._dispatch_proxy.records()

    def clear_dispatch_records(self) -> None:
        self._dispatch_proxy.clear_records()

    def set_workspace_root(self, root) -> None:
        self._registry.set_workspace_root(root)
        self._dispatch_proxy.set_workspace_root(root)

    def set_read_only(self, value: bool) -> None:
        self._registry.set_read_only(value)

    def set_system_prompt(self, prompt: str) -> None:
        self._history.set_system(prompt)

    def set_planner_worker_mode(self, enabled: bool) -> None:
        self._planner_worker_mode = enabled
        if enabled:
            self._registry.set_mode("planner")
            if not self._history.system_prompt or self._history.system_prompt == "":
                self._history.set_system(
                    self._planner_system_prompt if self._planner_system_prompt else PLANNER_SYSTEM_PROMPT
                )
        else:
            self._registry.set_mode("single")
            if not self._history.system_prompt or self._history.system_prompt == "":
                # Lazy import to avoid circular dependency at module level.
                from aura.prompts import SINGLE_SYSTEM_PROMPT as _SYS_PROMPT
                self._history.set_system(
                    self._single_system_prompt if self._single_system_prompt else _SYS_PROMPT
                )

    def set_temperature(self, temperature: float) -> None:
        self._temperature = temperature

    def set_custom_system_prompts(self, single: str, planner: str, worker: str) -> None:
        self._single_system_prompt = single
        self._planner_system_prompt = planner
        self._dispatch_proxy.set_worker_system_prompt(worker)
        # Apply to current history if appropriate
        if self._planner_worker_mode:
            if planner:
                self._history.set_system(planner)
        else:
            if single:
                self._history.set_system(single)

    def set_worker_model(self, model: ModelId) -> None:
        self._dispatch_proxy.set_worker_model(model)

    def set_worker_thinking(self, thinking: ThinkingMode) -> None:
        self._dispatch_proxy.set_worker_thinking(thinking)

    def set_worker_temperature(self, temperature: float) -> None:
        self._dispatch_proxy.set_worker_temperature(temperature)

    def set_auto_commit_enabled(self, enabled: bool) -> None:
        self._auto_commit_enabled = enabled
        self._dispatch_proxy.set_auto_commit_enabled(enabled)

    def set_provider(self, provider: ProviderId) -> None:
        """Recreate the internal client for a new provider."""
        # Capture current dispatch proxy settings before recreating.
        old_worker_temp = self._dispatch_proxy._worker_temperature
        old_worker_prompt = self._dispatch_proxy._worker_system_prompt
        old_auto_commit = self._dispatch_proxy._auto_commit_enabled
        self._provider = provider
        self._client = DeepSeekClient(provider=provider)
        self._manager = ConversationManager(self._client, self._history, self._registry)
        self._dispatch_proxy = _DispatchProxy(
            parent_widget=self._parent_widget,
            client=self._client,
            registry_factory=self._make_worker_registry,
            approval_proxy=self._approval_proxy,
            workspace_root=self._registry.workspace_root,
            provider=provider,
        )
        # Propagate saved settings to the new dispatch proxy.
        self._dispatch_proxy.set_worker_temperature(old_worker_temp)
        self._dispatch_proxy.set_worker_system_prompt(old_worker_prompt)
        self._dispatch_proxy.set_auto_commit_enabled(old_auto_commit)
        # Re-wire dispatch proxy signals.
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

    def reset_history(self) -> None:
        self._history.messages.clear()
        self._index_to_id.clear()
        self._index_to_name.clear()
        self._dispatch_proxy.clear_records()
        self._approval_proxy.reset_approve_all()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

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
    ) -> None:
        self._dispatch_proxy.user_dispatched(tool_call_id, goal, files, spec, acceptance)

    def user_cancelled_dispatch(self, tool_call_id: str) -> None:
        self._dispatch_proxy.user_cancelled(tool_call_id)

    # ---- send / cancel ----------------------------------------------------

    def send(self, model: ModelId, thinking: ThinkingMode) -> None:
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
            auto_commit_enabled=self._auto_commit_enabled,
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
        if approval and self._approval_proxy.last_event is not None:
            ev = self._approval_proxy.last_event
            self.diffDecided.emit(
                tool_id,
                str(approval),
                str(ev["rel_path"]),
                str(ev["old_content"]),
                str(ev["new_content"]),
                bool(ev["is_new_file"]),
            )
            self._approval_proxy.last_event = None
        self.toolResult.emit(tool_id, name, ok, result, extras)

    @Slot(str, str, list, str, str)
    def _on_worker_dispatch_requested(
        self,
        tool_call_id: str,
        goal: str,
        files: list,
        spec: str,
        acceptance: str,
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
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
            self._thread.deleteLater()
        if self._worker is not None:
            self._worker.deleteLater()
        self._thread = None
        self._worker = None
        self.finished.emit()


def _dummy_root():
    return Path.home()
