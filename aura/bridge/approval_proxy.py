"""Qt thread-crossing proxy that marshals approval requests from any worker
thread onto the GUI thread using BlockingQueuedConnection."""

from __future__ import annotations

import threading
from typing import Any

from PySide6.QtCore import QMetaObject, QObject, Qt, Slot

from aura.conversation.tools import ApprovalDecision, ApprovalRequest
from aura.gui.diff_dialog import DiffApprovalDialog


class _ApprovalProxy(QObject):
    """Marshals approval requests from any worker thread onto the GUI thread."""

    def __init__(self, parent_widget) -> None:
        super().__init__()
        self._parent_widget = parent_widget
        self._lock = threading.Lock()
        self._last_decision: ApprovalDecision = ApprovalDecision(action="reject")
        self._last_request: ApprovalRequest | None = None
        self._last_event: dict[str, Any] | None = None
        self._approve_all_session: bool = False

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        if self._approve_all_session:
            with self._lock:
                self._last_event = self._event_from_request(request, "approve")
            return ApprovalDecision(action="approve")
        with self._lock:
            self._last_request = request
            QMetaObject.invokeMethod(
                self,
                "_open_dialog",
                Qt.ConnectionType.BlockingQueuedConnection,
            )
            return self._last_decision

    def set_approve_all_session(self, enabled: bool) -> None:
        """Enable or disable auto-approve for all remaining diffs this session."""
        self._approve_all_session = enabled

    def consume_last_event(self) -> dict[str, Any] | None:
        """Return and clear the last approval event (rel_path, old/new content, etc.)."""
        ev = self._last_event
        self._last_event = None
        return ev

    def reset_approve_all(self) -> None:
        self._approve_all_session = False

    @Slot()
    def _open_dialog(self) -> None:
        req = self._last_request
        if req is None:
            self._last_decision = ApprovalDecision(action="reject")
            return
        dlg = DiffApprovalDialog(req, parent=self._parent_widget)
        dlg.exec()
        self._last_decision = dlg.decision()
        if self._last_decision.action == "approve_all":
            self._approve_all_session = True
            self._last_decision = ApprovalDecision(action="approve")
        self._last_event = self._event_from_request(req, self._last_decision.action)

    @staticmethod
    def _event_from_request(
        req: ApprovalRequest, decision: str
    ) -> dict[str, Any]:
        return {
            "rel_path": req.rel_path,
            "old_content": req.old_content,
            "new_content": req.new_content,
            "is_new_file": req.is_new_file,
            "decision": decision,
        }
