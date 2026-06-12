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
        result = _grep_ripgrep(
            workspace_root,
            pattern,
            regex_mode,
            case_sensitive,
            max_results,
            include_pattern,
            rg_path=rg_path,
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
            rg_path=rg_path,
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


def _maybe_retry_as_regex(
    result: dict[str, Any],
    grep_fn: Any,
    workspace_root: Path,
    pattern: str,
    regex_mode: bool,
    case_sensitive: bool,
    max_results: int,
    include_pattern: str | None,
    **kwargs: Any,
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
        **kwargs,
    )
    if retry.get("ok"):
        retry["auto_regex_retry"] = True
        retry["summary"] = _build_summary(retry)
        return retry

    result["auto_regex_retry"] = True
    result["regex_retry_error"] = retry.get("error")
    result["summary"] = _build_summary(result)
    return result


def _collect_candidate_files(workspace_root: Path, include_pattern: str | None) -> tuple[list[Path], int]:
    candidates: list[Path] = []
    skipped_files = 0

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
    return candidates, skipped_files


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
    candidates, skipped_files = _collect_candidate_files(root, include)

    cmd = [exe, "--json", "--column", "--hidden"]
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

        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            if data.get("type") != "match":
                continue

            if len(matches) >= max_results:
                truncated = True
                continue

            match_data = data["data"]
            raw_match_path = match_data["path"]["text"]
            abs_match_path = Path(raw_match_path)
            if not abs_match_path.is_absolute():
                abs_match_path = (root / abs_match_path).resolve()
            else:
                abs_match_path = abs_match_path.resolve()

            try:
                rel_path = Path(os.path.relpath(abs_match_path, root_resolved)).as_posix()
            except Exception:
                rel_path = raw_match_path

            matches.append({
                "path": rel_path,
                "line_number": match_data["line_number"],
                "line": match_data["lines"]["text"].strip(),
                "match_column": match_data["submatches"][0]["start"],
            })

        return _build_result(
            matches=matches,
            engine="ripgrep",
            searched_files=len(candidates),
            skipped_files=skipped_files,
            skipped_details=[],
            truncated=truncated,
            regex_mode=regex,
            include_pattern=include,
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
    candidates, skipped_files = _collect_candidate_files(workspace_root, include_pattern)
    searched_files = 0
    skipped_details: list[dict[str, Any]] = []

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
    )
