"""Worker completion/result assembly for bridge dispatch."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aura.conversation.worker_completion._shell_pipeline import (
    _is_benign_search_no_match,
    _is_no_match_only_output,
    _split_simple_pipeline,
    _pipeline_segment_starts_with_search,
)
from aura.conversation.worker_completion._summary_formatters import (
    _final_report_claims_failure,
    _final_report_claims_validation,
    _format_recoverable_write_failure,
    _format_structured_worker_failure,
    _format_worker_write_failure,
    _parse_structured_worker_failure,
)
from aura.bridge.event_relay import WorkerEventRelay
from aura.bridge.worker_report import _build_worker_summary, _dedupe_summary_writes
from aura.conversation import (
    History,
    WorkerDispatchRequest,
    WorkerDispatchResult,
    WorkerMismatch,
    WorkerTaskSpec,
)
from aura.conversation.path_utils import (
    is_validation_scratch_path as _is_validation_scratch_path,
    normalize_worker_path as _normalize_worker_path,
)
from aura.conversation.tool_limits import WRITE_TOOLS
from aura.conversation.validation_orchestrator import (
    MALFORMED_VALIDATION_COMMAND,
    MISSING_DEPENDENCY,
    MISSING_EXECUTABLE,
    NO_TESTS_COLLECTED,
    POLICY_BLOCKED,
    TEST_SELECTION_EMPTY,
    TIMEOUT,
)
from aura.conversation.worker_outcome import WorkerOutcomeStatus
from aura.validation.selector import ValidationPlan

_log = logging.getLogger(__name__)

__all__ = [
    "WorkerCompletionAssembly",
    "WorkerCompletionResult",
    "_check_read_before_edit",
    "_last_assistant_content",
    "prepare_worker_completion_result",
]

RECOVERABLE_WORKER_WRITE_FAILURE_CLASSES = {
    "edit_mechanics_symbol_not_found",
    "edit_mechanics_old_str_not_found",
    "edit_mechanics_ambiguous_match",
    "edit_mechanics_stale_line_range",
    "edit_mechanics_multi_edit_spin",
    "patch_hunk_not_found",
    "patch_hunk_ambiguous",
    "patch_file_hash_mismatch",
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


@dataclass
class WorkerCompletionResult:
    result: WorkerDispatchResult
    summary: str
    modified_files: list[str]
    extras: dict[str, Any]
    status: str
    structured_failure: dict[str, Any]
    task_shape_summary: dict[str, Any]
    result_errors: list[str]
    continuation: dict[str, Any]


@dataclass
class WorkerCompletionAssembly:
    req: WorkerDispatchRequest
    worker_history: History
    task_spec: WorkerTaskSpec
    relay: WorkerEventRelay
    context_gearbox: dict[str, Any]
    internal_error: str | None
    completion: dict[str, Any]
    messages: dict[str, Any]
    outcome: dict[str, Any]

    def build_result(self, *, validation_selector: ValidationPlan | None) -> WorkerCompletionResult:
        summary, modified_files, extras, task_shape_summary = _build_worker_result_payload(
            req=self.req,
            worker_history=self.worker_history,
            task_spec=self.task_spec,
            relay=self.relay,
            context_gearbox=self.context_gearbox,
            internal_error=self.internal_error,
            completion=self.completion,
            messages=self.messages,
            outcome=self.outcome,
            validation_selector=validation_selector,
        )

        from aura.conversation.worker_handback import annotate_worker_result_extras

        extras = annotate_worker_result_extras(
            extras,
            status=self.outcome["status"],
            structured_failure=self.messages.get("structured_failure"),
        )

        continuation = self.completion["continuation"]
        result = WorkerDispatchResult(
            ok=self.outcome["ok"],
            summary=summary,
            status=self.outcome["status"],
            cancelled=False,
            needs_followup=self.outcome["needs_followup"],
            phase_boundary=self.outcome["phase_boundary"],
            followup_reason=(
                str(self.relay.phase_boundary_info.get("reason"))
                if self.relay.phase_boundary_info
                else None
            ),
            recoverable=self.outcome["recoverable"],
            completed=continuation.get("completed", []),
            remaining=continuation.get("remaining", []),
            modified_files=modified_files,
            validation=continuation.get("validation_text"),
            suggested_next_spec=continuation.get("recommended_next_step"),
            extras=extras,
            mismatch=self.outcome["mismatch"],
        )
        return WorkerCompletionResult(
            result=result,
            summary=summary,
            modified_files=modified_files,
            extras=extras,
            status=self.outcome["status"],
            structured_failure=self.messages["structured_failure"],
            task_shape_summary=task_shape_summary,
            result_errors=self.messages["result_errors"],
            continuation=continuation,
        )


def prepare_worker_completion_result(
    *,
    req: WorkerDispatchRequest,
    worker_history: History,
    task_spec: WorkerTaskSpec,
    relay: WorkerEventRelay,
    context_gearbox: dict[str, Any],
    internal_error: str | None,
    cleaned_scratch_files: list[str],
    final_validation_commands: list[str],
    workspace_root: Path | None,
    preserve_scratch_records: bool,
) -> WorkerCompletionAssembly:
    completion = _collect_worker_completion_data(
        req=req,
        worker_history=worker_history,
        relay=relay,
        final_validation_commands=final_validation_commands,
        preserve_scratch_records=preserve_scratch_records,
    )
    messages = _build_worker_completion_messages(
        req=req,
        relay=relay,
        completion=completion,
        internal_error=internal_error,
        cleaned_scratch_files=cleaned_scratch_files,
        workspace_root=workspace_root,
    )
    outcome = _classify_worker_completion(
        relay=relay,
        completion=completion,
        messages=messages,
        internal_error=internal_error,
    )
    return WorkerCompletionAssembly(
        req=req,
        worker_history=worker_history,
        task_spec=task_spec,
        relay=relay,
        context_gearbox=context_gearbox,
        internal_error=internal_error,
        completion=completion,
        messages=messages,
        outcome=outcome,
    )


def _collect_worker_completion_data(
    *,
    req: WorkerDispatchRequest,
    worker_history: History,
    relay: WorkerEventRelay,
    final_validation_commands: list[str],
    preserve_scratch_records: bool,
) -> dict[str, Any]:
    final_report = _last_assistant_content(worker_history)
    continuation = _parse_continuation_report(final_report)
    is_partial = bool(continuation.get("status") == "needs_followup" or continuation.get("remaining"))
    claimed_validation = _final_report_claims_validation(final_report) or bool(continuation.get("validation_text"))

    diagnostic_environment_caveats = (
        []
        if preserve_scratch_records
        else _diagnostic_environment_caveats(relay)
    )
    _filter_scratch_write_records(relay, preserve_scratch=preserve_scratch_records)
    validation_results = _validation_results_for_task(
        relay.validation_results,
        getattr(relay, "terminal_results", []),
        final_validation_commands,
    )
    validation_command_issues = _validation_command_issues_for_task(
        getattr(relay, "terminal_results", [])
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
    not_applied_writes = list(getattr(relay, "not_applied_writes", []))
    unrecovered_not_applied_writes = _unrecovered_not_applied_writes(relay.tool_results)

    acceptance_unverified = False
    if req.acceptance.strip():
        if not is_partial and not claimed_validation and not validation_ran:
            acceptance_unverified = True
    validation_not_run = bool(relay.write_results) and not validation_ran
    is_implementation = not (
        "blueprint" in req.spec.lower()[:200]
        or "inspect" in req.goal.lower()[:100]
        or "diagnostic" in req.goal.lower()[:100]
    )

    return {
        "final_report": final_report,
        "continuation": continuation,
        "diagnostic_environment_caveats": diagnostic_environment_caveats,
        "validation_results": validation_results,
        "validation_command_issues": validation_command_issues,
        "has_writes": has_writes,
        "internal_recovery_steers": internal_recovery_steers,
        "write_failures": write_failures,
        "source_inspection_blockers": source_inspection_blockers,
        "terminal_policy_blockers": terminal_policy_blockers,
        "environment_setup_blockers": environment_setup_blockers,
        "failed_validation": failed_validation,
        "not_applied_writes": not_applied_writes,
        "unrecovered_not_applied_writes": unrecovered_not_applied_writes,
        "acceptance_unverified": acceptance_unverified,
        "validation_not_run": validation_not_run,
        "is_implementation": is_implementation,
    }


def _build_worker_completion_messages(
    *,
    req: WorkerDispatchRequest,
    relay: WorkerEventRelay,
    completion: dict[str, Any],
    internal_error: str | None,
    cleaned_scratch_files: list[str],
    workspace_root: Path | None,
) -> dict[str, Any]:
    result_errors = list(relay.api_errors)
    if internal_error:
        result_errors.insert(0, "Harness error due to an internal Worker exception.")

    final_report = completion["final_report"]
    continuation = completion["continuation"]
    write_failures = completion["write_failures"]
    source_inspection_blockers = completion["source_inspection_blockers"]
    terminal_policy_blockers = completion["terminal_policy_blockers"]
    environment_setup_blockers = completion["environment_setup_blockers"]
    failed_validation = completion["failed_validation"]
    validation_not_run = completion["validation_not_run"]
    validation_command_issues = completion["validation_command_issues"]
    diagnostic_environment_caveats = completion["diagnostic_environment_caveats"]
    acceptance_unverified = completion["acceptance_unverified"]

    structured_failure = _parse_structured_worker_failure(final_report)
    if structured_failure:
        if structured_failure.get("status") == "needs_planner_resolution":
            if not continuation.get("mismatch"):
                continuation["mismatch"] = structured_failure.get("mismatch")
            continuation["status"] = "needs_planner_resolution"
            continuation["reason"] = "planner_resolution_needed"
        else:
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
        for v in failed_validation:
            cmd = v["command"][:80]
            result_errors.append(f"Validation command failed (exit code {v['exit_code']}): {cmd}")

    if workspace_root is None:
        edited_without_read = _check_read_before_edit(
            relay.read_files,
            relay.read_outline_files,
            relay.edited_existing_files,
        )
    else:
        edited_without_read = _check_read_before_edit(
            relay.read_files,
            relay.read_outline_files,
            relay.edited_existing_files,
            file_exists=_workspace_file_exists(workspace_root),
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
    if validation_command_issues:
        result_caveats.append("Validation command issue(s) were recorded; code validation failures are reported separately.")
    for caveat in diagnostic_environment_caveats:
        if caveat not in result_caveats:
            result_caveats.append(caveat)

    if cleaned_scratch_files:
        result_caveats.append(
            "Cleaned Worker-created root validation scratch file(s): "
            + ", ".join(cleaned_scratch_files[:5])
        )

    if acceptance_unverified:
        has_deterministic_proof = (
            completion.get("has_writes")
            and bool(completion.get("validation_results"))
            and not completion.get("failed_validation")
        )
        if not has_deterministic_proof:
            result_caveats.append("Worker final report did not clearly mention validation or acceptance verification.")

    if not structured_failure and _final_report_claims_failure(final_report):
        phrase_caveat = (
            "Worker final report mentioned possible blocker, failed validation, "
            "or incomplete verification."
        )
        result_caveats.append(phrase_caveat)

    if workspace_root is not None and completion["has_writes"] and relay.touched_files:
        try:
            from aura.code_intel.audit import audit_changed_files

            touched = sorted(relay.touched_files)
            audit_findings = audit_changed_files(workspace_root, touched)
            blocked = [f for f in audit_findings if f.severity in ("error",)]
            if blocked:
                failure_files = sorted({f.file for f in blocked})
                msg = (
                    "Post-edit structural audit found high-severity issues in: "
                    + ", ".join(failure_files[:5])
                    + ". Fix these before declaring success."
                )
                result_errors.append(msg)
                for bf in blocked[:5]:
                    result_errors.append(f"  {bf.file}:{bf.line}: {bf.message}")
        except Exception:
            _log.exception("Post-edit structural audit failed")

    if (
        completion["is_implementation"]
        and not structured_failure
        and not relay.touched_files
        and not relay.failed_tool_results
        and not internal_error
        and not relay.api_errors
    ):
        result_errors.append(
            "Harness no-progress: Worker made no changes, reported no blocker, "
            "and ran no meaningful validation."
        )

    return {
        "structured_failure": structured_failure,
        "recoverable_write_failures": recoverable_write_failures,
        "failed_write_tools": failed_write_tools,
        "result_errors": result_errors,
        "result_caveats": result_caveats,
    }


def _classify_worker_completion(
    *,
    relay: WorkerEventRelay,
    completion: dict[str, Any],
    messages: dict[str, Any],
    internal_error: str | None,
) -> dict[str, Any]:
    continuation = completion["continuation"]
    result_errors = messages["result_errors"]
    result_caveats = messages["result_caveats"]
    structured_failure = messages["structured_failure"]
    recoverable_write_failures = messages["recoverable_write_failures"]
    failed_validation = completion["failed_validation"]
    not_applied_writes = completion["not_applied_writes"]
    unrecovered_not_applied_writes = completion["unrecovered_not_applied_writes"]
    source_inspection_blockers = completion["source_inspection_blockers"]
    terminal_policy_blockers = completion["terminal_policy_blockers"]
    environment_setup_blockers = completion["environment_setup_blockers"]
    diagnostic_environment_caveats = completion["diagnostic_environment_caveats"]
    acceptance_unverified = completion["acceptance_unverified"]
    validation_not_run = completion["validation_not_run"]
    write_failures = completion["write_failures"]
    is_implementation = completion["is_implementation"]

    phase_boundary = relay.phase_boundary_info is not None
    mismatch = WorkerMismatch.from_dict(continuation.get("mismatch"))
    has_planner_resolution_mismatch = (
        mismatch is not None
        or continuation.get("status") == "needs_planner_resolution"
    )

    has_hard_failure = bool(result_errors)
    structured_failure_class = str(structured_failure.get("failure_class") or "")
    has_harness_no_progress_failure = structured_failure_class in {
        "harness_no_progress",
        "worker_flow_zero_work_no_progress",
    }
    has_internal_failure = bool(
        internal_error
        or relay.api_errors
        or has_harness_no_progress_failure
    )
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
    has_source_inspection_blocker = bool(source_inspection_blockers)
    has_terminal_policy_blocker = bool(terminal_policy_blockers)
    has_environment_setup_blocker = bool(environment_setup_blockers)
    has_diagnostic_environment_blocker = bool(diagnostic_environment_caveats) and not relay.write_results
    has_no_work = not relay.touched_files and not relay.failed_tool_results and not internal_error and not relay.api_errors
    has_no_progress_failure = has_harness_no_progress_failure or (
        has_no_work and is_implementation and not structured_failure
    )
    has_unverified_acceptance = acceptance_unverified or validation_not_run

    if has_planner_resolution_mismatch:
        ok = False
        needs_followup = True
        recoverable = True
    elif has_no_progress_failure:
        ok = False
        needs_followup = False
        recoverable = False
    elif has_hard_failure:
        ok = False
        needs_followup = not has_internal_failure
        recoverable = (
            has_validation_failure
            or has_source_inspection_blocker
            or has_terminal_policy_blocker
            or has_environment_setup_blocker
        ) and not has_internal_failure
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
        validation_never_ran = any(
            "validation did not run" in str(c).lower()
            for c in result_caveats
        )
        if (bool(relay.write_results)
            and not failed_validation
            and not result_errors
            and not has_recoverable_edit_blocker
            and not internal_error
            and not validation_never_ran):
            ok = True
            needs_followup = False
            recoverable = False
        else:
            ok = False
            needs_followup = True
            recoverable = True
    else:
        ok = True
        needs_followup = False
        recoverable = False

    summary_continuation = dict(continuation)

    if has_no_progress_failure:
        summary_continuation["status"] = "harness_no_progress"
        summary_continuation["reason"] = (
            "Worker made no changes and no concrete external blocker was found."
        )
    if has_recoverable_edit_blocker:
        if not summary_continuation.get("status"):
            summary_continuation["status"] = "needs_followup"
        if result_caveats:
            if not summary_continuation.get("reason"):
                summary_continuation["reason"] = result_caveats[0]
    if validation_not_run and not has_recoverable_edit_blocker and not has_planner_resolution_mismatch:
        summary_continuation["status"] = "validation_not_run"
        summary_continuation["reason"] = "Files changed but validation did not run."
    if has_diagnostic_environment_blocker and not summary_continuation.get("status"):
        summary_continuation["status"] = "needs_followup"
        summary_continuation["reason"] = diagnostic_environment_caveats[0]

    status = _compute_outcome_status(
        ok=ok,
        needs_followup=needs_followup,
        recoverable=recoverable,
        has_internal_failure=has_internal_failure,
        has_validation_failure=has_validation_failure,
        has_recoverable_edit_blocker=has_recoverable_edit_blocker,
        has_source_inspection_blocker=has_source_inspection_blocker,
        has_environment_setup_blocker=has_environment_setup_blocker or has_diagnostic_environment_blocker,
        has_no_work=has_no_work,
        is_implementation=is_implementation,
        has_unverified_acceptance=has_unverified_acceptance,
        has_hard_failure=has_hard_failure,
        has_no_progress_failure=has_no_progress_failure,
        has_applied_writes=bool(relay.write_results),
        result_errors=result_errors,
        result_caveats=result_caveats,
        continuation=summary_continuation,
        structured_failure=structured_failure,
        write_failures=write_failures,
    )
    return {
        "mismatch": mismatch,
        "phase_boundary": phase_boundary,
        "summary_continuation": summary_continuation,
        "status": status,
        "ok": ok,
        "needs_followup": needs_followup,
        "recoverable": recoverable,
    }


def _build_worker_result_payload(
    *,
    req: WorkerDispatchRequest,
    worker_history: History,
    task_spec: WorkerTaskSpec,
    relay: WorkerEventRelay,
    context_gearbox: dict[str, Any],
    internal_error: str | None,
    completion: dict[str, Any],
    messages: dict[str, Any],
    outcome: dict[str, Any],
    validation_selector: ValidationPlan | None,
) -> tuple[str, list[str], dict[str, Any], dict[str, Any]]:
    result_errors = messages["result_errors"]
    result_caveats = messages["result_caveats"]
    structured_failure = messages["structured_failure"]
    validation_results = completion["validation_results"]
    validation_command_issues = completion["validation_command_issues"]
    unrecovered_not_applied_writes = completion["unrecovered_not_applied_writes"]
    not_applied_writes = completion["not_applied_writes"]
    failed_write_tools = messages["failed_write_tools"]
    internal_recovery_steers = completion["internal_recovery_steers"]
    recoverable_write_failures = messages["recoverable_write_failures"]
    source_inspection_blockers = completion["source_inspection_blockers"]
    terminal_policy_blockers = completion["terminal_policy_blockers"]
    environment_setup_blockers = completion["environment_setup_blockers"]
    validation_not_run = completion["validation_not_run"]
    summary_continuation = outcome["summary_continuation"]
    status = outcome["status"]
    recoverable = outcome["recoverable"]
    needs_followup = outcome["needs_followup"]
    mismatch = outcome["mismatch"]

    summary = _build_worker_summary(
        req,
        worker_history,
        relay.write_results,
        result_errors,
        summary_continuation,
        result_caveats,
        validation_results=validation_results,
        validation_command_issues=validation_command_issues,
        not_applied_writes=unrecovered_not_applied_writes,
        status=status,
        internal_error=internal_error,
    )
    modified_files = _applied_modified_files(relay.write_results)
    task_shape_summary = (
        task_spec.task_shape.to_summary_dict()
        if task_spec.task_shape is not None
        else {}
    )
    task_shape_ms = (
        getattr(task_spec.task_shape, "_task_shape_ms", None)
        if task_spec.task_shape is not None
        else None
    )
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
        "terminal_results": getattr(relay, "terminal_results", []),
        "validation_results": validation_results,
        "validation_command_issues": validation_command_issues,
        "errors": result_errors,
        "caveats": result_caveats,
        "worker_internal_error": bool(internal_error),
        "internal_error": internal_error or "",
        "validation_not_run": validation_not_run,
        "recoverable": recoverable,
        "needs_followup": needs_followup,
        "phase_boundary": relay.phase_boundary_info or {},
        "task_shape": task_shape_summary,
        "context_gearbox": context_gearbox,
        "limit": (
            relay.phase_boundary_info
            if relay.phase_boundary_info and relay.phase_boundary_info.get("limit_reached")
            else {}
        ),
    }
    if isinstance(task_shape_ms, (int, float)):
        extras["task_shape_ms"] = task_shape_ms

    if structured_failure.get("failure_class") in {
        "harness_no_progress",
        "worker_flow_zero_work_no_progress",
    }:
        details = structured_failure.get("details")
        extras["failure_class"] = structured_failure.get("failure_class")
        extras["harness_no_progress"] = details if isinstance(details, dict) else {}
    elif (
        status == "harness_error"
        and completion["is_implementation"]
        and not relay.write_results
    ):
        extras["failure_class"] = "harness_no_progress"
        extras["harness_no_progress"] = {
            "failure_class": "worker_zero_work_no_progress",
            "worker_flow_reason": "",
            "tool_counts": {
                "tool_results": len(relay.tool_results),
                "failed_tool_results": len(relay.failed_tool_results),
                "terminal_results": len(relay.terminal_results),
                "validation_results": len(validation_results),
            },
            "zero_work_recovery_attempted": False,
            "last_steering_message": "",
            "internal_recovery_steer_count": len(internal_recovery_steers),
            "phase_boundary": relay.phase_boundary_info or {},
            "errors": list(result_errors),
        }

    if mismatch is not None:
        extras["planner_resolution_needed"] = True
        extras["mismatch_kind"] = mismatch.kind
        extras["mismatch_question"] = mismatch.question_for_planner

    extras["validation_selector"] = validation_selector
    return summary, modified_files, extras, task_shape_summary


def _compute_outcome_status(
    ok: bool,
    needs_followup: bool,
    recoverable: bool,
    has_internal_failure: bool,
    has_validation_failure: bool,
    has_recoverable_edit_blocker: bool,
    has_source_inspection_blocker: bool,
    has_no_work: bool,
    is_implementation: bool,
    has_unverified_acceptance: bool,
    has_hard_failure: bool,
    has_no_progress_failure: bool,
    result_errors: list[str],
    result_caveats: list[str],
    continuation: dict[str, Any],
    has_applied_writes: bool = False,
    structured_failure: dict[str, Any] | None = None,
    write_failures: list[dict[str, Any]] | None = None,
    has_environment_setup_blocker: bool = False,
) -> str:
    """Map the boolean severity classification to a WorkerOutcomeStatus."""
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
    if "approval_rejected" in failure_classes:
        return WorkerOutcomeStatus.approval_rejected.value
    structured_status = str(continuation.get("status") or "")
    if structured_status == "needs_planner_resolution":
        return WorkerOutcomeStatus.needs_planner_resolution.value
    if has_recoverable_edit_blocker or (
        not has_applied_writes
        and any(
            fc == "edit_mechanics_blocked" or fc in EDIT_TRANSACTION_FAILURE_CLASSES
            for fc in failure_classes
        )
    ):
        return WorkerOutcomeStatus.edit_mechanics_blocked.value
    if has_validation_failure or any(fc.startswith("validation_") for fc in failure_classes):
        return WorkerOutcomeStatus.validation_failed.value
    if (
        has_internal_failure
        or has_no_progress_failure
        or any(
            fc
            in {
                "harness_error",
                "harness_no_progress",
                "internal_error",
                "worker_flow_zero_work_no_progress",
                "worker_internal_error",
            }
            for fc in failure_classes
        )
    ):
        return WorkerOutcomeStatus.harness_error.value
    if has_source_inspection_blocker or "source_inspection_command_blocked" in failure_classes:
        return WorkerOutcomeStatus.needs_followup.value
    if has_environment_setup_blocker or any(fc.startswith("project_environment_missing_") for fc in failure_classes):
        return WorkerOutcomeStatus.needs_followup.value
    if has_hard_failure:
        if structured_status == "phased":
            return WorkerOutcomeStatus.needs_followup.value
        return WorkerOutcomeStatus.needs_followup.value
    if has_no_work and is_implementation:
        return WorkerOutcomeStatus.needs_followup.value
    if has_unverified_acceptance:
        validation_never_ran = any(
            "validation did not run" in str(caveat).lower()
            for caveat in result_caveats
        )
        if (has_applied_writes
            and not has_validation_failure
            and not result_errors
            and not has_recoverable_edit_blocker
            and not has_internal_failure
            and not validation_never_ran):
            if result_caveats:
                return WorkerOutcomeStatus.completed_with_caveats.value
            return WorkerOutcomeStatus.completed.value
        else:
            return WorkerOutcomeStatus.needs_followup.value
    if ok and result_caveats:
        return WorkerOutcomeStatus.completed_with_caveats.value
    if ok:
        return WorkerOutcomeStatus.completed.value
    return WorkerOutcomeStatus.needs_followup.value


def _last_assistant_content(history: History) -> str:
    for msg in reversed(history.messages):
        if msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""


def _is_recoverable_worker_write_failure(result: dict[str, Any]) -> bool:
    if result.get("internal_recovery_steer"):
        return True
    failure_class = str(result.get("failure_class") or "")
    if failure_class == "syntax_invalid" and result.get("recoverable") is False:
        return False
    return failure_class in RECOVERABLE_WORKER_WRITE_FAILURE_CLASSES


def _unrecovered_validation_failures(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        if result.get("ok"):
            continue
        if result.get("counts_as_product_failure") is False:
            continue
        if _is_benign_search_no_match(result):
            continue
        command = str(result.get("command", ""))
        targets = set(_py_compile_targets(command))
        if targets and _later_py_compile_passes(results[index + 1:], targets):
            continue
        failures.append(result)
    return failures


_VALIDATION_COMMAND_ISSUE_CLASSES = {
    MALFORMED_VALIDATION_COMMAND,
    NO_TESTS_COLLECTED,
    TEST_SELECTION_EMPTY,
    MISSING_DEPENDENCY,
    MISSING_EXECUTABLE,
    POLICY_BLOCKED,
    TIMEOUT,
}


def _validation_command_issues_for_task(
    terminal_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for record in terminal_results:
        classification = str(record.get("validation_classification") or record.get("classification") or "")
        normalized = bool(record.get("validation_command_normalized") or record.get("normalized"))
        if (
            classification not in _VALIDATION_COMMAND_ISSUE_CLASSES
            and not normalized
        ):
            continue
        if record.get("counts_as_product_failure") is True:
            continue
        key = (
            str(record.get("validation_raw_text") or record.get("raw_text") or ""),
            str(record.get("command") or ""),
            classification,
        )
        if key in seen:
            continue
        seen.add(key)
        issues.append(record)
    return issues


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
        normalized = _normalize_worker_path(path)
        if result.get("ok") and (
            result.get("applied") is True or result.get("deleted") is True
        ):
            pending.pop(normalized, None)
            continue
        if result.get("applied") is False or str(result.get("write_outcome") or "").startswith("not_applied_"):
            if _is_edit_mechanics_not_applied(result):
                pending[normalized] = result
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
    """Return paths of existing files that were edited without being read."""
    if file_exists is None:
        file_exists = lambda p: Path(p).exists()  # noqa: E731
    all_read = {
        _normalize_worker_path(path)
        for path in (set(read_files) | set(read_outline_files))
    }
    return [
        p for p in edited_existing_files
        if _normalize_worker_path(p) not in all_read
        and file_exists(_normalize_worker_path(p))
    ]


def _workspace_file_exists(workspace_root: Path):
    root = Path(workspace_root).resolve()

    def exists(path: str) -> bool:
        try:
            candidate = Path(_normalize_worker_path(path))
            if not candidate.is_absolute():
                candidate = root / candidate
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            return False
        return resolved.exists()

    return exists


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
    for attr in ("not_applied_writes", "failed_tool_results"):
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

    for key in ("error", "result_preview", "output", "output_preview"):
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

    mismatch_raw = section("mismatch").strip()
    if mismatch_raw.startswith("{"):
        try:
            mismatch_data = json.loads(mismatch_raw)
        except (json.JSONDecodeError, TypeError):
            mismatch_data = {"raw": mismatch_raw}
    elif mismatch_raw:
        mismatch_data = {"raw": mismatch_raw}
    else:
        mismatch_data = None

    return {
        "status": section("status"),
        "reason": section("reason"),
        "completed": list_section("completed"),
        "modified_files": list_section("modified_files"),
        "validation_text": section("validation"),
        "remaining": list_section("remaining"),
        "recommended_next_step": section("recommended_next_step"),
        "mismatch": mismatch_data,
    }
