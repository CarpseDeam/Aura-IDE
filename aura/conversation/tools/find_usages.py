"""find_usages — find all occurrences of a symbol in the workspace (word-boundary based)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from aura.conversation.tools.grep import grep_files


def find_usages(
    workspace_root: Path,
    symbol: str,
    include_pattern: str | None = None,
    max_results: int = 100,
    case_sensitive: bool = False,
    cancel_event: Any | None = None,
) -> dict[str, Any]:
    """Find all usages of *symbol* across the workspace.

    Uses word-boundary regex (``\\b{symbol}\\b``) so that searching for
    ``count_items`` won't match ``recount_items`` or ``count_items_count``.

    Returns the same shape as grep_files::
        {"ok": bool, "matches": list, "truncated": bool, "symbol": str, ...}
    """
    if not symbol or not symbol.strip():
        return {"ok": False, "error": "symbol is required"}

    # Escape the symbol for regex, then wrap in word boundaries
    escaped = re.escape(symbol.strip())
    word_boundary_pattern = f"\\b{escaped}\\b"

    return grep_files(
        workspace_root=workspace_root,
        pattern=word_boundary_pattern,
        regex_mode=True,
        case_sensitive=case_sensitive,
        max_results=max_results,
        include_pattern=include_pattern,
        cancel_event=cancel_event,
    )
