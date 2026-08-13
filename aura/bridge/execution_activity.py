"""Execution Activity — append-only execution heartbeat, projected from event bus.

This is the first real consumer/projector of Aura's event-bus foundation.
It transforms low-level AuraEvent facts into readable activity entries
for the bridge/GUI.

Core invariant:
    Execution Activity = append-only execution heartbeat.
    Final report = truthful proof receipt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from aura.events import (
    EXECUTION_COMMAND_FINISHED,
    EXECUTION_COMMAND_STARTED,
    EXECUTION_FAILED,
    EXECUTION_FILE_CHANGED,
    EXECUTION_FINAL_REPORT_FINISHED,
    EXECUTION_FINAL_REPORT_STARTED,
    EXECUTION_TOOL_FINISHED,
    EXECUTION_TOOL_STARTED,
    EXECUTION_VALIDATION_FINISHED,
    EXECUTION_VALIDATION_STARTED,
    AuraEvent,
    EventBus,
)

# ── Activity entry ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ActivityEntry:
    """One immutable entry in the Execution Activity log.

    ``kind`` is a short machine-readable label (e.g. ``"tool_started"``,
    ``"file_changed"``).  ``message`` is a human-readable summary line.
    ``detail`` carries context such as the tool name, file path, or exit code.

    The remaining fields carry identity context so downstream projectors
    can correlate entries without reaching into other subsystems.
    """

    kind: str
    message: str
    detail: str = ""
    timestamp: float = 0.0
    run_id: str = ""

    def __post_init__(self) -> None:
        if self.timestamp == 0.0:
            object.__setattr__(self, "timestamp", time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "detail": self.detail,
            "timestamp": self.timestamp,
            "run_id": self.run_id,
        }


# ── Controller ──────────────────────────────────────────────────────────────

# Optional callback invoked after every append.  Receives the full snapshot.
_OnChange = Callable[[list[ActivityEntry]], None]


def _kind_from_topic(topic: str) -> str:
    """Derive a short activity kind from a dotted topic string."""
    return topic.replace(".", "_")


def _format_tool_name(payload: dict[str, Any]) -> str:
    return str(payload.get("name") or payload.get("tool_name") or "")


def _format_file_path(payload: dict[str, Any]) -> str:
    return str(payload.get("path") or payload.get("rel_path") or "")


class ExecutionActivityController:
    """Append-only, bounded projector of execution activity from the event bus.

    Usage::

        bus = EventBus()
        controller = ExecutionActivityController(bus)

        # ... later, in bridge code that has Qt access:
        controller.set_on_change(lambda snapshot: emit_signal(snapshot))

        # The controller records activity and never owns lifecycle.
    """

    def __init__(
        self,
        bus: EventBus,
        maxlen: int = 200,
    ) -> None:
        self._entries: list[ActivityEntry] = []
        self._maxlen = maxlen
        self._on_change: _OnChange | None = None
        self._run_id: str = ""

        self._subscribe(bus)

    # ── public API ──────────────────────────────────────────────────────────

    def set_on_change(self, callback: _OnChange | None) -> None:
        """Register a callback invoked after every append with the full snapshot."""
        self._on_change = callback

    def snapshot(self) -> list[ActivityEntry]:
        """Return a copy of all stored entries."""
        return list(self._entries)

    def snapshot_dicts(self) -> list[dict[str, Any]]:
        """Return entries as plain dicts, ready for serialisation / bridge relay."""
        return [e.to_dict() for e in self._entries]

    def clear(self) -> None:
        """Remove all entries (test teardown / conversation reset)."""
        self._entries.clear()

    def set_run_id(self, run_id: str) -> None:
        self._run_id = run_id

    # ── event subscriptions ─────────────────────────────────────────────────

    def _subscribe(self, bus: EventBus) -> None:
        bus.subscribe(EXECUTION_TOOL_STARTED, self._on_tool_started)
        bus.subscribe(EXECUTION_TOOL_FINISHED, self._on_tool_finished)
        bus.subscribe(EXECUTION_FILE_CHANGED, self._on_file_changed)
        bus.subscribe(EXECUTION_COMMAND_STARTED, self._on_command_started)
        bus.subscribe(EXECUTION_COMMAND_FINISHED, self._on_command_finished)
        bus.subscribe(EXECUTION_VALIDATION_STARTED, self._on_validation_started)
        bus.subscribe(EXECUTION_VALIDATION_FINISHED, self._on_validation_finished)
        bus.subscribe(EXECUTION_FINAL_REPORT_STARTED, self._on_final_report_started)
        bus.subscribe(EXECUTION_FINAL_REPORT_FINISHED, self._on_final_report_finished)
        bus.subscribe(EXECUTION_FAILED, self._on_execution_failed)

    # ── individual event handlers ───────────────────────────────────────────

    def _append(self, entry: ActivityEntry) -> None:
        self._entries.append(entry)
        if len(self._entries) > self._maxlen:
            self._entries.pop(0)
        if self._on_change is not None:
            self._on_change(self._entries)

    def _on_tool_started(self, ev: AuraEvent) -> None:
        name = _format_tool_name(ev.payload)
        msg = f"Tool started: {name}" if name else "Tool started"
        self._append(ActivityEntry(
            kind="tool_started",
            message=msg,
            detail=name,
            run_id=ev.run_id or self._run_id,
        ))

    def _on_tool_finished(self, ev: AuraEvent) -> None:
        name = _format_tool_name(ev.payload)
        ok = ev.payload.get("ok")
        status = "completed" if ok is not False else "failed"
        msg = f"Tool {status}: {name}" if name else f"Tool {status}"
        self._append(ActivityEntry(
            kind=f"tool_{status}",
            message=msg,
            detail=name,
            run_id=ev.run_id or self._run_id,
        ))

    def _on_file_changed(self, ev: AuraEvent) -> None:
        path = _format_file_path(ev.payload)
        action = str(ev.payload.get("action") or "modified")
        msg = f"File {action}: {path}" if path else f"File {action}"
        self._append(ActivityEntry(
            kind="file_changed",
            message=msg,
            detail=path,
            run_id=ev.run_id or self._run_id,
        ))

    def _on_command_started(self, ev: AuraEvent) -> None:
        cmd = str(ev.payload.get("command") or "")
        msg = "Command started"
        if cmd and len(cmd) <= 120:
            msg += f": {cmd}"
        self._append(ActivityEntry(
            kind="command_started",
            message=msg,
            detail=cmd[:120] if cmd else "",
            run_id=ev.run_id or self._run_id,
        ))

    def _on_command_finished(self, ev: AuraEvent) -> None:
        exit_code = ev.payload.get("exit_code")
        cmd = str(ev.payload.get("command") or "")
        status = f"exit {exit_code}" if exit_code is not None else "finished"
        msg = f"Command {status}"
        if cmd and len(cmd) <= 80:
            msg += f": {cmd}"
        self._append(ActivityEntry(
            kind="command_finished",
            message=msg,
            detail=str(exit_code) if exit_code is not None else "",
            run_id=ev.run_id or self._run_id,
        ))

    def _on_validation_started(self, ev: AuraEvent) -> None:
        label = str(ev.payload.get("label") or ev.payload.get("command") or "")
        msg = "Validation started"
        if label and len(label) <= 120:
            msg += f": {label}"
        self._append(ActivityEntry(
            kind="validation_started",
            message=msg,
            detail=label[:120] if label else "",
            run_id=ev.run_id or self._run_id,
        ))

    def _on_validation_finished(self, ev: AuraEvent) -> None:
        ok = ev.payload.get("ok")
        label = str(ev.payload.get("label") or ev.payload.get("command") or "")
        status = "passed" if ok is not False else "failed"
        msg = f"Validation {status}"
        if label and len(label) <= 120:
            msg += f": {label}"
        self._append(ActivityEntry(
            kind=f"validation_{status}",
            message=msg,
            detail=label[:120] if label else "",
            run_id=ev.run_id or self._run_id,
        ))

    def _on_final_report_started(self, ev: AuraEvent) -> None:
        self._append(ActivityEntry(
            kind="final_report_started",
            message="Final report started",
            run_id=ev.run_id or self._run_id,
        ))

    def _on_final_report_finished(self, ev: AuraEvent) -> None:
        ok = ev.payload.get("ok")
        status = "completed" if ok is not False else "failed"
        msg = f"Final report {status}"
        self._append(ActivityEntry(
            kind=f"final_report_{status}",
            message=msg,
            run_id=ev.run_id or self._run_id,
        ))

    def _on_execution_failed(self, ev: AuraEvent) -> None:
        error = str(ev.payload.get("error") or ev.message or "")
        msg = "Execution failed"
        if error and len(error) <= 200:
            msg += f": {error}"
        self._append(ActivityEntry(
            kind="execution_failed",
            message=msg,
            detail=error[:200] if error else "",
            run_id=ev.run_id or self._run_id,
        ))
