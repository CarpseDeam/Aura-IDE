"""Compound read-only task context tool."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from aura.config import SKIP_DIRS, SKIP_FILE_SUFFIXES
from aura.conversation.context_pack.budget import BudgetTracker
from aura.conversation.context_pack.dependency_hints import find_dependency_hints
from aura.conversation.context_pack.file_summary import summarize_file
from aura.conversation.context_pack.models import ContextPackSection
from aura.conversation.context_pack.test_hints import find_test_hints
from aura.conversation.tools._types import ToolExecResult
from aura.paths import safe_is_relative_to, safe_relative_to


DEFAULT_MAX_CHARS = 16000
_MAX_QUERY_HITS = 24
_MAX_SYMBOL_HITS_PER_SYMBOL = 16


class TaskContextHandlersMixin:
    """Thin ToolRegistry handler wrapper for read_task_context."""

    def _handle_read_task_context(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = read_task_context(self._root, args)
        return ToolExecResult(ok=payload.get("ok", False), payload=payload)


def read_task_context(workspace_root: Path, args: dict[str, Any]) -> dict[str, Any]:
    """Return a compact, bounded task context packet without mutation or subprocesses."""
    max_chars = _coerce_max_chars(args.get("max_chars", DEFAULT_MAX_CHARS))
    raw_files = args.get("files") or []
    raw_symbols = args.get("symbols") or []
    query = str(args.get("query") or "").strip()
    include_dependents = bool(args.get("include_dependents", True))
    include_tests = bool(args.get("include_tests", True))

    files, caveats = _normalize_files(workspace_root, raw_files)
    symbols, symbol_caveats = _normalize_string_list(raw_symbols, "symbols")
    caveats.extend(symbol_caveats)

    tracker = BudgetTracker(max_chars)
    header_lines = ["Task Context"]
    if files:
        header_lines.append(f"Files: {', '.join(files)}")
    if query:
        header_lines.append(f"Query: {query}")
    if symbols:
        header_lines.append(f"Symbols: {', '.join(symbols)}")
    if len(header_lines) == 1:
        header_lines.append("(no files, query, or symbols requested)")
        caveats.append("No files, query, or symbols were provided.")
    tracker.add_section("\n".join(header_lines))

    for rel_path in files:
        section = summarize_file(workspace_root, rel_path)
        if section.caveat:
            caveats.append(f"{rel_path}: {section.caveat}")
        tracker.add_section(_format_section(section))

    if query:
        query_section, query_truncated = _query_context_section(workspace_root, query)
        if query_truncated:
            caveats.append("Query hits were truncated.")
        tracker.add_section(_format_section(query_section))

    if symbols:
        symbol_section, symbol_truncated = _symbol_context_section(workspace_root, symbols)
        if symbol_truncated:
            caveats.append("Symbol hits were truncated.")
        tracker.add_section(_format_section(symbol_section))

    if include_tests and files:
        tracker.add_section(_format_section(find_test_hints(workspace_root, files)))

    if include_dependents and files:
        dep_section = find_dependency_hints(workspace_root, files)
        if dep_section.caveat:
            caveats.append(dep_section.caveat)
        tracker.add_section(_format_section(dep_section))

    if tracker.truncated:
        caveats.append(f"context truncated at max_chars={max_chars}")

    return {
        "ok": True,
        "files": files,
        "query": query or None,
        "symbols": symbols,
        "context": tracker.content,
        "truncated": tracker.truncated,
        "caveats": _dedupe(caveats),
    }


def _coerce_max_chars(value: Any) -> int:
    try:
        max_chars = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS
    return max(1, max_chars)


def _normalize_files(workspace_root: Path, value: Any) -> tuple[list[str], list[str]]:
    if not isinstance(value, list):
        return [], ["files must be a list; ignoring invalid value."]

    files: list[str] = []
    caveats: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            caveats.append(f"ignored non-string file path: {raw!r}")
            continue
        rel_path, caveat = _normalize_file_path(workspace_root, raw)
        if caveat:
            caveats.append(caveat)
        if rel_path and rel_path not in files:
            files.append(rel_path)
            if not (workspace_root / rel_path).exists():
                caveats.append(f"{rel_path}: missing")
    return files, caveats


def _normalize_file_path(workspace_root: Path, raw: str) -> tuple[str | None, str | None]:
    stripped = raw.strip()
    if not stripped:
        return None, "ignored empty file path"
    candidate = Path(stripped)
    if ".." in candidate.parts:
        return None, f"ignored path with '..': {raw}"
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (workspace_root / stripped.lstrip("/\\")).resolve()
    if not safe_is_relative_to(resolved, workspace_root):
        return None, f"ignored path outside workspace: {raw}"
    return safe_relative_to(resolved, workspace_root).as_posix(), None


def _normalize_string_list(value: Any, label: str) -> tuple[list[str], list[str]]:
    if value in (None, ""):
        return [], []
    if not isinstance(value, list):
        return [], [f"{label} must be a list; ignoring invalid value."]
    items: list[str] = []
    caveats: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            caveats.append(f"ignored non-string {label} entry: {raw!r}")
            continue
        text = raw.strip()
        if text and text not in items:
            items.append(text)
    return items, caveats


def _query_context_section(workspace_root: Path, query: str) -> tuple[ContextPackSection, bool]:
    terms = _query_terms(query)
    if not terms:
        return ContextPackSection("Query Hits", ["(query has no searchable terms)"]), False

    hits: list[tuple[int, dict[str, Any]]] = []
    truncated = False
    for path in _iter_text_candidates(workspace_root):
        rel = safe_relative_to(path, workspace_root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            score = _line_score(line, rel, terms)
            if score <= 0:
                continue
            if len(hits) >= _MAX_QUERY_HITS:
                truncated = True
                break
            hits.append((score, {"path": rel, "line_number": line_number, "line": line.strip()}))
        if truncated:
            break

    if not hits:
        return ContextPackSection("Query Hits", ["(no query hits found)"]), False

    hits.sort(key=lambda item: (-item[0], item[1]["path"], item[1]["line_number"]))
    body_lines = [
        f"{hit['path']}:{hit['line_number']}: {hit['line']}"
        for _, hit in hits[:_MAX_QUERY_HITS]
    ]
    return ContextPackSection("Query Hits", body_lines), truncated


def _symbol_context_section(workspace_root: Path, symbols: list[str]) -> tuple[ContextPackSection, bool]:
    body_lines: list[str] = []
    truncated = False
    for symbol in symbols:
        pattern = re.compile(rf"\b{re.escape(symbol)}\b", re.IGNORECASE)
        body_lines.append(f"{symbol}:")
        count = 0
        for path in _iter_text_candidates(workspace_root):
            rel = safe_relative_to(path, workspace_root).as_posix()
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if not pattern.search(line):
                    continue
                body_lines.append(f"  {rel}:{line_number}: {line.strip()}")
                count += 1
                if count >= _MAX_SYMBOL_HITS_PER_SYMBOL:
                    truncated = True
                    break
            if count >= _MAX_SYMBOL_HITS_PER_SYMBOL:
                break
        if count == 0:
            body_lines.append("  (no hits found)")
    return ContextPackSection("Symbol Hits", body_lines), truncated


def _iter_text_candidates(workspace_root: Path):
    for path in sorted(workspace_root.rglob("*"), key=lambda item: safe_relative_to(item, workspace_root).as_posix()):
        if not path.is_file():
            continue
        rel = safe_relative_to(path, workspace_root)
        if _should_skip(rel):
            continue
        yield path


def _should_skip(rel_path: Path) -> bool:
    if any(part in SKIP_DIRS or part.startswith(".") for part in rel_path.parts):
        return True
    return rel_path.suffix in SKIP_FILE_SUFFIXES


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for term in re.findall(r"[A-Za-z0-9_./-]+", query.lower()):
        if len(term) < 2:
            continue
        if term not in terms:
            terms.append(term)
    return terms


def _line_score(line: str, rel_path: str, terms: list[str]) -> int:
    haystack = f"{rel_path}\n{line}".lower()
    return sum(haystack.count(term) for term in terms)


def _format_section(section: ContextPackSection) -> str:
    lines = [section.heading, ""]
    lines.extend(section.body_lines)
    if section.caveat:
        lines.extend(["", f"Caveat: {section.caveat}"])
    return "\n".join(lines)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
