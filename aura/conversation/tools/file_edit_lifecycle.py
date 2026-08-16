"""File-edit lifecycle tracking: proposed -> awaiting_approval -> applied|rejected|failed.

Wraps the approval callback for one write-tool task so the existing write
pipeline (``_write_mixin.py`` / ``write_transaction.py`` / ``fs_write.py``)
stays the sole write authority. This module only observes the proposal an
approval request already carries and the final ``ToolExecResult`` the write
owner already returned; it never writes, approves, or rejects anything
itself.

One ``FileEditLifecycleTracker`` is created per write-tool task in
``ToolRoundRunner._process_task``, so concurrent tasks never share mutable
correlation state -- each task owns its own tracker instance and its own
wrapped approval callback closure, unlike the single shared
``_last_event``/``consume_last_event`` correlation this replaces for the
editor-projection path.
"""
from __future__ import annotations

from typing import Callable

from aura.client import FileEditChange, FileEditLifecycle
from aura.conversation.path_utils import normalize_execution_path
from aura.conversation.tools._types import (
    ApprovalCallback,
    ApprovalDecision,
    ApprovalRequest,
    ToolExecResult,
)

FILE_EDIT_LIFECYCLE_TOOLS = frozenset({"write_file", "patch_file", "delete_file"})

LifecycleEventCallback = Callable[[FileEditLifecycle], None]

_REJECTED_ACTIONS = frozenset({"reject", "reject_all"})


def _action_for(tool_name: str, is_new_file: bool) -> str:
    if tool_name == "delete_file":
        return "delete"
    return "create" if is_new_file else "modify"


class FileEditLifecycleTracker:
    """Tracks one write-tool task's proposal through to its final outcome."""

    def __init__(
        self, *, tool_call_id: str, tool_name: str, on_event: LifecycleEventCallback
    ) -> None:
        self._tool_call_id = tool_call_id
        self._tool_name = tool_name
        self._on_event = on_event
        self._proposed_changes: tuple[FileEditChange, ...] = ()
        self._terminal_emitted = False

    def wrap_approval_cb(self, approval_cb: ApprovalCallback) -> ApprovalCallback:
        """Return an approval callback that also emits proposed/awaiting/rejected.

        The wrapped callback still delegates the actual decision to
        *approval_cb* -- the real GUI/CLI approval mechanism -- unchanged.
        """

        def _wrapped(req: ApprovalRequest) -> ApprovalDecision:
            changes = tuple(
                FileEditChange(
                    change_id=f"{self._tool_call_id}:{index}",
                    path=normalize_execution_path(change.rel_path),
                    action=_action_for(self._tool_name, change.is_new_file),
                    old_content=change.old_content,
                    new_content=change.new_content,
                )
                for index, change in enumerate(req.file_changes)
            )
            self._proposed_changes = changes
            self._emit("proposed", changes)
            self._emit("awaiting_approval", changes)

            decision = approval_cb(req)

            if decision.action in _REJECTED_ACTIONS:
                self._terminal_emitted = True
                self._emit("rejected", changes, reason=decision.action)
            return decision

        return _wrapped

    def emit_outcome(self, exec_result: ToolExecResult) -> None:
        """Emit the final applied/failed phase from the write owner's own result.

        No-op when the proposal was already resolved (rejected above) or
        never formed (approval_cb was never reached -- e.g. read-only mode or
        a pre-approval validation failure blocked the call before any
        proposal existed).
        """
        if self._terminal_emitted or not self._proposed_changes:
            return
        self._terminal_emitted = True
        payload = exec_result.payload if isinstance(exec_result.payload, dict) else {}
        if payload.get("applied") is True:
            self._emit("applied", self._resolve_applied_changes(payload))
        else:
            reason = str(
                payload.get("failure_class")
                or payload.get("write_outcome")
                or payload.get("error")
                or "not_applied"
            )
            self._emit("failed", self._proposed_changes, reason=reason)

    def emit_outcome_failed(self, reason: str) -> None:
        """Emit "failed" when the write owner raised instead of returning a result."""
        if self._terminal_emitted or not self._proposed_changes:
            return
        self._terminal_emitted = True
        self._emit("failed", self._proposed_changes, reason=reason)

    def _resolve_applied_changes(self, payload: dict) -> tuple[FileEditChange, ...]:
        """Re-key the proposed changes against the write owner's own applied paths.

        A multi-file ``patch_file`` transaction reports its applied files
        under ``files``; single ``write_file``/``delete_file`` calls report
        one ``rel_path``/``path``. Only paths the write owner itself confirms
        are ever marked applied.
        """
        applied_paths: list[str] = []
        files = payload.get("files")
        if isinstance(files, list):
            for entry in files:
                if isinstance(entry, dict):
                    candidate = entry.get("rel_path") or entry.get("path")
                    if isinstance(candidate, str) and candidate:
                        applied_paths.append(normalize_execution_path(candidate))
        else:
            candidate = payload.get("rel_path") or payload.get("path")
            if isinstance(candidate, str) and candidate:
                applied_paths.append(normalize_execution_path(candidate))

        if not applied_paths:
            return self._proposed_changes
        applied_set = set(applied_paths)
        matched = tuple(c for c in self._proposed_changes if c.path in applied_set)
        return matched or self._proposed_changes

    def _emit(
        self, phase: str, changes: tuple[FileEditChange, ...], reason: str = ""
    ) -> None:
        self._on_event(
            FileEditLifecycle(
                tool_call_id=self._tool_call_id,
                tool_name=self._tool_name,
                phase=phase,
                changes=changes,
                reason=reason,
            )
        )


__all__ = ["FILE_EDIT_LIFECYCLE_TOOLS", "FileEditLifecycleTracker"]
