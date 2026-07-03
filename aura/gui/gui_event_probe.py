"""Temporary GUI event probe for tracking whole-window flash sources."""
from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QEvent, QTimer, Qt
from PySide6.QtWidgets import QApplication, QWidget

from aura.startup_logging import logs_dir

_log = logging.getLogger(__name__)

_EVENT_TYPES = {
    QEvent.Type.Show,
    QEvent.Type.Hide,
    QEvent.Type.ShowToParent,
    QEvent.Type.HideToParent,
    QEvent.Type.Polish,
    QEvent.Type.PolishRequest,
    QEvent.Type.StyleChange,
    QEvent.Type.LayoutRequest,
    QEvent.Type.Resize,
    QEvent.Type.Move,
    QEvent.Type.ChildAdded,
    QEvent.Type.ChildRemoved,
    QEvent.Type.ParentChange,
    QEvent.Type.ParentAboutToChange,
    QEvent.Type.UpdateRequest,
    QEvent.Type.Paint,
}

_DUMP_AFTER_MS = 4000
_MAX_DUMPS = 20
_RING_SIZE = 50000


class GuiEventProbe(QObject):
    """QApplication event filter with a rolling GUI event buffer."""

    def __init__(self, window: QWidget, parent: QObject | None = None) -> None:
        super().__init__(parent or window)
        self._window = window
        self._records: deque[dict[str, Any]] = deque(maxlen=_RING_SIZE)
        self._start = time.monotonic()
        self._dump_count = 0
        self._seen_activity = 0
        self._last_dump_reason = ""
        self._event_counts: Counter[str] = Counter()

    def install(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        app.installEventFilter(self)
        self.mark("probe_installed")
        _log.warning("GUI_EVENT_PROBE installed")

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._is_hotkey(event):
            self.dump("hotkey_ctrl_alt_f12")
            return False

        if event.type() in _EVENT_TYPES and isinstance(watched, QWidget):
            if self._belongs_to_window(watched):
                record = self._record_for(watched, event)
                self._records.append(record)
                self._event_counts[record["event"]] += 1
        return False

    def mark(self, name: str, **payload: Any) -> None:
        record = {
            "t_ms": self._elapsed_ms(),
            "kind": "marker",
            "name": name,
            "payload": payload,
        }
        self._records.append(record)
        _log.warning("GUI_EVENT_PROBE marker %s %s", name, payload)

    def mark_and_dump(self, name: str, **payload: Any) -> None:
        self.mark(name, **payload)
        self._schedule_dump(name)

    def on_worker_activity(self, tool_call_id: str, entries: list) -> None:
        if not isinstance(entries, list):
            return
        new_entries = entries[self._seen_activity :]
        self._seen_activity = len(entries)
        for entry in new_entries:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("kind") or "")
            if kind in {"campaign_started", "step_started", "step_completed", "step_failed"}:
                self.mark_and_dump(
                    f"activity_{kind}",
                    tool_call_id=tool_call_id,
                    step_id=entry.get("step_id", ""),
                    campaign_id=entry.get("campaign_id", ""),
                    message=entry.get("message", ""),
                )

    def on_worker_todo(self, tool_call_id: str, items: list) -> None:
        self.mark_and_dump(
            "worker_todo_updated",
            tool_call_id=tool_call_id,
            item_count=len(items) if isinstance(items, list) else -1,
        )

    def on_worker_usage(
        self,
        tool_call_id: str,
        model: str,
        prompt: int,
        completion: int,
        hit: int,
        miss: int,
    ) -> None:
        self.mark_and_dump(
            "worker_usage",
            tool_call_id=tool_call_id,
            model=model,
            prompt=prompt,
            completion=completion,
            hit=hit,
            miss=miss,
        )

    def dump(self, reason: str) -> None:
        if self._dump_count >= _MAX_DUMPS:
            return
        self._dump_count += 1
        path = self._dump_path(reason)
        payload = {
            "reason": reason,
            "dump_index": self._dump_count,
            "elapsed_ms": self._elapsed_ms(),
            "event_counts": dict(self._event_counts),
            "records": list(self._records),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=True, indent=2)
            _log.warning("GUI_EVENT_PROBE dumped %s records to %s", len(self._records), path)
        except Exception:
            _log.exception("GUI_EVENT_PROBE failed to dump %s", path)

    def _schedule_dump(self, reason: str) -> None:
        self._last_dump_reason = reason
        QTimer.singleShot(_DUMP_AFTER_MS, lambda r=reason: self.dump(r))

    def _record_for(self, widget: QWidget, event: QEvent) -> dict[str, Any]:
        parent = widget.parent()
        parent_widget = parent if isinstance(parent, QWidget) else None
        size = widget.size()
        return {
            "t_ms": self._elapsed_ms(),
            "kind": "qt_event",
            "event": _event_name(event.type()),
            "widget": type(widget).__name__,
            "object_name": widget.objectName(),
            "visible": widget.isVisible(),
            "size": [size.width(), size.height()],
            "parent": type(parent_widget).__name__ if parent_widget is not None else "",
            "parent_object_name": parent_widget.objectName() if parent_widget is not None else "",
        }

    def _belongs_to_window(self, widget: QWidget) -> bool:
        if widget is self._window:
            return True
        try:
            return widget.window() is self._window or self._window.isAncestorOf(widget)
        except RuntimeError:
            return False

    def _is_hotkey(self, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress:
            return False
        key = getattr(event, "key", lambda: None)()
        modifiers = getattr(event, "modifiers", lambda: Qt.KeyboardModifier.NoModifier)()
        return (
            key == Qt.Key.Key_F12
            and bool(modifiers & Qt.KeyboardModifier.ControlModifier)
            and bool(modifiers & Qt.KeyboardModifier.AltModifier)
        )

    def _elapsed_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)

    def _dump_path(self, reason: str) -> Path:
        safe_reason = "".join(c if c.isalnum() or c in "-_" else "_" for c in reason)[:80]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return logs_dir() / f"gui-event-probe-{stamp}-{os.getpid()}-{self._dump_count:02d}-{safe_reason}.json"


def install_gui_event_probe(window: QWidget, bridge: QObject) -> GuiEventProbe | None:
    """Install the temporary probe unless disabled by env var."""
    if os.environ.get("AURA_GUI_EVENT_PROBE", "1").lower() in {"0", "false", "no"}:
        return None

    probe = GuiEventProbe(window, parent=window)
    probe.install()

    _connect_if_present(bridge, "workerStarted", lambda tid: probe.mark_and_dump("worker_started", tool_call_id=tid))
    _connect_if_present(bridge, "workerFinished", lambda tid, ok, summary, followup, status: probe.mark_and_dump(
        "worker_finished",
        tool_call_id=tid,
        ok=ok,
        needs_followup=followup,
        status=status,
    ))
    _connect_if_present(bridge, "workerActivityUpdated", probe.on_worker_activity)
    _connect_if_present(bridge, "workerTodoUpdated", probe.on_worker_todo)
    _connect_if_present(bridge, "workerUsage", probe.on_worker_usage)
    _connect_if_present(bridge, "workflowStateChanged", lambda state: probe.mark(
        "workflow_state_changed",
        tool_call_id=getattr(state, "tool_call_id", ""),
        status=str(getattr(getattr(state, "status", ""), "value", getattr(state, "status", ""))),
    ))
    return probe


def _connect_if_present(obj: QObject, signal_name: str, callback: Any) -> None:
    signal = getattr(obj, signal_name, None)
    if signal is None:
        return
    try:
        signal.connect(callback)
    except Exception:
        _log.exception("GUI_EVENT_PROBE failed to connect %s", signal_name)


def _event_name(event_type: QEvent.Type) -> str:
    try:
        return event_type.name
    except Exception:
        return str(int(event_type))
