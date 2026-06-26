"""Thin wrappers around existing dependency helpers for the Worker Context Pack."""

from __future__ import annotations

import logging
from pathlib import Path

from aura.code_intel.adapter import get_adapter
from aura.conversation.context_pack.models import ContextPackSection

logger = logging.getLogger(__name__)


def find_dependency_hints(workspace_root: Path, files: list[str]) -> ContextPackSection:
    """For each target file, collect dependency and reference hints.

    Uses the code-intel adapter for each file to extract:
    - **Dependencies**: workspace-relative paths this file imports.
    - **References**: workspace-relative paths that reference symbols from
      this file (dependents).

    This is best-effort; failures produce a compact caveat and continue.
    """
    body_lines: list[str] = []
    caveats: list[str] = []

    for rel_path in files:
        full_path = workspace_root / rel_path
        if not full_path.exists():
            caveats.append(f"{rel_path}: (missing)")
            continue

        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            caveats.append(f"{rel_path}: (unreadable: {exc})")
            continue

        adapter = get_adapter(rel_path, content=content)
        if adapter is None:
            caveats.append(f"{rel_path}: (no adapter)")
            continue

        # Dependencies
        try:
            deps = adapter.dependencies(rel_path, content)
        except Exception as exc:
            logger.debug("dependencies() failed for %s: %s", rel_path, exc)
            deps = []
            caveats.append(f"{rel_path}: dependencies unavailable")

        # References (dependents)
        try:
            refs = adapter.references(rel_path, content)
        except Exception as exc:
            logger.debug("references() failed for %s: %s", rel_path, exc)
            refs = []
            caveats.append(f"{rel_path}: references unavailable")

        if deps or refs:
            body_lines.append(f"  {rel_path}:")
            if deps:
                body_lines.append("    Dependencies:")
                for dep in deps:
                    body_lines.append(f"      {dep}")
            if refs:
                body_lines.append("    References:")
                for ref in refs:
                    body_lines.append(f"      {ref}")

    if not body_lines and not caveats:
        return ContextPackSection(
            heading="Dependency Hints",
            body_lines=["(no dependency information available)"],
        )

    if not body_lines:
        body_lines = ["(no dependency information available)"]

    return ContextPackSection(
        heading="Dependency Hints",
        body_lines=body_lines,
        caveat="; ".join(caveats) if caveats else None,
    )
