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

from aura.conversation.tools._types import ApprovalRequest, ToolExecResult

# Import the registry module so we can look up functions at call time.
# This creates a circular import, but Python handles it because
# `registry` is already in sys.modules by the time this module is loaded.
from aura.conversation.tools import registry as _reg


class WriteHandlersMixin:
    """Handlers for write tools — guards + approval + backup."""

    def _handle_write_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled."})
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended edits in the 'spec' field of the dispatch."
                    ),
                },
            )
        return self._handle_write("write_file", args, approval_cb, reject_all)

    def _handle_edit_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled."})
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended edits in the 'spec' field of the dispatch."
                    ),
                },
            )
        return self._handle_write("edit_file", args, approval_cb, reject_all)

    def _handle_edit_symbol(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled."})
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended edits in the 'spec' field of the dispatch."
                    ),
                },
            )
        return self._handle_write("edit_symbol", args, approval_cb, reject_all)

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
                payload={"ok": False, "error": "User rejected all writes in this turn."},
                extras={"rejected_all": True},
            )

        path_arg = args.get("path", "")
        target = self._resolve_in_root(path_arg)

        if name == "write_file":
            content = args.get("content", "")
            if not isinstance(content, str):
                return ToolExecResult(
                    ok=False, payload={"ok": False, "error": "content must be a string"}
                )
            proposal = _reg.propose_write(self._root, target, content)
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=proposal)
            req = ApprovalRequest(
                tool_name="write_file",
                rel_path=proposal["rel_path"],
                old_content=proposal["old_content"],
                new_content=proposal["new_content"],
                is_new_file=proposal["is_new_file"],
            )
        elif name == "edit_file":
            old_str = args.get("old_str", "")
            new_str = args.get("new_str", "")
            if not isinstance(old_str, str) or not isinstance(new_str, str):
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": "old_str and new_str must be strings"},
                )
            proposal = _reg.propose_edit(self._root, target, old_str, new_str)
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=proposal)
            req = ApprovalRequest(
                tool_name="edit_file",
                rel_path=proposal["rel_path"],
                old_content=proposal["old_content"],
                new_content=proposal["new_content"],
                is_new_file=False,
            )
        else:  # edit_symbol
            symbol_type = args.get("symbol_type", "")
            symbol_name = args.get("symbol_name", "")
            new_definition = args.get("new_definition", "")
            class_name = args.get("class_name")
            if not isinstance(symbol_type, str) or not isinstance(symbol_name, str) or not isinstance(new_definition, str):
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": "symbol_type, symbol_name, and new_definition must be strings"},
                )
            proposal = _reg.propose_edit_symbol(
                self._root, target, symbol_type, symbol_name, new_definition, class_name
            )
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=proposal)
            req = ApprovalRequest(
                tool_name="edit_symbol",
                rel_path=proposal["rel_path"],
                old_content=proposal["old_content"],
                new_content=proposal["new_content"],
                is_new_file=False,
            )

        decision = approval_cb(req)

        if decision.action == "reject":
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "User rejected this change."},
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
                },
                extras={
                    "approval": "reject_all",
                    "rel_path": req.rel_path,
                    "approval_metadata": decision.metadata,
                },
            )

        # Approve — back up if file exists, write new content.
        target.parent.mkdir(parents=True, exist_ok=True)
        backup_path = _reg.backup_existing(self._root, target)
        target.write_text(req.new_content, encoding="utf-8")
        rel_backup = (
            backup_path.relative_to(self._root).as_posix() if backup_path is not None else None
        )
        return ToolExecResult(
            ok=True,
            payload={
                "ok": True,
                "path": req.rel_path,
                "applied": name,
                "is_new_file": req.is_new_file,
                "backup": rel_backup,
            },
            extras={
                "approval": "approve",
                "rel_path": req.rel_path,
                "approval_metadata": decision.metadata,
            },
        )
