"""Pending dispatch state owner.

Owns the _DispatchPending dataclass and the thread-safe pending map.
Does NOT own Worker execution, Qt signals, TODO policy, or result
classification.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any

from aura.conversation import WorkerDispatchRequest


class _DispatchPending:
    """Mutable state for one pending dispatch decision.

    Lives from the moment the Planner requests dispatch until the user
    dispatches, cancels, or the decision times out.
    """

    def __init__(self, request: WorkerDispatchRequest) -> None:
        self.request = request
        self.edited_request: WorkerDispatchRequest | None = None
        self.cancelled: bool = False
        self.decision_event: threading.Event = threading.Event()
        self.cancel_event: threading.Event | None = None


class DispatchPendingMap:
    """Thread-safe pending dispatch registry.

    Owns the _pending dict and all lock-guarded operations on it.
    The DispatchProxy delegates pending lifecycle to this owner so that
    dispatch.py no longer holds the map or its locking policy directly.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, _DispatchPending] = {}

    # -- registry ---------------------------------------------------------

    def register(
        self, tool_call_id: str, request: WorkerDispatchRequest
    ) -> _DispatchPending:
        """Create and store a new pending entry. Returns the entry."""
        pending = _DispatchPending(request=request)
        with self._lock:
            self._pending[tool_call_id] = pending
        return pending

    def get(self, tool_call_id: str) -> _DispatchPending | None:
        """Return the pending entry for *tool_call_id*, or None."""
        with self._lock:
            return self._pending.get(tool_call_id)

    def pop(self, tool_call_id: str) -> _DispatchPending | None:
        """Remove and return the pending entry for *tool_call_id*."""
        with self._lock:
            return self._pending.pop(tool_call_id, None)

    # -- resolution -------------------------------------------------------

    def resolve_dispatched(
        self,
        tool_call_id: str,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
        summary: str,
    ) -> bool:
        """Apply the user's edited dispatch request and unblock the planner.

        Returns True if a matching pending entry was found.
        """
        pending = self.get(tool_call_id)
        if pending is None:
            return False
        pending.edited_request = replace(
            pending.request,
            goal=goal,
            files=list(files),
            spec=spec,
            acceptance=acceptance,
            summary=summary,
        )
        pending.cancelled = False
        pending.decision_event.set()
        return True

    def resolve_cancelled(self, tool_call_id: str) -> bool:
        """Mark the pending entry as cancelled and unblock the planner.

        Returns True if a matching pending entry was found.
        """
        pending = self.get(tool_call_id)
        if pending is None:
            return False
        pending.cancelled = True
        pending.decision_event.set()
        return True

    def cancel_all(self) -> None:
        """Unblock every pending decision wait and signal every running worker's
        cancel event so that a Stop action takes immediate effect."""
        with self._lock:
            for _tool_id, pending in list(self._pending.items()):
                if not pending.decision_event.is_set():
                    pending.cancelled = True
                    pending.decision_event.set()
                if pending.cancel_event is not None:
                    pending.cancel_event.set()


__all__ = [
    "_DispatchPending",
    "DispatchPendingMap",
]
