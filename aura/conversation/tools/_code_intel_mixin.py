"""Mixin providing CodeIntel handler methods for ToolRegistry."""

from __future__ import annotations

from aura.conversation.tools._types import ToolExecResult


class CodeIntelHandlersMixin:
    """Handlers for code-intelligence read-only tools."""

    def _handle_code_intel_outline(self, args, approval_cb, reject_all) -> ToolExecResult:
        from aura.code_intel.index import get_cached_index

        path = args.get("path", "")
        if not path:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "path required"})
        try:
            index = get_cached_index(self._root)
            index.refresh()
            result = index.get_outline(path)
        except Exception as e:
            return ToolExecResult(ok=False, payload={"ok": False, "error": str(e)})
        return ToolExecResult(ok=True, payload={"ok": True, "path": path, "outline": result})

    def _handle_code_intel_references(self, args, approval_cb, reject_all) -> ToolExecResult:
        from aura.code_intel.index import get_cached_index

        symbol = args.get("symbol", "")
        if not symbol:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "symbol required"})
        file = args.get("file")
        try:
            index = get_cached_index(self._root)
            index.refresh()
            refs = index.get_references_to(symbol, file=file)
        except Exception as e:
            return ToolExecResult(ok=False, payload={"ok": False, "error": str(e)})
        compact = [
            {"source_file": r.source_file, "target_symbol": r.target_symbol, "line": r.line, "kind": r.kind}
            for r in refs
        ]
        return ToolExecResult(ok=True, payload={"ok": True, "symbol": symbol, "references": compact, "count": len(compact)})

    def _handle_code_intel_dependents(self, args, approval_cb, reject_all) -> ToolExecResult:
        from aura.code_intel.index import get_cached_index

        path = args.get("path", "")
        if not path:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "path required"})
        try:
            index = get_cached_index(self._root)
            index.refresh()
            deps = index.get_blast_radius(path)
        except Exception as e:
            return ToolExecResult(ok=False, payload={"ok": False, "error": str(e)})
        return ToolExecResult(ok=True, payload={"ok": True, "path": path, "dependents": deps, "count": len(deps)})

    def _handle_code_intel_audit(self, args, approval_cb, reject_all) -> ToolExecResult:
        from aura.code_intel.audit import audit_changed_files

        paths = args.get("paths", [])
        if not paths:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "paths required"})
        try:
            findings = audit_changed_files(self._root, paths)
        except Exception as e:
            return ToolExecResult(ok=False, payload={"ok": False, "error": str(e)})
        compact = [
            {"file": f.file, "line": f.line, "message": f.message, "severity": f.severity, "kind": f.kind}
            for f in findings
        ]
        return ToolExecResult(ok=True, payload={"ok": True, "findings": compact, "count": len(compact)})

    def _handle_inspect_code(self, args, approval_cb, reject_all) -> ToolExecResult:
        """Thin forward to CodeInspector — no semantic logic lives here."""
        raw_path = args.get("path", "")
        if not raw_path:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "path required"})
        line = args.get("line")
        if line is not None and not isinstance(line, int):
            return ToolExecResult(ok=False, payload={"ok": False, "error": "line must be an integer"})
        symbol = args.get("symbol")
        if symbol is not None and not isinstance(symbol, str):
            return ToolExecResult(ok=False, payload={"ok": False, "error": "symbol must be a string"})
        try:
            target = self._resolve_in_root(raw_path)
        except ValueError as e:
            return ToolExecResult(ok=False, payload={"ok": False, "error": str(e)})
        result = self._code_inspector.inspect(
            target, line=line, symbol=(symbol or None), cancel_event=self.active_cancel_event
        )
        return ToolExecResult(ok=bool(result.get("ok", False)), payload=result)
