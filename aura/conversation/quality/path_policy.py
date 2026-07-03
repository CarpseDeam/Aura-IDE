from __future__ import annotations

from pathlib import PurePosixPath

_IGNORED_PATH_PARTS = frozenset({
    ".github",
    "doc",
    "docs",
    "scratch",
    "test",
    "tests",
    "tmp",
})
_DOC_SUFFIXES = frozenset({".adoc", ".md", ".rst", ".txt"})


def normalize_path(path: str) -> str:
    return str(path).replace("\\", "/").lstrip("/")


def normalize_changed_files(changed_files: list[str]) -> list[str]:
    return sorted({
        normalize_path(path)
        for path in changed_files
        if str(path).strip()
    })


def is_ignored_quality_path(path: str) -> bool:
    normalized = normalize_path(path)
    posix = PurePosixPath(normalized)
    parts = {part.lower() for part in posix.parts}
    if parts & _IGNORED_PATH_PARTS:
        return True
    return posix.suffix.lower() in _DOC_SUFFIXES


def is_production_path(path: str) -> bool:
    return bool(normalize_path(path)) and not is_ignored_quality_path(path)
