"""Owner of the ``inspect_code`` evidence packet.

Assembles a bounded, language-neutral neighborhood around one source
location or symbol from what :mod:`aura.code_intel` already knows: the
per-workspace :class:`~aura.code_intel.index.CodeIntelIndex` for symbols and
parser diagnostics, and the filesystem for a bounded source excerpt. This is
local structural evidence only — resolved declaration/target, bounded source
excerpt, parser diagnostics, and provenance for one file. It does not search
the workspace for other occurrences of a symbol; ``grep_search`` and
``search_codebase`` own that global discovery. Uncertainty is represented
truthfully through structured fields rather than a blanket capability list:
``resolution``, ``provenance``, null declaration ranges, ``bounded_window``,
``parser_bounded``, and ``truncated``. A declaration end range is reported
only when the resolving adapter genuinely parsed one (see
``SymbolInfo.end_line`` / ``end_column``); otherwise the result says so
rather than implying one.
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

#: Parser diagnostics returned for the target file.
DIAGNOSTIC_RETURN_CAP = 20

#: Provenance label for a resolved symbol / diagnostics, by adapter language
#: id. Anything not listed here comes from the generic tree-sitter adapter.
_ADAPTER_PROVENANCE: dict[str, str] = {
    "python": "aura_parser",
    "text": "lexical_heuristic",
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

        return {
            "ok": True,
            "path": rel_path,
            "target": self._build_target(rel_path, language, line, symbol, resolved, resolution),
            "source_excerpt": self._build_excerpt(abs_path, rel_path, anchor_line, resolved),
            "diagnostics": self._build_diagnostics(diagnostics, language),
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
