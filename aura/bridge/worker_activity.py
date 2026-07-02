"""Worker Activity — append-only execution heartbeat, projected from event bus.

This is the first real consumer/projector of Aura's event-bus foundation.
It transforms low-level AuraEvent facts into readable activity entries
for the bridge/GUI — without mutating TODO state.

Core invariant:
    TODO = semantic Planner checklist progress.
    Worker Activity = append-only execution heartbeat.
    Final report = truthful proof receipt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from aura.events import (
    DISPATCH_CAMPAIGN_STARTED,
    DISPATCH_STEP_COMPLETED,
    DISPATCH_STEP_STARTED,
    WORKER_TOOL_STARTED,
    WORKER_TOOL_FINISHED,
    WORKER_FILE_CHANGED,
    WORKER_COMMAND_STARTED,
    WORKER_COMMAND_FINISHED,
    WORKER_VALIDATION_STARTED,
    WORKER_VALIDATION_FINISHED,
    WORKER_FINAL_REPORT_STARTED,
    WORKER_FINAL_REPORT_FINISHED,
    WORKER_FAILED,
    AuraEvent,
    EventBus,
    ALL,
)

# ── Activity entry ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ActivityEntry:
    """One immutable entry in the Worker Activity log.

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
    campaign_id: str = ""
    step_id: str = ""

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
            "campaign_id": self.campaign_id,
            "step_id": self.step_id,
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


class WorkerActivityController:
    """Append-only, bounded projector of worker activity from the event bus.

    Usage::

        bus = EventBus()
        controller = WorkerActivityController(bus)

        # ... later, in bridge code that has Qt access:
        controller.set_on_change(lambda snapshot: emit_signal(snapshot))

        # The controller never mutates TODO state and never infers lifecycle.
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
        bus.subscribe(DISPATCH_CAMPAIGN_STARTED, self._on_campaign_started)
        bus.subscribe(DISPATCH_STEP_STARTED, self._on_step_started)
        bus.subscribe(DISPATCH_STEP_COMPLETED, self._on_step_completed)
        bus.subscribe(WORKER_TOOL_STARTED, self._on_tool_started)
        bus.subscribe(WORKER_TOOL_FINISHED, self._on_tool_finished)
        bus.subscribe(WORKER_FILE_CHANGED, self._on_file_changed)
        bus.subscribe(WORKER_COMMAND_STARTED, self._on_command_started)
        bus.subscribe(WORKER_COMMAND_FINISHED, self._on_command_finished)
        bus.subscribe(WORKER_VALIDATION_STARTED, self._on_validation_started)
        bus.subscribe(WORKER_VALIDATION_FINISHED, self._on_validation_finished)
        bus.subscribe(WORKER_FINAL_REPORT_STARTED, self._on_final_report_started)
        bus.subscribe(WORKER_FINAL_REPORT_FINISHED, self._on_final_report_finished)
        bus.subscribe(WORKER_FAILED, self._on_worker_failed)

    # ── individual event handlers ───────────────────────────────────────────

    def _append(self, entry: ActivityEntry) -> None:
        self._entries.append(entry)
        if len(self._entries) > self._maxlen:
            self._entries.pop(0)
        if self._on_change is not None:
            self._on_change(self._entries)

    def _on_campaign_started(self, ev: AuraEvent) -> None:
        goal = str(ev.payload.get("goal") or ev.message or "")
        msg = f"Campaign started"
        if goal:
            msg += f": {goal[:120]}"
        self._append(ActivityEntry(
            kind="campaign_started",
            message=msg,
            detail=goal[:120] if goal else "",
            campaign_id=ev.campaign_id or ev.run_id,
            run_id=ev.run_id or self._run_id,
        ))

    def _on_step_started(self, ev: AuraEvent) -> None:
        desc = str(ev.payload.get("description") or ev.payload.get("step_id") or "")
        msg = f"Step started"
        if desc:
            msg += f": {desc[:120]}"
        self._append(ActivityEntry(
            kind="step_started",
            message=msg,
            detail=desc[:120] if desc else "",
            step_id=ev.step_id,
            campaign_id=ev.campaign_id,
            run_id=ev.run_id or self._run_id,
        ))

    def _on_step_completed(self, ev: AuraEvent) -> None:
        desc = str(ev.payload.get("description") or ev.payload.get("step_id") or ev.step_id or "")
        ok = ev.payload.get("ok")
        status = "completed" if ok is not False else "failed"
        msg = f"Step {status}"
        if desc:
            msg += f": {desc[:120]}"
        self._append(ActivityEntry(
            kind=f"step_{status}",
            message=msg,
            detail=desc[:120] if desc else "",
            step_id=ev.step_id,
            campaign_id=ev.campaign_id,
            run_id=ev.run_id or self._run_id,
        ))

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
        msg = f"Command started"
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
        msg = f"Validation started"
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

    def _on_worker_failed(self, ev: AuraEvent) -> None:
        error = str(ev.payload.get("error") or ev.message or "")
        msg = "Worker failed"
        if error and len(error) <= 200:
            msg += f": {error}"
        self._append(ActivityEntry(
            kind="worker_failed",
            message=msg,
            detail=error[:200] if error else "",
            run_id=ev.run_id or self._run_id,
        ))
