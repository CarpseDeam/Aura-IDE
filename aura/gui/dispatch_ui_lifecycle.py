"""GUI-side lifecycle owner for visible Worker dispatch SpecCards.

No longer owns WorkflowState transitions — those originate in the backend
_DispatchProxy. This module only creates/wires/removes SpecCards and forwards
user button-clicks (dispatch, cancel) to the bridge.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from aura.conversation.workflow_state import WorkflowState

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from aura.bridge.qt_bridge import ConversationBridge
    from aura.gui.chat_view import ChatView

_log = logging.getLogger(__name__)


class DispatchUiLifecycle:
    """Owns SpecCard creation, wiring, and removal."""

    def __init__(
        self,
        *,
        bridge: ConversationBridge,
        chat: ChatView,
        parent_widget: QWidget | None,
        active_workflow: Callable[[], WorkflowState | None],
    ) -> None:
        self._bridge = bridge
        self._chat = chat
        self._parent_widget = parent_widget
        self._active_workflow = active_workflow
        self._wired_spec_cards: set[str] = set()
        self._visible_dispatch_card_id: str | None = None

    def get_spec_card(self, tool_call_id: str):
        return self._chat.get_spec_card(tool_call_id)

    def begin_visible_dispatch(self, tool_call_id: str) -> None:
        previous_card_id = self._visible_dispatch_card_id
        if previous_card_id and previous_card_id != tool_call_id:
            self._chat.remove_spec_card(previous_card_id)
            self._wired_spec_cards.discard(previous_card_id)
        self._visible_dispatch_card_id = tool_call_id

    def begin_auto_dispatch(self, tool_call_id: str) -> None:
        previous_card_id = self._visible_dispatch_card_id
        if previous_card_id:
            self._chat.remove_spec_card(previous_card_id)
            self._wired_spec_cards.discard(previous_card_id)
        self._visible_dispatch_card_id = None

    def show_spec_card(
        self,
        *,
        tool_call_id: str,
        goal: str,
        file_list: list,
        spec: str,
        acceptance: str,
        summary: str,
        step_list: list,
    ) -> bool:
        try:
            if hasattr(self._chat, "prepare_spec_card"):
                self._chat.prepare_spec_card(tool_call_id)
            if step_list:
                card = self._chat.add_spec_card(
                    tool_call_id,
                    goal,
                    file_list,
                    spec,
                    acceptance,
                    summary,
                    steps=step_list,
                )
            else:
                card = self._chat.add_spec_card(
                    tool_call_id,
                    goal,
                    file_list,
                    spec,
                    acceptance,
                    summary,
                )
        except Exception as exc:
            logging.exception("Failed to render worker dispatch spec card")
            try:
                self._chat.add_error(
                    "Dispatch UI Error",
                    f"Could not render the dispatch card: {type(exc).__name__}: {exc}",
                )
            except Exception:
                logging.exception("Failed to show dispatch UI error")
            if self._bridge.auto_dispatch:
                self._bridge.user_dispatched(
                    tool_call_id,
                    goal,
                    file_list,
                    spec,
                    acceptance,
                    summary,
                )
            else:
                self._bridge.user_cancelled_dispatch(tool_call_id)
            return False

        state = self._active_workflow()
        if hasattr(card, "update_workflow_state") and state is not None:
            card.update_workflow_state(state)
        if tool_call_id not in self._wired_spec_cards:
            card.dispatch_clicked.connect(self._on_dispatch_clicked)
            card.edit_clicked.connect(self._on_edit_spec_clicked)
            card.cancel_clicked.connect(self._on_cancel_dispatch_clicked)
            self._wired_spec_cards.add(tool_call_id)

        return True

    def mark_worker_started(self, tool_call_id: str) -> None:
        card = self.get_spec_card(tool_call_id)
        if card:
            card.mark_worker_running()

    def mark_worker_cancelled(self, tool_call_id: str) -> None:
        card = self.get_spec_card(tool_call_id)
        if card:
            card.worker_cancelled()
        self.clear_active_spec_card(tool_call_id)

    def clear_active_spec_card(self, tool_call_id: str) -> None:
        """Remove the active plan card once the workflow reaches a terminal state."""
        self._chat.remove_spec_card(tool_call_id)
        if self._visible_dispatch_card_id == tool_call_id:
            self._visible_dispatch_card_id = None
        self._wired_spec_cards.discard(tool_call_id)
        self._chat.scroll_to_bottom(force=True)

    def _on_dispatch_clicked(self, tool_call_id: str) -> None:
        """Dispatch the spec card's current values directly.

        Only forwards the user intent to the bridge.  The backend
        _DispatchProxy owns the WorkflowState transition.
        """
        _log.info("dispatch_clicked tool_call_id=%s", tool_call_id)
        card = self.get_spec_card(tool_call_id)
        if card is None:
            return
        goal, files, spec, acceptance, summary = card.current_spec()
        accepted = self._bridge.user_dispatched(
            tool_call_id,
            goal,
            files,
            spec,
            acceptance,
            summary,
        )
        if not accepted:
            card.mark_stale()

    def _on_edit_spec_clicked(self, tool_call_id: str) -> None:
        """Open the SpecEditDialog pre-populated with the spec card's values."""
        from aura.gui.spec_edit_dialog import SpecEditDialog

        card = self.get_spec_card(tool_call_id)
        if card is None:
            return
        goal, files, spec, acceptance, summary = card.current_spec()
        dlg = SpecEditDialog(goal, files, spec, acceptance, summary, parent=self._parent_widget)
        if dlg.exec() == SpecEditDialog.DialogCode.Accepted:
            card.update_spec(dlg.goal(), dlg.files(), dlg.spec(), dlg.acceptance(), dlg.summary())
            self._chat.scroll_to_bottom(force=True)

    def _on_cancel_dispatch_clicked(self, tool_call_id: str) -> None:
        """Cancel the pending dispatch.

        Only forwards the user intent to the bridge.  The backend
        _DispatchProxy owns the WorkflowState transition.
        """
        accepted = self._bridge.user_cancelled_dispatch(tool_call_id)
        if not accepted:
            card = self.get_spec_card(tool_call_id)
            if card:
                card.mark_stale()
        else:
            self.clear_active_spec_card(tool_call_id)
