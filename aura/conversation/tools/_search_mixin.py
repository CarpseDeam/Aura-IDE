"""Mixin providing search/query handler methods for ToolRegistry.

Expected on self:
    _root: Path  (workspace root)
    _codebase_index: CodebaseIndex | None
    _code_intel_index: CodeIntelIndex  (shared with CodebaseIndex, never duplicated)
    _external_read: ExternalReadAccess  (this turn's external read allowlist)

Functions are looked up through *registry* at call time so that
``unittest.mock.patch("aura.conversation.tools.registry.<name>")``
in test_tool_registry.py takes effect correctly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.config import SEARCH_CODEBASE_TOP_K

# Import the registry module so we can look up functions at call time.
# This creates a circular import, but Python handles it because
# `registry` is already in sys.modules by the time this module is loaded.
from aura.conversation.tools import registry as _reg
from aura.conversation.tools._types import ToolExecResult
from aura.paths import safe_relative_to


class SearchHandlersMixin:
    """Handlers for search/query tools that need workspace-root access."""

    def _resolve_search_scope(self, raw: str) -> tuple[Path, bool]:
        """Resolve grep_search's optional scope to ``(target, external)``.

        The scope is an ordinary path argument: workspace-relative by default,
        or an absolute file/directory this turn's user text authorized. An
        absolute scope nobody authorized fails here rather than being clamped
        back to the workspace, because silently searching somewhere else would
        answer a question nobody asked.
        """
        text = str(raw).strip()
        if text == "":
            raise ValueError("path must not be empty")
        target = self._resolve_readable(text)
        external = self._is_external_target(target)
        if not target.exists():
            raise ValueError(f"search path '{raw}' does not exist")
        return target, external

    def _handle_grep_search(self, args, approval_cb, reject_all) -> ToolExecResult:
        pattern = args.get("pattern", "")
        if not pattern:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "pattern is required"})
        regex_mode = args.get("regex_mode")
        if regex_mode is None:
            regex_mode = True

        include_pattern = args.get("include_pattern")
        raw_scope = args.get("path")
        search_root = self._root
        external = False
        if raw_scope is not None and str(raw_scope).strip() != "":
            try:
                target, external = self._resolve_search_scope(raw_scope)
            except ValueError as exc:
                return ToolExecResult(
                    ok=False,
                    payload={
                        "ok": False,
                        "error": str(exc),
                        "failure_class": "search_scope_unauthorized",
                    },
                )
            if target.is_dir():
                search_root = target
            else:
                # A file scope searches exactly that file, expressed to the
                # engine the way it already expresses a single-file scope.
                if include_pattern:
                    return ToolExecResult(
                        ok=False,
                        payload={
                            "ok": False,
                            "error": (
                                "include_pattern is not valid when 'path' names a "
                                "single file"
                            ),
                        },
                    )
                search_root = target.parent
                include_pattern = target.name

        payload = _reg.grep_files(
            workspace_root=search_root,
            pattern=pattern,
            regex_mode=bool(regex_mode),
            case_sensitive=bool(args.get("case_sensitive", False)),
            max_results=int(args.get("max_results", 50)),
            include_pattern=include_pattern,
            cancel_event=self.active_cancel_event,
        )
        payload = self._rebase_search_payload(payload, search_root, external)
        return ToolExecResult(ok=payload.get("ok", False), payload=payload)

    def _rebase_search_payload(
        self, payload: dict[str, Any], search_root: Path, external: bool
    ) -> dict[str, Any]:
        """Report result paths the way the caller can act on them.

        A search rooted somewhere other than the workspace root still has to
        return paths ``read_file`` accepts: workspace-relative for a workspace
        scope, absolute for an authorized external one. Only the scope the
        caller asked for appears — no other authorized location is named.
        """
        if not isinstance(payload, dict) or not payload.get("ok"):
            return payload
        if external:
            payload["external"] = True
            payload["read_only"] = True
            payload["source"] = "external"
            payload["search_root"] = search_root.as_posix()

            def render(rel: str) -> str:
                return (search_root / rel).as_posix()
        else:
            if search_root == self._root:
                return payload
            prefix = safe_relative_to(search_root, self._root).as_posix()
            payload["search_root"] = prefix

            def render(rel: str) -> str:
                return f"{prefix}/{rel}" if prefix not in ("", ".") else rel

        for match in payload.get("matches") or []:
            if isinstance(match, dict) and match.get("path"):
                match["path"] = render(str(match["path"]))
        for skipped in payload.get("skipped_details") or []:
            if isinstance(skipped, dict) and skipped.get("path"):
                skipped["path"] = render(str(skipped["path"]))
        return payload

    def _handle_find_usages(self, args, approval_cb, reject_all) -> ToolExecResult:
        symbol = args.get("symbol", "")
        if not symbol:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "symbol is required"})
        payload = _reg.find_usages(
            workspace_root=self._root,
            symbol=symbol,
            include_pattern=args.get("include_pattern"),
            max_results=int(args.get("max_results", 100)),
            case_sensitive=bool(args.get("case_sensitive", False)),
            cancel_event=self.active_cancel_event,
        )
        return ToolExecResult(ok=payload.get("ok", False), payload=payload)

    def _handle_search_codebase(self, args, approval_cb, reject_all) -> ToolExecResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "query is required"})
        top_k = int(args.get("top_k", SEARCH_CODEBASE_TOP_K))
        source = args.get("source", "workspace")
        if source != "workspace":
            # There is one external read authority, and it is grep_search's
            # path scope. A second one here would be a second permission
            # system, so a replayed historical call fails truthfully instead.
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": (
                        "search_codebase only searches the active workspace; "
                        "search an authorized external location with grep_search"
                    ),
                },
            )

        if self._codebase_index is None:
            self._codebase_index = _reg.CodebaseIndex(
                self._root, code_intel_index=self._code_intel_index
            )
        result = _reg._search_codebase(
            workspace_root=self._root,
            query=query,
            top_k=top_k,
            _index=self._codebase_index,
        )
        return ToolExecResult(ok=result.get("ok", False), payload=result)
