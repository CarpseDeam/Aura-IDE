"""Pure helper for patch_file state-based recovery blocking.

Extracted from ``ConversationManager._worker_patch_file_state_block`` so the
policy can be unit-tested without instantiating a full manager.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.conversation.edit_recovery_state import (
    worker_file_state_for_path,
    worker_path_is_existing_file,
)
from aura.conversation.worker_recovery_payload import (
    blocked_tool_result,
    recovery_payload,
)


def patch_file_state_block(
    *,
    tool_call_id: str,
    name: str,
    path: str,
    args: dict[str, Any],
    worker_file_state: dict[str, dict[str, Any]],
    workspace_root: Path,
) -> dict[str, Any] | None:
    """Return a blocked-tool payload or ``None`` if the patch_file is allowed.

    Returns ``None`` when:
    - The target path is **not** an existing worker file.
    - The *expected_file_hash* matches a fresh worker-file read state.

    Returns a blocked-tool payload with ``patch_file_missing_expected_hash``
    when the file exists but no ``expected_file_hash`` was provided.

    Returns a blocked-tool payload with ``patch_file_hash_mismatch`` when
    the provided hash does not match the latest successful read hash, or
    when the read state is not fresh for patch.
    """
    # ------------------------------------------------------------------
    # 1.  Refuse patch_file on non-existing files (creating files does
    #     not need expected_file_hash).
    # ------------------------------------------------------------------
    if not worker_path_is_existing_file(workspace_root, path):
        return None

    # ------------------------------------------------------------------
    # 2.  Existing file without a hash  →  block.
    # ------------------------------------------------------------------
    expected_hash = args.get("expected_file_hash")
    if not isinstance(expected_hash, str) or not expected_hash:
        payload = recovery_payload(
            path=path,
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
        return blocked_tool_result(tool_call_id, name, payload)

    # ------------------------------------------------------------------
    # 3.  Existing file with stale / mismatched hash  →  block.
    # ------------------------------------------------------------------
    state = worker_file_state_for_path(worker_file_state, path)
    known_hash = str(state.get("content_hash") or "") if state else ""
    if not state or known_hash != expected_hash or not state.get("fresh_for_patch"):
        payload = recovery_payload(
            path=path,
            failure_class="patch_file_hash_mismatch",
            error=(
                "patch_file expected_file_hash does not match the Worker's "
                "latest successful read for this file."
            ),
            suggested_next_tool="read_file",
            suggested_next_action=(
                "Re-read the file with read_file or read_file_range, then retry patch_file once "
                "with expected_file_hash set to the new content_hash."
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
        return blocked_tool_result(tool_call_id, name, payload)

    # ------------------------------------------------------------------
    # 4.  Hash matches and file is fresh  →  allow.
    # ------------------------------------------------------------------
    return None
