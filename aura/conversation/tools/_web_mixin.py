"""Mixin providing web handler methods for ToolRegistry.

Expected on self:
    _web_handler: WebHandler instance
"""

from __future__ import annotations

from aura.conversation.tools._types import ToolExecResult


class WebHandlersMixin:
    """Handlers for web tools — thin wrappers around WebHandler."""

    def _handle_web_search(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._web_handler.handle_web_search(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_web_fetch(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._web_handler.handle_web_fetch(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)
