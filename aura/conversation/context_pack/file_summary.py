"""Read-only target-file summaries for the Worker Context Pack."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from aura.code_intel.adapter import get_adapter
from aura.conversation.context_pack.models import ContextPackSection

logger = logging.getLogger(__name__)

_SMALL_FILE_MAX_LINES = 50
_SMALL_FILE_MAX_BYTES = 2048


def summarize_file(workspace_root: Path, rel_path: str) -> ContextPackSection:
    """Build a ``ContextPackSection`` summarising a single target file.

    The section includes file stats (size, line count), an outline from the
    code-intel adapter, and a full-content snippet for small files.
    """
    full_path = workspace_root / rel_path

    if not full_path.exists():
        return ContextPackSection(
            heading=f"File: {rel_path}",
            body_lines=["(file not found)"],
            caveat="(missing)",
        )

    try:
        file_size = os.path.getsize(full_path)
    except OSError:
        file_size = 0

    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return ContextPackSection(
            heading=f"File: {rel_path}",
            body_lines=[f"(unreadable: {exc})"],
            caveat="(unreadable)",
        )

    line_count = content.count("\n")
    if content and not content.endswith("\n"):
        line_count += 1

    body_lines: list[str] = []
    body_lines.append(f"Size: {file_size} bytes, {line_count} lines")

    # Small-file snippet
    is_small = line_count < _SMALL_FILE_MAX_LINES and file_size < _SMALL_FILE_MAX_BYTES
    if is_small and content.strip():
        body_lines.append("")
        body_lines.append("Snippet:")
        body_lines.append(content.rstrip("\n"))

    # Code-intel outline
    adapter = get_adapter(rel_path, content=content)
    if adapter is not None:
        try:
            outline: dict[str, Any] = adapter.outline(rel_path, content)
            _append_outline(body_lines, outline)
        except Exception as exc:
            logger.debug("Outline failed for %s: %s", rel_path, exc)
            body_lines.append("")
            body_lines.append("Outline: (unavailable)")
            return ContextPackSection(
                heading=f"File: {rel_path}",
                body_lines=body_lines,
                caveat="outline unavailable",
            )
    else:
        body_lines.append("")
        body_lines.append("Outline: (no adapter)")

    return ContextPackSection(
        heading=f"File: {rel_path}",
        body_lines=body_lines,
    )


def _append_outline(body_lines: list[str], outline: dict[str, Any]) -> None:
    """Append outline information to *body_lines*."""
    body_lines.append("")
    body_lines.append("Outline:")

    imports: list[str] = outline.get("imports", [])
    if imports:
        body_lines.append("  Imports:")
        for imp in imports:
            body_lines.append(f"    {imp}")

    classes: list[dict[str, Any]] = outline.get("classes", [])
    if classes:
        body_lines.append("  Classes:")
        for cls in classes:
            bases = cls.get("bases", [])
            base_str = f"({', '.join(bases)})" if bases else ""
            body_lines.append(f"    {cls['name']}{base_str}")
            for method in cls.get("methods", []):
                body_lines.append(f"      {method}")

    functions: list[dict[str, Any]] = outline.get("functions", [])
    if functions:
        body_lines.append("  Functions:")
        for func in functions:
            sig = func.get("signature", func["name"])
            body_lines.append(f"    {sig}")
