"""Mixin providing search/query handler methods for ToolRegistry.

Expected on self:
    _root: Path  (workspace root)
    _codebase_index: CodebaseIndex | None

Functions are looked up through *registry* at call time so that
``unittest.mock.patch("aura.conversation.tools.registry.<name>")``
in test_tool_registry.py takes effect correctly.
"""

from __future__ import annotations

from aura.config import SEARCH_CODEBASE_TOP_K
from aura.conversation.tools._types import ToolExecResult

# Import the registry module so we can look up functions at call time.
# This creates a circular import, but Python handles it because
# `registry` is already in sys.modules by the time this module is loaded.
from aura.conversation.tools import registry as _reg


class SearchHandlersMixin:
    """Handlers for search/query tools that need workspace-root access."""

    def _handle_grep_search(self, args, approval_cb, reject_all) -> ToolExecResult:
        pattern = args.get("pattern", "")
        if not pattern:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "pattern is required"})
        return ToolExecResult(
            ok=True,
            payload=_reg.grep_files(
                workspace_root=self._root,
                pattern=pattern,
                regex_mode=bool(args.get("regex_mode", False)),
                case_sensitive=bool(args.get("case_sensitive", False)),
                max_results=int(args.get("max_results", 50)),
                include_pattern=args.get("include_pattern"),
            ),
        )

    def _handle_find_usages(self, args, approval_cb, reject_all) -> ToolExecResult:
        symbol = args.get("symbol", "")
        if not symbol:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "symbol is required"})
        return ToolExecResult(
            ok=True,
            payload=_reg.find_usages(
                workspace_root=self._root,
                symbol=symbol,
                include_pattern=args.get("include_pattern"),
                max_results=int(args.get("max_results", 100)),
                case_sensitive=bool(args.get("case_sensitive", False)),
            ),
        )

    def _handle_search_codebase(self, args, approval_cb, reject_all) -> ToolExecResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "query is required"})
        top_k = int(args.get("top_k", SEARCH_CODEBASE_TOP_K))
        if self._codebase_index is None:
            self._codebase_index = _reg.CodebaseIndex(self._root)
        result = _reg._search_codebase(
            workspace_root=self._root,
            query=query,
            top_k=top_k,
            _index=self._codebase_index,
        )
        return ToolExecResult(ok=result.get("ok", False), payload=result)
