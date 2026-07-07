"""Worker candidate finalization after a no-tool-call response."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from aura.client import Event
from aura.conversation.completion_guard import assistant_message_text
from aura.conversation.edit_recovery_state import edit_recovery_details
from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.path_utils import (
    is_validation_scratch_path as _is_validation_scratch_path,
)
from aura.conversation.path_utils import (
    normalize_worker_path as _normalize_worker_path,
)
from aura.conversation.syntax_repair_state import (
    has_terminal_syntax_failure,
    set_syntax_repair_state,
    syntax_repair_paths,
)
from aura.conversation.terminal_syntax import is_python_path
from aura.conversation.validation_failure_routing import (
    route_validation_failure,
)
from aura.conversation.worker_final_report_guard import (
    WORKER_FINAL_REPORT_PROOF_REQUIRED_TEXT,
    worker_final_report_missing_proof,
)
from aura.conversation.worker_final_validation import (
    emit_explicit_validation_result,
    emit_explicit_validation_runs,
    run_explicit_validation_commands,
)
from aura.conversation.worker_flow import WORKER_FLOW_ZERO_WORK_RECOVERY_TEXT
from aura.work_artifact.model import ValidationCommandSpec
from aura.conversation.worker_fingerprints import fingerprint_paths
from aura.conversation.worker_recovery_messages import (
    PATCH_CANDIDATE_INVALID_SYNTAX_ACTION,
    WORKER_AUTO_PY_COMPILE_INSTRUCTION,
    WORKER_BATCHED_VALIDATION_INSTRUCTION,
    WORKER_DEPENDENT_CONTRACT_INSTRUCTION,
    WORKER_EDIT_RECOVERY_INSTRUCTION,
    WORKER_IMPORT_FAILURE_INSTRUCTION,
    WORKER_LAUNCH_FAILURE_INSTRUCTION,
)
from aura.conversation.worker_validation import (
    emit_auto_dependent_import_info,
    emit_auto_import_result,
    emit_auto_launch_result,
    emit_auto_py_compile_result,
    run_focused_py_compile,
)
from aura.dependency_context import compute_dependents
from aura.verify import run_dependent_import_check, run_focused_import_check


@dataclass(frozen=True)
class _ValidationFinding:
    rung: str
    paths: tuple[str, ...]
    diagnostics: str
    dependent_paths: tuple[str, ...] = ()
    command: str | None = None

EventCallback = Callable[[Event], None]
WorkerFinalizationAction = Literal["continue", "finished", "none"]

# ── Zero-work / blocker helpers (moved from manager.py for gate access) ──────

_ALLOWED_ZERO_WORK_FAILURE_CLASSES = frozenset(
    {
        "approval_rejected",
        "cancelled",
        "conflicting_spec",
        "dispatch_blocked",
        "dispatch_not_started",
        "external_validation_runtime_missing",
        "file_not_found",
        "impossible_spec",
        "missing_file",
        "missing_path",
        "missing_required_file",
        "path_not_found",
        "permission_denied",
        "required_path_missing",
        "runtime_environment_missing",
        "source_inspection_command_blocked",
        "tool_failure",
        "tool_permission_denied",
        "user_cancelled",
        "validation_environment_missing",
        "write_rejected",
    }
)

_ALLOWED_ZERO_WORK_FAILURE_PREFIXES = (
    "project_environment_missing_",
    "permission_",
)

_ALLOWED_ZERO_WORK_BLOCKER_RE = re.compile(
    r"\b(?:required\s+)?(?:file|path|directory)\b.{0,80}\b"
    r"(?:missing|not\s+found|does\s+not\s+exist|unavailable)\b|"
    r"\b(?:permission|access)\s+denied\b|"
    r"\b(?:cannot|can't|could\s+not|couldn't|unable\s+to)\s+(?:read|write|access)\b|"
    r"\b(?:missing|unavailable)\s+(?:runtime|environment|tool|dependency|executable)\b|"
    r"\b(?:conflicting|impossible)\s+(?:spec|requirements?)\b",
    re.IGNORECASE | re.DOTALL,
)


def _candidate_final_payload(full_message: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(full_message, dict):
        return {}
    try:
        parsed = json.loads(assistant_message_text(full_message))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _candidate_final_has_real_zero_work_blocker(
    full_message: dict[str, Any] | None,
) -> bool:
    payload = _candidate_final_payload(full_message)
    if payload.get("status") == "mismatch_detected":
        return bool(
            payload.get("mismatch")
            or payload.get("question")
            or payload.get("question_for_planner")
            or payload.get("error")
        )
    failure_class = str(payload.get("failure_class") or "")
    if failure_class in _ALLOWED_ZERO_WORK_FAILURE_CLASSES:
        return True
    if any(failure_class.startswith(prefix) for prefix in _ALLOWED_ZERO_WORK_FAILURE_PREFIXES):
        return True
    if payload.get("reject") or payload.get("dispatch_not_started"):
        return True
    return bool(_ALLOWED_ZERO_WORK_BLOCKER_RE.search(assistant_message_text(full_message or {})))


def _worker_has_zero_applied_writes(state: _SendState) -> bool:
    return not _worker_has_concrete_file_progress(state)


def _worker_has_concrete_file_progress(state: _SendState) -> bool:
    flow = state.worker_flow
    write_actions = int(getattr(flow.state, "write_actions", 0) or 0) if flow else 0
    return bool(
        write_actions > 0
        or state.worker_app_writes
        or state.syntax_validation_required
    )


def _worker_has_attempted_write(state: _SendState) -> bool:
    flow = state.worker_flow
    write_intents = int(getattr(flow.state, "write_intents", 0) or 0) if flow else 0
    return write_intents > 0 or bool(state.write_attempts_by_path)


def _worker_artifact_item_validated_done(state: _SendState) -> bool:
    if not (state.worker_artifact_id and state.worker_artifact_item_id):
        return False
    if not state.worker_explicit_validation_passed:
        return False
    return True


# ── Recovery nudge helpers ───────────────────────────────────────────────────


def _append_worker_zero_work_recovery(
    state: _SendState,
    history: History,
    *,
    reason: str,
    steering: str,
) -> None:
    details = [
        WORKER_FLOW_ZERO_WORK_RECOVERY_TEXT,
        "",
        "Internal recovery context:",
        f"- worker_flow_reason: {reason}",
    ]
    if steering:
        details.append(f"- last_steering: {steering}")
    history.append_user_text("\n".join(details))
    state.worker_flow_nudge_count += 1
    state.worker_flow_last_reason = reason
    state.worker_flow_last_steering = steering


def _append_worker_thrash_recovery(
    state: _SendState,
    history: History,
    *,
    reason: str,
    steering: str,
) -> None:
    details = [
        "Worker Flow internal continuation:",
        "Use the context already gathered. Do not restart orientation or restate the plan.",
        "Choose the next smallest safe edit, make it now, and validate it.",
        "If no safe edit is possible, return a real blocker with the exact missing file, permission, tool, or dispatch mismatch.",
        "",
        "Internal recovery context:",
        f"- worker_flow_reason: {reason}",
    ]
    if steering:
        details.append(f"- last_steering: {steering}")
    history.append_user_text("\n".join(details))
    state.worker_flow_nudge_count += 1
    state.worker_flow_last_reason = reason
    state.worker_flow_last_steering = steering


def _handle_worker_zero_work_final(
    state: _SendState,
    history: History,
    on_event: EventCallback,
    finish_worker_recoverable_followup: Callable[..., None],
) -> str:
    """Recover or fail internally when Worker tries to finish with no work.

    Caller is expected to have already checked
    ``state.worker_explicit_validation_passed`` (terminal-success) before
    calling this — the artifact-validated-done and validation-passed bailouts
    that lived here in manager.py are handled at the gate level.
    """
    if not _worker_has_zero_applied_writes(state):
        return "none"
    if _worker_has_attempted_write(state):
        return "none"
    if state.reject_all_for_turn:
        return "none"
    if _candidate_final_has_real_zero_work_blocker(state.candidate_final_message):
        return "none"

    reason = state.worker_flow_last_reason or "zero_work_final"
    fp = f"zero_work|{reason}|{state.worker_flow_last_steering}"
    if state.progress_monitor is not None and state.progress_monitor.check(
        fp, state.write_attempt_count()
    ).progressing:
        _append_worker_zero_work_recovery(
            state, history,
            reason=reason,
            steering=state.worker_flow_last_steering,
        )
        return "nudged"

    # Stalled — fail (terminal-success already checked by caller).
    finish_worker_recoverable_followup(
        on_event,
        failure_class="worker_flow_zero_work_no_progress",
        error=(
            "Worker could not make progress after internal zero-work "
            "recovery. Handing step back for planner resolution."
        ),
        details={
            "reason": reason,
            "steering": state.worker_flow_last_steering,
        },
    )
    return "finished"


def handle_worker_candidate_finalization(
    *,
    state: _SendState,
    full_message: dict,
    history: History,
    workspace_root,
    on_event: EventCallback,
    finish_worker_recoverable_followup: Callable[..., None],
    handle_worker_flow_steering: Callable[[_SendState, EventCallback], str],
    declared_run_command: str | None = None,
    explicit_validation_commands: list[ValidationCommandSpec] | None = None,
) -> WorkerFinalizationAction:
    state.candidate_final_message = full_message

    if state.worker_needs_final_report:
        return _release_candidate_final(
            state=state,
            history=history,
            on_event=on_event,
            finish_worker_recoverable_followup=finish_worker_recoverable_followup,
        )

    if has_terminal_syntax_failure(state.syntax_repair_required):
        if not state.worker_recovery_nudge_sent:
            diagnostic_parts = []
            for path, s in state.syntax_repair_required.items():
                if s.get("repair_failed") and s.get("error"):
                    diagnostic_parts.append(f"{path}:\n{s['error']}")
            diagnostic_text = "\n\n".join(diagnostic_parts)
            instruction = (
                "Terminal py_compile still failing after repair. "
                "Re-read the failing Python file, fix the syntax error, "
                "then re-run python -m py_compile. "
                "Finish only after py_compile passes."
            )
            if diagnostic_text:
                instruction += f"\n\nDiagnostic output:\n{diagnostic_text}"
            history.append_user_text(instruction)
            state.worker_recovery_nudge_sent = True
            state.discard_worker_candidate_final()
            return "continue"
        failing_paths = sorted(
            p for p, s in state.syntax_repair_required.items()
            if s.get("repair_failed")
        )
        finish_worker_recoverable_followup(
            on_event,
            failure_class="syntax_invalid",
            error="Python syntax still fails after two repair attempts.",
            details={
                "failing_files": failing_paths,
                "suggested_next_tool": "dispatch_to_worker",
                "suggested_next_action": (
                    "Redispatch with a narrower edit target or "
                    "different approach to the failing file."
                ),
                "dispatch_mismatch": True,
                "worker_confusion_question": (
                    "Worker could not repair Python syntax errors "
                    "after two repair attempts"
                    + (": " + ", ".join(failing_paths) if failing_paths else ".")
                ),
            },
        )
        return "finished"

    # Carry import-verification paths forward for re-check.
    if state.import_verification_required:
        for path in state.import_verification_required:
            state.syntax_validation_required.add(path)

    edit_recovery_pending = bool(
        state.edit_fallback_required
        or state.line_range_reread_required
        or state.patch_invalid_syntax_required
    )
    syntax_repair_pending = bool(syntax_repair_paths(state.syntax_repair_required))
    if edit_recovery_pending or syntax_repair_pending:
        if not state.worker_recovery_nudge_sent:
            if edit_recovery_pending:
                if (
                    state.patch_invalid_syntax_required
                    and not state.edit_fallback_required
                    and not state.line_range_reread_required
                ):
                    instruction = PATCH_CANDIDATE_INVALID_SYNTAX_ACTION
                else:
                    instruction = WORKER_EDIT_RECOVERY_INSTRUCTION
            else:
                # Distinguish craft-gate failures from terminal py_compile failures.
                any_repair_failed = any(
                    s.get("repair_failed")
                    for s in state.syntax_repair_required.values()
                )
                if any_repair_failed:
                    instruction = (
                        "Previous py_compile failed. Re-read the touched "
                        "Python file, repair it with patch_file, then run python -m py_compile again. "
                        "Finish only after py_compile passes."
                    )
                else:
                    instruction = (
                        "Validation caught invalid Python in the following file(s). "
                        "Re-read the file, repair with patch_file, "
                        "then run python -m py_compile. "
                        "Finish only after py_compile passes."
                    )
            if not edit_recovery_pending:
                diagnostic_parts = []
                for path, s in state.syntax_repair_required.items():
                    if (
                        not s.get("awaiting_validation")
                        and not s.get("repair_failed")
                        and s.get("error")
                    ):
                        diagnostic_parts.append(f"{path}:\n{s['error']}")
                diagnostic_text = "\n\n".join(diagnostic_parts)
                if diagnostic_text:
                    instruction += f"\n\nDiagnostic output:\n{diagnostic_text}"
            history.append_user_text(instruction)
            state.worker_recovery_nudge_sent = True
            state.discard_worker_candidate_final()
            return "continue"
        error_parts = [
            "Worker stopped before recovering from a recoverable failure."
        ]
        details: dict[str, object] = {}
        if syntax_repair_pending:
            sync_paths = sorted(syntax_repair_paths(state.syntax_repair_required))
            error_parts.append(f" Syntax repair pending on: {', '.join(sync_paths)}.")
            details["syntax_paths"] = sync_paths
        if edit_recovery_pending:
            error_parts.append(" Edit mechanics recovery pending.")
            details.update(edit_recovery_details(
                state.edit_fallback_required,
                state.line_range_reread_required,
            ))
            if state.patch_invalid_syntax_required:
                details["patch_invalid_syntax_paths"] = sorted(
                    state.patch_invalid_syntax_required
                )
        details.update({
            "suggested_next_tool": "dispatch_to_worker",
            "suggested_next_action": (
                "Redispatch with exact edit regions for "
                "the files that failed to apply."
            ),
            "dispatch_mismatch": True,
            "worker_confusion_question": (
                "Worker exhausted edit-mechanics recovery; "
                "could not apply edits for the targeted files."
            ),
        })
        finish_worker_recoverable_followup(
            on_event,
            failure_class="worker_recovery_exhausted",
            error="".join(error_parts),
            details=details or None,
        )
        return "finished"

    state.syntax_validation_required.difference_update(
        path for path in set(state.syntax_validation_required)
        if _is_validation_scratch_path(path)
    )
    state.syntax_validation_required.difference_update(
        path for path in set(state.syntax_validation_required)
        if not is_python_path(_normalize_worker_path(path))
    )
    if state.syntax_validation_required:
        product_paths = sorted(
            _normalize_worker_path(path) for path in state.syntax_validation_required
            if not _is_validation_scratch_path(path)
            and is_python_path(_normalize_worker_path(path))
        )
        if product_paths:
            action = _run_structural_tier(
                state=state,
                history=history,
                workspace_root=workspace_root,
                on_event=on_event,
                product_paths=product_paths,
            )
            if action != "none":
                return action

    action = _run_behavioral_tier(
        state=state,
        history=history,
        workspace_root=workspace_root,
        on_event=on_event,
        finish_worker_recoverable_followup=finish_worker_recoverable_followup,
        declared_run_command=declared_run_command,
        explicit_validation_commands=explicit_validation_commands,
    )
    if action != "none":
        return action

    # Terminal-success check: validation passed always wins first,
    # before any recovery nudge or failure path.
    if state.worker_explicit_validation_passed:
        return _release_candidate_final(
            state=state,
            history=history,
            on_event=on_event,
            finish_worker_recoverable_followup=finish_worker_recoverable_followup,
        )

    if (
        state.worker_flow is not None
        and state.worker_flow.requires_validation_before_final()
    ):
        if not state.worker_validation_nudge_sent:
            history.append_user_text(state.worker_flow.validation_required_text())
            state.worker_validation_nudge_sent = True
            state.discard_worker_candidate_final()
            return "continue"
        state.discard_worker_candidate_final()
        classification = state.worker_flow.changed_file_classification()
        suggested_next_action = (
            "Run a docs-appropriate validation command if available, or state "
            "that no Python/source files changed and no docs-specific command "
            "is available, then provide the final report."
            if classification.docs_only
            else (
                "Run the smallest relevant py_compile or pytest "
                "command, then provide the final report."
            )
        )
        finish_worker_recoverable_followup(
            on_event,
            failure_class="worker_validation_required",
            error=(
                "Worker stopped after changing files without running "
                "focused validation after one validation nudge."
            ),
            details={
                "suggested_next_tool": "run_terminal_command",
                "suggested_next_action": suggested_next_action,
                "dispatch_mismatch": True,
            },
        )
        return "finished"

    flow_steering_action = handle_worker_flow_steering(state, on_event)
    if flow_steering_action != "none":
        state.discard_worker_candidate_final()
        if flow_steering_action == "finished":
            return "finished"
        return "continue"

    zero_work_action = _handle_worker_zero_work_final(
        state, history, on_event, finish_worker_recoverable_followup,
    )
    if zero_work_action != "none":
        state.discard_worker_candidate_final()
        if zero_work_action == "finished":
            return "finished"
        return "continue"

    return _release_candidate_final(
        state=state,
        history=history,
        on_event=on_event,
        finish_worker_recoverable_followup=finish_worker_recoverable_followup,
    )


def _run_structural_tier(
    *,
    state: _SendState,
    history: History,
    workspace_root,
    on_event: EventCallback,
    product_paths: list[str],
) -> WorkerFinalizationAction:
    fp_struct = fingerprint_paths(set(product_paths), workspace_root)
    if fp_struct and fp_struct == state.last_structural_ok_fingerprint:
        state.syntax_validation_required.clear()
        for path in product_paths:
            state.import_verification_required.discard(path)
        if state.worker_flow is not None:
            state.worker_flow.mark_validation_satisfied()
        logging.getLogger(__name__).info("Skipping structural validation: fingerprint match")
        return "none"

    findings: list[_ValidationFinding] = []

    all_ok, diagnostics = run_focused_py_compile(
        product_paths,
        workspace_root=workspace_root,
    )
    emit_auto_py_compile_result(
        paths=product_paths,
        ok=all_ok,
        diagnostics=diagnostics,
        on_event=on_event,
        workspace_root=workspace_root,
    )

    compiled_paths = []
    if all_ok:
        compiled_paths = product_paths
    else:
        for path in product_paths:
            path_ok, path_diag = run_focused_py_compile(
                [path],
                workspace_root=workspace_root,
            )
            if path_ok:
                compiled_paths.append(path)
            else:
                findings.append(_ValidationFinding(rung="py_compile", paths=(path,), diagnostics=path_diag))
                set_syntax_repair_state(state.syntax_repair_required, path, {
                    "error": path_diag,
                    "failed_repairs": 0,
                })

    state.syntax_validation_required.clear()

    imported_paths = []
    if compiled_paths:
        import_ok, import_diag = run_focused_import_check(
            Path(workspace_root),
            compiled_paths,
        )
        if import_ok:
            imported_paths = compiled_paths
        else:
            emit_auto_import_result(
                paths=compiled_paths,
                diagnostics=import_diag,
                on_event=on_event,
                workspace_root=workspace_root,
            )
            for path in compiled_paths:
                path_ok, path_diag = run_focused_import_check(
                    Path(workspace_root),
                    [path],
                )
                if path_ok:
                    imported_paths.append(path)
                else:
                    findings.append(_ValidationFinding(rung="import", paths=(path,), diagnostics=path_diag))
                    state.import_verification_required.add(path)

    for path in imported_paths:
        state.import_verification_required.discard(path)

    if imported_paths:
        fp_dep = fingerprint_paths(set(imported_paths), workspace_root)
        try:
            if fp_dep and fp_dep == state.last_dependent_ok_fingerprint:
                pass
            else:
                deps = compute_dependents(Path(workspace_root), imported_paths)
                deps = deps[:15]
                if deps:
                    gating_paths, gating_diag, info_diag = run_dependent_import_check(
                        Path(workspace_root),
                        imported_paths,
                        deps,
                    )
                    if info_diag:
                        emit_auto_dependent_import_info(
                            paths=deps,
                            diagnostics=info_diag,
                            on_event=on_event,
                            workspace_root=workspace_root,
                        )
                    if gating_paths:
                        emit_auto_import_result(
                            paths=gating_paths,
                            diagnostics=gating_diag,
                            on_event=on_event,
                            workspace_root=workspace_root,
                        )
                        for path in imported_paths:
                            path_deps = compute_dependents(Path(workspace_root), [path])[:15]
                            if path_deps:
                                path_gating, path_gating_diag, _ = run_dependent_import_check(
                                    Path(workspace_root),
                                    [path],
                                    path_deps,
                                )
                                if path_gating:
                                    findings.append(_ValidationFinding(
                                        rung="dependent_import",
                                        paths=(path,),
                                        diagnostics=path_gating_diag,
                                        dependent_paths=tuple(path_gating)
                                    ))
                                    state.import_verification_required.add(path)
                    else:
                        if fp_dep:
                            state.last_dependent_ok_fingerprint = fp_dep
        except Exception:
            logging.getLogger(__name__).warning(
                "Dependent import check failed non-fatally",
                exc_info=True,
            )

    if not findings:
        if fp_struct:
            state.last_structural_ok_fingerprint = fp_struct
        if state.worker_flow is not None:
            state.worker_flow.mark_validation_satisfied()
        return "none"

    if len(findings) == 1:
        f = findings[0]
        if f.rung == "py_compile":
            instruction = WORKER_AUTO_PY_COMPILE_INSTRUCTION.format(diagnostics=f.diagnostics)
        elif f.rung == "import":
            instruction = WORKER_IMPORT_FAILURE_INSTRUCTION.format(diagnostics=f.diagnostics)
        elif f.rung == "dependent_import":
            instruction = WORKER_DEPENDENT_CONTRACT_INSTRUCTION.format(
                edited_files=", ".join(f.paths),
                dependent_files=", ".join(f.dependent_paths),
                diagnostics=f.diagnostics,
            )
        history.append_user_text(instruction)
        state.discard_worker_candidate_final()
        return "continue"

    sections = []
    for f in findings:
        title = {
            "py_compile": "Syntax Error",
            "import": "Import Error",
            "dependent_import": "Dependent Import Error",
        }.get(f.rung, f.rung)
        
        path_str = ", ".join(f.paths)
        if f.rung == "dependent_import" and f.dependent_paths:
            path_str += f" (broke {', '.join(f.dependent_paths)})"
            
        sections.append(f"### {title}\nPaths: {path_str}\n\n{f.diagnostics.strip()}")
        
    instruction = WORKER_BATCHED_VALIDATION_INSTRUCTION.format(
        num_problems=len(findings),
        findings_sections="\n\n".join(sections),
    )
    history.append_user_text(instruction)
    state.discard_worker_candidate_final()
    return "continue"

def _record_explicit_validation_runs(
    state: _SendState,
    val_result: object,
    write_snapshot: int,
) -> None:
    """Record each ``ValidationRunResult`` from explicit validation into the
    state ledger, using source ``"final_explicit"``."""
    for run in (getattr(val_result, "runs", None) or []):
        raw_output = str(getattr(run, "output", None) or "")
        payload = {
            "command": getattr(run, "command", ""),
            "exit_code": getattr(run, "exit_code", None),
            "output": raw_output,
            "output_preview": raw_output[:500],
            "validation_classification": getattr(run, "classification", ""),
            "classification": getattr(run, "classification", ""),
            "counts_as_product_failure": getattr(run, "counts_as_product_failure", False),
            "counts_as_validation": True,
        }
        state.validation_ledger.observe_tool_payload(
            payload, write_snapshot, source="final_explicit"
        )


def _run_behavioral_tier(
    *,
    state: _SendState,
    history: History,
    workspace_root,
    on_event: EventCallback,
    finish_worker_recoverable_followup: Callable[..., None],
    declared_run_command: str | None,
    explicit_validation_commands: list[ValidationCommandSpec] | None,
) -> WorkerFinalizationAction:
    findings: list[_ValidationFinding] = []
    
    if declared_run_command:
        fp = fingerprint_paths(state.worker_app_writes, workspace_root)
        try:
            if fp and fp == state.last_launch_ok_fingerprint:
                emit_auto_launch_result(
                    command=declared_run_command,
                    ok=True,
                    output="(skipped: no app-source change since last successful launch)",
                    on_event=on_event,
                    workspace_root=workspace_root,
                )
            else:
                from aura.sandbox import SandboxExecutor
                sandbox = SandboxExecutor(
                    mode="host",
                    workspace_root=Path(workspace_root),
                )
                watch = sandbox.run_and_watch(
                    declared_run_command,
                    window_seconds=10,
                )
                if not (watch.ok and watch.exited_early):
                    emit_auto_launch_result(
                        command=declared_run_command,
                        ok=False,
                        output=watch.output,
                        on_event=on_event,
                        workspace_root=workspace_root,
                    )
                    findings.append(_ValidationFinding(
                        rung="launch",
                        paths=(),
                        diagnostics=watch.output,
                        command=declared_run_command
                    ))
                else:
                    if fp:
                        state.last_launch_ok_fingerprint = fp
                    emit_auto_launch_result(
                        command=declared_run_command,
                        ok=True,
                        output=watch.output,
                        on_event=on_event,
                        workspace_root=workspace_root,
                    )
        except Exception:
            logging.getLogger(__name__).warning(
                "Launch verification failed non-fatally",
                exc_info=True,
            )

    if explicit_validation_commands:
        current_snapshot = state.applied_write_count()

        # ── Ledger skip: avoid duplicate rerun when all explicit
        #    commands already have fresh passed proof. ──────────────
        cmd_strings = [
            vc.command if isinstance(vc, ValidationCommandSpec) else str(vc)
            for vc in explicit_validation_commands
        ]
        if state.validation_ledger.has_fresh_passed_commands(
            cmd_strings, current_snapshot,
        ):
            state.explicit_validation_fingerprints.clear()
            state.explicit_validation_edit_snapshot = current_snapshot
            state.mark_explicit_validation_passed()
            if state.worker_flow is not None:
                state.worker_flow.mark_validation_satisfied()
        else:
            val_result = run_explicit_validation_commands(
                workspace_root=Path(workspace_root),
                commands=explicit_validation_commands,
            )

            # Record results into the ledger for future skip checks.
            _record_explicit_validation_runs(state, val_result, current_snapshot)

            validation_runs = getattr(val_result, "runs", None)
            if validation_runs:
                emit_explicit_validation_runs(
                    runs=validation_runs,
                    on_event=on_event,
                    workspace_root=workspace_root,
                )
            if not val_result.ok:
                if not validation_runs:
                    emit_explicit_validation_result(
                        command=val_result.command,
                        ok=False,
                        output=val_result.diagnostics,
                        on_event=on_event,
                        workspace_root=workspace_root,
                    )

                edits_since_last_pass = (
                    state.write_attempt_count()
                    > state.explicit_validation_edit_snapshot
                )
                state.explicit_validation_edit_snapshot = state.write_attempt_count()
                verdict = route_validation_failure(
                    val_result=val_result,
                    fingerprint_memory=state.explicit_validation_fingerprints,
                    edits_since_last_pass=edits_since_last_pass,
                )
                if verdict.action == "handback":
                    finish_worker_recoverable_followup(
                        on_event,
                        **verdict.handback_details,
                    )
                    return "finished"

                # fix_command or repair
                history.append_user_text(verdict.instruction)
                state.discard_worker_candidate_final()
                return "continue"
            else:
                state.explicit_validation_fingerprints.clear()
                state.explicit_validation_edit_snapshot = current_snapshot
                state.mark_explicit_validation_passed()
                if state.worker_flow is not None:
                    state.worker_flow.mark_validation_satisfied()

    if not findings:
        return "none"

    if len(findings) == 1:
        f = findings[0]
        if f.rung == "launch":
            instruction = WORKER_LAUNCH_FAILURE_INSTRUCTION.format(
                command=f.command,
                output=f.diagnostics,
            )
        # explicit_validation findings are no longer appended -- they are
        # handled directly by the router above.
        history.append_user_text(instruction)
        state.discard_worker_candidate_final()
        return "continue"

    sections = []
    for f in findings:
        title = {
            "launch": "Launch Failure",
        }.get(f.rung, f.rung)
        
        diag = f.diagnostics.strip()
        cmd = f"Command: {f.command}\n\n" if f.command else ""
        sections.append(f"### {title}\n{cmd}{diag}")
        
    instruction = WORKER_BATCHED_VALIDATION_INSTRUCTION.format(
        num_problems=len(findings),
        findings_sections="\n\n".join(sections),
    )
    history.append_user_text(instruction)
    state.discard_worker_candidate_final()
    return "continue"

def _release_candidate_final(
    *,
    state: _SendState,
    history: History,
    on_event: EventCallback,
    finish_worker_recoverable_followup: Callable[..., None],
) -> WorkerFinalizationAction:
    if state.candidate_final_message is not None:
        if worker_final_report_missing_proof(
            state,
            state.candidate_final_message,
            ignore_prior_nudge=True,
        ):
            if state.worker_final_report_proof_nudge_sent:
                state.discard_worker_candidate_final()
                finish_worker_recoverable_followup(
                    on_event,
                    failure_class="worker_final_report_missing_proof",
                    error=(
                        "Worker changed files but did not provide validation or "
                        "acceptance proof after the final-report proof nudge."
                    ),
                    details={
                        "suggested_next_action": (
                            "Provide a final report with changed files, validation "
                            "command/result, and acceptance verification."
                        ),
                    },
                )
                return "finished"
            history.append_user_text(WORKER_FINAL_REPORT_PROOF_REQUIRED_TEXT)
            state.worker_final_report_proof_nudge_sent = True
            state.discard_worker_candidate_final()
            return "continue"
        history.append_assistant(state.candidate_final_message)
        state.candidate_final_message = None
    if state.stream_buffer is not None:
        state.stream_buffer.flush(on_event)
    return "finished"
