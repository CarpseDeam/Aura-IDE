"""Mixin providing write handler methods for ToolRegistry.

Expected on self:
    _root: Path  (workspace root)
    _read_only: bool
    _mode: RegistryMode
    _resolve_in_root(path: str) -> Path  (method on ToolRegistry)

Functions are looked up through *registry* at call time so that
``unittest.mock.patch("aura.conversation.tools.registry.<name>")``
in test_tool_registry.py takes effect correctly.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
from pathlib import Path

from aura.conversation.tools._types import ApprovalRequest, ToolExecResult
from aura.conversation.tools.write_payloads import _mark_not_applied, _mark_delete_not_applied
from aura.conversation.path_utils import normalize_worker_path as _shared_normalize_worker_path
from aura.paths import safe_relative_to

# Import the registry module so we can look up functions at call time.
# This creates a circular import, but Python handles it because
# `registry` is already in sys.modules by the time this module is loaded.

from aura.conversation.tools import registry as _reg

PATCH_FILE_REPAIR_ACTION = (
    "Re-read the current file and inspect proposed_context. Treat joined Python statements "
    "or swallowed newlines as a likely patch boundary issue. Retry patch_file with a larger "
    "enclosing block: the line before, the edited lines, and the line after. Use the current "
    "expected_file_hash. Keep existing-file recovery on patch_file; do not use write_file as "
    "a fallback for this existing-file edit."
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
    ".aura/planner.txt",
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
    normalized = _normalize_worker_path(rel_path).lstrip("/")
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
            "suggested_next_tool": "patch_file",
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



def _normalize_worker_path(path: str) -> str:
    return _shared_normalize_worker_path(path)


def _is_validation_scratch_path(path: str) -> bool:
    normalized = _normalize_worker_path(path)
    name = normalized.rsplit("/", 1)[-1]
    if not name.endswith(".py"):
        return False
    if normalized.startswith(".aura/tmp/") or "/" not in normalized:
        return _is_scratch_python_name(name)
    return False


def _is_aura_tmp_scratch_path(path: str) -> bool:
    normalized = _normalize_worker_path(path)
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


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    temp_path: Path | None = None
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(delete=False, dir=target.parent) as tmp:
            temp_path = Path(tmp.name)
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        if target.exists():
            os.chmod(temp_path, stat.S_IMODE(target.stat().st_mode))
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


class WriteHandlersMixin:
    """Handlers for write tools — guards + approval + backup."""

    def _handle_write_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload=_mark_not_applied({"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled.", "failure_class": "read_only"}))
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied({
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended edits in the 'spec' field of the dispatch."
                    ),
                    "failure_class": "internal_error",
                }),
            )
        return self._handle_write("write_file", args, approval_cb, reject_all)

    def _handle_delete_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload=_mark_delete_not_applied({"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled.", "failure_class": "read_only"}))
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended deletion in the 'spec' field of the dispatch."
                    ),
                    "failure_class": "internal_error",
                }),
            )
        return self._handle_delete(args, approval_cb, reject_all)

    def _handle_patch_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload=_mark_not_applied({"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled.", "failure_class": "read_only"}))
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied({
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended edits in the 'spec' field of the dispatch."
                    ),
                    "failure_class": "internal_error",
                }),
            )
        return self._handle_write("patch_file", args, approval_cb, reject_all)

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

        self._capture_before_write(self, rel_path)
        backup_path = _reg.backup_existing(self._root, target)
        target.unlink()

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
                            "Validation scratch files should use run_terminal_command "
                            "with python -c, or create and remove a temporary file "
                            "inside one terminal command."
                        ),
                        "failure_class": "validation_scratch_banned",
                        "suggested_next_tool": "run_terminal_command",
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
                        "suggested_next_tool": "run_terminal_command",
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
            replacement_reason = args.get("replacement_reason")
            if (
                self._mode == "worker"
                and target.exists()
                and not (
                    args.get("full_replace_existing") is True
                    and isinstance(replacement_reason, str)
                    and replacement_reason.strip()
                )
            ):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({
                        "ok": False,
                        "path": rel_path,
                        "rel_path": rel_path,
                        "error": (
                            "write_file on an existing file in Worker mode requires "
                            "full_replace_existing=true and a non-empty replacement_reason."
                        ),
                        "failure_class": "write_file_existing_file_requires_patch",
                        "suggested_next_tool": "patch_file",
                        "suggested_next_action": (
                            "Use patch_file for existing-file edits. Only use write_file on an existing file "
                            "when the task explicitly requires a full-file replacement and full_replace_existing is true."
                        ),
                    }, "write_file_existing_file_requires_patch"),
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
        elif name == "patch_file":
            edits = args.get("edits")
            expected_file_hash = args.get("expected_file_hash")
            description = args.get("description")
            if not isinstance(edits, list):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({"ok": False, "error": "edits must be a list", "failure_class": "internal_error"}),
                )
            if expected_file_hash is not None and not isinstance(expected_file_hash, str):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({"ok": False, "error": "expected_file_hash must be a string when supplied", "failure_class": "internal_error"}),
                )
            if description is not None and not isinstance(description, str):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({"ok": False, "error": "description must be a string when supplied", "failure_class": "internal_error"}),
                )
            proposal = _reg.propose_patch_file(
                self._root,
                target,
                edits,
                expected_file_hash=expected_file_hash,
                description=description,
            )
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=_mark_not_applied(proposal))

            syntax_error = _python_syntax_error_payload(proposal)
            if syntax_error is not None:
                return ToolExecResult(ok=False, payload=syntax_error)

            req = ApprovalRequest(
                tool_name="patch_file",
                rel_path=proposal["rel_path"],
                old_content=proposal["old_content"],
                new_content=proposal["new_content"],
                is_new_file=False,
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

        # Approve — capture pre-write state, back up, write new content.
        self._capture_before_write(self, req.rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        backup_path = _reg.backup_existing(self._root, target)
        _atomic_write_bytes(target, req.new_content.encode("utf-8"))

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
        if name == "patch_file":
            payload["hunk_count"] = proposal.get("hunk_count", 0)
        return ToolExecResult(
            ok=True,
            payload=payload,
            extras={
                "approval": "approve",
                "rel_path": req.rel_path,
                "approval_metadata": decision.metadata,
            },
        )
