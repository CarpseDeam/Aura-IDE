"""Outcome classification for worker dispatch results — extracted from worker_completion_result.py."""

from __future__ import annotations

import re
from typing import Any

from aura.bridge.event_relay import WorkerEventRelay
from aura.conversation import WorkerDispatchRequest, WorkerMismatch
from aura.conversation.worker_outcome import WorkerOutcomeStatus

# Internal: maximum number of validation commands on an artifact item that
# still qualifies as "did not explicitly declare behavioral/UI validation".
# When zero commands are declared (or only non-behavioral commands), skipped
# behavioral validation is a caveat, not a blocker.
_ARTIFACT_MAX_DECLARED_BEHAVIORAL_CMDS = 3

__all__ = [
    "EDIT_TRANSACTION_FAILURE_CLASSES",
    "_classify_worker_completion",
    "_compute_outcome_status",
    "_is_explicit_validation_only_request",
    "_is_true_phase_boundary",
    "_terminal_failure_status",
]

EDIT_TRANSACTION_FAILURE_CLASSES = {
    "edit_transaction_hash_mismatch",
    "edit_transaction_symbol_not_found",
    "edit_transaction_ambiguous_symbol",
    "edit_transaction_invalid_operation",
    "edit_transaction_invalid_syntax",
    "edit_transaction_not_applicable",
}


def _classify_worker_completion(
    *,
    relay: WorkerEventRelay,
    completion: dict[str, Any],
    messages: dict[str, Any],
    internal_error: str | None,
    req: WorkerDispatchRequest | None = None,
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

    phase_boundary = _is_true_phase_boundary(relay.phase_boundary_info)
    mismatch = WorkerMismatch.from_dict(continuation.get("mismatch"))
    has_planner_resolution_mismatch = mismatch is not None

    has_hard_failure = bool(result_errors)
    structured_failure_class = str(structured_failure.get("failure_class") or "")
    has_harness_no_progress_failure = structured_failure_class in {
        "harness_no_progress",
    }
    has_internal_failure = bool(
        internal_error
        or relay.api_errors
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
    has_no_work = (
        not relay.touched_files
        and not completion["validation_results"]
        and not relay.failed_tool_results
        and not internal_error
        and not relay.api_errors
    )
    has_no_implementation_work = (
        is_implementation
        and not relay.touched_files
        and bool(completion["validation_results"])
        and not failed_validation
        and not relay.failed_tool_results
        and not internal_error
        and not relay.api_errors
    )
    has_no_progress_failure = has_harness_no_progress_failure or (
        (has_no_work or has_no_implementation_work) and is_implementation and not structured_failure
    )
    has_unverified_acceptance = acceptance_unverified or validation_not_run
    behavioral = completion.get("behavioral_validation", {})
    has_required_behavioral_skipped = bool(behavioral.get("skipped", []))

    # Bounded WorkArtifact items: skipped behavioral/UI validation is a
    # caveat, not a blocker.  The primary gate for an artifact item is its
    # declared validation commands (e.g. compileall) and file writes.
    _is_artifact_item = (
        req is not None
        and bool(req.artifact_id)
        and bool(req.artifact_item_id)
    )

    if has_planner_resolution_mismatch:
        ok = False
        needs_followup = True
        recoverable = True
    elif has_required_behavioral_skipped and not has_internal_failure:
        # Behavioral/UI validation was explicitly declared and skipped.
        # For ALL item types (flat dispatch and WorkArtifact), this is a
        # recoverable failure — never ok.  The artifact-item normalizer
        # in dispatch.py may later normalize when the item did not
        # actually declare behavioral commands.
        ok = False
        needs_followup = True
        recoverable = True
    elif has_no_progress_failure:
        ok = False
        needs_followup = True
        recoverable = True
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
        summary_continuation["status"] = "no_progress"
        summary_continuation["reason"] = (
            "Worker made no changes and no concrete external blocker was found — continuation, not failure."
        )
    if has_recoverable_edit_blocker:
        if not summary_continuation.get("status"):
            summary_continuation["status"] = "edit_mechanics_blocked"
        if result_caveats:
            if not summary_continuation.get("reason"):
                summary_continuation["reason"] = result_caveats[0]
    if validation_not_run and not has_recoverable_edit_blocker and not has_planner_resolution_mismatch:
        summary_continuation["status"] = "validation_not_run"
        summary_continuation["reason"] = "Files changed but validation did not run."
    if has_diagnostic_environment_blocker and not summary_continuation.get("status"):
        summary_continuation["status"] = "harness_error"
        summary_continuation["reason"] = diagnostic_environment_caveats[0]
    if has_required_behavioral_skipped and not summary_continuation.get("status"):
        skipped_cmds = behavioral.get("skipped", [])
        summary_continuation["status"] = "validation_failed"
        reason = "Required behavioral validation skipped: " + (skipped_cmds[0] if skipped_cmds else "(unknown)")
        summary_continuation["reason"] = reason

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
        has_required_behavioral_skipped=has_required_behavioral_skipped,
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
    has_required_behavioral_skipped: bool = False,
) -> str:
    """Map the boolean severity classification to a WorkerOutcomeStatus."""
    structured_failure = structured_failure or {}
    write_failures = write_failures or []
    failure_classes = [
        str(item.get("failure_class") or "")
        for item in [structured_failure, *write_failures]
        if isinstance(item, dict)
    ]
    if "approval_rejected" in failure_classes:
        return WorkerOutcomeStatus.approval_rejected.value
    structured_status = str(continuation.get("status") or "")
    if structured_status == "mismatch_detected":
        return _terminal_failure_status(
            has_validation_failure=has_validation_failure,
            has_recoverable_edit_blocker=has_recoverable_edit_blocker,
        )
    if has_recoverable_edit_blocker or (
        not has_applied_writes
        and any(
            fc == "edit_mechanics_blocked" or fc in EDIT_TRANSACTION_FAILURE_CLASSES
            for fc in failure_classes
        )
    ):
        return WorkerOutcomeStatus.edit_mechanics_blocked.value
    if has_validation_failure or has_required_behavioral_skipped or any(fc.startswith("validation_") for fc in failure_classes):
        return WorkerOutcomeStatus.validation_failed.value
    if (
        has_internal_failure
        or any(
            fc
            in {
                "harness_error",
                "internal_error",
                "worker_internal_error",
            }
            for fc in failure_classes
        )
    ):
        return WorkerOutcomeStatus.harness_error.value
    if has_source_inspection_blocker or "source_inspection_command_blocked" in failure_classes:
        return WorkerOutcomeStatus.harness_error.value
    if has_environment_setup_blocker or any(fc.startswith("project_environment_missing_") for fc in failure_classes):
        return WorkerOutcomeStatus.harness_error.value
    if has_hard_failure:
        if structured_status == "phased":
            return WorkerOutcomeStatus.harness_error.value
        return WorkerOutcomeStatus.harness_error.value
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
            return WorkerOutcomeStatus.validation_failed.value
    if ok and result_caveats:
        return WorkerOutcomeStatus.completed_with_caveats.value
    if ok:
        return WorkerOutcomeStatus.completed.value
    return _terminal_failure_status(
        has_validation_failure=has_validation_failure,
        has_recoverable_edit_blocker=has_recoverable_edit_blocker,
    )


def _terminal_failure_status(
    *,
    has_validation_failure: bool,
    has_recoverable_edit_blocker: bool,
) -> str:
    if has_validation_failure:
        return WorkerOutcomeStatus.validation_failed.value
    if has_recoverable_edit_blocker:
        return WorkerOutcomeStatus.edit_mechanics_blocked.value
    return WorkerOutcomeStatus.harness_error.value


def _is_true_phase_boundary(info: dict[str, Any] | None) -> bool:
    if not isinstance(info, dict):
        return False
    return bool(info)


def _is_explicit_validation_only_request(req: WorkerDispatchRequest) -> bool:
    text = " ".join(
        part for part in (req.goal, req.spec, req.acceptance, req.summary) if part
    ).lower()
    if not text:
        return False
    patterns = (
        r"\bvalidation[- ]only\b",
        r"\btests?[- ]only\b",
        r"\bverify[- ]only\b",
        r"\bonly\s+(?:run\s+)?(?:validation|tests?|checks?|verification)\b",
        r"\b(?:run\s+)?(?:validation|tests?|checks?|verification)\s+only\b",
        r"\bvalidate\s+only\b",
        r"\bonly\s+validate\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)
