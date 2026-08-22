"""Execution ledger tracking for ExecutionEventRelay.

Tracks file-mutation attempts that were not applied, on behalf of
ExecutionEventRelay.
"""

from __future__ import annotations

from typing import Any

from aura.bridge.execution_event_write_tracking import (
    _file_mutation_was_applied,
    _is_file_mutation_tool,
    _result_path,
)


class EventRelayExecutionLedger:
    """Owns the not-applied-write ledger state for ExecutionEventRelay.

    Exposes a mutable public attribute so ExecutionEventRelay can delegate to
    it and downstream completion code can read it directly.
    """

    def __init__(self) -> None:
        # File-mutation attempts that were not applied
        self.not_applied_writes: list[dict[str, Any]] = []

    def handle_tool_result(
        self,
        name: str,
        ok: bool,
        parsed: Any,
        extras: dict[str, Any],
    ) -> None:
        """Process a tool result for not-applied-write tracking."""
        if (
            not _file_mutation_was_applied(name, ok, parsed, extras)
            and _is_file_mutation_tool(name)
            and isinstance(parsed, dict)
        ):
            if parsed.get("applied") is False or str(
                parsed.get("write_outcome") or ""
            ).startswith("not_applied_"):
                write_record: dict[str, Any] = {
                    "tool": name,
                    "path": _result_path(parsed, extras),
                    "applied": False,
                    "write_outcome": parsed.get("write_outcome")
                    or "not_applied_edit_mechanics_blocked",
                    "failure_class": parsed.get("failure_class", ""),
                    "error": parsed.get("error", ""),
                    "craft_issues": parsed.get("craft_issues", []),
                    "pre_existing_environment_issues": parsed.get(
                        "pre_existing_environment_issues", []
                    ),
                    "introduced_environment_issues": parsed.get(
                        "introduced_environment_issues", []
                    ),
                }
                if parsed.get("craft_metadata"):
                    write_record["craft_metadata"] = parsed.get("craft_metadata")
                for key in (
                    "operation_index",
                    "failed_operation",
                    "reason",
                    "stale",
                    "ambiguous",
                    "not_found",
                    "candidate_count",
                    "candidates",
                ):
                    if key in parsed:
                        write_record[key] = parsed[key]
                self.not_applied_writes.append(write_record)

    def reset(self) -> None:
        """Clear all tracking fields so the ledger can be reused."""
        self.not_applied_writes.clear()
