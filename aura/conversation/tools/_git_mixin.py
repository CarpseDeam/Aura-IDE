"""Mixin providing git handler methods for ToolRegistry.

Expected on self:
    _git_handler: GitHandler instance
"""

from __future__ import annotations

from aura.conversation.tools._types import ToolExecResult


class GitHandlersMixin:
    """Handlers for git tools — thin wrappers around GitHandler."""

    def _handle_git_status(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_status(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_git_diff(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_diff(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_git_log(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_log(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_git_show(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_show(args)
        return ToolExecResult(ok=payload.get("ok", False), payload=payload)

    def _handle_git_log_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_log_file(args)
        return ToolExecResult(ok=payload.get("ok", False), payload=payload)

    def _handle_git_branch_list(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_branch_list(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_git_stash_list(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_stash_list(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_git_stash_show(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_stash_show(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)
