"""Write/mutation tracking helpers for WorkerEventRelay.

Constants and pure functions for detecting file-mutation tools,
and extracting paths from tool results.
"""

from __future__ import annotations

from typing import Any

from aura.conversation.tool_limits import WRITE_TOOLS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TERMINAL_OUTPUT_CAPTURE_CHARS = 4000
TERMINAL_OUTPUT_PREVIEW_CHARS = 200

LEGACY_EDIT_TOOLS = frozenset(
    {
        "edit_file",
        "edit_symbol",
        "edit_line_range",
        "apply_edit_transaction",
    }
)
FILE_MUTATION_TOOLS = frozenset(WRITE_TOOLS) | LEGACY_EDIT_TOOLS

PATH_FIELDS = ("path", "rel_path", "file", "filename", "target_path")

# ---------------------------------------------------------------------------
# Path extraction from tool payloads / results / extras
# ---------------------------------------------------------------------------


def _result_path(parsed: dict[str, Any], extras: dict[str, Any]) -> str:
    for source in (parsed, extras):
        for field in PATH_FIELDS:
            value = source.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


# ---------------------------------------------------------------------------
# File-mutation tool detection
# ---------------------------------------------------------------------------


def _is_file_mutation_tool(name: str) -> bool:
    return name in FILE_MUTATION_TOOLS


def _file_mutation_was_applied(
    name: str, ok: bool, parsed: Any, extras: dict[str, Any]
) -> bool:
    if not _is_file_mutation_tool(name):
        return False
    if ok is False or not isinstance(parsed, dict):
        return False
    if parsed.get("applied") is True:
        return bool(_result_path(parsed, extras))
    if parsed.get("applied") is False:
        return False
    return parsed.get("ok") is True and bool(_result_path(parsed, extras))
