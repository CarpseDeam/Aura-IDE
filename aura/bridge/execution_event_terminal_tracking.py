"""Terminal command and validation result tracking for ExecutionEventRelay.

Handles ``shell`` tool result processing, including output truncation,
validation classification, and bus event emission.
"""

from __future__ import annotations

from typing import Any, Callable

from aura.bridge.execution_event_errors import (
    _is_validation_terminal_record,
)
from aura.bridge.execution_event_write_tracking import (
    TERMINAL_OUTPUT_CAPTURE_CHARS,
    TERMINAL_OUTPUT_PREVIEW_CHARS,
)
from aura.conversation.validation_truth import validation_payload_passed
from aura.events import (
    EXECUTION_COMMAND_FINISHED,
    EXECUTION_VALIDATION_FINISHED,
)


class EventRelayTerminalTracker:
    """Tracks terminal command results and classifies validation attempts.

    Owns terminal_results and validation_results lists and provides
    a handle_tool_result method called from ExecutionEventRelay.relay().
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

        Only processes ``shell`` when *parsed* contains command, exit_code,
        and ok keys.
        """
        if tool_name != "shell":
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
        VALIDATION_TRUTH_FIELDS = (
            "counts_as_validation",
            "validation_classification",
            "classification",
            "counts_as_product_failure",
            "command_outcome_classification",
            "validation_traceback_detected",
            "validation_was_timeout",
            "validation_source",
            "cwd",
            "working_directory",
        )
        for key in VALIDATION_TRUTH_FIELDS:
            if key in parsed:
                record[key] = parsed[key]
        if tool_name == "shell" and parsed.get("auto_validation"):
            record["auto_validation"] = True

        self.terminal_results.append(record)
        self._emit_bus_event(EXECUTION_COMMAND_FINISHED, {
            "command": record["command"],
            "exit_code": record["exit_code"],
            "ok": record["ok"],
        })
        if _is_validation_terminal_record(record):
            validation_ok = validation_payload_passed(record)
            record["validation_ok"] = validation_ok
            self.validation_results.append(record)
            self._emit_bus_event(EXECUTION_VALIDATION_FINISHED, {
                "command": record["command"],
                "ok": validation_ok,
                "exit_code": record["exit_code"],
            })

    def reset(self) -> None:
        self.terminal_results.clear()
        self.validation_results.clear()
