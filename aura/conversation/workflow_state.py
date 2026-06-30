"""Deterministic active Worker workflow state."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from typing import Any

from aura.conversation._workflow_parse import (
    _compact_title,
    _parse_json_object,
    _first_string,
    _int_or_none,
    _failure_text,
    _environment_caveats,
    _not_applied_outcome,
    _first_error,
)

from aura.conversation.dispatch import WorkerOutcomeStatus, normalize_outcome_status
from aura.conversation.tool_limits import WRITE_TOOLS


class WorkflowStatus(str, enum.Enum):
    intent_captured = "intent_captured"
    plan_ready = "plan_ready"
    planner_resolving = "planner_resolving"
    dispatched = "dispatched"
    editing = "editing"
    validating = "validating"
    blocked = "blocked"
    failed_retryable = "failed_retryable"
    failed_nonrecoverable = "failed_nonrecoverable"
    done = "done"
    cancelled = "cancelled"


class ValidationStatus(str, enum.Enum):
    not_run = "not_run"
    running = "running"
    passed = "passed"
    failed = "failed"
    mixed = "mixed"


@dataclass(frozen=True)
class ValidationCommandRun:
    command: str
    ok: bool | None = None
    exit_code: int | None = None


@dataclass(frozen=True)
class WorkflowState:
    tool_call_id: str
    task_title: str
    user_intent_summary: str
    status: WorkflowStatus
    pending_user_action: str = ""
    changed_files: tuple[str, ...] = ()
    validation_commands_run: tuple[ValidationCommandRun, ...] = ()
    validation_status: ValidationStatus = ValidationStatus.not_run
    blocker_reason: str = ""
    failure_reason: str = ""
    follow_up_required: bool = False
    write_outcome: str = ""
    caveats: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    @classmethod
    def intent_captured(
        cls,
        tool_call_id: str,
        goal: str,
        *,
        summary: str = "",
    ) -> "WorkflowState":
        return cls(
            tool_call_id=tool_call_id,
            task_title=_compact_title(goal or summary or "Worker task"),
            user_intent_summary=(summary or goal).strip(),
            status=WorkflowStatus.intent_captured,
        )

    def with_status(
        self,
        status: WorkflowStatus,
        *,
        pending_user_action: str | None = None,
        blocker_reason: str | None = None,
        failure_reason: str | None = None,
        follow_up_required: bool | None = None,
    ) -> "WorkflowState":
        return replace(
            self,
            status=status,
            pending_user_action=self.pending_user_action if pending_user_action is None else pending_user_action,
            blocker_reason=self.blocker_reason if blocker_reason is None else blocker_reason,
            failure_reason=self.failure_reason if failure_reason is None else failure_reason,
            follow_up_required=self.follow_up_required if follow_up_required is None else follow_up_required,
        )

    def with_changed_file(self, path: str) -> "WorkflowState":
        normalized = _normalize_path(path)
        if not normalized or normalized in self.changed_files:
            return self
        return replace(self, changed_files=(*self.changed_files, normalized))

    def with_changed_files(self, paths: list[str] | tuple[str, ...]) -> "WorkflowState":
        state = self
        for path in paths:
            state = state.with_changed_file(path)
        return state

    def with_validation_run(
        self,
        command: str,
        *,
        ok: bool | None = None,
        exit_code: int | None = None,
    ) -> "WorkflowState":
        command = command.strip()
        if not command:
            return self
        runs = list(self.validation_commands_run)
        runs.append(ValidationCommandRun(command=command, ok=ok, exit_code=exit_code))
        return replace(
            self,
            validation_commands_run=tuple(runs),
            validation_status=_validation_status(tuple(runs)),
        )

    def absorb_worker_tool_result(
        self,
        name: str,
        ok: bool,
        result: str,
        extras: dict[str, Any] | None = None,
    ) -> "WorkflowState":
        parsed = _parse_json_object(result)
        state = self

        if name in WRITE_TOOLS:
            path = _first_string(parsed, "path", "rel_path") or _first_string(extras or {}, "rel_path")
            applied = parsed.get("applied")
            write_outcome = str(parsed.get("write_outcome") or "")
            if ok and applied is True and path:
                state = state.with_changed_file(path).with_status(
                    WorkflowStatus.editing,
                    pending_user_action="",
                )
                if write_outcome:
                    state = replace(state, write_outcome=write_outcome)
                caveats = _environment_caveats(parsed)
                if caveats:
                    state = replace(state, caveats=(*state.caveats, *caveats))
            elif (not ok) or applied is False:
                reason = _failure_text(parsed, fallback=result)
                if not reason and write_outcome:
                    reason = write_outcome
                state = state.with_status(
                    WorkflowStatus.blocked,
                    blocker_reason=reason,
                    follow_up_required=True,
                )
                state = replace(
                    state,
                    write_outcome=write_outcome or state.write_outcome,
                    blockers=(*state.blockers, reason) if reason else state.blockers,
                )

        if name == "run_terminal_command" and isinstance(parsed, dict):
            command = str(parsed.get("command") or "")
            if command:
                state = state.with_validation_run(
                    command,
                    ok=bool(parsed.get("ok")) if "ok" in parsed else ok,
                    exit_code=_int_or_none(parsed.get("exit_code")),
                )
                if state.validation_status in {ValidationStatus.failed, ValidationStatus.mixed}:
                    state = state.with_status(
                        WorkflowStatus.blocked,
                        blocker_reason=f"Validation failed: {command}",
                        follow_up_required=True,
                    )
                else:
                    state = state.with_status(WorkflowStatus.validating)

        if name == "run_and_watch" and isinstance(parsed, dict):
            command = str(parsed.get("command") or "")
            if command:
                state = state.with_validation_run(
                    command,
                    ok=bool(parsed.get("ok")) if "ok" in parsed else ok,
                    exit_code=_int_or_none(parsed.get("exit_code")),
                )
                if state.validation_status in {ValidationStatus.failed, ValidationStatus.mixed}:
                    state = state.with_status(
                        WorkflowStatus.blocked,
                        blocker_reason=f"Validation failed: {command}",
                        follow_up_required=True,
                    )
                else:
                    state = state.with_status(WorkflowStatus.validating)

        return state

    def finish(
        self,
        *,
        ok: bool,
        summary: str,
        needs_followup: bool,
        status: str | None,
        modified_files: list[str] | None = None,
        validation: str | None = None,
        extras: dict[str, Any] | None = None,
    ) -> "WorkflowState":
        state = self.with_changed_files(modified_files or [])
        if validation and not state.validation_commands_run:
            state = state.with_validation_run(validation, ok=ok)

        extra_validation = extras.get("validation_results") if isinstance(extras, dict) else None
        if isinstance(extra_validation, list):
            for item in extra_validation:
                if isinstance(item, dict):
                    state = state.with_validation_run(
                        str(item.get("command") or ""),
                        ok=bool(item.get("ok")) if "ok" in item else None,
                        exit_code=_int_or_none(item.get("exit_code")),
                )

        outcome = normalize_outcome_status(status)
        failure_reason = _first_error(summary, extras)
        write_outcome = _first_string(extras or {}, "write_outcome") if isinstance(extras, dict) else ""
        if not write_outcome and isinstance(extras, dict):
            writes = extras.get("writes")
            if isinstance(writes, list) and writes:
                write_outcome = str(writes[-1].get("write_outcome") or "")
            not_applied = extras.get("not_applied_writes")
            if not write_outcome and isinstance(not_applied, list) and not_applied:
                write_outcome = str(not_applied[-1].get("write_outcome") or "")
        caveats = tuple(str(item) for item in (extras or {}).get("caveats", []) if isinstance(extras, dict))
        blockers = tuple(str(item) for item in (extras or {}).get("errors", []) if isinstance(extras, dict))
        if outcome == WorkerOutcomeStatus.cancelled.value:
            final_status = WorkflowStatus.cancelled
        elif outcome == WorkerOutcomeStatus.needs_planner_resolution.value or (
            isinstance(extras, dict) and extras.get("planner_resolution_needed")
        ):
            final_status = WorkflowStatus.planner_resolving
        elif ok and not needs_followup and not _not_applied_outcome(write_outcome):
            final_status = WorkflowStatus.done
        elif outcome in {
            WorkerOutcomeStatus.harness_error.value,
            WorkerOutcomeStatus.craft_rejected.value,
            WorkerOutcomeStatus.approval_rejected.value,
        }:
            final_status = WorkflowStatus.failed_nonrecoverable
        else:
            final_status = WorkflowStatus.failed_retryable

        # Compute blocker/failure reason specially for planner_resolving
        if final_status == WorkflowStatus.planner_resolving and isinstance(extras, dict):
            mismatch_question = extras.get("mismatch_question", "")
            if mismatch_question:
                blocker_reason = mismatch_question
                failure_reason = ""
            else:
                blocker_reason = failure_reason
        else:
            blocker_reason = (
                "" if final_status in {WorkflowStatus.done, WorkflowStatus.cancelled} else failure_reason
            )
            if final_status == WorkflowStatus.done:
                failure_reason = ""

        return replace(
            state,
            status=final_status,
            pending_user_action=_pending_action(final_status, needs_followup),
            blocker_reason=blocker_reason,
            failure_reason=failure_reason,
            follow_up_required=needs_followup
                or final_status in {WorkflowStatus.failed_retryable, WorkflowStatus.planner_resolving},
            validation_status=state.validation_status,
            write_outcome=write_outcome or state.write_outcome,
            caveats=(*state.caveats, *caveats),
            blockers=(*state.blockers, *blockers),
        )


def _normalize_path(path: str) -> str:
    normalized = str(path).replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _validation_status(runs: tuple[ValidationCommandRun, ...]) -> ValidationStatus:
    completed = [run.ok for run in runs if run.ok is not None]
    if not runs:
        return ValidationStatus.not_run
    if not completed:
        return ValidationStatus.running
    if all(completed):
        return ValidationStatus.passed
    if any(completed):
        return ValidationStatus.mixed
    return ValidationStatus.failed


def _pending_action(status: WorkflowStatus, needs_followup: bool) -> str:
    if status == WorkflowStatus.planner_resolving:
        return "Planner is resolving the Worker mismatch."
    if status == WorkflowStatus.failed_retryable:
        return "Review the blocker, then continue or revise the plan."
    if status == WorkflowStatus.failed_nonrecoverable:
        return "Review the failure before retrying."
    if needs_followup:
        return "Review the remaining work and continue."
    return ""


__all__ = [
    "ValidationCommandRun",
    "ValidationStatus",
    "WorkflowState",
    "WorkflowStatus",
]
