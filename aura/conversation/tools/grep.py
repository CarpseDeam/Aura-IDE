"""grep_search — search file contents across the workspace."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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


def _safe_relative_to(path: Path, root: Path) -> Path:
    """Safely compute relative path, handling Windows case-insensitivity."""
    from aura.paths import safe_relative_to
    return safe_relative_to(path, root)


def grep_files(
    workspace_root: Path,
    pattern: str,
    regex_mode: bool = False,
    case_sensitive: bool = False,
    max_results: int = 50,
    include_pattern: str | None = None,
) -> dict[str, Any]:
    """Search file contents under workspace_root for the given pattern."""
    if not pattern:
        return {"ok": False, "error": "pattern is required"}

    rg_path = shutil.which("rg")
    if rg_path:
        return _grep_ripgrep(
            workspace_root,
            pattern,
            regex_mode,
            case_sensitive,
            max_results,
            include_pattern,
            rg_path=rg_path,
        )

    return _grep_python(
        workspace_root,
        pattern,
        regex_mode,
        case_sensitive,
        max_results,
        include_pattern,
    )


def _looks_like_regex(pattern: str) -> bool:
    """Return true when the pattern contains obvious regex syntax."""
    if "(?" in pattern:
        return True
    if re.search(r"\\(?:[(){}\[\].+*?|^$]|[swdb])", pattern):
        return True
    return re.search(r"(?<!\\)(?:\^|\$|\*|\+|\?|\(|\)|\{|\}|\[|\]|\|)", pattern) is not None


def _build_result(
    *,
    matches: list[dict[str, Any]],
    engine: str,
    searched_files: int,
    skipped_files: int,
    regex_mode: bool,
    include_pattern: str | None,
    auto_regex_retry: bool = False,
    truncated: bool = False,
    skipped_details: list[dict[str, Any]] | None = None,
    regex_hint: str | None = None,
) -> dict[str, Any]:
    result = {
        "ok": True,
        "matches": matches,
        "engine": engine,
        "searched_files": searched_files,
        "skipped_files": skipped_files,
        "skipped_details": skipped_details or [],
        "truncated": truncated,
        "regex_mode": regex_mode,
        "auto_regex_retry": auto_regex_retry,
        "include_pattern": include_pattern,
    }
    if regex_hint:
        result["regex_hint"] = regex_hint
    result["summary"] = _build_summary(result)
    return result


def _build_summary(result: dict[str, Any]) -> str:
    matches = len(result.get("matches", []))
    state = "found matches" if matches else "found no matches"
    include_pattern = result.get("include_pattern")
    include_note = f" include_pattern={include_pattern!r}." if include_pattern else ""
    return (
        f"{result.get('engine', 'search')} {state}. "
        f"searched_files={result.get('searched_files', 0)}, "
        f"skipped_files={result.get('skipped_files', 0)}, "
        f"regex_mode={result.get('regex_mode', False)}, "
        f"auto_regex_retry={result.get('auto_regex_retry', False)}, "
        f"truncated={result.get('truncated', False)}."
        f"{include_note}"
    )


def _regex_hint(pattern: str, regex_mode: bool, matches: list[dict[str, Any]]) -> str | None:
    if regex_mode or matches or not _looks_like_regex(pattern):
        return None
    return "Pattern looks like regex syntax but regex_mode is false; search was treated as literal text."


def _rg_path_text_to_rel(raw_path: str, root: Path, root_resolved: Path) -> str:
    abs_match_path = Path(raw_path)
    if not abs_match_path.is_absolute():
        abs_match_path = (root / abs_match_path).resolve()
    else:
        abs_match_path = abs_match_path.resolve()

    try:
        return Path(os.path.relpath(abs_match_path, root_resolved)).as_posix()
    except Exception:
        return raw_path


def _grep_ripgrep(
    root: Path,
    pattern: str,
    regex: bool,
    case_sensitive: bool,
    max_results: int,
    include: str | None,
    rg_path: str | None = None,
) -> dict[str, Any]:
    exe = rg_path or shutil.which("rg") or "rg"

    cmd = [exe, "--json", "--column", "--hidden", "--no-ignore"]
    if not regex:
        cmd.append("--fixed-strings")
    if not case_sensitive:
        cmd.append("--ignore-case")
    if include:
        cmd.extend(["--glob", include])
    for skip in sorted(SKIP_DIRS):
        cmd.extend(["--glob", f"!{skip}/"])
        cmd.extend(["--glob", f"!{skip}/*"])
    for suffix in sorted(SKIP_FILE_SUFFIXES):
        cmd.extend(["--glob", f"!*{suffix}"])
    cmd.extend(["--", pattern, str(root)])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            **get_subprocess_kwargs(),
        )
        if proc.returncode not in (0, 1):
            return {"ok": False, "error": proc.stderr or f"ripgrep failed with code {proc.returncode}"}

        matches: list[dict[str, Any]] = []
        truncated = False
        root_resolved = root.resolve()
        searched_paths: set[str] = set()
        summary_searches: int | None = None

        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = data.get("type")
            if event_type == "begin":
                path_text = data.get("data", {}).get("path", {}).get("text")
                if path_text:
                    searched_paths.add(_rg_path_text_to_rel(path_text, root, root_resolved))
                continue

            if event_type == "summary":
                searches = data.get("data", {}).get("stats", {}).get("searches")
                if isinstance(searches, int):
                    summary_searches = searches
                continue

            if event_type != "match":
                continue

            match_data = data["data"]
            raw_match_path = match_data["path"]["text"]
            rel_path = _rg_path_text_to_rel(raw_match_path, root, root_resolved)
            searched_paths.add(rel_path)

            if len(matches) >= max_results:
                truncated = True
                continue

            matches.append({
                "path": rel_path,
                "line_number": match_data["line_number"],
                "line": match_data["lines"]["text"].strip(),
                "match_column": match_data["submatches"][0]["start"],
            })

        searched_files = summary_searches if summary_searches is not None else len(searched_paths)
        return _build_result(
            matches=matches,
            engine="ripgrep",
            searched_files=searched_files,
            skipped_files=0,
            skipped_details=[],
            truncated=truncated,
            regex_mode=regex,
            include_pattern=include,
            regex_hint=_regex_hint(pattern, regex, matches),
        )
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
    """Search file contents under workspace_root for the given pattern."""
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
    searched_files = 0
    skipped_files = 0
    skipped_details: list[dict[str, Any]] = []
    candidates: list[Path] = []

    if include_pattern:
        for file_path in workspace_root.rglob(include_pattern):
            if not file_path.is_file():
                continue
            rel_path = _safe_relative_to(file_path, workspace_root)
            if _should_skip(rel_path):
                skipped_files += 1
                continue
            candidates.append(file_path)
    else:
        for root_dir, dirs, files in os.walk(workspace_root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for filename in sorted(files):
                file_path = Path(root_dir) / filename
                rel_path = _safe_relative_to(file_path, workspace_root)
                if _should_skip(rel_path):
                    skipped_files += 1
                    continue
                candidates.append(file_path)

    candidates.sort(key=lambda path: _safe_relative_to(path, workspace_root).as_posix())

    for file_path in candidates:
        rel = _safe_relative_to(file_path, workspace_root).as_posix()
        try:
            with open(file_path, "r", encoding="utf-8", errors="strict") as f:
                for line_num, raw_line in enumerate(f, start=1):
                    line = raw_line.rstrip("\n").rstrip("\r")
                    if compiled is not None:
                        match = compiled.search(line)
                        if match:
                            if len(matches) >= max_results:
                                return _build_result(
                                    matches=matches,
                                    engine="python",
                                    searched_files=searched_files + 1,
                                    skipped_files=skipped_files,
                                    skipped_details=skipped_details,
                                    truncated=True,
                                    regex_mode=regex_mode,
                                    include_pattern=include_pattern,
                                    regex_hint=_regex_hint(pattern, regex_mode, matches),
                                )
                            matches.append({
                                "path": rel,
                                "line_number": line_num,
                                "line": line.strip(),
                                "match_column": match.start(),
                            })
                    else:
                        search_line = line if case_sensitive else line.lower()
                        search_pattern = pattern if case_sensitive else pattern.lower()
                        col = search_line.find(search_pattern)
                        if col != -1:
                            if len(matches) >= max_results:
                                return _build_result(
                                    matches=matches,
                                    engine="python",
                                    searched_files=searched_files + 1,
                                    skipped_files=skipped_files,
                                    skipped_details=skipped_details,
                                    truncated=True,
                                    regex_mode=regex_mode,
                                    include_pattern=include_pattern,
                                    regex_hint=_regex_hint(pattern, regex_mode, matches),
                                )
                            matches.append({
                                "path": rel,
                                "line_number": line_num,
                                "line": line.strip(),
                                "match_column": col,
                            })
            searched_files += 1

        except UnicodeDecodeError:
            skipped_files += 1
            skipped_details.append({"path": rel, "reason": "binary or non-UTF-8 encoding"})
        except PermissionError:
            skipped_files += 1
            skipped_details.append({"path": rel, "reason": "permission denied"})
        except OSError:
            skipped_files += 1
            skipped_details.append({"path": rel, "reason": "read error"})

    return _build_result(
        matches=matches,
        engine="python",
        searched_files=searched_files,
        skipped_files=skipped_files,
        skipped_details=skipped_details,
        truncated=False,
        regex_mode=regex_mode,
        include_pattern=include_pattern,
        regex_hint=_regex_hint(pattern, regex_mode, matches),
    )
