"""Worker validation scratch cleanup helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aura.conversation.path_utils import (
    is_validation_scratch_path,
    normalize_worker_path,
)

_log = logging.getLogger(__name__)


def _validation_scratch_files(workspace_root: Path) -> set[Path]:
    """Return current root-level validation scratch files under *workspace_root*."""
    return _root_check_files(workspace_root)


def _cleanup_new_validation_scratch_files(
    workspace_root: Path,
    scratch_before: set[Path],
) -> list[str]:
    """Delete Worker-created root validation scratch files and return rel paths."""
    before = {path.resolve() for path in scratch_before}
    cleaned: list[str] = []
    for path in sorted(_root_check_files(workspace_root)):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in before:
            continue
        try:
            path.unlink()
        except OSError:
            _log.warning("Failed to clean Worker validation scratch file: %s", path)
            continue
        cleaned.append(path.name)
    return cleaned


def _root_check_files(workspace_root: Path) -> set[Path]:
    if not workspace_root.exists():
        return set()
    return {
        path
        for path in workspace_root.iterdir()
        if path.is_file() and _is_root_validation_scratch_path(path.name)
    }


def _request_allows_root_check_files(req: Any) -> bool:
    """Return True when the dispatch explicitly targets root scratch files."""
    paths: list[str] = []
    paths.extend(str(path) for path in (getattr(req, "files", []) or []))
    for region in getattr(req, "target_regions", []) or []:
        if isinstance(region, dict):
            paths.append(str(region.get("path") or ""))
    return any(_is_root_validation_scratch_path(path) for path in paths)


def _is_root_validation_scratch_path(path: str) -> bool:
    normalized = normalize_worker_path(path)
    return bool(normalized and "/" not in normalized and is_validation_scratch_path(normalized))


__all__ = [
    "_cleanup_new_validation_scratch_files",
    "_request_allows_root_check_files",
    "_root_check_files",
    "_validation_scratch_files",
]
