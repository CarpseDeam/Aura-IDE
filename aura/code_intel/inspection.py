"""Owner of the ``inspect_code`` evidence packet.

Assembles a bounded, language-neutral neighborhood around one source
location or symbol from what :mod:`aura.code_intel` already knows: the
per-workspace :class:`~aura.code_intel.index.CodeIntelIndex` for symbols and
parser diagnostics, the filesystem for a bounded source excerpt, and
``find_usages`` for lexical occurrences. It does not add semantic
capabilities the underlying adapters lack — it packages what they already
provide and states plainly what they do not: no resolved references, no call
graph, no type inference. A declaration end range is reported only when the
resolving adapter genuinely parsed one (see ``SymbolInfo.end_line`` /
``end_column``); otherwise the result says so rather than implying one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.code_intel.index import CodeIntelIndex
from aura.config import MAX_READ_BYTES
from aura.paths import safe_relative_to

#: Lines of context kept before the resolved/anchor line in the source excerpt.
EXCERPT_CONTEXT_BEFORE_LINES = 8
#: Total lines the source excerpt window may span.
EXCERPT_MAX_LINES = 160
#: Character cap on the excerpt text, independent of line count (long lines).
EXCERPT_MAX_CHARS = 9000

#: Internal search depth used to count lexical occurrences.
OCCURRENCE_SEARCH_CAP = 200
#: Occurrences actually returned to the model.
OCCURRENCE_RETURN_CAP = 20

#: Parser diagnostics returned for the target file.
DIAGNOSTIC_RETURN_CAP = 20

#: Limitations that hold for every result: this is a parser/lexical layer,
#: never an LSP, so these capabilities are never available regardless of
#: which adapter served the request.
_BASE_LIMITATIONS: tuple[str, ...] = (
    "resolved_references_unavailable",
    "call_graph_unavailable",
    "type_resolution_unavailable",
)

#: Provenance label for a resolved symbol / diagnostics, by adapter language
#: id. Anything not listed here comes from the generic tree-sitter adapter.
_ADAPTER_PROVENANCE: dict[str, str] = {
    "python": "aura_parser",
    "text": "lexical_heuristic",
    "godot_project": "aura_godot_adapter",
    "godot_resource": "aura_godot_adapter",
    "gdscript": "aura_godot_adapter",
}


def _parser_provenance(language: str) -> str:
    return _ADAPTER_PROVENANCE.get(language, "tree_sitter")


def _detect_language(rel_path: str) -> str:
    """Best-effort language id for a file the index has not parsed."""
    from aura.code_intel.adapter import get_adapter

    adapter = get_adapter(rel_path)
    return adapter.language_id if adapter is not None else "unknown"


def _resolve_target(
    symbols: list[Any], line: int | None, symbol: str | None
) -> tuple[Any | None, str]:
    """Pick the best symbol match for the requested anchor, and how it was found.

    Returns ``(SymbolInfo | None, resolution_label)``. A name match is
    preferred over a line match; a line match must be an exact declaration
    line to count as exact, otherwise the nearest preceding declaration is
    used and labeled as such — never claimed as an exact resolution.
    """
    if symbol:
        name_matches = [s for s in symbols if s.name == symbol]
        if name_matches:
            if line is not None:
                best = min(name_matches, key=lambda s: abs(s.line - line))
            else:
                best = min(name_matches, key=lambda s: s.line)
            return best, "exact_symbol_match"

    if line is not None:
        exact = [s for s in symbols if s.line == line]
        if exact:
            return exact[0], "exact_line_match"
        preceding = [s for s in symbols if s.line <= line]
        if preceding:
            return max(preceding, key=lambda s: s.line), "nearest_preceding_declaration"

    return None, "unresolved"


class CodeInspector:
    """Workspace-scoped owner of the ``inspect_code`` operation.

    Symbol/diagnostic data comes from the workspace :class:`CodeIntelIndex`
    injected by its owner (:class:`~aura.conversation.tools.registry.ToolRegistry`).
    A workspace-root change replaces this object rather than mutating it, so
    an inspector never outlives the index it was built for.
    """

    def __init__(self, workspace_root: Path, index: CodeIntelIndex) -> None:
        self._root = workspace_root.resolve()
        self._index = index

    def inspect(
        self,
        abs_path: Path,
        *,
        line: int | None,
        symbol: str | None,
        cancel_event: Any | None = None,
    ) -> dict[str, Any]:
        if not abs_path.exists():
            return {"ok": False, "error": f"file not found: {abs_path.name}"}
        if not abs_path.is_file():
            return {"ok": False, "error": f"not a regular file: {abs_path.name}"}

        rel_path = safe_relative_to(abs_path, self._root).as_posix()

        self._index.ensure_fresh(rel_path)

        file_info = self._index.get_file(rel_path)
        symbols = self._index.get_symbols(rel_path)
        diagnostics = self._index.get_diagnostics(rel_path)
        language = file_info.language if file_info is not None else _detect_language(rel_path)

        resolved, resolution = _resolve_target(symbols, line, symbol)
        anchor_line = resolved.line if resolved is not None else (line if line is not None else 1)
        has_exact_range = resolved is not None and resolved.end_line is not None

        occurrence_symbol = symbol or (resolved.name if resolved is not None else None)
        occurrences = self._find_occurrences(occurrence_symbol, cancel_event)

        return {
            "ok": True,
            "path": rel_path,
            "target": self._build_target(rel_path, language, line, symbol, resolved, resolution),
            "source_excerpt": self._build_excerpt(abs_path, rel_path, anchor_line, resolved),
            "occurrences": occurrences,
            "diagnostics": self._build_diagnostics(diagnostics, language),
            "limitations": self._limitations(occurrences is not None, has_exact_range),
        }

    # -- section builders -----------------------------------------------

    def _build_target(
        self,
        rel_path: str,
        language: str,
        line: int | None,
        symbol: str | None,
        resolved: Any | None,
        resolution: str,
    ) -> dict[str, Any]:
        target: dict[str, Any] = {
            "resolution": resolution,
            "language": language,
            "path": rel_path,
            "anchor_line": line,
            "anchor_symbol": symbol,
            "name": None,
            "kind": None,
            "declaration_line": None,
            "declaration_end_line": None,
            "declaration_end_column": None,
            "signature": None,
            "enclosing": None,
            "provenance": None,
        }
        if resolved is not None:
            target.update({
                "name": resolved.name,
                "kind": resolved.kind,
                "declaration_line": resolved.line,
                "declaration_end_line": resolved.end_line,
                "declaration_end_column": resolved.end_column,
                "signature": resolved.signature,
                "enclosing": resolved.parent,
                "provenance": _parser_provenance(language),
            })
        return target

    def _build_excerpt(
        self, abs_path: Path, rel_path: str, anchor_line: int, resolved: Any | None
    ) -> dict[str, Any]:
        has_exact_range = resolved is not None and resolved.end_line is not None
        if has_exact_range:
            start = resolved.line
            requested_end = resolved.end_line
        else:
            start = max(1, anchor_line - EXCERPT_CONTEXT_BEFORE_LINES)
            requested_end = start + EXCERPT_MAX_LINES - 1

        try:
            file_size = abs_path.stat().st_size
            # Bounded read: never pull more than the existing hard cap off
            # disk, even for a file far larger than that cap.
            with open(abs_path, "rb") as f:
                raw = f.read(MAX_READ_BYTES)
        except OSError as exc:
            return {
                "path": rel_path,
                "start_line": start,
                "end_line": start,
                "text": "",
                "truncated": False,
                "bounded_window": not has_exact_range,
                "parser_bounded": has_exact_range,
                "provenance": "filesystem",
                "error": str(exc),
            }

        size_truncated = file_size > MAX_READ_BYTES
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        total_lines = len(lines)

        actual_start = min(start, max(total_lines, 1))
        full_end = min(requested_end, total_lines)
        # Even an exact parser-owned range is still bounded by the same hard
        # line cap as a context window — a huge body gets truncated, not a
        # blown-out response.
        window_cap_end = actual_start + EXCERPT_MAX_LINES - 1
        actual_end = min(full_end, window_cap_end)

        selected = lines[actual_start - 1:actual_end] if total_lines else []
        excerpt_text = "\n".join(selected)

        char_truncated = len(excerpt_text) > EXCERPT_MAX_CHARS
        if char_truncated:
            excerpt_text = excerpt_text[:EXCERPT_MAX_CHARS]

        if has_exact_range:
            range_truncated = actual_end < full_end
            truncated = bool(char_truncated or size_truncated or range_truncated)
        else:
            truncated = bool(char_truncated or size_truncated or actual_end < total_lines)

        return {
            "path": rel_path,
            "start_line": actual_start,
            "end_line": actual_end,
            "text": excerpt_text,
            "truncated": truncated,
            "bounded_window": not has_exact_range,
            "parser_bounded": has_exact_range,
            "provenance": "filesystem",
        }

    def _find_occurrences(
        self, symbol_name: str | None, cancel_event: Any | None
    ) -> dict[str, Any] | None:
        if not symbol_name:
            return None

        from aura.conversation.tools.find_usages import find_usages

        result = find_usages(
            self._root,
            symbol_name,
            max_results=OCCURRENCE_SEARCH_CAP,
            cancel_event=cancel_event,
        )
        if not result.get("ok"):
            return {
                "symbol": symbol_name,
                "total": 0,
                "returned": 0,
                "truncated": False,
                "items": [],
                "provenance": "lexical_search",
                "error": result.get("error"),
            }

        matches = result.get("matches", [])
        total_found = len(matches)
        engine_truncated = bool(result.get("truncated"))
        returned = matches[:OCCURRENCE_RETURN_CAP]
        items = [
            {"path": m["path"], "line": m["line_number"], "context": m["line"]}
            for m in returned
        ]
        return {
            "symbol": symbol_name,
            "total": total_found,
            "total_is_lower_bound": engine_truncated,
            "returned": len(items),
            "truncated": bool(engine_truncated or total_found > len(items)),
            "items": items,
            "provenance": "lexical_search",
        }

    def _build_diagnostics(self, diagnostics: list[Any], language: str) -> dict[str, Any]:
        capped = diagnostics[:DIAGNOSTIC_RETURN_CAP]
        return {
            "total": len(diagnostics),
            "returned": len(capped),
            "truncated": len(diagnostics) > len(capped),
            "items": [
                {"line": d.line, "message": d.message, "severity": d.severity}
                for d in capped
            ],
            "provenance": _parser_provenance(language),
        }

    def _limitations(self, has_occurrences: bool, has_exact_range: bool) -> list[str]:
        items = list(_BASE_LIMITATIONS)
        if not has_exact_range:
            items.append("exact_body_range_unavailable")
        if has_occurrences:
            items.append("occurrences_are_lexical")
        return items
