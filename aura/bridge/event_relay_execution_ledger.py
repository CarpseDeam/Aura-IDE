"""Execution ledger tracking for WorkerEventRelay.

Tracks applied/not-applied file mutations, read-before-edit evidence,
touched/created/edited/deleted file ledgers, and WORKER_FILE_CHANGED
bus event emission.

Owns write_results, not_applied_writes, read_files, read_outline_files,
touched_files, wrote_new_files, and edited_existing_files on behalf of
WorkerEventRelay.
"""

from __future__ import annotations

from typing import Any, Callable

from aura.bridge.event_relay_write_tracking import (
    _file_mutation_was_applied,
    _is_file_mutation_tool,
    _result_path,
)
from aura.events import WORKER_FILE_CHANGED


class EventRelayExecutionLedger:
    """Owns the execution-ledger state for WorkerEventRelay.

    Handles applied file mutations, not-applied write attempts, read-before-edit
    tracking, and WORKER_FILE_CHANGED bus event emission.

    Exposes mutable public attributes so WorkerEventRelay can delegate to them
    and downstream completion code can read/filter them directly.
    """

    def __init__(
        self,
        emit_bus_event: Callable[[str, dict], None],
    ) -> None:
        self._emit_bus_event = emit_bus_event
        # Applied file-mutation records
        self.write_results: list[dict[str, Any]] = []
        # File-mutation attempts that were not applied
        self.not_applied_writes: list[dict[str, Any]] = []
        # Paths read via read_file / read_files / read_file_range
        self.read_files: set[str] = set()
        # Paths read via read_file_outline
        self.read_outline_files: set[str] = set()
        # All paths touched by applied file mutations
        self.touched_files: set[str] = set()
        # Paths of newly created files
        self.wrote_new_files: list[str] = []
        # Paths of existing files that were edited
        self.edited_existing_files: list[str] = []

    def handle_tool_result(
        self,
        name: str,
        ok: bool,
        parsed: Any,
        extras: dict[str, Any],
    ) -> None:
        """Process a tool result for execution-ledger tracking.

        Handles applied file mutations, not-applied write attempts, and
        read-before-edit tracking in the same order as the original inline
        code in WorkerEventRelay.relay() to preserve behavior exactly.
        """
        # ------------------------------------------------------------------
        # 1. Applied file mutations
        # ------------------------------------------------------------------
        if _file_mutation_was_applied(name, ok, parsed, extras):
            path = _result_path(parsed, extras)
            is_new_file = bool(parsed.get("is_new_file", False))
            deleted = bool(parsed.get("deleted"))
            write_record: dict[str, Any] = {
                "tool": name,
                "path": path,
                "is_new_file": is_new_file,
                "deleted": deleted,
                "applied": True,
                "applied_tool": parsed.get("applied_tool") or name,
                "write_outcome": parsed.get("write_outcome")
                or ("deleted" if deleted else "applied"),
                "backup": parsed.get("backup"),
            }
            if parsed.get("pre_existing_environment_issues"):
                write_record["pre_existing_environment_issues"] = parsed.get(
                    "pre_existing_environment_issues"
                )
            if parsed.get("craft_metadata"):
                write_record["craft_metadata"] = parsed.get("craft_metadata")
            if "start_line" in parsed:
                write_record["start_line"] = parsed.get("start_line")
            if "end_line" in parsed:
                write_record["end_line"] = parsed.get("end_line")
            if "hunk_count" in parsed:
                write_record["hunk_count"] = parsed.get("hunk_count")
            if "operation_count" in parsed:
                write_record["operation_count"] = parsed.get("operation_count")
            self.write_results.append(write_record)
            if path:
                action = (
                    "created"
                    if is_new_file
                    else ("deleted" if deleted else "modified")
                )
                self._emit_bus_event(
                    WORKER_FILE_CHANGED,
                    {
                        "path": path,
                        "action": action,
                        "tool": name,
                    },
                )
                self.touched_files.add(path)
                if is_new_file:
                    self.wrote_new_files.append(path)
                elif not deleted:
                    self.edited_existing_files.append(path)

        # ------------------------------------------------------------------
        # 2. Not-applied write attempts  (elif — mutually exclusive with
        #    applied mutation above)
        # ------------------------------------------------------------------
        elif _is_file_mutation_tool(name) and isinstance(parsed, dict):
            if parsed.get("applied") is False or str(
                parsed.get("write_outcome") or ""
            ).startswith("not_applied_"):
                write_record = {
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

        # ------------------------------------------------------------------
        # 3. Read-before-edit tracking  (independent of mutation status)
        # ------------------------------------------------------------------
        if isinstance(parsed, dict):
            if ok and name == "read_file":
                path = parsed.get("path")
                if (
                    parsed.get("ok") is True
                    and parsed.get("truncated") is not True
                    and isinstance(path, str)
                    and path
                ):
                    self.read_files.add(path)
            if ok and name == "read_files":
                files = parsed.get("files")
                if isinstance(files, dict):
                    for path_key, result in files.items():
                        if not isinstance(result, dict):
                            continue
                        path = result.get("path") or path_key
                        if (
                            result.get("ok") is True
                            and result.get("truncated") is not True
                            and isinstance(path, str)
                            and path
                        ):
                            self.read_files.add(path)
            if ok and name == "read_file_range":
                path = parsed.get("path")
                if (
                    parsed.get("ok") is True
                    and isinstance(parsed.get("content_hash"), str)
                    and isinstance(path, str)
                    and path
                ):
                    self.read_files.add(path)
            if ok and name == "read_file_outline":
                path = parsed.get("path")
                if isinstance(path, str) and path:
                    self.read_outline_files.add(path)

    def reset(self) -> None:
        """Clear all tracking fields so the ledger can be reused."""
        self.write_results.clear()
        self.not_applied_writes.clear()
        self.read_files.clear()
        self.read_outline_files.clear()
        self.touched_files.clear()
        self.wrote_new_files.clear()
        self.edited_existing_files.clear()
