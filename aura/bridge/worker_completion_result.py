"""Worker completion/result assembly for bridge dispatch."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aura.bridge.event_relay import WorkerEventRelay
from aura.bridge.worker_completion_messages import (
    _build_worker_completion_messages,
    _check_read_before_edit,
    _diagnostic_environment_caveats,
)
from aura.bridge.worker_outcome_classifier import (
    _classify_worker_completion,
    _is_explicit_validation_only_request,
    _is_true_phase_boundary,
)
from aura.bridge.worker_report import _dedupe_summary_writes
from aura.bridge.worker_result_payload import (
    _build_worker_result_payload,
    _unrecovered_not_applied_writes,
)
from aura.bridge.worker_validation_results import (
    _assess_required_behavioral_validation,
    _filter_scratch_validation_results,
    _unrecovered_validation_failures,
    _validation_command_issues_for_task,
    _validation_results_for_task,
)
from aura.conversation import (
    History,
    WorkerDispatchRequest,
    WorkerDispatchResult,
    WorkerTaskSpec,
)
from aura.conversation.path_utils import (
    is_validation_scratch_path as _is_validation_scratch_path,
)
from aura.conversation.tool_limits import WRITE_TOOLS
from aura.conversation.worker_completion._summary_formatters import (
    _final_report_claims_validation,
)
from aura.validation.selector import ValidationPlan

_log = logging.getLogger(__name__)

__all__ = [
    "WorkerCompletionAssembly",
    "WorkerCompletionResult",
    "_check_read_before_edit",
    "_last_assistant_content",
    "prepare_worker_completion_result",
]

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
                if _is_true_phase_boundary(self.relay.phase_boundary_info)
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
    is_partial = bool(continuation.get("remaining"))
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
    validation_only = _is_explicit_validation_only_request(req)
    is_implementation = not validation_only and not (
        "blueprint" in req.spec.lower()[:200]
        or "inspect" in req.goal.lower()[:100]
        or "diagnostic" in req.goal.lower()[:100]
    )

    behavioral_validation = _assess_required_behavioral_validation(
        validation_commands=final_validation_commands,
        validation_results=validation_results,
        validation_command_issues=validation_command_issues,
    )

    return {
        "final_report": final_report,
        "continuation": continuation,
        "diagnostic_environment_caveats": diagnostic_environment_caveats,
        "validation_results": validation_results,
        "validation_command_issues": validation_command_issues,
        "behavioral_validation": behavioral_validation,
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
        "validation_only": validation_only,
        "is_implementation": is_implementation,
    }




def _last_assistant_content(history: History) -> str:
    for msg in reversed(history.messages):
        if msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""



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
