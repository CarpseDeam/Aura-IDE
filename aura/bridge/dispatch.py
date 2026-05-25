"""Dispatch proxy, pending state, and worker result helpers.

Routes dispatch_to_worker calls through the GUI (SpecCard) and runs
the worker manager when the user clicks Dispatch.
"""

from __future__ import annotations

import logging
import json
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
    "_check_read_before_edit",
]

DISPATCH_TIMEOUT = 300.0

RECOVERABLE_WORKER_WRITE_FAILURE_CLASSES = {
    "edit_mechanics_symbol_not_found",
    "edit_mechanics_old_str_not_found",
    "edit_mechanics_ambiguous_match",
    "edit_mechanics_stale_line_range",
    "syntax_invalid",
}


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
        scratch_before = _validation_scratch_files(self._workspace_root) if self._workspace_root is not None else set()
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

        cleaned_scratch_files: list[str] = []
        if self._workspace_root is not None and not _request_allows_root_check_files(req):
            cleaned_scratch_files = _cleanup_new_validation_scratch_files(self._workspace_root, scratch_before)
            if cleaned_scratch_files:
                cleaned_set = set(cleaned_scratch_files)
                relay.write_results = [
                    item for item in relay.write_results if item.get("path") not in cleaned_set
                ]
                relay.touched_files.difference_update(cleaned_set)
                relay.wrote_new_files = [path for path in relay.wrote_new_files if path not in cleaned_set]
                relay.edited_existing_files = [
                    path for path in relay.edited_existing_files if path not in cleaned_set
                ]

        final_report = _last_assistant_content(worker_history)
        continuation = _parse_continuation_report(final_report)
        is_partial = bool(continuation.get("status") == "needs_followup" or continuation.get("remaining"))
        claimed_validation = _final_report_claims_validation(final_report) or bool(continuation.get("validation_text"))

        _filter_scratch_write_records(relay)
        has_writes = bool(relay.write_results)
        internal_recovery_steers = [
            r for r in relay.failed_tool_results if r.get("internal_recovery_steer")
        ]
        write_failures = [
            r
            for r in relay.failed_tool_results
            if r["name"] in WRITE_TOOLS and not r.get("internal_recovery_steer")
        ]
        failed_validation = _unrecovered_validation_failures(relay.validation_results)
        validation_ran = bool(relay.validation_results)

        # Compute acceptance-unverified
        acceptance_unverified = False
        if req.acceptance.strip():
            if not is_partial and not claimed_validation and not validation_ran:
                acceptance_unverified = True

        # Build structured errors and caveats
        result_errors = list(relay.api_errors)
        if internal_error:
            result_errors.insert(0, "Worker failed due to an internal error.")

        structured_failure = _parse_structured_worker_failure(final_report)
        if structured_failure:
            result_errors.append(_format_structured_worker_failure(structured_failure))

        recoverable_write_failures = [
            r for r in write_failures if _is_recoverable_worker_write_failure(r)
        ]
        failed_write_tools = [
            r for r in write_failures if not _is_recoverable_worker_write_failure(r)
        ]
        for r in failed_write_tools:
            result_errors.append(_format_worker_write_failure(r))

        if not structured_failure:
            # Failed validation commands are hard errors
            for v in failed_validation:
                cmd = v["command"][:80]
                result_errors.append(f"Validation command failed (exit code {v['exit_code']}): {cmd}")

        if not structured_failure and _final_report_claims_failure(final_report):
            result_errors.append(
                "Worker final report claims a blocker, failed validation, failed acceptance, "
                "or unverified acceptance."
            )

        # Read-before-edit enforcement
        edited_without_read = _check_read_before_edit(
            relay.read_files, relay.read_outline_files, relay.edited_existing_files,
        )
        if edited_without_read:
            result_errors.append(
                "Worker edited existing file(s) without reading them first: "
                + ", ".join(edited_without_read[:5])
            )

        result_caveats: list[str] = []

        if recoverable_write_failures and not relay.write_results and not structured_failure:
            result_caveats.append(_format_recoverable_write_failure(recoverable_write_failures[0]))

        if cleaned_scratch_files:
            result_caveats.append(
                "Cleaned Worker-created root validation scratch file(s): "
                + ", ".join(cleaned_scratch_files[:5])
            )

        if acceptance_unverified:
            result_caveats.append("Worker final report did not clearly mention validation or acceptance verification.")

        # No-work detection
        phase_boundary = relay.phase_boundary_info is not None
        is_implementation = not (
            "blueprint" in req.spec.lower()[:200]
            or "inspect" in req.goal.lower()[:100]
            or "diagnostic" in req.goal.lower()[:100]
        )
        if is_implementation and not relay.touched_files and not relay.failed_tool_results and not internal_error and not relay.api_errors:
            result_caveats.append("Worker made no changes, reported no blocker, and ran no meaningful validation.")

        # Severity-based classification
        has_hard_failure = bool(result_errors)
        has_recoverable_edit_blocker = bool(recoverable_write_failures) and not relay.write_results
        has_no_work = not relay.touched_files and not relay.failed_tool_results and not internal_error and not relay.api_errors
        has_unverified_acceptance = acceptance_unverified

        # Is this a broad/risky/multi-file task that should have used TODO?
        files_count = len(req.files)
        is_broad = files_count >= 3 or bool(req.allowed_responsibilities) or bool(req.risk_notes)

        # Determine severity
        if has_hard_failure:
            ok = False
            needs_followup = False
            recoverable = False
        elif has_recoverable_edit_blocker:
            ok = False
            needs_followup = True
            recoverable = True
        elif has_no_work and is_implementation:
            ok = False
            needs_followup = True
            recoverable = True
        elif has_unverified_acceptance:
            ok = False
            needs_followup = True
            recoverable = True
        elif is_broad and not relay.todo_used and relay.touched_files:
            # Broad task skipped TODO but did work — caveat, not failure
            ok = True
            needs_followup = False
            recoverable = False
            result_caveats.append("Broad/multi-file task did not use update_todo_list — consider a visible plan next time.")
        else:
            ok = True
            needs_followup = False
            recoverable = False

        summary_continuation = dict(continuation)
        if has_recoverable_edit_blocker:
            if not summary_continuation.get("status"):
                summary_continuation["status"] = "needs_followup"
            if result_caveats:
                if not summary_continuation.get("reason"):
                    summary_continuation["reason"] = result_caveats[0]

        summary = _build_worker_summary(
            req,
            worker_history,
            relay.write_results,
            result_errors,
            summary_continuation,
            result_caveats,
        )
        modified_files = continuation.get("modified_files") or [
            str(w["path"]) for w in relay.write_results if isinstance(w.get("path"), str) and w.get("path")
        ]
        modified_files = [
            path for path in modified_files
            if not _is_validation_scratch_path(str(path))
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

                written_files = [
                    w["path"] for w in relay.write_results
                    if isinstance(w.get("path"), str)
                    and w.get("path")
                    and not _is_validation_scratch_path(str(w.get("path")))
                ]
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
                "failed_write_tools": failed_write_tools,
                "internal_recovery_steers": internal_recovery_steers,
                "recoverable_write_failures": recoverable_write_failures,
                "validation_results": relay.validation_results,
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
        "- Read every file before editing it. Call read_file (or read_files) on every listed file. Read before any write.",
        "- Do not move unrelated behavior into entry points.",
        "- Do not create demo, prototype, or phase files unless explicitly requested.",
        "- Do not invent broad architecture outside the task scope.",
        "- Do not hide failure behind success-looking output.",
        "- Do not satisfy acceptance with placeholder behavior.",
        "- If a requested responsibility does not belong in a listed file, inspect and choose the smallest correct neighboring module, or report the mismatch.",
        "- Use update_todo_list for broad or risky work; small localized tasks may proceed directly after reading.",
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


def _parse_structured_worker_failure(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    if parsed.get("ok") is not False:
        return {}
    failure_class = parsed.get("failure_class")
    error = parsed.get("error")
    if not failure_class or not error:
        return {}
    return parsed


def _format_structured_worker_failure(result: dict[str, Any]) -> str:
    error = str(result.get("error") or "Worker failed.")
    failure_class = str(result.get("failure_class") or "worker_failed")
    return f"{error} ({failure_class})."


def _is_recoverable_worker_write_failure(result: dict[str, Any]) -> bool:
    if result.get("internal_recovery_steer"):
        return True
    failure_class = str(result.get("failure_class") or "")
    if failure_class == "syntax_invalid" and result.get("recoverable") is False:
        return False
    return failure_class in RECOVERABLE_WORKER_WRITE_FAILURE_CLASSES


def _format_worker_write_failure(result: dict[str, Any]) -> str:
    name = str(result.get("name") or "write_tool")
    path = str(result.get("path") or "")
    error = str(result.get("error") or result.get("result_preview") or "unknown error")
    failure_class = str(result.get("failure_class") or "internal_error")
    target = f" on {path}" if path else ""
    return f"Worker write tool '{name}' failed{target}: {error} ({failure_class})."


def _format_recoverable_write_failure(result: dict[str, Any]) -> str:
    name = str(result.get("name") or "write_tool")
    path = str(result.get("path") or "")
    error = str(result.get("error") or result.get("result_preview") or "recoverable edit mechanics failure")
    suggested = str(result.get("suggested_next_tool") or result.get("suggested_tool") or "edit_line_range")
    target = f" on {path}" if path else ""
    return f"Recoverable edit mechanics failure from {name}{target}: {error}. Next tactic: {suggested}."


def _unrecovered_validation_failures(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        if result.get("ok"):
            continue
        command = str(result.get("command", ""))
        targets = set(_py_compile_targets(command))
        if targets and _later_py_compile_passes(results[index + 1:], targets):
            continue
        failures.append(result)
    return failures


def _later_py_compile_passes(results: list[dict[str, Any]], targets: set[str]) -> bool:
    for result in results:
        if not result.get("ok"):
            continue
        later_targets = set(_py_compile_targets(str(result.get("command", ""))))
        if targets and targets.issubset(later_targets):
            return True
    return False


def _py_compile_targets(command: str) -> list[str]:
    if "py_compile" not in command:
        return []
    matches = re.findall(r"(?<![\w.-])([A-Za-z0-9_./\\:\-]+\.py)(?![\w.-])", command)
    return [_normalize_py_compile_path(m) for m in matches if not m.endswith("py_compile.py")]


def _normalize_py_compile_path(raw: str) -> str:
    p = raw.strip().replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p

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

    # Severity prefix
    if errors:
        lines.append(f"Worker failed — {errors[0]}")
    elif continuation.get("status") == "needs_followup":
        reason = continuation.get("reason", "") or (caveats[0] if caveats else "needs further work")
        lines.append(f"Worker needs follow-up — {reason}")
    elif caveats:
        lines.append(f"Worker completed with caveats — {caveats[0]}")
    else:
        lines.append("Worker completed successfully.")

    # 1. Errors first
    if errors:
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


def _check_read_before_edit(
    read_files: set[str],
    read_outline_files: set[str],
    edited_existing_files: list[str],
    *,
    file_exists: Any = None,
) -> list[str]:
    """Return paths of existing files that were edited without being read.

    Splits Path-exists into a callable to allow testing without the filesystem.
    """
    if file_exists is None:
        file_exists = lambda p: Path(p).exists()  # noqa: E731
    all_read = read_files | set(read_outline_files)
    return [
        p for p in edited_existing_files
        if p not in all_read and file_exists(p)
    ]



def _normalize_worker_path(path: str) -> str:
    normalized = str(path).replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _is_validation_scratch_path(path: str) -> bool:
    normalized = _normalize_worker_path(path)
    if not (normalized.startswith(".aura/tmp/") and normalized.endswith(".py")):
        return False
    name = normalized.rsplit("/", 1)[-1]
    return name.startswith(("dump", "_check", "check", "tmp"))


def _validation_scratch_files(root: Path | None) -> set[Path]:
    if root is None:
        return set()

    files = set(_root_check_files(root))
    tmp_dir = root / ".aura" / "tmp"
    if tmp_dir.is_dir():
        for pattern in ("dump*.py", "_check*.py", "check*.py", "tmp*.py"):
            files.update(path for path in tmp_dir.glob(pattern) if path.is_file())
    return files


def _cleanup_new_validation_scratch_files(root: Path, before: set[Path]) -> list[str]:
    cleaned: list[str] = []
    for path in _validation_scratch_files(root):
        if path in before:
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        cleaned.append(rel)
    return sorted(cleaned)


def _filter_scratch_write_records(relay: Any) -> None:
    def keep(path: object) -> bool:
        return not _is_validation_scratch_path(str(path or ""))

    relay.write_results = [
        item for item in relay.write_results
        if keep(item.get("path") if isinstance(item, dict) else "")
    ]
    relay.touched_files = {path for path in relay.touched_files if keep(path)}
    relay.wrote_new_files = [path for path in relay.wrote_new_files if keep(path)]
    relay.edited_existing_files = [path for path in relay.edited_existing_files if keep(path)]


def _root_check_files(root: Path | None) -> set[Path]:
    if root is None:
        return set()
    try:
        return {path.resolve() for path in root.glob("_check*.py") if path.is_file()}
    except OSError:
        return set()


def _request_allows_root_check_files(req: WorkerDispatchRequest) -> bool:
    text = " ".join([req.goal, req.spec, req.acceptance, req.summary]).lower()
    if "_check" in text:
        return True
    return any(Path(path).name.startswith("_check") for path in req.files)


def _cleanup_new_root_check_files(root: Path, before: set[Path]) -> list[str]:
    cleaned: list[str] = []
    for path in _root_check_files(root):
        if path in before:
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        try:
            path.unlink()
            cleaned.append(rel)
        except OSError:
            continue
    return cleaned


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
