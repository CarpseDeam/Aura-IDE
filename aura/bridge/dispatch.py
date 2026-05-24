"""Dispatch proxy, pending state, and worker result helpers.

Routes dispatch_to_worker calls through the GUI (SpecCard) and runs
the worker manager when the user clicks Dispatch.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QObject,
    Signal,
)

from aura.bridge.approval_proxy import _ApprovalProxy
from aura.bridge.event_relay import WorkerEventRelay
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
    WorkerTaskSpec,
    normalize_worker_task,
)
from aura.conversation.tool_limits import WRITE_TOOLS
from aura.conversation.persistence import WorkerDispatchRecord
from aura.prompts import (
    WORKER_SYSTEM_PROMPT,
    build_tier1_context,
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

DISPATCH_TIMEOUT = 300.0


class _DispatchPending:
    def __init__(self, request: WorkerDispatchRequest) -> None:
        self.request = request
        self.edited_request: WorkerDispatchRequest | None = None
        self.cancelled: bool = False
        self.decision_event: threading.Event = threading.Event()
        self.cancel_event: threading.Event | None = None


class _DispatchProxy(QObject):
    showSpecCard = Signal(str, str, list, str, str, str)  # tool_id, goal, files, spec, acceptance, summary
    workerStarted = Signal(str)  # tool_id
    workerFinished = Signal(str, bool, str, bool)  # tool_id, ok, summary, needs_followup
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
    workerAgentProcessFinished = Signal(str, str, object)  # parent_tool_id, process_id, exit_code

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
        self._max_tool_rounds: int | None = None

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

    def set_max_tool_rounds(self, value: int | None) -> None:
        self._max_tool_rounds = value

    def records(self) -> list[WorkerDispatchRecord]:
        return list(self._records)

    def set_records(self, records: list[WorkerDispatchRecord]) -> None:
        self._records = list(records)

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

        signaled = pending.decision_event.wait(timeout=DISPATCH_TIMEOUT)
        if not signaled:
            with self._lock:
                self._pending.pop(tool_call_id, None)
            return WorkerDispatchResult(
                ok=False,
                recoverable=True,
                summary="Plan expired — click Dispatch again or Cancel",
                extras={"dispatch_not_started": True, "dispatch_approval_timeout": True},
            )

        if pending.cancelled:
            with self._lock:
                self._pending.pop(tool_call_id, None)
            return WorkerDispatchResult(
                ok=False,
                summary="Cancelled",
                cancelled=True,
                extras={"dispatch_not_started": True, "dispatch_cancelled": True},
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
    ) -> bool:
        with self._lock:
            pending = self._pending.get(tool_call_id)
        if pending is None:
            logging.warning(
                f"user_dispatched: tool_call_id '{tool_call_id}' is not pending or has already timed out/resolved."
            )
            return False
        pending.edited_request = replace(
            pending.request,
            goal=goal,
            files=list(files),
            spec=spec,
            acceptance=acceptance,
            summary=summary,
        )
        pending.cancelled = False
        pending.decision_event.set()
        return True

    def user_cancelled(self, tool_call_id: str) -> bool:
        with self._lock:
            pending = self._pending.get(tool_call_id)
        if pending is None:
            logging.warning(
                f"user_cancelled: tool_call_id '{tool_call_id}' is not pending or has already timed out/resolved."
            )
            return False
        pending.cancelled = True
        pending.decision_event.set()
        return True

    def cancel_all_pending(self) -> None:
        """Called when the user hits Stop. Unblocks any planner waiting for a
        dispatch decision AND signals any running worker to cancel."""
        if self._approval_proxy is not None:
            self._approval_proxy.cancel_active_dialog()

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
        # Refresh Tier 1 context so a prior Worker pass's blueprint is visible.
        if self._workspace_root is not None:
            try:
                tier1_context = build_tier1_context(self._workspace_root)
            except Exception:
                tier1_context = self._tier1_context
        else:
            tier1_context = self._tier1_context
        full_prompt = inject_tier1_context(base_prompt, tier1_context)
        full_prompt = inject_private_worker_style(full_prompt)
        worker_history.set_system(full_prompt)
        task_spec = normalize_worker_task(req)
        worker_history.append_user_text(_format_spec_as_user_message(task_spec))

        worker_registry = self._registry_factory("worker")
        # Set the Planner contract on the worker's registry for contract gate checks
        if task_spec.contract is not None:
            worker_registry.set_contract(task_spec.contract)
        worker_manager = ConversationManager(worker_history, worker_registry)

        self.workerStarted.emit(tool_call_id)
        cancel_event = threading.Event()
        pending.cancel_event = cancel_event

        relay = WorkerEventRelay(
            approval_proxy=self._approval_proxy,
            worker_model=str(self._worker_model),
        )
        # Forward relay signals to the dispatch proxy's signals for the UI.
        relay.reasoningDelta.connect(self.workerReasoningDelta)
        relay.contentDelta.connect(self.workerContentDelta)
        relay.toolCallStart.connect(self.workerToolCallStart)
        relay.toolCallArgs.connect(self.workerToolCallArgs)
        relay.toolCallEnd.connect(self.workerToolCallEnd)
        relay.usage.connect(self.workerUsage)
        relay.streamDone.connect(self.workerStreamDone)
        relay.apiError.connect(self.workerApiError)
        relay.toolResult.connect(self.workerToolResult)
        relay.diffDecided.connect(self.workerDiffDecided)
        relay.todoListUpdated.connect(self.workerTodoListUpdated)
        relay.terminalOutput.connect(self.workerTerminalOutput)
        relay.agentProcessStarted.connect(self.workerAgentProcessStarted)
        relay.agentProcessOutput.connect(self.workerAgentProcessOutput)
        relay.agentProcessFinished.connect(self.workerAgentProcessFinished)

        internal_error: str | None = None
        try:
            worker_manager.send(
                on_event=lambda ev: relay.relay(tool_call_id, ev),
                approval_cb=self._approval_proxy.request_approval,
                cancel_event=cancel_event,
                model=self._worker_model,
                thinking=self._worker_thinking,
                dispatch_cb=None,
                temperature=self._worker_temperature,
                hook_name='generate_worker_code',
                max_tool_rounds=self._max_tool_rounds,
            )
        except Exception as exc:
            from aura.config import redact_secrets

            internal_error = redact_secrets(f"{type(exc).__name__}: {exc}")

        if cancel_event.is_set():
            worker_history.pop_if_empty_assistant_message()

        final_report = _last_assistant_content(worker_history)
        continuation = _parse_continuation_report(final_report)
        is_partial = bool(continuation.get("status") == "needs_followup" or continuation.get("remaining"))
        claimed_validation = _final_report_claims_validation(final_report) or bool(continuation.get("validation_text"))

        has_writes = bool(relay.write_results)
        failed_write_tools = [r for r in relay.failed_tool_results if r["name"] in WRITE_TOOLS]
        failed_validation = [v for v in relay.validation_results if not v["ok"]]
        validation_ran = bool(relay.validation_results)
        missing_validation_after_writes = has_writes and not validation_ran

        # Compute acceptance-unverified
        acceptance_unverified = False
        if req.acceptance.strip():
            if not is_partial and not claimed_validation and not validation_ran:
                acceptance_unverified = True

        # Build structured errors and caveats
        result_errors = list(relay.api_errors)
        if internal_error:
            result_errors.insert(0, "Worker failed due to an internal error.")

        # Failed write tools are hard errors
        for r in failed_write_tools:
            result_errors.append(f"Worker write tool '{r['name']}' reported failure.")

        # Failed validation commands are hard errors
        for v in failed_validation:
            cmd = v["command"][:80]
            result_errors.append(f"Validation command failed (exit code {v['exit_code']}): {cmd}")

        if _final_report_claims_failure(final_report):
            result_errors.append(
                "Worker final report claims a blocker, failed validation, failed acceptance, "
                "or unverified acceptance."
            )

        result_caveats: list[str] = []

        if missing_validation_after_writes:
            result_caveats.append("Worker modified files but ran no validation command.")

        if acceptance_unverified:
            result_caveats.append("Worker final report did not clearly mention validation or acceptance verification.")

        phase_boundary = relay.phase_boundary_info is not None

        # Determine ok: only true when no errors, no caveats, no failures,
        # and either writes exist or final evidence exists.
        ok = (
            not result_errors
            and not result_caveats
            and not phase_boundary
            and not failed_write_tools
            and not failed_validation
            and not missing_validation_after_writes
            and not acceptance_unverified
            and bool(relay.write_results or final_report)
        )

        # Determine needs_followup and recoverable
        needs_followup = (
            phase_boundary
            or bool(failed_validation)
            or missing_validation_after_writes
            or acceptance_unverified
        )
        recoverable = (
            phase_boundary
            or bool(failed_validation)
            or missing_validation_after_writes
            or acceptance_unverified
            or bool(failed_write_tools)
        )
        if internal_error:
            needs_followup = False
            recoverable = False

        summary = _build_worker_summary(
            req,
            worker_history,
            relay.write_results,
            result_errors,
            continuation,
            result_caveats,
        )
        modified_files = continuation.get("modified_files") or [
            str(w["path"]) for w in relay.write_results if isinstance(w.get("path"), str) and w.get("path")
        ]

        spec_dict = req.to_dict()
        spec_dict["task_spec"] = task_spec.to_dict()
        record = WorkerDispatchRecord(
            after_message_index=-1,
            tool_call_id=tool_call_id,
            spec=spec_dict,
            worker_history=list(worker_history.messages),
            result_summary=summary,
        )
        self._records.append(record)

        # Auto-save this dispatch record to project memory (Tier 2).
        if self._workspace_root is not None:
            from aura.conversation.persistence import save_dispatch_record_to_memory
            save_dispatch_record_to_memory(record, self._workspace_root)

        # Auto-commit if worker made changes — fire in background so dispatch isn't blocked.
        if self._auto_commit_enabled and self._workspace_root is not None and relay.write_results:
            try:
                from aura.git_ops import auto_commit

                written_files = [w["path"] for w in relay.write_results if isinstance(w.get("path"), str) and w.get("path")]
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

        self.workerFinished.emit(tool_call_id, ok, summary, needs_followup)
        return WorkerDispatchResult(
            ok=ok,
            summary=summary,
            cancelled=False,
            needs_followup=needs_followup,
            phase_boundary=phase_boundary,
            followup_reason=(
                str(relay.phase_boundary_info.get("reason")) if relay.phase_boundary_info else None
            ),
            recoverable=recoverable,
            completed=continuation.get("completed", []),
            remaining=continuation.get("remaining", []),
            modified_files=modified_files,
            validation=continuation.get("validation_text"),
            suggested_next_spec=continuation.get("recommended_next_step"),
            extras={
                "writes": relay.write_results,
                "errors": result_errors,
                "caveats": result_caveats,
                "worker_internal_error": bool(internal_error),
                "internal_error": internal_error or "",
                "phase_boundary": relay.phase_boundary_info or {},
                "limit": (
                    relay.phase_boundary_info
                    if relay.phase_boundary_info and relay.phase_boundary_info.get("limit_reached")
                    else {}
                ),
            },
        )


def _format_spec_as_user_message(task: WorkerTaskSpec | WorkerDispatchRequest) -> str:
    """Format a structured task spec (or raw dispatch request) as a user message
    for the worker. Accepts both types for backward compatibility."""
    if isinstance(task, WorkerDispatchRequest):
        task = normalize_worker_task(task)

    def _lines(items: list[str], default: str = "(none listed)") -> str:
        if not items:
            return default
        return "\n".join(f"- {item}" for item in items)

    parts = [
        "Goal",
        task.goal,
        "",
        "Files",
        _lines(task.files),
        "",
        "Builder Note",
        task.builder_note,
        "",
        "Allowed Responsibilities",
        _lines(task.allowed_responsibilities),
        "",
        "Forbidden Responsibilities",
        _lines(task.forbidden_responsibilities),
        "",
        "Required Outputs",
        _lines(task.required_outputs),
        "",
        "Non-Goals",
        _lines(task.non_goals),
        "",
        "Acceptance / Validation",
        task.acceptance,
    ]

    if task.validation_commands:
        parts.extend([
            "",
            "Validation Commands",
            "```",
            "\n".join(task.validation_commands),
            "```",
        ])

    parts.extend([
        "",
        "Worker Contract",
        "- Listed files are the expected working set. Read every one before editing.",
        "- Do not move unrelated behavior into entry points.",
        "- Do not create demo, prototype, or phase files unless explicitly requested.",
        "- Do not invent broad architecture outside the task scope.",
        "- Do not hide failure behind success-looking output.",
        "- Do not satisfy acceptance with placeholder behavior.",
        "- If a requested responsibility does not belong in a listed file, inspect and choose the smallest correct neighboring module, or report the mismatch.",
        "- Start with a TODO list and keep it updated.",
        "- Build the smallest complete implementation.",
        "- Own exact edits, validation, and code-quality decisions.",
        "- Code must work and be easy to work on.",
        "- Avoid public-library, tutorial, or demo ceremony unless requested.",
        "- Avoid module summary docstrings and Args/Returns/Raises in normal app/tool code.",
        "- Do not add fake architecture.",
        "- Helpers return values or raise; CLI/UI/app boundary reports.",
        "- Validate actual behavior when practical.",
        "- Do not report Done unless acceptance passed.",
        "",
        "Begin. Read the listed files first, then make the change(s).",
    ])

    return "\n".join(parts)


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
