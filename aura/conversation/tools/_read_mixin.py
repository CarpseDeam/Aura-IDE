"""Mixin providing read-file handler methods for ToolRegistry.

Expected on self:
    _fs_handler: FsReadHandler instance
"""

from __future__ import annotations

from aura.conversation.tools._types import ToolExecResult


class ReadHandlersMixin:
    """Handlers for read-file tools — thin wrappers around FsReadHandler."""

    def _handle_read_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._fs_handler.handle_read_file(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_read_files(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._fs_handler.handle_read_files(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_list_directory(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._fs_handler.handle_list_directory(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_glob(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._fs_handler.handle_glob(args)
        return ToolExecResult(ok=payload.get("ok", False), payload=payload)

    def _handle_read_file_outline(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._fs_handler.handle_read_file_outline(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_read_file_range(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._fs_handler.handle_read_file_range(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)
