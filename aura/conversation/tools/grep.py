"""grep_search — search file contents across the workspace."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from aura.config import MAX_GLOB_RESULTS, SKIP_DIRS, SKIP_FILE_SUFFIXES


def _should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts & SKIP_DIRS:
        return True
    if path.name.startswith("."):
        return True
    if path.suffix in SKIP_FILE_SUFFIXES:
        return True
    return False


def grep_files(
    workspace_root: Path,
    pattern: str,
    regex_mode: bool = False,
    case_sensitive: bool = False,
    max_results: int = 50,
    include_pattern: str | None = None,
) -> dict[str, Any]:
    """Search file contents under workspace_root for the given pattern.

    Returns a dict with keys:
      - ok: bool
      - matches: list of {path, line_number, line, match_column}
      - truncated: whether max_results was hit
      - error (if any)
    """
    if not pattern:
        return {"ok": False, "error": "pattern is required"}

    try:
        if regex_mode:
            flags = 0 if case_sensitive else re.IGNORECASE
            compiled = re.compile(pattern, flags)
        else:
            compiled = None
    except re.error as exc:
        return {"ok": False, "error": f"invalid regex: {exc}"}

    matches: list[dict[str, Any]] = []

    # Collect candidate files via rglob with optional include_pattern filter
    candidates: list[Path] = []
    for p in workspace_root.rglob(include_pattern or "*"):
        if _should_skip(p.relative_to(workspace_root)):
            continue
        if p.is_file():
            candidates.append(p)

    for file_path in candidates:
        if len(matches) >= max_results:
            break
        rel = file_path.relative_to(workspace_root).as_posix()
        try:
            raw = file_path.read_bytes()
            # Try UTF-8, fall back to latin-1 for binary-ish files
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                # Skip files that aren't valid UTF-8 or latin-1 text
                try:
                    text = raw.decode("latin-1")
                except UnicodeDecodeError:
                    continue
        except (OSError, PermissionError):
            continue

        for line_num, line in enumerate(text.splitlines(), start=1):
            if len(matches) >= max_results:
                break
            if compiled is not None:
                m = compiled.search(line)
                if m:
                    matches.append({
                        "path": rel,
                        "line_number": line_num,
                        "line": line.strip(),
                        "match_column": m.start(),
                    })
            else:
                # Plain substring search
                search_line = line if case_sensitive else line.lower()
                search_pattern = pattern if case_sensitive else pattern.lower()
                col = search_line.find(search_pattern)
                if col != -1:
                    matches.append({
                        "path": rel,
                        "line_number": line_num,
                        "line": line.strip(),
                        "match_column": col,
                    })

    return {
        "ok": True,
        "matches": matches,
        "truncated": len(matches) >= max_results,
        "pattern": pattern,
        "regex_mode": regex_mode,
        "case_sensitive": case_sensitive,
    }
