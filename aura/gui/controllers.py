from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QObject, Signal


class ToolStreamController(QObject):
    """
    Controller that manages the lifecycle of a tool call's streaming arguments.
    It sits between the bridge and the UI, handling buffering and parsing.

    Note: this controller only buffers and pretty-prints raw arguments for
    generic argument/log display. It never extracts a file path or file
    content from partial JSON -- the workspace editor's authoritative content
    comes exclusively from the file-edit lifecycle projection
    (``aura.gui.editor.file_edit_projection``), never from here.
    """

    # Emitted whenever arguments are updated (pretty-printed if possible)
    args_updated = Signal(str)
    # Emitted when the tool state changes ("running", "done", "failed")
    state_changed = Signal(str)
    # Emitted when the tool call is finished with the full result
    result_finalized = Signal(dict)
    # Emitted when the tool call is finished with a formatted result string
    result_finalized_text = Signal(str)

    def __init__(self, tool_name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tool_name = tool_name
        self._buffer = ""
        self._state = "running"

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def buffer(self) -> str:
        return self._buffer

    def append_fragment(self, fragment: str) -> None:
        """Append a fragment of JSON arguments and emit a display-only update."""
        self._buffer += fragment

        try:
            parsed = json.loads(self._buffer)
            if not isinstance(parsed, dict):
                return
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            self.args_updated.emit(pretty)
        except json.JSONDecodeError:
            # Buffer is still incomplete JSON — emit raw buffer for now
            self.args_updated.emit(self._buffer)

    def finalize(self, ok: bool, result_text: str) -> None:
        """Finalize the tool call with the result."""
        self._state = "done" if ok else "failed"
        self.state_changed.emit(self._state)

        result_dict: dict[str, Any] = {}
        formatted_result = result_text
        try:
            result_dict = json.loads(result_text)
            if isinstance(result_dict, dict):
                formatted_result = json.dumps(result_dict, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            result_dict = {"raw_result": result_text}

        self.result_finalized.emit(result_dict)
        self.result_finalized_text.emit(formatted_result)
