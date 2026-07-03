"""Built-in Worker lifecycle gates for first-party write safety."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from aura.conversation import _edit_shapes
from aura.conversation.edit_recovery_state import worker_file_state_for_path
from aura.conversation.path_utils import normalize_worker_path
from aura.conversation.worker_recovery_payload import recovery_payload
from aura.lifecycle.context import HookContext
from aura.lifecycle.decisions import GateDecision
from aura.lifecycle.matchers import HookMatcher
from aura.lifecycle.registry import LifecycleHooks

WRITE_READ_TOOLS = {
    "patch_file",
    "write_file",
    "delete_file",
    "edit_file",
    "edit_symbol",
    "edit_line_range",
    "apply_edit_transaction",
}


def register_builtin_worker_gates(
    lifecycle: LifecycleHooks,
) -> list[Callable[[], None]]:
    """Register first-party Worker pre-tool gate handlers."""
    matcher = HookMatcher("worker.pre_tool_use", role="worker")
    return [
        lifecycle.register_gate(
            matcher,
            worker_existing_file_read_gate,
            name="worker_existing_file_read_gate",
            source="builtin",
        ),
        lifecycle.register_gate(
            matcher,
            worker_patch_file_hash_gate,
            name="worker_patch_file_hash_gate",
            source="builtin",
        ),
    ]


def worker_existing_file_read_gate(ctx: HookContext) -> GateDecision:
    """Require read/context evidence before Worker mutates existing files."""
    if ctx.topic != "worker.pre_tool_use" or ctx.role != "worker":
        return GateDecision.allow()
    if ctx.tool_name not in WRITE_READ_TOOLS:
        return GateDecision.allow()

    args = _payload_args(ctx)
    raw_path = _edit_shapes.tool_path(ctx.tool_name, args)
    target = _resolve_worker_target(ctx.payload, raw_path)
    if target.block_reason:
        return _block(
            target.block_reason,
            {
                "blocked_payload": _workspace_escape_payload(
                    path=target.normalized_path or str(raw_path or ""),
                    tool_name=ctx.tool_name,
                )
            },
        )
    if not target.normalized_path or not target.resolved_path:
        return GateDecision.allow()
    if not target.resolved_path.is_file():
        return GateDecision.allow()
    if _has_read_or_context_evidence(ctx.payload, target.normalized_path):
        return GateDecision.allow()

    payload = recovery_payload(
        path=target.normalized_path,
        failure_class="worker_existing_file_not_read",
        error=(
            "Worker attempted to modify an existing file without current "
            "read or target-file context evidence."
        ),
        suggested_next_tool="read_file",
        suggested_next_action=(
            "Read the file, then retry the write with the current file contents "
            "in context."
        ),
    )
    payload["applied"] = False
    payload["write_outcome"] = "not_applied_edit_mechanics_blocked"
    return _block("worker_existing_file_not_read", {"blocked_payload": payload})


def worker_patch_file_hash_gate(ctx: HookContext) -> GateDecision:
    """Require patch_file expected hashes to match latest Worker read state."""
    if ctx.topic != "worker.pre_tool_use" or ctx.role != "worker":
        return GateDecision.allow()
    if ctx.tool_name != "patch_file":
        return GateDecision.allow()

    args = _payload_args(ctx)
    raw_path = _edit_shapes.tool_path(ctx.tool_name, args)
    target = _resolve_worker_target(ctx.payload, raw_path)
    if target.block_reason:
        return GateDecision.allow()
    if not target.normalized_path or not target.resolved_path:
        return GateDecision.allow()
    if not target.resolved_path.is_file():
        return GateDecision.allow()
    if not _has_read_or_context_evidence(ctx.payload, target.normalized_path):
        return GateDecision.allow()

    expected_hash = args.get("expected_file_hash")
    if not isinstance(expected_hash, str) or not expected_hash:
        payload = recovery_payload(
            path=target.normalized_path,
            failure_class="patch_file_missing_expected_hash",
            error=(
                "Worker patch_file on an existing file requires expected_file_hash "
                "from the latest successful read."
            ),
            suggested_next_tool="read_file",
            suggested_next_action=(
                "Read the file, then retry patch_file with expected_file_hash "
                "set to the returned content_hash."
            ),
        )
        payload["applied"] = False
        payload["write_outcome"] = "not_applied_edit_mechanics_blocked"
        return _block("patch_file_missing_expected_hash", {"blocked_payload": payload})

    worker_file_state = _payload_worker_file_state(ctx.payload)
    state = worker_file_state_for_path(worker_file_state, target.normalized_path)
    known_hash = str(state.get("content_hash") or "") if state else ""
    if not state or known_hash != expected_hash or not state.get("fresh_for_patch"):
        payload = recovery_payload(
            path=target.normalized_path,
            failure_class="patch_file_hash_mismatch",
            error=(
                "patch_file expected_file_hash does not match the Worker's "
                "latest successful read for this file."
            ),
            suggested_next_tool="read_file",
            suggested_next_action=(
                "Re-read the file with read_file or read_file_range, then retry "
                "patch_file once with expected_file_hash set to the new content_hash."
            ),
        )
        payload["applied"] = False
        payload["write_outcome"] = "not_applied_edit_mechanics_blocked"
        payload["recoverable"] = True
        payload["stale"] = True
        payload["expected_file_hash"] = expected_hash
        if known_hash:
            payload["latest_read_content_hash"] = known_hash
        if state and state.get("last_read_tool"):
            payload["last_read_tool"] = state.get("last_read_tool")
        return _block("patch_file_hash_mismatch", {"blocked_payload": payload})

    return GateDecision.allow()


class _ResolvedTarget:
    def __init__(
        self,
        *,
        normalized_path: str = "",
        resolved_path: Path | None = None,
        block_reason: str = "",
    ) -> None:
        self.normalized_path = normalized_path
        self.resolved_path = resolved_path
        self.block_reason = block_reason


def _payload_args(ctx: HookContext) -> dict[str, Any]:
    args = ctx.payload.get("args") if isinstance(ctx.payload, dict) else None
    return dict(args) if isinstance(args, dict) else {}


def _payload_worker_file_state(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    state = payload.get("worker_file_state") if isinstance(payload, dict) else None
    if not isinstance(state, dict):
        return {}
    return {
        str(path): dict(value)
        for path, value in state.items()
        if isinstance(value, dict)
    }


def _resolve_worker_target(
    payload: dict[str, Any] | None,
    raw_path: str,
) -> _ResolvedTarget:
    workspace_root = payload.get("workspace_root") if isinstance(payload, dict) else None
    if not workspace_root:
        return _ResolvedTarget(block_reason="worker_workspace_root_missing")

    path_text = str(raw_path or "").strip()
    if not path_text:
        return _ResolvedTarget()

    try:
        root = Path(str(workspace_root)).resolve()
        candidate = Path(path_text)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        relpath = resolved.relative_to(root).as_posix()
    except (OSError, ValueError):
        return _ResolvedTarget(
            normalized_path=normalize_worker_path(path_text),
            block_reason="worker_workspace_path_escape",
        )
    if not relpath:
        return _ResolvedTarget(block_reason="worker_workspace_path_escape")
    return _ResolvedTarget(
        normalized_path=normalize_worker_path(relpath),
        resolved_path=resolved,
    )


def _has_read_or_context_evidence(
    payload: dict[str, Any] | None,
    normalized_path: str,
) -> bool:
    worker_file_state = _payload_worker_file_state(payload)
    if worker_file_state_for_path(worker_file_state, normalized_path) is not None:
        return True
    loaded_target_files = payload.get("loaded_target_files") if isinstance(payload, dict) else None
    return _path_in_list(normalized_path, loaded_target_files)


def _path_in_list(path: str, values: Any) -> bool:
    if not isinstance(values, list):
        return False
    normalized = normalize_worker_path(path)
    for value in values:
        if normalize_worker_path(str(value)) == normalized:
            return True
    return False


def _workspace_escape_payload(*, path: str, tool_name: str) -> dict[str, Any]:
    payload = recovery_payload(
        path=path,
        failure_class="worker_workspace_path_escape",
        error="Worker write target is outside the workspace.",
        suggested_next_tool="none",
        suggested_next_action="Choose a path inside the workspace before retrying.",
        recoverable=False,
    )
    payload["tool"] = tool_name
    payload["applied"] = False
    payload["write_outcome"] = "not_applied_path_blocked"
    return payload


def _block(reason: str, metadata: dict[str, Any]) -> GateDecision:
    return GateDecision(
        allowed=False,
        blocked=True,
        reason=reason,
        severity="error",
        metadata=metadata,
    )


__all__ = [
    "WRITE_READ_TOOLS",
    "register_builtin_worker_gates",
    "worker_existing_file_read_gate",
    "worker_patch_file_hash_gate",
]
