"""Read-only Reference Folder tool schema."""
from __future__ import annotations

from typing import Any

READ_REFERENCE_FILE_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_reference_file",
        "description": (
            "Read a UTF-8 text file from the single user-attached read-only Reference Folder — "
            "an external folder distinct from the active workspace. The path argument is relative "
            "to that Reference Folder, not the workspace. By default returns the full contents "
            "(capped at 200KB); pass offset/limit to read a specific window of lines instead. "
            "Nothing in the Reference Folder can be modified or executed through this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Reference-Folder-relative path, e.g. 'src/auth.py'.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Optional first line to read (1-based, inclusive).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional number of lines to read starting at offset.",
                },
            },
            "required": ["path"],
        },
    },
}
