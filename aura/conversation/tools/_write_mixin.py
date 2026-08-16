"""Mixin providing write handler methods for ToolRegistry.

Expected on self:
    _root: Path  (workspace root)
    _read_only: bool
    _resolve_in_root(path: str) -> Path  (method on ToolRegistry)
    _refresh_code_intel_paths(paths) -> None  (method on ToolRegistry)

Functions are looked up through *registry* at call time so that
``unittest.mock.patch("aura.conversation.tools.registry.<name>")``
in test_tool_registry.py takes effect correctly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.conversation.path_utils import normalize_execution_path as _shared_normalize_execution_path

# Import the registry module so we can look up functions at call time.
# This creates a circular import, but Python handles it because
# `registry` is already in sys.modules by the time this module is loaded.
from aura.conversation.tools import registry as _reg
from aura.conversation.tools._types import ApprovalFileChange, ApprovalRequest, ToolExecResult
from aura.conversation.tools.fs_write import (
    _raw_sha256,
    stale_approval_reason,
)
from aura.conversation.tools.fs_write import (
    atomic_write_bytes as _atomic_write_bytes,
)
from aura.conversation.tools.write_payloads import _mark_delete_not_applied, _mark_not_applied
from aura.conversation.tools.write_transaction import (
    commit_patch_transaction,
    normalize_patch_args,
    propose_patch_transaction,
)
from aura.paths import safe_relative_to

PATCH_FILE_REPAIR_ACTION = (
    "Re-read the current file and inspect proposed_context. Treat joined Python statements "
    "or swallowed newlines as a likely patch boundary issue. Retry apply_patch with "
    "operation=patch and a larger enclosing block: the line before, the edited lines, and the "
    "line after. Use the current expected_file_hash. Keep existing-file recovery on "
    "operation=patch; do not use operation=replace as a fallback for this existing-file edit."
)

def _proposal_context(text: str, line: int | None, radius: int = 4) -> dict:
    lines = str(text).splitlines()
    error_line = line if isinstance(line, int) and line > 0 else None
    if not lines:
        return {
            "error_line": error_line,
            "start_line": 0,
            "end_line": 0,
            "lines": [],
        }

    context_line = min(error_line or 1, len(lines))
    radius = max(0, radius)
    start_line = max(1, context_line - radius)
    end_line = min(len(lines), context_line + radius)
    return {
        "error_line": error_line,
        "start_line": start_line,
        "end_line": end_line,
        "lines": [
            {"line": number, "text": lines[number - 1]}
            for number in range(start_line, end_line + 1)
        ],
    }

_AURA_DELETE_ALLOWED_PREFIXES = (
    ".aura/tmp/",
    ".aura/drones/",
    ".aura/drone-build/",
    ".aura/startup-smoke-profile/",
)

_AURA_DELETE_PROTECTED_PATHS = (
    ".aura",
    ".aura/backups",
    ".aura/browse_monitor_state.json",
    ".aura/config",
    ".aura/conversations",
    ".aura/hazards.db",
    ".aura/handoffs",
    ".aura/memory.db",
    ".aura/project.json",
    ".aura/project_blueprint.md",
    ".aura/secrets",
    ".aura/settings",
    ".aura/threads",
    ".aura/toolist.txt",
    ".aura/tokens",
    ".aura/tools",
)

_AURA_DELETE_PROTECTED_PREFIXES = (
    ".aura/backups/",
    ".aura/config/",
    ".aura/conversations/",
    ".aura/handoffs/",
    ".aura/secrets/",
    ".aura/settings/",
    ".aura/threads/",
    ".aura/tokens/",
    ".aura/tools/",
)


def _is_delete_protected_path(rel_path: str) -> bool:
    normalized = _normalize_execution_path(rel_path).lstrip("/")
    name = normalized.rsplit("/", 1)[-1]
    if normalized == ".git" or normalized.startswith(".git/"):
        return True
    if name == ".env" or name.startswith(".env."):
        return True
    if normalized in _AURA_DELETE_PROTECTED_PATHS:
        return True
    if normalized.startswith(_AURA_DELETE_ALLOWED_PREFIXES):
        return False
    return normalized.startswith(_AURA_DELETE_PROTECTED_PREFIXES)


def _python_syntax_error_payload(proposal: dict) -> dict | None:
    path = str(proposal.get("rel_path") or proposal.get("path") or "")
    if not path.endswith(".py"):
        return None
    proposed_content = str(proposal.get("new_content") or "")
    try:
        compile(proposed_content, path or "<proposal>", "exec")
    except SyntaxError as exc:
        syntax_line = exc.lineno if isinstance(exc.lineno, int) else None
        payload = {
            "ok": False,
            "path": path,
            "rel_path": path,
            "error": f"replacement produces invalid Python: {exc}",
            "failure_class": "syntax_invalid",
            "syntax_valid": False,
            "proposed_context": _proposal_context(proposed_content, syntax_line),
            "suggested_next_tool": "apply_patch",
            "suggested_next_action": PATCH_FILE_REPAIR_ACTION,
        }
        if syntax_line is not None:
            payload["syntax_error_line"] = syntax_line
        if isinstance(exc.offset, int):
            payload["syntax_error_offset"] = exc.offset
        if isinstance(exc.text, str):
            payload["syntax_error_text"] = exc.text.rstrip("\r\n")
        return _mark_not_applied(
            payload,
            "syntax_invalid",
        )
    return None


def _is_new_root_validation_scratch(root: Path, target: Path) -> bool:
    return (
        target.parent == root
        and not target.exists()
        and _is_scratch_python_name(target.name)
        and target.suffix == ".py"
    )



def _normalize_execution_path(path: str) -> str:
    return _shared_normalize_execution_path(path)


def _is_validation_scratch_path(path: str) -> bool:
    normalized = _normalize_execution_path(path)
    name = normalized.rsplit("/", 1)[-1]
    if not name.endswith(".py"):
        return False
    if normalized.startswith(".aura/tmp/") or "/" not in normalized:
        return _is_scratch_python_name(name)
    return False


def _is_aura_tmp_scratch_path(path: str) -> bool:
    normalized = _normalize_execution_path(path)
    return normalized.startswith(".aura/tmp/") and _is_validation_scratch_path(normalized)


def _is_scratch_python_name(name: str) -> bool:
    return name.startswith(
        (
            "dump",
            "_check",
            "check",
            "tmp",
            "_tmp",
            "_inspect",
            "inspect",
            "diagnostic",
            "_diagnostic",
        )
    )


_APPLY_PATCH_OPERATIONS = frozenset({"create", "replace", "patch", "delete"})


def _validate_apply_patch_shape(args: dict) -> dict | None:
    """Reject a field/operation mismatch before it reaches any write owner.

    Returns a focused correction payload naming the wrong field, or None when
    the shape matches the chosen operation.
    """
    operation = args.get("operation")
    if operation not in _APPLY_PATCH_OPERATIONS:
        return _mark_not_applied({
            "ok": False,
            "error": (
                "operation must be one of create, replace, patch, delete; "
                f"got {operation!r}"
            ),
            "failure_class": "invalid_arguments",
        })
    if operation in ("create", "replace"):
        if "edits" in args or "files" in args:
            return _mark_not_applied({
                "ok": False,
                "error": f"operation={operation!r} takes path+content, not edits/files",
                "failure_class": "invalid_arguments",
            })
        if not isinstance(args.get("content"), str):
            return _mark_not_applied({
                "ok": False,
                "error": f"operation={operation!r} requires a string content field",
                "failure_class": "invalid_arguments",
            })
    elif operation == "patch":
        if "content" in args:
            return _mark_not_applied({
                "ok": False,
                "error": "operation='patch' takes path+edits or files, not content",
                "failure_class": "invalid_arguments",
            })
    elif operation == "delete":
        if "content" in args or "edits" in args or "files" in args:
            return _mark_not_applied({
                "ok": False,
                "error": "operation='delete' takes only path (and optional reason)",
                "failure_class": "invalid_arguments",
            })
    return None


class WriteHandlersMixin:
    """Handlers for write tools — guards + approval + backup."""

    def _handle_apply_patch(self, args, approval_cb, reject_all) -> ToolExecResult:
        """Route the one model-facing mutation tool to its write owner.

        ``operation`` selects the existing write owner unchanged — ``create``
        and ``replace`` both reuse ``_handle_write_file`` (whose own
        ``propose_write`` already derives whether the target is new from the
        target itself, not from the caller's stated intent), ``patch`` reuses
        ``_handle_patch_file`` (single- or multi-file transaction, unchanged
        argument shape), and ``delete`` reuses ``_handle_delete_file``. No
        parallel write engine exists here.
        """
        shape_error = _validate_apply_patch_shape(args)
        if shape_error is not None:
            return ToolExecResult(ok=False, payload=shape_error)
        operation = args.get("operation")
        if operation in ("create", "replace"):
            return self._handle_write_file(args, approval_cb, reject_all)
        if operation == "patch":
            return self._handle_patch_file(args, approval_cb, reject_all)
        return self._handle_delete_file(args, approval_cb, reject_all)

    def _handle_write_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload=_mark_not_applied({"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled.", "failure_class": "read_only"}))
        return self._handle_write("write_file", args, approval_cb, reject_all)

    def _handle_delete_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload=_mark_delete_not_applied({"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled.", "failure_class": "read_only"}))
        return self._handle_delete(args, approval_cb, reject_all)

    def _handle_patch_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload=_mark_not_applied({"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled.", "failure_class": "read_only"}))
        if reject_all:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {"ok": False, "error": "User rejected all writes in this turn.", "failure_class": "approval_rejected"},
                    "approval_rejected",
                ),
                extras={"rejected_all": True},
            )

        specs, description, failure = normalize_patch_args(args)
        if failure is not None:
            return ToolExecResult(ok=False, payload=_mark_not_applied(failure))

        transaction, failure = propose_patch_transaction(
            self._root, self._resolve_in_root, specs, description
        )
        if failure is not None:
            return ToolExecResult(ok=False, payload=_mark_not_applied(failure))

        changes = tuple(
            ApprovalFileChange(f.rel_path, f.old_content, f.new_content, False)
            for f in transaction.files
        )
        primary = changes[0]
        req = ApprovalRequest(
            tool_name="patch_file",
            rel_path=primary.rel_path,
            old_content=primary.old_content,
            new_content=primary.new_content,
            is_new_file=False,
            changes=changes,
        )
        decision = approval_cb(req)

        if decision.action == "reject":
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {"ok": False, "error": "User rejected this change.", "path": req.rel_path, "failure_class": "approval_rejected"},
                    "approval_rejected",
                ),
                extras={
                    "approval": "reject",
                    "rel_path": req.rel_path,
                    "approval_metadata": decision.metadata,
                },
            )
        if decision.action == "reject_all":
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": "User rejected this change and all further writes in this turn.",
                    "path": req.rel_path,
                    "failure_class": "approval_rejected",
                    "applied": False,
                    "write_outcome": "not_applied_user_rejected",
                },
                extras={
                    "approval": "reject_all",
                    "rel_path": req.rel_path,
                    "approval_metadata": decision.metadata,
                },
            )

        result = commit_patch_transaction(
            self._root,
            transaction,
            capture_before_write=lambda rel_path: self._capture_before_write(self, rel_path),
            atomic_write_bytes=_atomic_write_bytes,
        )
        ok = bool(result.get("ok"))
        if ok:
            result.setdefault("applied_tool", "patch_file")
            self._refresh_code_intel_paths(
                entry.get("rel_path") or entry.get("path", "")
                for entry in result.get("files", [])
                if isinstance(entry, dict)
            )
        elif result.get("workspace_state") == "potentially_partial":
            self._refresh_code_intel_paths(result.get("attempted_written_files", []))
        elif result.get("rolled_back") is True:
            self._refresh_code_intel_paths(result.get("rolled_back_files", []))
        return ToolExecResult(
            ok=ok,
            payload=result if ok else _mark_not_applied(result),
            extras={
                "approval": "approve",
                "rel_path": req.rel_path,
                "approval_metadata": decision.metadata,
            },
        )

    def _handle_delete(
        self,
        args: dict,
        approval_cb,
        reject_all: bool,
    ) -> ToolExecResult:
        reason = args.get("reason", "")
        if reason is None:
            reason = ""
        if not isinstance(reason, str):
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "error": "reason must be a string",
                    "failure_class": "delete_file_invalid_path",
                    "reason": "",
                }, "delete_file_invalid_path"),
            )
        if reject_all:
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied(
                    {"ok": False, "error": "User rejected all writes in this turn.", "failure_class": "approval_rejected", "reason": reason},
                    "approval_rejected",
                ),
                extras={"rejected_all": True},
            )

        path_arg = args.get("path", "")
        if not isinstance(path_arg, str) or not path_arg.strip():
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "path": path_arg if isinstance(path_arg, str) else "",
                    "error": "path must be a non-empty string",
                    "failure_class": "delete_file_invalid_path",
                    "reason": reason,
                }, "delete_file_invalid_path"),
            )
        if any(char in path_arg for char in "*?[]"):
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "path": path_arg,
                    "error": "delete_file does not accept globs or wildcard paths",
                    "failure_class": "delete_file_invalid_path",
                    "reason": reason,
                }, "delete_file_invalid_path"),
            )
        try:
            target = self._resolve_in_root(path_arg)
        except ValueError as exc:
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "path": path_arg,
                    "error": str(exc),
                    "failure_class": "delete_file_workspace_escape",
                    "reason": reason,
                }, "delete_file_workspace_escape"),
            )

        rel_path = safe_relative_to(target, self._root).as_posix()
        if _is_delete_protected_path(rel_path):
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "path": rel_path,
                    "rel_path": rel_path,
                    "error": "delete_file cannot delete protected workspace metadata or environment files",
                    "failure_class": "delete_file_protected_path",
                    "reason": reason,
                }, "delete_file_protected_path"),
            )
        if not target.exists():
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "path": rel_path,
                    "rel_path": rel_path,
                    "error": "delete_file target does not exist",
                    "failure_class": "delete_file_missing",
                    "reason": reason,
                }, "delete_file_missing"),
            )
        if target.is_dir():
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "path": rel_path,
                    "rel_path": rel_path,
                    "error": "delete_file cannot delete directories",
                    "failure_class": "delete_file_is_directory",
                    "reason": reason,
                }, "delete_file_is_directory"),
            )
        if not target.is_file():
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "path": rel_path,
                    "rel_path": rel_path,
                    "error": "delete_file target must be a regular file",
                    "failure_class": "delete_file_invalid_path",
                    "reason": reason,
                }, "delete_file_invalid_path"),
            )

        old_content = target.read_text(encoding="utf-8", errors="replace")
        try:
            delete_snapshot = {
                "exists": True,
                "is_file": True,
                "raw_content_hash": _raw_sha256(target),
                "size": target.stat().st_size,
            }
        except OSError:
            delete_snapshot = None
        req = ApprovalRequest(
            tool_name="delete_file",
            rel_path=rel_path,
            old_content=old_content,
            new_content="",
            is_new_file=False,
        )
        decision = approval_cb(req)

        if decision.action == "reject":
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied(
                    {"ok": False, "error": "User rejected this deletion.", "path": rel_path, "rel_path": rel_path, "failure_class": "approval_rejected", "reason": reason},
                    "approval_rejected",
                ),
                extras={
                    "approval": "reject",
                    "rel_path": rel_path,
                    "approval_metadata": decision.metadata,
                },
            )
        if decision.action == "reject_all":
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied(
                    {
                        "ok": False,
                        "error": "User rejected this deletion and all further writes in this turn.",
                        "path": rel_path,
                        "rel_path": rel_path,
                        "failure_class": "approval_rejected",
                        "reason": reason,
                    },
                    "approval_rejected",
                ),
                extras={
                    "approval": "reject_all",
                    "rel_path": rel_path,
                    "approval_metadata": decision.metadata,
                },
            )

        # Verify the target the user approved still exists and is unchanged;
        # deleting different content than what was shown would be a surprise.
        stale_reason = stale_approval_reason(target, delete_snapshot)
        if stale_reason is not None:
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "path": rel_path,
                    "rel_path": rel_path,
                    "error": (
                        "The delete target changed after approval and before the "
                        f"deletion: {stale_reason}. Nothing was deleted."
                    ),
                    "failure_class": "stale_approval",
                    "stale_approval": True,
                    "write_outcome": "not_deleted_stale_approval",
                    "suggested_tool": "read_file",
                    "suggested_next_tool": "read_file",
                    "suggested_next_action": (
                        "Re-read the file and the surrounding directory, then "
                        "re-propose the deletion if it is still correct."
                    ),
                    "reason": reason,
                }, "stale_approval"),
                extras={
                    "approval": "approve",
                    "stale_approval": True,
                    "rel_path": rel_path,
                },
            )

        self._capture_before_write(self, rel_path)
        backup_path = _reg.backup_existing(self._root, target)
        target.unlink()
        self._refresh_code_intel_paths([rel_path])

        rel_backup = (
            safe_relative_to(backup_path, self._root).as_posix() if backup_path is not None else None
        )
        return ToolExecResult(
            ok=True,
            payload={
                "ok": True,
                "applied": True,
                "path": rel_path,
                "rel_path": rel_path,
                "deleted": True,
                "write_outcome": "deleted",
                "applied_tool": "delete_file",
                "is_new_file": False,
                "backup": rel_backup,
                "backup_path": rel_backup,
                "reason": reason,
            },
            extras={
                "approval": "approve",
                "rel_path": rel_path,
                "approval_metadata": decision.metadata,
            },
        )

    @staticmethod
    def _capture_before_write(
        instance: Any, rel_path: str,
    ) -> None:
        """Pre-write capture hook: call before a file mutation.

        If the owning ``ToolRegistry`` has a ``RestorePointManager`` with
        open sessions, this captures the current file state for each
        session.  No-op when no manager is set or no sessions are open.
        """
        mgr = getattr(instance, "_restore_point_manager", None)
        if mgr is not None:
            mgr.capture_path(rel_path)

    def _handle_write(
        self,
        name: str,
        args: dict,
        approval_cb,
        reject_all: bool,
    ) -> ToolExecResult:
        if reject_all:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {"ok": False, "error": "User rejected all writes in this turn.", "failure_class": "approval_rejected"},
                    "approval_rejected",
                ),
                extras={"rejected_all": True},
            )

        path_arg = args.get("path", "")
        target = self._resolve_in_root(path_arg)
        if name == "write_file":
            rel_path = safe_relative_to(target, self._root).as_posix()
            if _is_validation_scratch_path(rel_path) and not _is_aura_tmp_scratch_path(rel_path):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({
                        "ok": False,
                        "path": rel_path,
                        "rel_path": rel_path,
                        "error": (
                            "Validation scratch files should use shell "
                            "with python -c, or create and remove a temporary file "
                            "inside one terminal command."
                        ),
                        "failure_class": "validation_scratch_banned",
                        "suggested_next_tool": "shell",
                        "suggested_next_action": (
                            "Use python -c for scratch validation, or create and remove "
                            "a temporary file inside one terminal command."
                        ),
                    }),
                )
            if _is_new_root_validation_scratch(self._root, target):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({
                        "ok": False,
                        "path": rel_path,
                        "rel_path": rel_path,
                        "error": "Root-level _check*.py validation scratch files are not allowed.",
                        "failure_class": "validation_scratch_banned",
                        "suggested_next_tool": "shell",
                        "suggested_next_action": (
                            "Use python -c, an existing focused test, or .aura/tmp "
                            "with cleanup."
                        ),
                    }),
                )

        if name == "write_file":
            content = args.get("content", "")
            if not isinstance(content, str):
                return ToolExecResult(
                    ok=False, payload=_mark_not_applied({"ok": False, "error": "content must be a string", "failure_class": "internal_error"})
                )
            if _is_aura_tmp_scratch_path(rel_path):
                is_new_file = not target.exists()
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_bytes(target, content.encode("utf-8"))
                self._refresh_code_intel_paths([rel_path])
                return ToolExecResult(
                    ok=True,
                    payload={
                        "ok": True,
                        "path": rel_path,
                        "applied": True,
                        "applied_tool": "write_file",
                        "write_outcome": "diagnostic_scratch_applied",
                        "is_new_file": is_new_file,
                        "diagnostic_scratch": True,
                    },
                )
            proposal = _reg.propose_write(self._root, target, content)
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=_mark_not_applied(proposal))

            syntax_error = _python_syntax_error_payload(proposal)
            if syntax_error is not None:
                return ToolExecResult(ok=False, payload=syntax_error)

            req = ApprovalRequest(
                tool_name="write_file",
                rel_path=proposal["rel_path"],
                old_content=proposal["old_content"],
                new_content=proposal["new_content"],
                is_new_file=proposal.get("is_new_file", False),
            )
        else:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {"ok": False, "error": f"unknown write tool: {name}", "failure_class": "internal_error"}
                ),
            )

        decision = approval_cb(req)

        if decision.action == "reject":
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {"ok": False, "error": "User rejected this change.", "path": req.rel_path, "failure_class": "approval_rejected"},
                    "approval_rejected",
                ),
                extras={
                    "approval": "reject",
                    "rel_path": req.rel_path,
                    "approval_metadata": decision.metadata,
                },
            )
        if decision.action == "reject_all":
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": "User rejected this change and all further writes in this turn.",
                    "path": req.rel_path,
                    "failure_class": "approval_rejected",
                    "applied": False,
                    "write_outcome": "not_applied_user_rejected",
                },
                extras={
                    "approval": "reject_all",
                    "rel_path": req.rel_path,
                    "approval_metadata": decision.metadata,
                },
            )

        # Approve — but first verify the live target still matches the exact
        # snapshot the user approved.  If it changed while approval was
        # pending, applying the approved bytes would clobber different
        # content: apply nothing and ask the model to re-read and re-propose.
        stale_reason = stale_approval_reason(target, proposal.get("_target_snapshot"))
        if stale_reason is not None:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied({
                    "ok": False,
                    "path": req.rel_path,
                    "rel_path": req.rel_path,
                    "error": (
                        "The target changed after approval and before the write "
                        f"landed: {stale_reason}. Nothing was written."
                    ),
                    "failure_class": "stale_approval",
                    "stale_approval": True,
                    "recoverable": True,
                    "write_outcome": "not_applied_stale_approval",
                    "suggested_tool": "read_file",
                    "suggested_next_tool": "read_file",
                    "suggested_next_action": (
                        "Re-read the current file, then re-propose the change with "
                        "the current content and expected_file_hash."
                    ),
                }, "stale_approval"),
                extras={
                    "approval": "approve",
                    "stale_approval": True,
                    "rel_path": req.rel_path,
                },
            )

        # Capture pre-write state, back up, write new content.
        self._capture_before_write(self, req.rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        backup_path = _reg.backup_existing(self._root, target)
        _atomic_write_bytes(target, req.new_content.encode("utf-8"))
        self._refresh_code_intel_paths([req.rel_path])

        rel_backup = (
            safe_relative_to(backup_path, self._root).as_posix() if backup_path is not None else None
        )
        payload = {
            "ok": True,
            "path": req.rel_path,
            "applied": True,
            "applied_tool": name,
            "write_outcome": proposal.get("write_outcome") or "applied",
            "is_new_file": req.is_new_file,
            "backup": rel_backup,
        }
        if proposal.get("pre_existing_environment_issues"):
            payload["pre_existing_environment_issues"] = proposal.get("pre_existing_environment_issues")
        if proposal.get("checks_warned"):
            payload["checks_warned"] = proposal.get("checks_warned")
        return ToolExecResult(
            ok=True,
            payload=payload,
            extras={
                "approval": "approve",
                "rel_path": req.rel_path,
                "approval_metadata": decision.metadata,
            },
        )
