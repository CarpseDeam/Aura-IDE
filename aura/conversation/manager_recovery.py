"""Worker recovery logic extracted from ConversationManager.

Module-level functions for worker tool recovery: blocking invalid edit shapes,
handling patch syntax failures, managing file state, and updating recovery state.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aura.conversation import _edit_shapes
from aura.conversation._recovery_tool_policy import (
    WORKER_RECOVERY_ALWAYS_ALLOWED,
    syntax_repair_tool_allowed,
)
from aura.conversation.edit_orchestrator import (
    EditRetryLedger,
    load_file_edit_profile,
    strategy_decision_for_attempt,
)
from aura.conversation.path_utils import (
    is_validation_scratch_path as _is_validation_scratch_path,
)
from aura.conversation.path_utils import (
    normalize_worker_path as _normalize_worker_path,
)
from aura.conversation.syntax_repair_state import (
    discard_syntax_validation_path,
    has_terminal_syntax_failure,
    pop_syntax_repair_state,
    set_syntax_repair_state,
    syntax_repair_paths,
    syntax_repair_state_for_path,
)
from aura.conversation.syntax_terminal_state import update_syntax_state_from_terminal
from aura.conversation.terminal_syntax import is_python_path
from aura.conversation.tool_limits import WRITE_TOOLS
from aura.conversation.worker_patch_state_policy import patch_file_state_block
from aura.conversation.worker_recovery_messages import (
    PATCH_CANDIDATE_INVALID_SYNTAX_ACTION,
)
from aura.conversation.worker_recovery_payload import (
    blocked_tool_result,
    is_recoverable_phase_boundary,
    parse_tool_payload,
    record_recovery_block,
    recovery_payload,
)
from aura.conversation.worker_recovery_state import (
    clear_patch_failed_shapes_for_path,
    normalized_state_value,
    pop_normalized_key,
    pop_normalized_recovery_key,
    record_reads_for_recovery,
)

EDIT_MECHANICS_FAILURE_CLASSES = {
    "edit_mechanics_symbol_not_found",
    "edit_mechanics_old_str_not_found",
    "edit_mechanics_ambiguous_match",
    "patch_hunk_not_found",
    "patch_hunk_ambiguous",
    "patch_file_hash_mismatch",
}

EDIT_TRANSACTION_FAILURE_CLASSES = {
    "edit_transaction_hash_mismatch",
    "edit_transaction_symbol_not_found",
    "edit_transaction_ambiguous_symbol",
    "edit_transaction_invalid_operation",
    "edit_transaction_invalid_syntax",
    "edit_transaction_not_applicable",
}

PATCH_CANDIDATE_INVALID_SYNTAX_FAILURE_CLASS = "patch_candidate_invalid_syntax"
PATCH_CANDIDATE_INVALID_SYNTAX_REPEATED_CLASS = "patch_candidate_invalid_syntax_repeated"


def _is_worker_app_source_path(path: str) -> bool:
    normalized = _normalize_worker_path(path).replace("\\", "/")
    if not is_python_path(normalized) or _is_validation_scratch_path(normalized):
        return False
    parts = normalized.split("/")
    if "tests" in parts:
        return False
    name = parts[-1]
    return not (
        (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
    )


def worker_recovery_block(
    workspace_root: Path | None,
    *,
    tool_call_id: str,
    name: str,
    args: dict[str, Any],
    edit_failed_shapes: set[str],
    edit_fallback_required: dict[str, dict[str, Any]],
    recovery_block_counts: dict[str, int],
    line_range_reread_required: dict[str, dict[str, Any]],
    syntax_repair_required: dict[str, dict[str, Any]],
    syntax_validation_required: set[str],
    write_attempts_by_path: dict[str, int],
    worker_file_state: dict[str, dict[str, Any]] | None = None,
    patch_failed_cycles: dict[str, int] | None = None,
    patch_invalid_syntax_required: dict[str, dict[str, Any]] | None = None,
    edit_retry_ledger: EditRetryLedger | None = None,
) -> dict[str, Any] | None:
    raw_path = _edit_shapes.tool_path(name, args)
    path = _normalize_worker_path(raw_path) if raw_path else ""
    worker_file_state = worker_file_state if worker_file_state is not None else {}
    patch_failed_cycles = patch_failed_cycles if patch_failed_cycles is not None else {}
    patch_invalid_syntax_required = (
        patch_invalid_syntax_required
        if patch_invalid_syntax_required is not None
        else {}
    )
    edit_retry_ledger = edit_retry_ledger if edit_retry_ledger is not None else EditRetryLedger()
    invalid_syntax_block = worker_patch_invalid_syntax_block(
        tool_call_id=tool_call_id,
        name=name,
        args=args,
        patch_invalid_syntax_required=patch_invalid_syntax_required,
        recovery_block_counts=recovery_block_counts,
    )
    if invalid_syntax_block is not None:
        return invalid_syntax_block
    if name in WORKER_RECOVERY_ALWAYS_ALLOWED:
        return None
    syntax_paths = syntax_repair_paths(syntax_repair_required)
    if syntax_paths and not syntax_repair_tool_allowed(name, args, syntax_paths):
        target = sorted(syntax_paths)[0]
        state = syntax_repair_state_for_path(syntax_repair_required, target)
        repair_failed = bool(state.get("repair_failed"))
        error_msg = (
            f"Python syntax is invalid in {target}. "
            + (
                "Syntax still fails after one repair attempt."
                if repair_failed
                else "Repair that file and pass py_compile before any unrelated tool call."
            )
        )
        diagnostic = state.get("error", "")
        if diagnostic:
            error_msg += f"\n\nDiagnostic output:\n{diagnostic}"
        payload = recovery_payload(
            path=target,
            failure_class="syntax_invalid",
            error=error_msg,
            suggested_next_tool="patch_file",
            suggested_next_action=(
                "Re-read the file, inspect proposed_context if present, then submit one corrected "
                "patch_file transaction with the current expected_file_hash. Run py_compile after "
                "the patch is applied."
            ),
            recoverable=not repair_failed,
        )
        record_recovery_block(payload, f"syntax:{target}:{name}", recovery_block_counts)
        return blocked_tool_result(tool_call_id, name, payload)

    if path:
        profile = load_file_edit_profile(workspace_root, path)
        decision = strategy_decision_for_attempt(
            ledger=edit_retry_ledger,
            name=name,
            args=args,
            path=path,
            profile=profile,
        )
        if decision is not None:
            payload = recovery_payload(
                path=decision.path,
                failure_class=decision.failure_class,
                error=decision.error,
                suggested_next_tool=decision.suggested_next_tool,
                suggested_next_action=decision.suggested_next_action,
                recoverable=decision.recoverable,
            )
            payload["applied"] = False
            payload["write_outcome"] = "not_applied_edit_strategy_blocked"
            payload["edit_mode"] = (
                decision.attempted_mode.value
                if decision.attempted_mode is not None
                else ""
            )
            payload["next_edit_mode"] = (
                decision.next_mode.value if decision.next_mode is not None else "none"
            )
            payload["repair_context"] = decision.repair_context
            record_recovery_block(
                payload,
                f"edit-strategy:{decision.path}:{name}:{payload['next_edit_mode']}",
                recovery_block_counts,
            )
            return blocked_tool_result(tool_call_id, name, payload)

    if (
        path
        and name in {"edit_file", "edit_line_range"}
        and write_attempts_by_path.get(path, 0) > 3
    ):
        payload = recovery_payload(
            path=path,
            failure_class="edit_mechanics_multi_edit_spin",
            error=(
                "Multiple failed or unapplied write attempts targeted the same file. "
                "Use patch_file for this existing-file edit."
            ),
            suggested_next_tool="patch_file",
            suggested_next_action=(
                "Re-read the file, then submit one patch_file containing all intended hunks."
            ),
        )
        record_recovery_block(payload, f"multi-edit-spin:{path}:{name}", recovery_block_counts)
        return blocked_tool_result(tool_call_id, name, payload)

    if name == "apply_edit_transaction" and path:
        shape = _edit_shapes.edit_shape_signature(name, args)
        if shape in edit_failed_shapes:
            if f"ambiguous-replace-text:{shape}" in edit_failed_shapes:
                payload = recovery_payload(
                    path=path,
                    failure_class="edit_transaction_ambiguous_symbol",
                    error=(
                        "Repeated ambiguous replace_text_once transaction. "
                        "Do not retry the same exact text shape."
                    ),
                    suggested_next_tool="patch_file",
                    suggested_next_action=(
                        "Re-read the file, then retry patch_file with occurrence or allow_multiple."
                    ),
                    recoverable=False,
                )
                record_recovery_block(payload, shape, recovery_block_counts)
                return blocked_tool_result(tool_call_id, name, payload)
            payload = recovery_payload(
                path=path,
                failure_class="edit_mechanics_blocked",
                error="Repeated apply_edit_transaction failure. Re-read the file and return a concise blocker instead of switching between edit tools.",
                suggested_next_tool="read_file",
                suggested_next_action="Re-read the file, then report the typed transaction blocker if the structured operation still cannot be applied safely.",
                recoverable=False,
            )
            record_recovery_block(payload, shape, recovery_block_counts)
            return blocked_tool_result(tool_call_id, name, payload)

    if name == "edit_line_range" and path in line_range_reread_required:
        payload = recovery_payload(
            path=path,
            failure_class="edit_mechanics_stale_line_range",
            error="Stale line range after a failed edit_line_range. Re-read the file before retrying line-range editing.",
            suggested_next_tool="read_file",
            suggested_next_action="Re-read the file before retrying an edit.",
        )
        record_recovery_block(payload, f"line-range-reread:{path}", recovery_block_counts)
        return blocked_tool_result(tool_call_id, name, payload)

    if name == "patch_file" and path:
        blocked = worker_patch_file_state_block(
            tool_call_id=tool_call_id,
            name=name,
            path=path,
            args=args,
            worker_file_state=worker_file_state,
            workspace_root=workspace_root,
        )
        if blocked is not None:
            return blocked
        shape = _edit_shapes.edit_shape_signature(name, args)
        failed_cycles = patch_failed_cycles.get(shape, 0)
        if failed_cycles >= 1:
            payload = recovery_payload(
                path=path,
                failure_class="patch_file_repeated_failure",
                error=(
                    "Repeated patch shape failed. This exact patch_file shape is blocked; "
                    "a different hunk shape for the same file is still valid."
                ),
                suggested_next_tool="patch_file",
                suggested_next_action=(
                    "Change the patch shape by adding occurrence, using more surrounding context, "
                    "changing the old block, or using a fresh expected_file_hash from a new read."
                ),
                recoverable=False,
            )
            payload["applied"] = False
            payload["write_outcome"] = "not_applied_edit_mechanics_blocked"
            payload["patch_failed_cycles"] = failed_cycles
            payload["patch_shape"] = _edit_shapes.shape_digest(shape)
            record_recovery_block(payload, f"patch-shape:{shape}", recovery_block_counts)
            return blocked_tool_result(tool_call_id, name, payload)

    if name in ("edit_file", "edit_symbol") and path in edit_fallback_required:
        prior = edit_fallback_required[path]
        block_key = _edit_shapes.edit_shape_signature(name, args)
        payload = recovery_payload(
            path=path,
            failure_class=str(prior.get("failure_class") or "edit_mechanics_blocked"),
            error="Repeated failed edit tactic. Do not retry this edit shape. Re-read the file and use patch_file for existing-file code changes.",
            suggested_next_tool="patch_file",
            suggested_next_action="Use read_file/read_file_outline, then submit one patch_file with exact hunks.",
        )
        payload["previous_error"] = prior.get("error", "")
        record_recovery_block(payload, block_key, recovery_block_counts)
        return blocked_tool_result(tool_call_id, name, payload)

    if name in ("edit_file", "edit_symbol", "edit_line_range"):
        shape = _edit_shapes.edit_shape_signature(name, args)
        if shape in edit_failed_shapes:
            payload = recovery_payload(
                path=path,
                failure_class="edit_mechanics_blocked",
                error="Repeated failed edit tactic. Do not retry this edit shape. Re-read the file and use patch_file for existing-file code changes.",
                suggested_next_tool="patch_file",
                suggested_next_action="Use read_file/read_file_outline, then submit one patch_file with exact hunks.",
            )
            record_recovery_block(payload, shape, recovery_block_counts)
            return blocked_tool_result(tool_call_id, name, payload)

    return None


def worker_patch_invalid_syntax_block(
    *,
    tool_call_id: str,
    name: str,
    args: dict[str, Any],
    patch_invalid_syntax_required: dict[str, dict[str, Any]],
    recovery_block_counts: dict[str, int],
) -> dict[str, Any] | None:
    if not patch_invalid_syntax_required:
        return None

    target_path = _pending_patch_invalid_syntax_path(
        name,
        args,
        patch_invalid_syntax_required,
    )
    pending_path = target_path or sorted(patch_invalid_syntax_required)[0]
    state = patch_invalid_syntax_required.get(pending_path) or {}
    if state.get("retry_failed") or state.get("blocked"):
        payload = recovery_payload(
            path=pending_path,
            failure_class=PATCH_CANDIDATE_INVALID_SYNTAX_REPEATED_CLASS,
            error=(
                "patch_file candidate syntax recovery already used its one retry. "
                "Stop and return a concise blocker."
            ),
            suggested_next_tool="none",
            suggested_next_action="Stop and summarize the blocker; do not call more tools for this patch.",
            recoverable=False,
        )
        payload["applied"] = False
        payload["write_outcome"] = "not_applied_edit_mechanics_blocked"
        payload["patch_shape"] = state.get("patch_shape")
        record_recovery_block(
            payload,
            f"patch-invalid-syntax-blocked:{pending_path}",
            recovery_block_counts,
        )
        return blocked_tool_result(tool_call_id, name, payload)

    if target_path and name in {"read_file", "read_file_range", "read_files"}:
        return None

    if not state.get("reread_done"):
        payload = recovery_payload(
            path=pending_path,
            failure_class=PATCH_CANDIDATE_INVALID_SYNTAX_FAILURE_CLASS,
            error="patch_file candidate syntax recovery requires re-reading the suggested range before any other tool.",
            suggested_next_tool="read_file_range",
            suggested_next_action=PATCH_CANDIDATE_INVALID_SYNTAX_ACTION,
        )
        payload["applied"] = False
        payload["write_outcome"] = "not_applied_edit_mechanics_blocked"
        payload["patch_shape"] = state.get("patch_shape")
        record_recovery_block(
            payload,
            f"patch-invalid-syntax-reread:{pending_path}:{name}",
            recovery_block_counts,
        )
        return blocked_tool_result(tool_call_id, name, payload)

    if name == "patch_file" and target_path:
        return None

    payload = recovery_payload(
        path=pending_path,
        failure_class=PATCH_CANDIDATE_INVALID_SYNTAX_FAILURE_CLASS,
        error="Target area was re-read. Retry patch_file once now; do not use unrelated tools.",
        suggested_next_tool="patch_file",
        suggested_next_action=PATCH_CANDIDATE_INVALID_SYNTAX_ACTION,
    )
    payload["applied"] = False
    payload["write_outcome"] = "not_applied_edit_mechanics_blocked"
    payload["patch_shape"] = state.get("patch_shape")
    record_recovery_block(
        payload,
        f"patch-invalid-syntax-retry:{pending_path}:{name}",
        recovery_block_counts,
    )
    return blocked_tool_result(tool_call_id, name, payload)


def worker_patch_file_state_block(
    *,
    tool_call_id: str,
    name: str,
    path: str,
    args: dict[str, Any],
    worker_file_state: dict[str, dict[str, Any]],
    workspace_root: Path | None,
) -> dict[str, Any] | None:
    return patch_file_state_block(
        tool_call_id=tool_call_id,
        name=name,
        path=path,
        args=args,
        worker_file_state=worker_file_state,
        workspace_root=workspace_root,
    )


def update_worker_recovery_state(
    workspace_root: Path | None,
    *,
    name: str,
    args: dict[str, Any],
    ok: bool,
    content: str,
    edit_failed_shapes: set[str],
    edit_fallback_required: dict[str, dict[str, Any]],
    line_range_reread_required: dict[str, dict[str, Any]],
    syntax_repair_required: dict[str, dict[str, Any]],
    syntax_validation_required: set[str],
    write_attempts_by_path: dict[str, int],
    worker_app_writes: set[str] | None = None,
    worker_file_state: dict[str, dict[str, Any]] | None = None,
    patch_failed_cycles: dict[str, int] | None = None,
    patch_invalid_syntax_required: dict[str, dict[str, Any]] | None = None,
    edit_retry_ledger: EditRetryLedger | None = None,
) -> str:
    worker_app_writes = worker_app_writes if worker_app_writes is not None else set()
    worker_file_state = worker_file_state if worker_file_state is not None else {}
    patch_failed_cycles = patch_failed_cycles if patch_failed_cycles is not None else {}
    patch_invalid_syntax_required = (
        patch_invalid_syntax_required
        if patch_invalid_syntax_required is not None
        else {}
    )
    edit_retry_ledger = edit_retry_ledger if edit_retry_ledger is not None else EditRetryLedger()
    parsed = parse_tool_payload(content)
    record_reads_for_recovery(
        name,
        args,
        parsed,
        line_range_reread_required,
        edit_fallback_required,
        worker_file_state,
    )
    raw_path = _edit_shapes.tool_path(name, args, parsed)
    path = _normalize_worker_path(raw_path) if raw_path else ""
    _mark_patch_invalid_syntax_reread(
        name,
        args,
        parsed,
        patch_invalid_syntax_required,
    )
    if name in WRITE_TOOLS and path and (
        not ok
        or (
            isinstance(parsed, dict)
            and parsed.get("ok")
            and parsed.get("applied") is False
        )
    ):
        write_attempts_by_path[path] = write_attempts_by_path.get(path, 0) + 1

    if ok:
        if name in WRITE_TOOLS and path:
            is_deletion = (
                name == "delete_file"
                or (isinstance(parsed, dict) and parsed.get("deleted") is True)
            )
            if is_deletion:
                edit_retry_ledger.clear_path(path)
                pop_normalized_recovery_key(edit_fallback_required, path)
                pop_normalized_recovery_key(line_range_reread_required, path)
                pop_normalized_key(worker_file_state, path)
                clear_patch_failed_shapes_for_path(patch_failed_cycles, path, _edit_shapes.parse_patch_shape)
                pop_normalized_recovery_key(patch_invalid_syntax_required, path)
                pop_syntax_repair_state(syntax_repair_required, path)
                discard_syntax_validation_path(syntax_validation_required, path)
                if _is_worker_app_source_path(path):
                    worker_app_writes.discard(path)
                return content

            edit_retry_ledger.clear_path(path)
            pop_normalized_recovery_key(edit_fallback_required, path)
            pop_normalized_recovery_key(line_range_reread_required, path)
            pop_normalized_key(worker_file_state, path)
            clear_patch_failed_shapes_for_path(patch_failed_cycles, path, _edit_shapes.parse_patch_shape)
            pop_normalized_recovery_key(patch_invalid_syntax_required, path)
            if is_python_path(path) and not _is_validation_scratch_path(path):
                syntax_validation_required.add(path)
                if _is_worker_app_source_path(path):
                    worker_app_writes.add(path)
            state = syntax_repair_state_for_path(syntax_repair_required, path)
            if state:
                state["repair_attempted"] = True
                state["awaiting_validation"] = True
                set_syntax_repair_state(syntax_repair_required, path, state)
                if not _is_validation_scratch_path(path):
                    syntax_validation_required.add(path)
        return content

    if name in ("edit_file", "edit_symbol", "edit_line_range", "apply_edit_transaction"):
        edit_failed_shapes.add(_edit_shapes.edit_shape_signature(name, args))

    if not isinstance(parsed, dict):
        return content

    failure_class = str(parsed.get("failure_class", ""))
    shape = _edit_shapes.edit_shape_signature(name, args)
    if path and failure_class and failure_class not in {
        "approval_rejected",
        "internal_error",
        "path_error",
    }:
        profile = load_file_edit_profile(workspace_root, path)
        edit_mode = edit_retry_ledger.mode_for_tool_result(
            name=name,
            args=args,
            path=path,
            profile=profile,
        )
        if edit_mode is not None:
            edit_retry_ledger.record_failure(
                mode=edit_mode,
                path=path,
                failure_class=failure_class,
                shape=shape,
                error=str(parsed.get("error") or parsed.get("output") or ""),
            )
    prior_invalid_syntax = (
        normalized_state_value(patch_invalid_syntax_required, path)
        if path and name == "patch_file"
        else None
    )
    if (
        path
        and name == "patch_file"
        and isinstance(prior_invalid_syntax, dict)
        and prior_invalid_syntax.get("reread_done")
        and failure_class
    ):
        prior_invalid_syntax["retry_failed"] = True
        prior_invalid_syntax["blocked"] = True
        blocked_state = {
            **prior_invalid_syntax,
            "retry_failed": True,
            "blocked": True,
        }
        patch_invalid_syntax_required[_normalize_worker_path(path)] = blocked_state
        parsed.setdefault("applied", False)
        parsed.setdefault("write_outcome", "not_applied_edit_mechanics_blocked")
        parsed["recoverable"] = False
        parsed["failure_class"] = (
            PATCH_CANDIDATE_INVALID_SYNTAX_REPEATED_CLASS
            if failure_class == PATCH_CANDIDATE_INVALID_SYNTAX_FAILURE_CLASS
            else PATCH_CANDIDATE_INVALID_SYNTAX_FAILURE_CLASS
        )
        parsed["error"] = (
            "patch_file retry failed after candidate syntax recovery. "
            "Stop and return a concise blocker. "
            + str(parsed.get("error", ""))
        ).strip()
        parsed["suggested_next_tool"] = "none"
        parsed["suggested_next_action"] = (
            "Stop and summarize the blocker; do not call more tools for this patch."
        )
        parsed["patch_shape"] = prior_invalid_syntax.get("patch_shape")
        return json.dumps(parsed, ensure_ascii=False)

    if path and failure_class in EDIT_TRANSACTION_FAILURE_CLASSES:
        parsed.setdefault("applied", False)
        parsed.setdefault("write_outcome", "not_applied_edit_mechanics_blocked")
        parsed["recoverable"] = False
        parsed.pop("suggested_tool", None)
        parsed.pop("suggested_next_tool", None)
        if (
            failure_class == "edit_transaction_ambiguous_symbol"
            and _edit_shapes.has_replace_text_once_operation(args)
        ):
            edit_failed_shapes.add(f"ambiguous-replace-text:{shape}")
            parsed["suggested_next_action"] = (
                "Re-read the file, then retry patch_file with occurrence "
                "or allow_multiple."
            )
        else:
            parsed["suggested_next_action"] = (
                "Transaction could not be applied safely. Re-read the file "
                "and report this typed blocker if the operation is still not applicable."
            )
        content = json.dumps(parsed, ensure_ascii=False)
    elif path and name != "patch_file" and failure_class in EDIT_MECHANICS_FAILURE_CLASSES:
        parsed.setdefault("applied", False)
        parsed.setdefault("write_outcome", "not_applied_edit_mechanics_blocked")
        parsed.setdefault("tool", name)
        edit_fallback_required[path] = parsed
        parsed["recoverable"] = True
        parsed["suggested_next_tool"] = "patch_file"
        parsed["suggested_next_action"] = "Do not retry this low-level edit shape. Re-read the file and submit one patch_file for existing-file code changes."
        content = json.dumps(parsed, ensure_ascii=False)
    elif path and failure_class == "edit_mechanics_stale_line_range":
        parsed.setdefault("applied", False)
        parsed.setdefault("write_outcome", "not_applied_edit_mechanics_blocked")
        parsed.setdefault("tool", name)
        line_range_reread_required[path] = parsed
        parsed["recoverable"] = True
        parsed["suggested_next_tool"] = "read_file"
        parsed["suggested_next_action"] = "Re-read the file before retrying an edit."
        content = json.dumps(parsed, ensure_ascii=False)
    elif path and name == "patch_file" and failure_class == PATCH_CANDIDATE_INVALID_SYNTAX_FAILURE_CLASS:
        parsed.setdefault("applied", False)
        parsed.setdefault("write_outcome", "not_applied_edit_mechanics_blocked")
        parsed.setdefault("tool", name)
        patch_shape = _edit_shapes.edit_shape_signature(name, args)
        failed_cycles = patch_failed_cycles.get(patch_shape, 0) + 1
        patch_failed_cycles[patch_shape] = failed_cycles
        parsed["patch_failed_cycles"] = failed_cycles
        parsed["patch_shape"] = _edit_shapes.shape_digest(patch_shape)
        parsed["suggested_next_tool"] = "read_file_range"
        parsed["suggested_next_action"] = PATCH_CANDIDATE_INVALID_SYNTAX_ACTION
        if failed_cycles >= 2:
            parsed["recoverable"] = False
            parsed["failure_class"] = PATCH_CANDIDATE_INVALID_SYNTAX_REPEATED_CLASS
            parsed["error"] = (
                "Repeated patch_file candidate syntax failure for the same patch shape. "
                "Stop and return a concise blocker."
            )
            pop_normalized_recovery_key(patch_invalid_syntax_required, path)
        else:
            patch_invalid_syntax_required[_normalize_worker_path(path)] = {
                "failure_class": PATCH_CANDIDATE_INVALID_SYNTAX_FAILURE_CLASS,
                "error": parsed.get("error", ""),
                "shape": patch_shape,
                "patch_shape": parsed["patch_shape"],
                "reread_done": False,
                "retry_failed": False,
            }
            parsed["recoverable"] = True
        content = json.dumps(parsed, ensure_ascii=False)
    elif path and name == "patch_file" and failure_class in {"patch_hunk_not_found", "patch_hunk_ambiguous", "patch_file_hash_mismatch"}:
        parsed.setdefault("applied", False)
        parsed.setdefault("write_outcome", "not_applied_edit_mechanics_blocked")
        parsed.setdefault("tool", name)
        patch_shape = _edit_shapes.edit_shape_signature(name, args)
        failed_cycles = patch_failed_cycles.get(patch_shape, 0) + 1
        patch_failed_cycles[patch_shape] = failed_cycles
        parsed["patch_failed_cycles"] = failed_cycles
        parsed["patch_shape"] = _edit_shapes.shape_digest(patch_shape)
        parsed["stale"] = True
        parsed["suggested_next_tool"] = "read_file"
        if failure_class == "patch_file_hash_mismatch":
            parsed["suggested_next_action"] = (
                "Re-read the file with read_file or read_file_range, then retry patch_file once "
                "with expected_file_hash set to the returned content_hash."
            )
        else:
            parsed["suggested_next_action"] = (
                "Re-read the file, then retry patch_file once with current exact text "
                "and expected_file_hash set to the returned content_hash."
            )
        if failed_cycles >= 2:
            parsed["recoverable"] = False
            parsed["failure_class"] = "patch_file_repeated_failure"
            parsed["error"] = (
                "Repeated patch shape failed. This exact patch_file shape is blocked; "
                "a different hunk shape for the same file is still valid."
            )
            pop_normalized_recovery_key(edit_fallback_required, path)
        else:
            edit_fallback_required[path] = parsed
            parsed["recoverable"] = True
        content = json.dumps(parsed, ensure_ascii=False)
    elif path and failure_class == "syntax_invalid":
        parsed.setdefault("applied", False)
        parsed.setdefault("write_outcome", "not_applied_craft_rejected")
        state = syntax_repair_state_for_path(syntax_repair_required, path)
        if not state:
            state = {"failed_repairs": 0}
        set_syntax_repair_state(syntax_repair_required, path, state)
        state["awaiting_validation"] = False
        if name in WRITE_TOOLS:
            state["failed_repairs"] = int(state.get("failed_repairs", 0)) + 1
        parsed["suggested_next_tool"] = "patch_file"
        parsed["suggested_next_action"] = (
            "Re-read the file, inspect proposed_context if present, then submit one corrected "
            "patch_file transaction with the current expected_file_hash. Run py_compile after "
            "the patch is applied."
        )
        if int(state.get("failed_repairs", 0)) > 1:
            parsed["recoverable"] = False
            parsed["error"] = "Syntax repair failed after one repair attempt. " + str(parsed.get("error", ""))
        content = json.dumps(parsed, ensure_ascii=False)

    return content


def _pending_patch_invalid_syntax_path(
    name: str,
    args: dict[str, Any],
    pending: dict[str, dict[str, Any]],
) -> str:
    normalized_pending = {_normalize_worker_path(path): path for path in pending}
    if name == "read_files":
        paths = args.get("paths")
        if isinstance(paths, list):
            for raw in paths:
                normalized = _normalize_worker_path(str(raw))
                if normalized in normalized_pending:
                    return normalized_pending[normalized]
        return ""

    raw_path = _edit_shapes.tool_path(name, args)
    if not raw_path:
        return ""
    normalized = _normalize_worker_path(raw_path)
    return normalized_pending.get(normalized, "")


def _mark_patch_invalid_syntax_reread(
    name: str,
    args: dict[str, Any],
    parsed: Any,
    pending: dict[str, dict[str, Any]],
) -> None:
    if name not in {"read_file", "read_file_range", "read_files"}:
        return
    if not isinstance(parsed, dict):
        return

    def mark_one(path: str, result: dict[str, Any]) -> None:
        if result.get("ok") is not True:
            return
        if result.get("truncated") is True:
            return
        content_hash = result.get("content_hash")
        file_size = result.get("file_size")
        if not isinstance(content_hash, str) or not content_hash:
            return
        if not isinstance(file_size, int):
            return
        normalized = _normalize_worker_path(path)
        state = normalized_state_value(pending, normalized)
        if not isinstance(state, dict):
            return
        state["reread_done"] = True
        state["latest_read_content_hash"] = content_hash
        state["last_read_tool"] = name
        pending[normalized] = state

    if name == "read_files":
        files = parsed.get("files")
        if not isinstance(files, dict):
            return
        for path_key, result in files.items():
            if isinstance(result, dict):
                mark_one(str(result.get("path") or path_key), result)
        return

    path = _edit_shapes.tool_path(name, args, parsed)
    if path:
        mark_one(path, parsed)
