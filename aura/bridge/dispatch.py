"""Dispatch proxy, pending state, and worker result helpers.

Routes dispatch_to_worker calls through the GUI (SpecCard) and runs
the worker manager when the user clicks Dispatch.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QObject,
    Signal,
)

from aura.bridge.approval_proxy import _ApprovalProxy
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
from aura.prompts import (
    WORKER_SYSTEM_PROMPT,
    inject_private_worker_style,
    inject_tier1_context,
)

__all__ = [
    "_DispatchProxy",
    "_DispatchPending",
    "_format_spec_as_user_message",
    "_build_worker_summary",
    "_last_assistant_content",
]


class _DispatchPending:
    """Per-dispatch state on the bridge."""

    def __init__(self, request: WorkerDispatchRequest) -> None:
        self.request = request
        self.edited_request: WorkerDispatchRequest | None = None
        self.cancelled: bool = False
        self.decision_event: threading.Event = threading.Event()
        self.cancel_event: threading.Event | None = None


class _DispatchProxy(QObject):
    """Routes dispatch_to_worker calls through the GUI (SpecCard) and runs
    the worker manager when the user clicks Dispatch.

    The planner thread calls request_dispatch(); we marshal a "show card"
    signal to the GUI thread, then block on a threading.Event until the user
    clicks Dispatch (after which we run the worker on this same thread, then
    signal back) or Cancel (we just return immediately).
    """

    showSpecCard = Signal(str, str, list, str, str, str)  # tool_id, goal, files, spec, acceptance, summary
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
    workerAgentProcessStarted = Signal(str, str, str, str)  # parent_tool_id, process_id, label, command
    workerAgentProcessOutput = Signal(str, str, str)  # parent_tool_id, process_id, text
    workerAgentProcessFinished = Signal(str, str, int)  # parent_tool_id, process_id, exit_code

    def __init__(
        self,
        parent_widget,
        registry_factory,
        approval_proxy: _ApprovalProxy,
        workspace_root: Path | None = None,
        provider: ProviderId = "deepseek",
    ) -> None:
        super().__init__()
        self._parent_widget = parent_widget
        self._registry_factory = registry_factory
        self._approval_proxy = approval_proxy
        self._workspace_root = workspace_root
        self._provider = provider

        self._worker_model: ModelId = DEFAULT_WORKER_MODEL
        self._worker_thinking: ThinkingMode = DEFAULT_WORKER_THINKING
        self._worker_temperature: float = 0.7
        self._worker_system_prompt: str = ""
        self._auto_commit_enabled: bool = True
        self._tier1_context: str = ""

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

    def set_tier1_context(self, context: str) -> None:
        self._tier1_context = context

    def set_auto_approve(self, enabled: bool) -> None:
        self._approval_proxy.set_approve_all_session(enabled)

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
            tool_call_id, req.goal, list(req.files), req.spec, req.acceptance, req.summary
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
        summary: str,
    ) -> None:
        with self._lock:
            pending = self._pending.get(tool_call_id)
        if pending is None:
            return
        pending.edited_request = WorkerDispatchRequest(
            goal=goal, files=list(files), spec=spec, acceptance=acceptance, summary=summary
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
        base_prompt = self._worker_system_prompt if self._worker_system_prompt else WORKER_SYSTEM_PROMPT
        full_prompt = inject_tier1_context(base_prompt, self._tier1_context)
        full_prompt = inject_private_worker_style(full_prompt)
        worker_history.set_system(full_prompt)
        worker_history.append_user_text(_format_spec_as_user_message(req))

        worker_registry = self._registry_factory("worker")
        worker_manager = ConversationManager(worker_history, worker_registry)

        self.workerStarted.emit(tool_call_id)
        cancel_event = threading.Event()
        pending.cancel_event = cancel_event

        # Track worker tool calls for the structured report and to map
        # streaming index -> id for arg/end signals.
        index_to_id: dict[int, str] = {}
        write_results: list[dict[str, Any]] = []
        api_errors: list[str] = []
        phase_boundary_info: dict[str, Any] | None = None

        def on_event(ev: Event) -> None:
            nonlocal phase_boundary_info
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
                if approval:
                    last = self._approval_proxy.consume_last_event()
                    if last is not None:
                        self.workerDiffDecided.emit(
                            tool_call_id,
                            ev.tool_call_id,
                            str(approval),
                            str(last["rel_path"]),
                            str(last["old_content"]),
                            str(last["new_content"]),
                            bool(last["is_new_file"]),
                        )
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
                    isinstance(parsed, dict)
                    and parsed.get("recoverable")
                    and parsed.get("phase_boundary")
                ):
                    phase_boundary_info = parsed
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
            elif isinstance(ev, AgentProcessStarted):
                self.workerAgentProcessStarted.emit(
                    tool_call_id,
                    ev.process_id,
                    ev.label,
                    ev.command,
                )
            elif isinstance(ev, AgentProcessOutput):
                self.workerAgentProcessOutput.emit(tool_call_id, ev.process_id, ev.text)
            elif isinstance(ev, AgentProcessFinished):
                self.workerAgentProcessFinished.emit(tool_call_id, ev.process_id, ev.exit_code)

        try:
            worker_manager.send(
                on_event=on_event,
                approval_cb=self._approval_proxy.request_approval,
                cancel_event=cancel_event,
                model=self._worker_model,
                thinking=self._worker_thinking,
                dispatch_cb=None,
                temperature=self._worker_temperature,
                hook_name='generate_worker_code',
            )
        except Exception as exc:
            api_errors.append(f"{type(exc).__name__}: {exc}")

        if cancel_event.is_set():
            worker_history.pop_if_empty_assistant_message()

        final_report = _last_assistant_content(worker_history)
        continuation = _parse_continuation_report(final_report)
        result_errors = list(api_errors)
        result_caveats: list[str] = []
        if _final_report_claims_failure(final_report):
            result_errors.append(
                "Worker final report claims a blocker, failed validation, failed acceptance, "
                "or unverified acceptance."
            )
        if req.acceptance.strip() and not _final_report_claims_validation(final_report):
            result_caveats.append(
                "Worker final report did not clearly mention validation or acceptance verification."
            )
        summary = _build_worker_summary(
            req,
            worker_history,
            write_results,
            result_errors,
            continuation,
            result_caveats,
        )
        phase_boundary = phase_boundary_info is not None
        ok = (
            not result_errors
            and not result_caveats
            and not phase_boundary
            and bool(write_results or final_report)
        )
        modified_files = continuation.get("modified_files") or [
            str(w["path"]) for w in write_results if isinstance(w.get("path"), str) and w.get("path")
        ]

        record = WorkerDispatchRecord(
            after_message_index=-1,
            tool_call_id=tool_call_id,
            spec=req.to_dict(),
            worker_history=list(worker_history.messages),
            result_summary=summary,
        )
        self._records.append(record)

        # Auto-save this dispatch record to project memory (Tier 2).
        if self._workspace_root is not None:
            from aura.conversation.persistence import save_dispatch_record_to_memory
            save_dispatch_record_to_memory(record, self._workspace_root)

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
            needs_followup=phase_boundary,
            phase_boundary=phase_boundary,
            followup_reason=(
                str(phase_boundary_info.get("reason")) if phase_boundary_info else None
            ),
            recoverable=phase_boundary,
            completed=continuation.get("completed", []),
            remaining=continuation.get("remaining", []),
            modified_files=modified_files,
            validation=continuation.get("validation_text"),
            suggested_next_spec=continuation.get("recommended_next_step"),
            extras={
                "writes": write_results,
                "errors": result_errors,
                "caveats": result_caveats,
                "phase_boundary": phase_boundary_info or {},
                "limit": (
                    phase_boundary_info
                    if phase_boundary_info and phase_boundary_info.get("limit_reached")
                    else {}
                ),
            },
        )


def _format_spec_as_user_message(req: WorkerDispatchRequest) -> str:
    files_block = "\n".join(f"- {p}" for p in req.files) if req.files else "(none listed)"
    return (
        f"Goal: {req.goal}\n\n"
        f"Files involved:\n{files_block}\n\n"
        f"Builder note:\n{req.spec}\n\n"
        f"Acceptance criteria:\n{req.acceptance}\n\n"
        "Worker contract:\n"
        "- Read every listed file before editing.\n"
        "- Start with a TODO list and keep it updated.\n"
        "- Build the smallest complete implementation.\n"
        "- Own exact edits, validation, and code-quality decisions.\n"
        "- Code must work and be easy to work on.\n"
        "- Avoid public-library, tutorial, or demo ceremony unless requested.\n"
        "- Avoid module summary docstrings and Args/Returns/Raises in normal app/tool code.\n"
        "- Do not add fake architecture.\n"
        "- Helpers return values or raise; CLI/UI/app boundary reports.\n"
        "- Validate actual behavior when practical.\n"
        "- Do not report Done unless acceptance passed.\n\n"
        "Begin. Read the listed files first, then make the change(s)."
    )


def _last_assistant_content(history: History) -> str:
    for msg in reversed(history.messages):
        if msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""


def _final_report_claims_failure(content: str) -> bool:
    text = content.lower()
    return any(
        phrase in text
        for phrase in (
            "blocker",
            "blocked",
            "failed validation",
            "validation failed",
            "failed acceptance",
            "acceptance failed",
            "could not verify",
            "couldn't verify",
            "cannot verify",
            "unable to verify",
            "not verified",
            "could not run",
            "couldn't run",
            "unable to run",
            "tests failed",
            "pytest failed",
            "lint failed",
        )
    )


def _final_report_claims_validation(content: str) -> bool:
    text = content.lower()
    return any(
        phrase in text
        for phrase in (
            "verified",
            "validated",
            "validation",
            "passes",
            "pass",
            "pytest",
            "py_compile",
            "ruff",
            "mypy",
            "test",
            "check",
            "compiled",
            "exit code 0",
            "exits 0",
        )
    )


def _build_worker_summary(
    req: WorkerDispatchRequest,
    history: History,
    writes: list[dict[str, Any]],
    errors: list[str],
    continuation: dict[str, Any] | None = None,
    caveats: list[str] | None = None,
) -> str:
    lines: list[str] = []
    continuation = continuation or {}
    caveats = caveats or []

    # 1. Errors first
    if errors:
        lines.append("Worker encountered errors:")
        for err in errors:
            lines.append(f"  - {err}")

    if caveats:
        if lines:
            lines.append("")
        lines.append("Worker validation caveats:")
        for caveat in caveats:
            lines.append(f"  - {caveat}")

    # 2. Planner's intended summary (if no errors, or as context)
    if req.summary:
        if lines:
            lines.append("")
        lines.append(req.summary.strip())

    # 3. List of modified files
    if writes:
        if lines:
            lines.append("")
        lines.append("Files modified:")
        for w in writes:
            tag = "(new)" if w.get("is_new_file") else f"({w.get('tool')})"
            lines.append(f"  - {w.get('path')} {tag}")

    if continuation.get("remaining"):
        if lines:
            lines.append("")
        lines.append("Worker returned for planner follow-up. Remaining work:")
        for item in continuation["remaining"]:
            lines.append(f"  - {item}")

    if continuation.get("validation_text"):
        if lines:
            lines.append("")
        lines.append("Validation:")
        lines.append(str(continuation["validation_text"]).strip())

    if not lines:
        lines.append("Worker finished with no changes.")

    return "\n".join(lines).strip()


def _parse_continuation_report(content: str) -> dict[str, Any]:
    """Extract the worker continuation report fields from its final text."""
    if not content:
        return {}

    def section(name: str) -> str:
        match = re.search(
            rf"<{name}>\s*(.*?)\s*</{name}>",
            content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        return match.group(1).strip() if match else ""

    def list_section(name: str) -> list[str]:
        raw = section(name)
        if not raw:
            return []
        items: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith(("-", "*")):
                line = line[1:].strip()
            items.append(line)
        return items

    return {
        "status": section("status"),
        "reason": section("reason"),
        "completed": list_section("completed"),
        "modified_files": list_section("modified_files"),
        "validation_text": section("validation"),
        "remaining": list_section("remaining"),
        "recommended_next_step": section("recommended_next_step"),
    }
