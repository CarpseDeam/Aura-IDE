"""Dispatch proxy, pending state, and worker result helpers.

Routes dispatch_to_worker calls through the GUI (SpecCard) and runs
the worker manager when the user clicks Dispatch.
"""

from __future__ import annotations

import logging
import json
import re
import shlex
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
    WorkerOutcomeStatus,
    WorkerTaskSpec,
    normalize_worker_task,
)
from aura.conversation.project_profile import detect_project_profile
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
    "edit_mechanics_multi_edit_spin",
    "patch_hunk_not_found",
    "patch_hunk_ambiguous",
    "syntax_invalid",
}

EDIT_TRANSACTION_FAILURE_CLASSES = {
    "edit_transaction_hash_mismatch",
    "edit_transaction_symbol_not_found",
    "edit_transaction_ambiguous_symbol",
    "edit_transaction_invalid_operation",
    "edit_transaction_invalid_syntax",
    "edit_transaction_not_applicable",
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
    workerFinished = Signal(str, bool, str, bool, str)  # tool_id, ok, summary, needs_followup, status
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
        self._result_metadata: dict[str, dict[str, Any]] = {}

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

    def result_metadata(self, tool_call_id: str) -> dict[str, Any]:
        return dict(self._result_metadata.get(tool_call_id, {}))

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
        if self._workspace_root is not None:
            try:
                profile = detect_project_profile(self._workspace_root)
                task_spec = replace(task_spec, project_profile=profile)
            except Exception:
                logging.exception("Failed to detect project profile for worker context")
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
                explicit_validation_commands=task_spec.validation_commands,
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

        preserve_scratch_records = _request_allows_root_check_files(req)
        diagnostic_environment_caveats = (
            []
            if preserve_scratch_records
            else _diagnostic_environment_caveats(relay)
        )
        _filter_scratch_write_records(relay, preserve_scratch=preserve_scratch_records)
        validation_results = _validation_results_for_task(
            relay.validation_results,
            getattr(relay, "terminal_results", []),
            task_spec.validation_commands,
        )
        if not preserve_scratch_records:
            validation_results = _filter_scratch_validation_results(validation_results)
        has_writes = bool(relay.write_results)
        internal_recovery_steers = [
            r for r in relay.failed_tool_results if r.get("internal_recovery_steer")
        ]
        write_failures = [
            r
            for r in relay.failed_tool_results
            if r["name"] in WRITE_TOOLS and not r.get("internal_recovery_steer")
        ]
        source_inspection_blockers = [
            r
            for r in relay.failed_tool_results
            if r.get("failure_class") == "source_inspection_command_blocked"
        ]
        terminal_policy_blockers = [
            r
            for r in relay.failed_tool_results
            if r.get("failure_class")
            in {"source_inspection_command_blocked", "worker_terminal_not_validation"}
        ]
        environment_setup_blockers = [
            r
            for r in relay.failed_tool_results
            if r.get("failure_class") == "project_environment_missing_dependency"
            or r.get("environment_setup_needed")
        ]
        failed_validation = _unrecovered_validation_failures(validation_results)
        validation_ran = bool(validation_results)
        quality_bounces = list(getattr(relay, "quality_bounces", []))
        not_applied_writes = list(getattr(relay, "not_applied_writes", []))
        unrecovered_not_applied_writes = _unrecovered_not_applied_writes(relay.tool_results)

        # Compute acceptance-unverified
        acceptance_unverified = False
        if req.acceptance.strip():
            if not is_partial and not claimed_validation and not validation_ran:
                acceptance_unverified = True
        validation_not_run = bool(relay.write_results) and not validation_ran

        # Build structured errors and caveats
        result_errors = list(relay.api_errors)
        if internal_error:
            result_errors.insert(0, "Harness error due to an internal Worker exception.")

        structured_failure = _parse_structured_worker_failure(final_report)
        if (
            diagnostic_environment_caveats
            and structured_failure.get("failure_class") == "patch_quality_unresolved"
            and not relay.write_results
            and not getattr(relay, "quality_bounces", [])
            and not getattr(relay, "not_applied_writes", [])
        ):
            structured_failure = {}
        patch_quality_unresolved = (
            structured_failure
            if structured_failure.get("failure_class") == "patch_quality_unresolved"
            else {}
        )
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
            for r in source_inspection_blockers:
                command = str(r.get("blocked_command") or "")[:120]
                suffix = f": {command}" if command else "."
                result_errors.append(
                    "Terminal source inspection was blocked; Worker should retry with structured reads"
                    + suffix
                )
            for r in terminal_policy_blockers:
                if r.get("failure_class") == "source_inspection_command_blocked":
                    continue
                command = str(r.get("blocked_command") or "")[:120]
                suffix = f": {command}" if command else "."
                result_errors.append(
                    "Worker terminal command was blocked because it was not validation/build/test"
                    + suffix
                )
            for r in environment_setup_blockers:
                dependency = str(r.get("missing_dependency") or r.get("missing_tool") or "tool/dependency")
                command = str(r.get("blocked_command") or "")[:120]
                suffix = f": {command}" if command else "."
                label = "dependency" if r.get("missing_dependency") else "tool"
                result_errors.append(
                    f"Project environment missing {label} '{dependency}'"
                    + suffix
                )
            # Failed validation commands are hard errors
            for v in failed_validation:
                cmd = v["command"][:80]
                result_errors.append(f"Validation command failed (exit code {v['exit_code']}): {cmd}")

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
        for write in relay.write_results:
            issues = write.get("pre_existing_environment_issues")
            if isinstance(issues, list) and issues:
                first = issues[0]
                if isinstance(first, dict):
                    msg = str(first.get("message") or first.get("code") or "pre-existing environment issue")
                else:
                    msg = str(first)
                result_caveats.append(f"Pre-existing environment issue on {write.get('path')}: {msg}")

        if recoverable_write_failures and not relay.write_results and not structured_failure:
            result_caveats.append(_format_recoverable_write_failure(recoverable_write_failures[0]))
        if validation_not_run:
            result_caveats.append("Files changed but validation did not run.")
        for caveat in diagnostic_environment_caveats:
            if caveat not in result_caveats:
                result_caveats.append(caveat)

        if cleaned_scratch_files:
            result_caveats.append(
                "Cleaned Worker-created root validation scratch file(s): "
                + ", ".join(cleaned_scratch_files[:5])
            )

        if acceptance_unverified:
            result_caveats.append("Worker final report did not clearly mention validation or acceptance verification.")

        if not structured_failure and _final_report_claims_failure(final_report):
            phrase_caveat = (
                "Worker final report mentioned possible blocker, failed validation, "
                "or incomplete verification."
            )
            result_caveats.append(phrase_caveat)

        # No-work detection
        phase_boundary = relay.phase_boundary_info is not None
        is_implementation = not (
            "blueprint" in req.spec.lower()[:200]
            or "inspect" in req.goal.lower()[:100]
            or "diagnostic" in req.goal.lower()[:100]
        )
        if is_implementation and not relay.touched_files and not relay.failed_tool_results and not quality_bounces and not internal_error and not relay.api_errors:
            result_caveats.append("Worker made no changes, reported no blocker, and ran no meaningful validation.")

        # Severity-based classification
        has_hard_failure = bool(result_errors)
        has_internal_failure = bool(internal_error or relay.api_errors)
        has_validation_failure = bool(failed_validation)
        structured_recovery_exhausted = structured_failure.get("failure_class") == "worker_recovery_exhausted"
        has_recoverable_edit_blocker = (
            bool(unrecovered_not_applied_writes)
            or (
                structured_recovery_exhausted
                and (bool(recoverable_write_failures) or bool(not_applied_writes))
            )
            or (
                bool(recoverable_write_failures)
                and not relay.write_results
            )
        )
        has_quality_bounce_blocker = bool(quality_bounces) and not relay.write_results
        has_source_inspection_blocker = bool(source_inspection_blockers)
        has_terminal_policy_blocker = bool(terminal_policy_blockers)
        has_environment_setup_blocker = bool(environment_setup_blockers)
        has_diagnostic_environment_blocker = bool(diagnostic_environment_caveats) and not relay.write_results
        has_no_work = not relay.touched_files and not relay.failed_tool_results and not quality_bounces and not internal_error and not relay.api_errors
        has_unverified_acceptance = acceptance_unverified or validation_not_run

        # Is this a broad/risky/multi-file task that should have used TODO?
        files_count = len(req.files)
        is_broad = files_count >= 3 or bool(req.allowed_responsibilities) or bool(req.risk_notes)

        # Determine severity
        if has_hard_failure:
            ok = False
            needs_followup = not has_internal_failure
            recoverable = (
                has_validation_failure
                or has_source_inspection_blocker
                or has_terminal_policy_blocker
                or has_environment_setup_blocker
            ) and not has_internal_failure
        elif has_quality_bounce_blocker:
            ok = False
            needs_followup = True
            recoverable = True
        elif has_recoverable_edit_blocker:
            ok = False
            needs_followup = True
            recoverable = True
        elif has_diagnostic_environment_blocker:
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
        if validation_not_run and not has_recoverable_edit_blocker:
            summary_continuation["status"] = "validation_not_run"
            summary_continuation["reason"] = "Files changed but validation did not run."
        if has_diagnostic_environment_blocker and not summary_continuation.get("status"):
            summary_continuation["status"] = "needs_followup"
            summary_continuation["reason"] = diagnostic_environment_caveats[0]
        if patch_quality_unresolved:
            summary_continuation["status"] = "patch_quality_unresolved"
            summary_continuation["reason"] = str(
                patch_quality_unresolved.get("error") or "Patch quality needs repair."
            )
        elif has_quality_bounce_blocker:
            bounce = quality_bounces[0]
            summary_continuation["status"] = "patch_quality_unresolved"
            summary_continuation["reason"] = str(
                bounce.get("repair_instructions")
                or bounce.get("suggested_next_action")
                or "Patch quality needs repair."
            )

        status = _compute_outcome_status(
            ok=ok,
            needs_followup=needs_followup,
            recoverable=recoverable,
            has_internal_failure=has_internal_failure,
            has_validation_failure=has_validation_failure,
            has_recoverable_edit_blocker=has_recoverable_edit_blocker,
            has_quality_bounce_blocker=has_quality_bounce_blocker,
            has_source_inspection_blocker=has_source_inspection_blocker,
            has_environment_setup_blocker=has_environment_setup_blocker or has_diagnostic_environment_blocker,
            has_no_work=has_no_work,
            is_implementation=is_implementation,
            has_unverified_acceptance=has_unverified_acceptance,
            has_hard_failure=has_hard_failure,
            has_applied_writes=bool(relay.write_results),
            result_errors=result_errors,
            result_caveats=result_caveats,
            continuation=summary_continuation,
            structured_failure=structured_failure,
            write_failures=write_failures,
        )

        summary = _build_worker_summary(
            req,
            worker_history,
            relay.write_results,
            result_errors,
            summary_continuation,
            result_caveats,
            validation_results=validation_results,
            not_applied_writes=not_applied_writes,
            status=status,
            internal_error=internal_error,
        )
        modified_files = _applied_modified_files(relay.write_results)
        extras = {
            "writes": relay.write_results,
            "not_applied_writes": not_applied_writes,
            "unrecovered_not_applied_writes": unrecovered_not_applied_writes,
            "write_outcome": _final_write_outcome(relay.write_results, not_applied_writes, internal_error),
            "failed_write_tools": failed_write_tools,
            "internal_recovery_steers": internal_recovery_steers,
            "recoverable_write_failures": recoverable_write_failures,
            "source_inspection_blockers": source_inspection_blockers,
            "terminal_policy_blockers": terminal_policy_blockers,
            "environment_setup_blockers": environment_setup_blockers,
            "quality_bounces": quality_bounces,
            "patch_quality_unresolved": patch_quality_unresolved,
            "terminal_results": getattr(relay, "terminal_results", []),
            "validation_results": validation_results,
            "errors": result_errors,
            "caveats": result_caveats,
            "worker_internal_error": bool(internal_error),
            "internal_error": internal_error or "",
            "validation_not_run": validation_not_run,
            "recoverable": recoverable,
            "needs_followup": needs_followup,
            "phase_boundary": relay.phase_boundary_info or {},
            "limit": (
                relay.phase_boundary_info
                if relay.phase_boundary_info and relay.phase_boundary_info.get("limit_reached")
                else {}
            ),
        }

        auto_commit_allowed = _worker_result_allows_auto_commit(
            ok=ok,
            needs_followup=needs_followup,
            recoverable=recoverable,
            status=status,
            internal_error=internal_error,
            failed_validation=failed_validation,
            not_applied_writes=not_applied_writes,
            unrecovered_not_applied_writes=unrecovered_not_applied_writes,
            environment_setup_blockers=environment_setup_blockers,
            validation_not_run=validation_not_run,
            result_errors=result_errors,
        )
        extras["auto_commit_allowed"] = auto_commit_allowed
        extras["auto_commit_attempted"] = False
        extras["auto_commit_success"] = None
        extras["auto_commit_message"] = ""

        # Auto-commit only after a clean final Worker result.
        if (
            self._auto_commit_enabled
            and self._workspace_root is not None
            and auto_commit_allowed
        ):
            written_files = _applied_modified_files(relay.write_results)
            if written_files:
                extras["auto_commit_attempted"] = True
                try:
                    from aura.git_ops import auto_commit

                    success, message = auto_commit(
                        self._workspace_root,
                        req.goal,
                        written_files,
                        summary,
                    )
                    extras["auto_commit_success"] = success
                    extras["auto_commit_message"] = message
                except Exception as exc:
                    extras["auto_commit_success"] = False
                    extras["auto_commit_message"] = (
                        f"Auto-commit failed: {type(exc).__name__}: {exc}"
                    )

                auto_commit_message = extras["auto_commit_message"]
                if extras["auto_commit_success"]:
                    summary = f"{summary.rstrip()}\nAuto-commit: {auto_commit_message}"
                else:
                    summary = (
                        f"{summary.rstrip()}\n"
                        f"Auto-commit skipped/failed: {auto_commit_message}"
                    )

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

        self._result_metadata[tool_call_id] = {
            "modified_files": modified_files,
            "validation": continuation.get("validation_text"),
            "extras": extras,
        }
        self.workerFinished.emit(tool_call_id, ok, summary, needs_followup, status)
        return WorkerDispatchResult(
            ok=ok,
            summary=summary,
            status=status,
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
            extras=extras,
        )


def _compute_outcome_status(
    ok: bool,
    needs_followup: bool,
    recoverable: bool,
    has_internal_failure: bool,
    has_validation_failure: bool,
    has_recoverable_edit_blocker: bool,
    has_quality_bounce_blocker: bool,
    has_source_inspection_blocker: bool,
    has_no_work: bool,
    is_implementation: bool,
    has_unverified_acceptance: bool,
    has_hard_failure: bool,
    result_errors: list[str],
    result_caveats: list[str],
    continuation: dict[str, Any],
    has_applied_writes: bool = False,
    structured_failure: dict[str, Any] | None = None,
    write_failures: list[dict[str, Any]] | None = None,
    has_environment_setup_blocker: bool = False,
) -> str:
    """Map the boolean severity classification to a WorkerOutcomeStatus."""
    from aura.conversation.dispatch import WorkerOutcomeStatus as S

    structured_failure = structured_failure or {}
    write_failures = write_failures or []
    failure_classes = [
        str(item.get("failure_class") or "")
        for item in [structured_failure, *write_failures]
        if isinstance(item, dict)
    ]
    reject_flags = any(
        bool(item.get("reject"))
        for item in [structured_failure, *write_failures]
        if isinstance(item, dict)
    )
    structured_status = str(continuation.get("status") or "")

    if "approval_rejected" in failure_classes:
        return S.approval_rejected.value
    if "compiler_rejected" in failure_classes or reject_flags:
        return S.craft_rejected.value
    if (
        "patch_quality_unresolved" in failure_classes
        or structured_status == "patch_quality_unresolved"
        or has_quality_bounce_blocker
    ):
        return S.craft_bounced.value
    if has_recoverable_edit_blocker or (
        not has_applied_writes
        and any(
            fc == "edit_mechanics_blocked" or fc in EDIT_TRANSACTION_FAILURE_CLASSES
            for fc in failure_classes
        )
    ):
        return S.edit_mechanics_blocked.value
    if has_validation_failure or any(fc.startswith("validation_") for fc in failure_classes):
        return S.validation_failed.value
    if has_internal_failure or any(fc in {"internal_error", "worker_internal_error", "harness_error"} for fc in failure_classes):
        return S.harness_error.value
    if has_source_inspection_blocker or "source_inspection_command_blocked" in failure_classes:
        return S.needs_followup.value
    if has_environment_setup_blocker or any(fc.startswith("project_environment_missing_") for fc in failure_classes):
        return S.needs_followup.value
    if has_hard_failure:
        if structured_status == "phased":
            return S.needs_followup.value
        return S.needs_followup.value
    if has_no_work and is_implementation:
        return S.needs_followup.value
    if has_unverified_acceptance:
        return S.needs_followup.value
    if ok and result_caveats:
        return S.completed_with_caveats.value
    return S.completed.value


def _format_spec_as_user_message(task: WorkerTaskSpec | WorkerDispatchRequest) -> str:
    """Format a structured task spec (or raw dispatch request) as a user message
    for the worker. Accepts both types for backward compatibility."""
    if isinstance(task, WorkerDispatchRequest):
        task = normalize_worker_task(task)

    def _lines(items: list[str], default: str = "(none listed)") -> str:
        if not items:
            return default
        return "\n".join(f"- {item}" for item in items)

    parts: list[str] = []

    # ---- Project Profile (injected by dispatch) -----------------------------
    if task.project_profile is not None:
        summary = task.project_profile.summarize()
        parts.append("\u2500\u2500 Project Profile " + "\u2500" * 42)
        for line in summary.split("\n"):
            parts.append(line)
        parts.append("\u2500" * 60)
        parts.append("")

    parts.extend([
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
    ])

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
        "- Use patch_file for existing-file edits after reading the file.",
        "- Use write_file only for new files or intentional full-file replacement.",
        "- If patch_file fails, re-read the file and retry patch_file once; do not switch between edit tools.",
        "- Build the smallest complete implementation.",
        "- Own exact edits, validation, and code-quality decisions.",
        "- Use grep_search for searching.",
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
    if re.search(r"\bno\s+(?:blocker|blockers|blocked)\b", text):
        text = re.sub(r"\bno\s+(?:blocker|blockers|blocked)\b", "", text)
    return any(
        re.search(pattern, text)
        for pattern in (
            r"\bblocker(?:s)?\b",
            r"\bblocked\b",
            r"\bfailed\s+validation\b",
            r"\bvalidation\s+failed\b",
            r"\bfailed\s+acceptance\b",
            r"\bacceptance\s+failed\b",
            r"\bcould\s+not\s+verify\b",
            r"\bcouldn't\s+verify\b",
            r"\bcannot\s+verify\b",
            r"\bunable\s+to\s+verify\b",
            r"\bnot\s+verified\b",
            r"\bcould\s+not\s+run\b",
            r"\bcouldn't\s+run\b",
            r"\bunable\s+to\s+run\b",
            r"\btests?\s+failed\b",
            r"\bpytest\s+failed\b",
            r"\blint\s+failed\b",
        )
    )


def _final_report_claims_validation(content: str) -> bool:
    text = content.lower()
    if re.search(r"\bnot\s+(?:tested|validated|verified)\b", text):
        text = re.sub(r"\bnot\s+(?:tested|validated|verified)\b", "", text)
    return any(
        re.search(pattern, text)
        for pattern in (
            r"\bverified\b",
            r"\bvalidated\b",
            r"\bpytest\b",
            r"\bpy_compile\b",
            r"\bruff\b",
            r"\bmypy\b",
            r"\btests?\s+pass(?:ed|es)?\b",
            r"\bcompiled\b",
            r"\bexit\s+code\s+0\b",
            r"\bexits\s+0\b",
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
    error = str(result.get("error") or "Harness error.")
    failure_class = str(result.get("failure_class") or "worker_failed")
    detail = result.get("details")
    if isinstance(detail, dict) and detail:
        path = str(detail.get("path") or "")
        tool = str(detail.get("tool") or "")
        reason = str(detail.get("reason") or detail.get("failure_class") or "")
        op = detail.get("failed_operation")
        op_text = ""
        if isinstance(op, dict) and op:
            op_text = f" Failed operation: {json.dumps(op, ensure_ascii=False, sort_keys=True)}."
        target = f" Path: {path}." if path else ""
        tool_text = f" Tool: {tool}." if tool else ""
        reason_text = f" Reason: {reason}." if reason else ""
        return f"{error} ({failure_class}).{target}{tool_text}{reason_text}{op_text}"
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
    return f"Write tool '{name}' failed{target}: {error} ({failure_class})."


def _format_recoverable_write_failure(result: dict[str, Any]) -> str:
    name = str(result.get("name") or "write_tool")
    path = str(result.get("path") or "")
    error = str(result.get("error") or result.get("result_preview") or "recoverable edit mechanics failure")
    suggested = str(result.get("suggested_next_tool") or result.get("suggested_tool") or "patch_file")
    target = f" on {path}" if path else ""
    op = result.get("failed_operation")
    op_text = ""
    if isinstance(op, dict) and op:
        op_text = f" Failed operation: {json.dumps(op, ensure_ascii=False, sort_keys=True)}."
    return f"Recoverable edit mechanics failure from {name}{target}: {error}. Next tactic: {suggested}.{op_text}"


def _worker_result_allows_auto_commit(
    *,
    ok: bool,
    needs_followup: bool,
    recoverable: bool,
    status: str | None,
    internal_error: str | None,
    failed_validation: list[dict[str, Any]],
    not_applied_writes: list[dict[str, Any]],
    unrecovered_not_applied_writes: list[dict[str, Any]],
    environment_setup_blockers: list[dict[str, Any]],
    validation_not_run: bool,
    result_errors: list[str],
) -> bool:
    if not ok or needs_followup or recoverable:
        return False
    if status not in {WorkerOutcomeStatus.completed.value, WorkerOutcomeStatus.completed_with_caveats.value}:
        return False
    if internal_error or result_errors:
        return False
    if failed_validation or validation_not_run:
        return False
    if environment_setup_blockers:
        return False
    if not_applied_writes or unrecovered_not_applied_writes:
        return False
    return True


def _unrecovered_validation_failures(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        if result.get("ok"):
            continue
        if _is_benign_search_no_match(result):
            continue
        command = str(result.get("command", ""))
        targets = set(_py_compile_targets(command))
        if targets and _later_py_compile_passes(results[index + 1:], targets):
            continue
        failures.append(result)
    return failures


def _validation_results_for_task(
    validation_results: list[dict[str, Any]],
    terminal_results: list[dict[str, Any]],
    explicit_commands: list[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, Any, Any]] = set()

    def add(record: dict[str, Any]) -> None:
        key = (str(record.get("command") or ""), record.get("exit_code"), record.get("ok"))
        if key not in seen:
            records.append(record)
            seen.add(key)

    for record in validation_results:
        add(record)

    explicit = {command.strip() for command in explicit_commands if command.strip()}
    if explicit:
        for record in terminal_results:
            if str(record.get("command") or "").strip() in explicit:
                add(record)
    return records


def _is_benign_search_no_match(result: dict[str, Any]) -> bool:
    command = str(result.get("command") or "").strip()
    exit_code = result.get("exit_code")
    output = str(result.get("output") or result.get("output_preview") or "").strip()

    if exit_code != 1 or not command:
        return False
    if output and not _is_no_match_only_output(output):
        return False

    segments = _split_simple_pipeline(command)
    if not segments:
        return False
    return all(_pipeline_segment_starts_with_search(segment) for segment in segments)


def _is_no_match_only_output(output: str) -> bool:
    normalized = re.sub(r"\s+", " ", output.strip().lower())
    return normalized in {
        "no match",
        "no matches",
        "no matches found",
    }


def _split_simple_pipeline(command: str) -> list[str]:
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False

    for char in command:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            current.append(char)
            quote = char
            continue
        if char == "|":
            segment = "".join(current).strip()
            if not segment:
                return []
            segments.append(segment)
            current = []
            continue
        current.append(char)

    if quote:
        return []
    segment = "".join(current).strip()
    if not segment:
        return []
    segments.append(segment)
    return segments


def _pipeline_segment_starts_with_search(segment: str) -> bool:
    if re.search(r"(^|[^|])(?:&&|\|\||;|[<>])", segment):
        return False
    try:
        tokens = shlex.split(segment, posix=False)
    except ValueError:
        return False
    if not tokens:
        return False
    executable = tokens[0].strip("'\"").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    return executable in {"rg", "grep", "findstr"}


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
    validation_results: list[dict[str, Any]] | None = None,
    not_applied_writes: list[dict[str, Any]] | None = None,
    status: str | None = None,
    internal_error: str | None = None,
) -> str:
    continuation = continuation or {}
    caveats = caveats or []
    validation_results = validation_results or []
    not_applied_writes = not_applied_writes or []

    # Derive status if not provided (backward compat for callers without status)
    if not status:
        if continuation.get("status") == "patch_quality_unresolved":
            status = "craft_bounced"
        elif errors:
            if _is_internal_error_summary(errors[0]):
                status = "harness_error"
            elif errors[0].startswith("Validation command failed"):
                status = "validation_failed"
            else:
                status = "needs_followup"
        elif continuation.get("status") == "needs_followup":
            status = "needs_followup"
        elif caveats:
            status = "completed_with_caveats"
        else:
            status = "completed"

    STATUS_LABELS = {
        "completed": "✅  Worker completed successfully",
        "completed_with_caveats": "✅  Worker completed with caveats",
        "needs_followup": "⚠️  Worker needs follow-up",
        "validation_failed": "❌  Validation failed",
        "edit_mechanics_blocked": "⚠️  Edit mechanics blocked",
        "craft_bounced": "⚠️  Patch quality needs repair",
        "craft_rejected": "❌  Craft rejected",
        "scope_mismatch": "⚠️  Scope mismatch",
        "approval_rejected": "❌  Approval rejected",
        "cancelled": "🔶  Worker cancelled",
        "harness_error": "❌  Harness error",
    }
    ACTION_LABELS = {
        "completed": "None — ready for review",
        "completed_with_caveats": "Review caveats below",
        "needs_followup": "Re-dispatch with follow-up",
        "validation_failed": "Fix validation failure — see below",
        "edit_mechanics_blocked": "Re-dispatch — edit tool failure",
        "craft_bounced": "Re-dispatch — patch repair needed",
        "craft_rejected": "Review and re-specify",
        "scope_mismatch": "Review and re-specify",
        "approval_rejected": "N/A — was not approved",
        "cancelled": "N/A — was cancelled",
        "harness_error": "Check logs and retry",
    }

    status_label = STATUS_LABELS.get(status, "❓  Unknown outcome")
    action_needed = ACTION_LABELS.get(status, "Review details below")

    BORDER = "\u2550" * 38
    DIVIDER = "\u2500" * 38

    lines: list[str] = []
    displayed_writes = _dedupe_summary_writes(writes)

    # === Files changed count ===
    edited_count = sum(1 for w in displayed_writes if not w.get("is_new_file"))
    new_count = sum(1 for w in displayed_writes if w.get("is_new_file"))
    total_count = len(displayed_writes)
    if total_count > 0:
        files_changed_str = f"{total_count} ({edited_count} edited, {new_count} new)"
    else:
        files_changed_str = "0"

    # === Validation glance ===
    py_compile_results = [v for v in validation_results if "py_compile" in str(v.get("command", ""))]
    if not validation_results:
        validation_str = "\u2014 (not yet verified)"
    elif py_compile_results:
        pc_passed = sum(1 for v in py_compile_results if v.get("ok"))
        pc_total = len(py_compile_results)
        pc_ok = pc_passed == pc_total
        pc_prefix = "\u2713" if pc_ok else "\u2717"
        validation_str = f"{pc_prefix} py_compile ({pc_passed}/{pc_total} passed)"
    else:
        passed = sum(1 for v in validation_results if v.get("ok"))
        total = len(validation_results)
        ok = passed == total
        prefix = "\u2713" if ok else "\u2717"
        validation_str = f"{prefix} {passed}/{total} passed"

    # === Top section ===
    lines.append(BORDER)
    lines.append(f" {status_label}")
    lines.append(DIVIDER)

    # Glance line
    lines.append(f" Files changed   : {files_changed_str}")
    lines.append(f" Validation      : {validation_str}")
    lines.append(f" Action needed   : {action_needed}")
    lines.append(DIVIDER)

    # === Modified files ===
    if displayed_writes:
        lines.append("")
        lines.append(" Modified files:")
        for w in displayed_writes:
            tag = "(new)" if w.get("is_new_file") else "(edit)"
            path = str(w.get("path") or "").strip()
            lines.append(f"  \u2022 {path}   {tag}")
    else:
        lines.append("")
        lines.append(" Worker made no changes.")

    # === Validation detail ===
    if validation_results:
        passed_v = [v for v in validation_results if v.get("ok")]
        failed_v = [v for v in validation_results if not v.get("ok")]

        if passed_v:
            lines.append("")
            lines.append(" Validation:")
            for v in passed_v:
                cmd = str(v.get("command") or "")
                lines.append(f"  \u2022 {cmd}  \u2192  passed")

        if failed_v:
            lines.append("")
            lines.append(" Validation failures:")
            for v in failed_v:
                cmd = str(v.get("command") or "")
                exit_code = v.get("exit_code")
                exit_str = f" (exit {exit_code})" if exit_code is not None else ""
                lines.append(f"  \u2022 {cmd}  \u2192  failed{exit_str}")
                output = v.get("output") or v.get("output_preview") or ""
                if output:
                    first_line = output.strip().split("\n")[0][:200]
                    if first_line:
                        lines.append(f"    {first_line}")

    # === Harness errors ===
    if internal_error:
        lines.append("")
        lines.append(" Harness errors:")
        lines.append(f"  \u2022 {internal_error}")

    # === Other errors (filter harness prefix to avoid duplication) ===
    other_errors: list[str] = []
    for err in errors:
        if internal_error and ("Harness error" in err or "internal Worker exception" in err):
            continue
        other_errors.append(err)
    if other_errors:
        lines.append("")
        lines.append(" Errors:")
        for err in other_errors:
            lines.append(f"  \u2022 {err}")

    # === Caveats ===
    if caveats:
        lines.append("")
        lines.append(" Caveats:")
        for c in caveats:
            lines.append(f"  \u2022 {c}")

    # === Failed writes ===
    if not_applied_writes:
        lines.append("")
        lines.append(" Failed writes:")
        deduped_not_applied = _dedupe_summary_writes(not_applied_writes)
        for w in deduped_not_applied[:5]:
            path = str(w.get("path") or "(unknown path)")
            failure = str(w.get("failure_class") or "")
            lines.append(f"  \u2022 {path}   ({failure})")

    # === Summary ===
    if req.summary:
        lines.append("")
        lines.append(" Summary:")
        for s_line in req.summary.strip().split("\n"):
            lines.append(f" {s_line}")

    # === Remaining work ===
    remaining = continuation.get("remaining", [])
    if remaining:
        lines.append("")
        lines.append(" Remaining work:")
        for item in remaining:
            lines.append(f"  \u2022 {item}")

    lines.append(BORDER)
    return "\n".join(lines)


def _dedupe_summary_writes(writes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    for write in writes:
        path = str(write.get("path") or "").strip()
        if not path:
            continue
        existing = by_path.get(path)
        if existing is None:
            record = dict(write)
            record["path"] = path
            deduped.append(record)
            by_path[path] = record
            continue
        if write.get("is_new_file"):
            existing["is_new_file"] = True
    return deduped


def _is_internal_error_summary(error: str) -> bool:
    text = error.lower()
    return (
        text.startswith("harness error")
        or "internal worker exception" in text
        or "internal worker dispatch exception" in text
        or "worker_internal_error" in text
        or "internal_error" in text
    )


def _final_write_outcome(
    writes: list[dict[str, Any]],
    not_applied_writes: list[dict[str, Any]],
    internal_error: str | None,
) -> str:
    if internal_error:
        return "failed_harness_error"
    if writes:
        outcomes = [str(w.get("write_outcome") or "applied") for w in writes]
        if any(outcome == "applied_with_environment_caveat" for outcome in outcomes):
            return "applied_with_environment_caveat"
        return outcomes[-1] if outcomes else "applied"
    if not_applied_writes:
        return str(not_applied_writes[-1].get("write_outcome") or "not_applied_edit_mechanics_blocked")
    return "no_write_needed"


def _is_edit_mechanics_not_applied(record: dict[str, Any]) -> bool:
    failure_class = str(record.get("failure_class") or "")
    write_outcome = str(record.get("write_outcome") or "")
    if write_outcome == "not_applied_edit_mechanics_blocked":
        return True
    return (
        failure_class in RECOVERABLE_WORKER_WRITE_FAILURE_CLASSES
        or failure_class in EDIT_TRANSACTION_FAILURE_CLASSES
        or failure_class == "edit_mechanics_blocked"
    )


def _unrecovered_not_applied_writes(tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending: dict[str, dict[str, Any]] = {}
    for result in tool_results:
        if result.get("name") not in WRITE_TOOLS:
            continue
        path = str(result.get("path") or result.get("rel_path") or "")
        if not path:
            continue
        if result.get("ok") and result.get("applied") is True:
            pending.pop(path, None)
            continue
        if result.get("applied") is False or str(result.get("write_outcome") or "").startswith("not_applied_"):
            if _is_edit_mechanics_not_applied(result):
                pending[path] = result
    return list(pending.values())


def _applied_modified_files(writes: list[dict[str, Any]]) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for write in writes:
        path = write.get("path")
        if (
            write.get("applied") is True
            and isinstance(path, str)
            and path
            and not _is_validation_scratch_path(path)
            and path not in seen
        ):
            files.append(path)
            seen.add(path)
    return files


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
    name = normalized.rsplit("/", 1)[-1]
    if not name.endswith(".py"):
        return False
    if normalized.startswith(".aura/tmp/"):
        return _is_scratch_python_name(name)
    if "/" not in normalized:
        return _is_scratch_python_name(name)
    return False


def _is_scratch_python_name(name: str) -> bool:
    return name.startswith(
        (
            "dump",
            "_check",
            "check",
            "tmp",
            "_tmp",
            "_inspect",
            "inspect",
            "diagnostic",
            "_diagnostic",
        )
    )


def _filter_scratch_validation_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for result in results:
        command = str(result.get("command") or "")
        targets = _py_compile_targets(command)
        if targets and all(_is_validation_scratch_path(target) for target in targets):
            continue
        filtered.append(result)
    return filtered


def _diagnostic_environment_caveats(relay: Any) -> list[str]:
    dependencies: list[str] = []
    records: list[dict[str, Any]] = []
    for attr in ("quality_bounces", "not_applied_writes", "failed_tool_results"):
        value = getattr(relay, attr, [])
        if isinstance(value, list):
            records.extend(item for item in value if isinstance(item, dict))

    for record in records:
        path = str(record.get("path") or record.get("rel_path") or "")
        if not _is_validation_scratch_path(path):
            continue
        _collect_missing_dependencies(record, dependencies)

    for result in getattr(relay, "terminal_results", []):
        if not isinstance(result, dict) or result.get("ok"):
            continue
        command = str(result.get("command") or "")
        if not _command_mentions_scratch_path(command):
            continue
        _collect_missing_dependencies(result, dependencies)

    caveats: list[str] = []
    for dependency in dependencies:
        caveat = (
            "Diagnostic script could not run because "
            f"{dependency} is not installed in the project environment."
        )
        if caveat not in caveats:
            caveats.append(caveat)
    return caveats


def _collect_missing_dependencies(record: dict[str, Any], dependencies: list[str]) -> None:
    explicit = record.get("missing_dependency")
    if isinstance(explicit, str) and explicit:
        _append_unique(dependencies, explicit)

    for key in ("introduced_environment_issues", "craft_issues"):
        issues = record.get(key)
        if not isinstance(issues, list):
            continue
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            dependency = _dependency_from_text(str(issue.get("message") or ""))
            if dependency:
                _append_unique(dependencies, dependency)

    for key in ("error", "result_preview", "output", "output_preview", "repair_instructions"):
        dependency = _dependency_from_text(str(record.get(key) or ""))
        if dependency:
            _append_unique(dependencies, dependency)


def _dependency_from_text(text: str) -> str | None:
    patterns = (
        r"Import source '([^']+)' could not be resolved",
        r'Import source "([^"]+)" could not be resolved',
        r"No module named '([^']+)'",
        r'No module named "([^"]+)"',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).split(".", 1)[0]
    return None


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _command_mentions_scratch_path(command: str) -> bool:
    normalized = _normalize_worker_path(command)
    for token in re.split(r"\s+", normalized):
        token = token.strip("'\"")
        if _is_validation_scratch_path(token):
            return True
    return False


def _validation_scratch_files(root: Path | None) -> set[Path]:
    if root is None:
        return set()

    files = set(_root_check_files(root))
    tmp_dir = root / ".aura" / "tmp"
    if tmp_dir.is_dir():
        for pattern in ("dump*.py", "_check*.py", "check*.py", "tmp*.py", "_tmp*.py", "_inspect*.py", "inspect*.py", "diagnostic*.py", "_diagnostic*.py"):
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


def _filter_scratch_write_records(relay: Any, *, preserve_scratch: bool = False) -> None:
    if preserve_scratch:
        return

    def keep(path: object) -> bool:
        return not _is_validation_scratch_path(str(path or ""))

    relay.write_results = [
        item for item in relay.write_results
        if keep(item.get("path") if isinstance(item, dict) else "")
    ]
    relay.touched_files = {path for path in relay.touched_files if keep(path)}
    relay.wrote_new_files = [path for path in relay.wrote_new_files if keep(path)]
    relay.edited_existing_files = [path for path in relay.edited_existing_files if keep(path)]

    if hasattr(relay, "not_applied_writes"):
        relay.not_applied_writes = [
            item for item in relay.not_applied_writes
            if keep(item.get("path") if isinstance(item, dict) else "")
        ]
        relay.not_applied_writes = _dedupe_summary_writes(relay.not_applied_writes)
    if hasattr(relay, "failed_tool_results"):
        relay.failed_tool_results = [
            item for item in relay.failed_tool_results
            if keep(item.get("path") if isinstance(item, dict) else "")
        ]
    if hasattr(relay, "quality_bounces"):
        relay.quality_bounces = [
            item for item in relay.quality_bounces
            if keep(item.get("path") if isinstance(item, dict) else "")
        ]


def _root_check_files(root: Path | None) -> set[Path]:
    if root is None:
        return set()
    try:
        files: set[Path] = set()
        for pattern in ("_check*.py", "_tmp*.py", "tmp_*.py", "_inspect*.py", "inspect*.py", "diagnostic*.py", "_diagnostic*.py"):
            files.update(path.resolve() for path in root.glob(pattern) if path.is_file())
        return files
    except OSError:
        return set()


def _request_allows_root_check_files(req: WorkerDispatchRequest) -> bool:
    text = " ".join([req.goal, req.spec, req.acceptance, req.summary]).lower()
    if "_check" in text or "_tmp" in text or "tmp_" in text:
        return True
    if "_inspect" in text or "_diagnostic" in text:
        return True
    return any(
        Path(path).name.startswith(("_check", "_tmp", "tmp_", "_inspect", "inspect", "diagnostic", "_diagnostic"))
        for path in req.files
    )


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
