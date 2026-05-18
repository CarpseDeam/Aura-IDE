"""grep_search — search file contents across the workspace."""
from __future__ import annotations

import re
import subprocess
import shutil
from pathlib import Path
from typing import Any

from aura.config import SKIP_DIRS, SKIP_FILE_SUFFIXES, get_subprocess_kwargs


def _should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts & SKIP_DIRS:
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
    
    Uses 'ripgrep' (rg) if installed, otherwise falls back to sequential Python glob.
    """
    if not pattern:
        return {"ok": False, "error": "pattern is required"}

    rg_path = shutil.which("rg")
    if rg_path:
        result = _grep_ripgrep(
            workspace_root,
            pattern,
            regex_mode,
            case_sensitive,
            max_results,
            include_pattern,
        )
        return _maybe_retry_as_regex(
            result,
            _grep_ripgrep,
            workspace_root,
            pattern,
            regex_mode,
            case_sensitive,
            max_results,
            include_pattern,
        )

    result = _grep_python(
        workspace_root,
        pattern,
        regex_mode,
        case_sensitive,
        max_results,
        include_pattern,
    )
    return _maybe_retry_as_regex(
        result,
        _grep_python,
        workspace_root,
        pattern,
        regex_mode,
        case_sensitive,
        max_results,
        include_pattern,
    )


def _looks_like_regex(pattern: str) -> bool:
    """Return true for regex syntax models commonly emit by accident."""
    regex_tokens = ("|", r"\b", r"\d", r"\s", r"\w", ".*", ".+", "[", "(?")
    return any(token in pattern for token in regex_tokens)


def _maybe_retry_as_regex(
    result: dict[str, Any],
    grep_fn: Any,
    workspace_root: Path,
    pattern: str,
    regex_mode: bool,
    case_sensitive: bool,
    max_results: int,
    include_pattern: str | None,
) -> dict[str, Any]:
    if regex_mode or not result.get("ok") or result.get("matches") or not _looks_like_regex(pattern):
        return result

    retry = grep_fn(
        workspace_root,
        pattern,
        True,
        case_sensitive,
        max_results,
        include_pattern,
    )
    if retry.get("ok") and retry.get("matches"):
        retry["auto_regex_retry"] = True
        retry["original_regex_mode"] = False
        return retry
    return result


def _grep_ripgrep(
    root: Path,
    pattern: str,
    regex: bool,
    case_sensitive: bool,
    max_results: int,
    include: str | None
) -> dict[str, Any]:
    cmd = ["rg", "--json", "--column", "--max-count", str(max_results)]
    
    if not regex:
        cmd.append("--fixed-strings")
    if not case_sensitive:
        cmd.append("--ignore-case")
    
    if include:
        cmd.extend(["--glob", include])
    
    # Exclude common junk
    for skip in SKIP_DIRS:
        cmd.extend(["--glob", f"!{skip}/*"])
    for suffix in SKIP_FILE_SUFFIXES:
        cmd.extend(["--glob", f"!*{suffix}"])
    
    cmd.append(pattern)
    cmd.append(str(root))

    try:
        # ripgrep returns 1 if no matches found, 0 if matches found.
        # We handle this manually.
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            **get_subprocess_kwargs(),
        )
        if proc.returncode not in (0, 1):
            return {"ok": False, "error": proc.stderr or f"ripgrep failed with code {proc.returncode}"}

        import json
        matches = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            if data.get("type") == "match":
                m_data = data["data"]
                # Resolve the path from rg (which might be relative to where it ran)
                # to ensure it's absolute, then make it relative to our root.
                raw_match_path = m_data["path"]["text"]
                abs_match_path = (root / raw_match_path).resolve()
                try:
                    rel_path = abs_match_path.relative_to(root.resolve()).as_posix()
                except ValueError:
                    # Fallback if relative_to fails for some reason
                    rel_path = raw_match_path
                
                matches.append({
                    "path": rel_path,
                    "line_number": m_data["line_number"],
                    "line": m_data["lines"]["text"].strip(),
                    "match_column": m_data["submatches"][0]["start"],
                })
                if len(matches) >= max_results:
                    break

        return {
            "ok": True,
            "matches": matches,
            "truncated": len(matches) >= max_results,
            "engine": "ripgrep"
        }
    except Exception as exc:
        return {"ok": False, "error": f"ripgrep error: {exc}"}


def _grep_python(
    workspace_root: Path,
    pattern: str,
    regex_mode: bool,
    case_sensitive: bool,
    max_results: int,
    include_pattern: str | None,
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
    
    # Import locally to avoid circular dependency if config changes
    from aura.config import MAX_READ_BYTES

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
            # Prevent hanging on massive binary/log files during search fallback
            file_size = file_path.stat().st_size
            if file_size > MAX_READ_BYTES:
                # Skip massive files in pure python grep to avoid memory explosion
                continue
                
            with open(file_path, "rb") as f:
                raw = f.read()
                
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
