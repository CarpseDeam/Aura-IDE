"""Worker result payload assembly for bridge dispatch."""

from __future__ import annotations

from typing import Any

from aura.bridge.event_relay import WorkerEventRelay
from aura.bridge.worker_completion_messages import (
    RECOVERABLE_WORKER_WRITE_FAILURE_CLASSES,
)
from aura.bridge.worker_outcome_classifier import EDIT_TRANSACTION_FAILURE_CLASSES
from aura.bridge.worker_report import _build_worker_summary
from aura.conversation import History, WorkerDispatchRequest, WorkerTaskSpec
from aura.conversation.path_utils import (
    is_validation_scratch_path as _is_validation_scratch_path,
)
from aura.conversation.path_utils import (
    normalize_worker_path as _normalize_worker_path,
)
from aura.conversation.tool_limits import WRITE_TOOLS
from aura.validation.selector import ValidationPlan


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
    quality_findings = messages.get("quality_findings", [])
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
    phase_boundary = outcome["phase_boundary"]
    mismatch = outcome["mismatch"]

    behavioral_validation = completion.get("behavioral_validation", {})

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
        behavioral_validation=behavioral_validation,
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
        "required_behavioral_validation": behavioral_validation,
        "errors": result_errors,
        "caveats": result_caveats,
        "quality_findings": quality_findings,
        "worker_internal_error": bool(internal_error),
        "internal_error": internal_error or "",
        "validation_not_run": validation_not_run,
        "recoverable": recoverable,
        "needs_followup": needs_followup,
        "phase_boundary": relay.phase_boundary_info if phase_boundary else {},
        "task_shape": task_shape_summary,
        "context_gearbox": context_gearbox,
        "limit": (
            relay.phase_boundary_info
            if phase_boundary and relay.phase_boundary_info and relay.phase_boundary_info.get("limit_reached")
            else {}
        ),
    }
    if isinstance(task_shape_ms, (int, float)):
        extras["task_shape_ms"] = task_shape_ms

    no_progress_class = structured_failure.get("failure_class")
    if no_progress_class in {"harness_no_progress", "worker_flow_zero_work_no_progress"}:
        details = structured_failure.get("details")
        extras["no_progress"] = details if isinstance(details, dict) else {}

    if mismatch is not None:
        extras["mismatch_detected"] = True
        extras["mismatch_kind"] = mismatch.kind
        extras["mismatch_question"] = mismatch.question_for_planner

    extras["validation_selector"] = validation_selector

    # Include pre-existing validation failures discovered via attribution.
    preexisting = getattr(relay, "preexisting_validation_failures", None)
    if preexisting:
        extras["preexisting_failures"] = list(preexisting)

    return summary, modified_files, extras, task_shape_summary


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
