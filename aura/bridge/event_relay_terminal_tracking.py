"""Terminal command and validation result tracking for WorkerEventRelay.

Handles run_terminal_command and run_and_watch tool result processing,
including output truncation, validation classification, and bus event emission.
"""

from __future__ import annotations

from typing import Any, Callable

from aura.bridge.event_relay_errors import (
    _attach_validation_metadata,
    _is_validation_terminal_record,
)
from aura.bridge.event_relay_write_tracking import (
    TERMINAL_OUTPUT_CAPTURE_CHARS,
    TERMINAL_OUTPUT_PREVIEW_CHARS,
)
from aura.events import (
    WORKER_COMMAND_FINISHED,
    WORKER_VALIDATION_FINISHED,
)


class EventRelayTerminalTracker:
    """Tracks terminal command results and classifies validation attempts.

    Owns terminal_results and validation_results lists and provides
    a handle_tool_result method called from WorkerEventRelay.relay().
    """

    def __init__(
        self,
        emit_bus_event: Callable[[str, dict], None],
    ) -> None:
        self.terminal_results: list[dict[str, Any]] = []
        self.validation_results: list[dict[str, Any]] = []
        self._emit_bus_event = emit_bus_event

    def handle_tool_result(self, tool_name: str, parsed: dict[str, Any]) -> None:
        """Build a terminal result record, attach validation metadata, and emit events.

        Only processes run_terminal_command and run_and_watch when *parsed*
        contains command, exit_code, and ok keys.
        """
        if tool_name not in ("run_terminal_command", "run_and_watch"):
            return
        if not isinstance(parsed, dict):
            return
        if "command" not in parsed or "exit_code" not in parsed or "ok" not in parsed:
            return

        output = str(parsed.get("output") or "")
        record: dict[str, Any] = {
            "command": parsed.get("command", ""),
            "ok": parsed.get("ok", False),
            "exit_code": parsed.get("exit_code", -1),
            "output": output[:TERMINAL_OUTPUT_CAPTURE_CHARS],
            "output_preview": output[:TERMINAL_OUTPUT_PREVIEW_CHARS],
        }
        for key in (
            "terminal_command_role",
            "terminal_classification",
            "command_success",
            "terminal_no_matches",
        ):
            if key in parsed:
                record[key] = parsed[key]
        if tool_name == "run_terminal_command" and parsed.get("auto_validation"):
            record["auto_validation"] = True

        _attach_validation_metadata(record, parsed)
        self.terminal_results.append(record)
        self._emit_bus_event(WORKER_COMMAND_FINISHED, {
            "command": record["command"],
            "exit_code": record["exit_code"],
            "ok": record["ok"],
        })
        if _is_validation_terminal_record(record):
            self.validation_results.append(record)
            self._emit_bus_event(WORKER_VALIDATION_FINISHED, {
                "command": record["command"],
                "ok": record["ok"],
                "exit_code": record["exit_code"],
            })

    def reset(self) -> None:
        self.terminal_results.clear()
        self.validation_results.clear()
