"""Compound read-only task context tool.

Query and symbol lookups walk the workspace under a hard candidate-file cap, so
a large repository is only ever *partially* scanned. That makes a "no hits"
answer ambiguous: the symbol may be absent, or it may simply live in a file the
walk never reached. Every result therefore carries an explicit ``coverage``
block — the candidate limit, how many files were considered, whether coverage
was partial, and why the walk stopped — and a partial no-hit result says in
plain words that it is inconclusive.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

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

#: Most workspace files one query/symbol walk will open. Named and configurable
#: because it is the reason a result can be partial.
CANDIDATE_FILE_LIMIT = 500

#: Why a walk stopped — reported verbatim in ``coverage.stop_reason``.
STOP_NOT_SCANNED = "not_scanned"
STOP_CANDIDATE_FILE_LIMIT = "candidate_file_limit_reached"
STOP_WORKSPACE_EXHAUSTED = "workspace_exhausted"

_PARTIAL_NO_HIT_CAVEAT = (
    "INCONCLUSIVE: {label} produced no hits, but only {considered} of the "
    "workspace's files were examined (candidate limit {limit}). A no-hit "
    "result under partial coverage does NOT mean the code is absent — narrow "
    "the search with grep_search or name specific files instead of concluding "
    "it does not exist."
)


@dataclass
class _Coverage:
    """How much of the workspace this call actually looked at."""

    limit: int = CANDIDATE_FILE_LIMIT
    files_considered: int = 0
    scanned: bool = False
    hit_limit: bool = False
    passes: list[str] = field(default_factory=list)

    def record_pass(self, label: str, considered: int, hit_limit: bool) -> None:
        """Fold one walk's result in.

        Passes walk the same tree, so the file count is the widest single pass
        rather than a sum — reporting 1000 files considered for two 500-file
        passes over the same 500 files would be a lie.
        """
        self.scanned = True
        self.passes.append(label)
        self.files_considered = max(self.files_considered, considered)
        self.hit_limit = self.hit_limit or hit_limit

    @property
    def partial(self) -> bool:
        return self.scanned and self.hit_limit

    @property
    def stop_reason(self) -> str:
        if not self.scanned:
            return STOP_NOT_SCANNED
        return (
            STOP_CANDIDATE_FILE_LIMIT if self.hit_limit else STOP_WORKSPACE_EXHAUSTED
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_file_limit": self.limit,
            "files_considered": self.files_considered,
            "scanned": self.scanned,
            "partial": self.partial,
            "stop_reason": self.stop_reason,
            "passes": list(self.passes),
        }

    def no_hit_caveat(self, label: str) -> str | None:
        """Return the inconclusive-result caveat, or ``None`` if coverage was full."""
        if not self.partial:
            return None
        return _PARTIAL_NO_HIT_CAVEAT.format(
            label=label,
            considered=self.files_considered,
            limit=self.limit,
        )


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

    coverage = _Coverage(limit=_coerce_candidate_limit(args.get("max_candidate_files")))

    if query:
        query_section, query_truncated, query_had_hits = _query_context_section(
            workspace_root, query, coverage
        )
        if query_truncated:
            caveats.append("Query hits were truncated.")
        if not query_had_hits:
            no_hit_caveat = coverage.no_hit_caveat("the query scan")
            caveats.append(
                no_hit_caveat
                if no_hit_caveat
                else "The query found no hits across the full workspace scan."
            )
        tracker.add_section(_format_section(query_section))

    if symbols:
        symbol_section, symbol_truncated, missing_symbols = _symbol_context_section(
            workspace_root, symbols, coverage
        )
        if symbol_truncated:
            caveats.append("Symbol hits were truncated.")
        if missing_symbols:
            no_hit_caveat = coverage.no_hit_caveat("the symbol scan")
            names = ", ".join(missing_symbols)
            caveats.append(
                f"{no_hit_caveat} Symbols with no hits: {names}."
                if no_hit_caveat
                else f"No hits across the full workspace scan for: {names}."
            )
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

    if coverage.partial:
        caveats.append(
            f"Partial workspace coverage: {coverage.files_considered} files "
            f"examined, stopped at the candidate limit of {coverage.limit}. "
            f"Results below describe only the files that were reached."
        )

    return {
        "ok": True,
        "files": files,
        "query": query or None,
        "symbols": symbols,
        "context": tracker.content,
        "truncated": tracker.truncated,
        "coverage": coverage.to_dict(),
        "caveats": _dedupe(caveats),
    }


def _coerce_candidate_limit(value: Any) -> int:
    """Return a usable candidate-file cap, defaulting to CANDIDATE_FILE_LIMIT."""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return CANDIDATE_FILE_LIMIT
    return max(1, limit)


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


def _query_context_section(
    workspace_root: Path, query: str, coverage: _Coverage
) -> tuple[ContextPackSection, bool, bool]:
    """Return the query section, whether hits were truncated, and whether any hit."""
    terms = _query_terms(query)
    if not terms:
        return (
            ContextPackSection("Query Hits", ["(query has no searchable terms)"]),
            False,
            True,
        )

    hits: list[tuple[int, dict[str, Any]]] = []
    truncated = False
    walk = _CandidateWalk(workspace_root, coverage.limit)
    for path in walk:
        rel = safe_relative_to(path, workspace_root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            score = _line_score(line, rel, terms)
            if score <= 0:
                continue
            hits.append((score, {"path": rel, "line_number": line_number, "line": line.strip()}))
            if len(hits) >= _MAX_QUERY_HITS:
                truncated = True
                break
        if truncated:
            break

    coverage.record_pass("query", walk.files_considered, walk.hit_limit)

    if not hits:
        body_lines = ["(no query hits found)"]
        caveat = coverage.no_hit_caveat("the query scan")
        return (
            ContextPackSection("Query Hits", body_lines, caveat=caveat)
            if caveat
            else ContextPackSection("Query Hits", body_lines),
            False,
            False,
        )

    hits.sort(key=lambda item: (-item[0], item[1]["path"], item[1]["line_number"]))
    body_lines = [
        f"{hit['path']}:{hit['line_number']}: {hit['line']}"
        for _, hit in hits[:_MAX_QUERY_HITS]
    ]
    return ContextPackSection("Query Hits", body_lines), truncated, True


def _symbol_context_section(
    workspace_root: Path, symbols: list[str], coverage: _Coverage
) -> tuple[ContextPackSection, bool, list[str]]:
    """Return the symbol section, whether hits were truncated, and the misses."""
    body_lines: list[str] = []
    truncated = False
    missing: list[str] = []
    for symbol in symbols:
        pattern = re.compile(rf"\b{re.escape(symbol)}\b", re.IGNORECASE)
        body_lines.append(f"{symbol}:")
        count = 0
        walk = _CandidateWalk(workspace_root, coverage.limit)
        for path in walk:
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
        coverage.record_pass(f"symbol:{symbol}", walk.files_considered, walk.hit_limit)
        if count == 0:
            missing.append(symbol)
            body_lines.append("  (no hits found)")
            if coverage.partial:
                body_lines.append(
                    "  (coverage was partial — this is not evidence of absence)"
                )
    caveat = coverage.no_hit_caveat("the symbol scan") if missing else None
    section = (
        ContextPackSection("Symbol Hits", body_lines, caveat=caveat)
        if caveat
        else ContextPackSection("Symbol Hits", body_lines)
    )
    return section, truncated, missing


class _CandidateWalk:
    """One bounded walk over workspace text files that reports its own coverage.

    Iterating yields resolved file paths and updates ``files_considered`` /
    ``hit_limit`` as it goes, so a caller that stops early still knows how much
    of the workspace was actually reachable within the cap.
    """

    def __init__(self, workspace_root: Path, limit: int = CANDIDATE_FILE_LIMIT) -> None:
        self.workspace_root = workspace_root
        self.limit = limit
        self.files_considered = 0
        self.hit_limit = False

    def __iter__(self) -> Iterator[Path]:
        for path in _iter_text_candidates(self.workspace_root, max_files=self.limit):
            self.files_considered += 1
            if self.files_considered >= self.limit:
                self.hit_limit = True
            yield path


def _iter_text_candidates(
    workspace_root: Path, *, max_files: int = CANDIDATE_FILE_LIMIT
):
    yielded = 0
    root = workspace_root.resolve()
    stack = [root]

    while stack and yielded < max_files:
        directory = stack.pop()
        child_dirs: list[Path] = []
        try:
            with os.scandir(directory) as scanner:
                entries = sorted(
                    scanner,
                    key=lambda entry: entry.name.lower(),
                )
                for entry in entries:
                    entry_path = Path(entry.path)
                    try:
                        resolved = entry_path.resolve()
                    except OSError:
                        continue
                    if not safe_is_relative_to(resolved, root):
                        continue

                    rel = safe_relative_to(resolved, root)
                    if entry.is_dir(follow_symlinks=False):
                        if _should_skip_dir(rel):
                            continue
                        child_dirs.append(resolved)
                        continue

                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if _should_skip_file(rel):
                        continue

                    yield resolved
                    yielded += 1
                    if yielded >= max_files:
                        break
        except OSError:
            continue

        stack.extend(reversed(child_dirs))


def _should_skip_dir(rel_path: Path) -> bool:
    if any(part in SKIP_DIRS or part.startswith(".") for part in rel_path.parts):
        return True
    return False


def _should_skip_file(rel_path: Path) -> bool:
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
