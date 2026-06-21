"""Pure filesystem probe for measuring file sizes under aura/."""
from __future__ import annotations

from pathlib import Path


class ProbeFinding:
    __slots__ = ("path", "line_count")

    def __init__(self, path: str, line_count: int) -> None:
        self.path = path
        self.line_count = line_count


def probe_file_sizes(workspace_root: Path, budget: int) -> list[ProbeFinding]:
    """Walk aura/ for .py files and return those exceeding the line budget."""
    aura_dir = workspace_root / "aura"
    if not aura_dir.is_dir():
        return []
    try:
        paths = sorted(aura_dir.rglob("*.py"))
    except (OSError, PermissionError):
        return []

    _skip_dirs = frozenset({".aura", "__pycache__", "test", "tests"})
    findings: list[ProbeFinding] = []

    for path in paths:
        try:
            rel = path.relative_to(workspace_root)
        except ValueError:
            continue
        # Check parent directories only (exclude the filename itself)
        if any(part in _skip_dirs for part in rel.parts[:-1]):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        line_count = text.count("\n")
        if line_count > budget:
            findings.append(ProbeFinding(rel.as_posix(), line_count))

    findings.sort(key=lambda f: f.line_count, reverse=True)
    return findings
