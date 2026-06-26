"""Cheap likely test-file hints by path/name convention only."""

from __future__ import annotations

from pathlib import Path

from aura.conversation.context_pack.models import ContextPackSection


def find_test_hints(workspace_root: Path, files: list[str]) -> ContextPackSection:
    """For each target file, compute likely test file paths by convention.

    Checks the following conventions for each file:
    - ``tests/test_{stem}.py``
    - ``tests/{stem}_test.py``
    - ``tests/test_{stem}.{ext}`` (same extension)
    - Same-directory ``test_{stem}.py``
    """
    found: list[str] = []

    for rel_path in files:
        p = Path(rel_path)
        stem = p.stem
        ext = p.suffix.lstrip(".")

        candidates: list[str] = []

        # tests/test_{stem}.py
        candidates.append(f"tests/test_{stem}.py")
        # tests/{stem}_test.py
        candidates.append(f"tests/{stem}_test.py")
        # tests/test_{stem}.{ext}
        if ext:
            candidates.append(f"tests/test_{stem}.{ext}")
        # Same-directory test_{stem}.py
        parent = str(p.parent) if p.parent and str(p.parent) != "." else ""
        if parent:
            candidates.append(f"{parent}/test_{stem}.py")
        else:
            candidates.append(f"test_{stem}.py")

        for candidate in candidates:
            if (workspace_root / candidate).exists():
                if candidate not in found:
                    found.append(candidate)

    if not found:
        return ContextPackSection(
            heading="Test Hints",
            body_lines=["(no test file hints found by convention)"],
        )

    return ContextPackSection(
        heading="Test Hints",
        body_lines=found,
    )
