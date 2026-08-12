"""Mixin providing the read_reference_file handler for ToolRegistry.

Expected on self:
    _reference_root: ReferenceRootAccess instance
"""

from __future__ import annotations

from aura.conversation.tools._types import ToolExecResult
from aura.conversation.tools.fs_handler import FsReadHandler


class ReferenceReadHandlersMixin:
    """Handler for read_reference_file — reuses FsReadHandler over the
    currently attached Reference Folder rather than forking a second reader."""

    def _handle_read_reference_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        reference = self._reference_root
        if not reference.is_available:
            payload = {
                "ok": False,
                "failure_class": "reference_root_unavailable",
                "error": "no Reference Folder is attached; the user must attach one first",
            }
            return ToolExecResult(ok=False, payload=payload)

        handler = FsReadHandler(reference.root, reference.resolve)
        payload = handler.handle_read_file(args)
        if payload.get("ok"):
            payload["source"] = "reference"
            payload["reference_name"] = reference.name
            payload["read_only"] = True
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)
