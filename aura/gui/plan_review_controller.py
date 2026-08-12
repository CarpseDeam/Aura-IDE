"""GUI-side coordination for the Plan Review card lifecycle.

Connects one ``PlanReviewProxy``'s ``reviewRequested`` signal to the chat
view, and forwards the card's Implement / Edit Plan / Cancel actions back
through the proxy so the blocked worker thread resumes. One active review at
a time is sufficient: normal production runs one turn, and the worker thread
blocks in ``request_review`` until this controller resolves it.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aura.conversation.plan_review import ApprovedPlan

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from aura.bridge.plan_review_proxy import PlanReviewProxy
    from aura.gui.cards.plan_review_card import PlanReviewCard
    from aura.gui.chat_view import ChatView

_log = logging.getLogger(__name__)


class PlanReviewController:
    """Owns Plan Review card creation, wiring, and resolution forwarding."""

    def __init__(
        self,
        *,
        proxy: "PlanReviewProxy",
        chat: "ChatView",
        parent_widget: "QWidget | None",
    ) -> None:
        self._proxy = proxy
        self._chat = chat
        self._parent_widget = parent_widget
        self._active_card: "PlanReviewCard | None" = None
        self._edited: bool = False
        proxy.reviewRequested.connect(self._on_review_requested)

    def _on_review_requested(
        self,
        review_id: str,
        goal: str,
        files: list,
        spec: str,
        acceptance: str,
        summary: str,
    ) -> None:
        self._edited = False
        try:
            card = self._chat.add_plan_review_card(
                review_id, goal, list(files), spec, acceptance, summary
            )
        except Exception:
            _log.exception("Failed to render Plan Review card")
            self._proxy.resolve_cancelled(review_id)
            return
        card.implement_clicked.connect(self._on_implement_clicked)
        card.edit_clicked.connect(self._on_edit_clicked)
        card.cancel_clicked.connect(self._on_cancel_clicked)
        self._active_card = card

    def _card_for(self, review_id: str) -> "PlanReviewCard | None":
        card = self._active_card
        if card is not None and card.review_id() == review_id:
            return card
        return None

    def _on_implement_clicked(self, review_id: str) -> None:
        card = self._card_for(review_id)
        if card is None:
            return
        goal, files, spec, acceptance, summary = card.current_plan()
        plan = ApprovedPlan(
            goal=goal, files=tuple(files), spec=spec, acceptance=acceptance, summary=summary,
        )
        self._proxy.resolve_approved(review_id, plan, user_edited=self._edited)
        self._chat.record_plan_review(
            goal, files, spec, acceptance, summary,
            approved=True, user_edited=self._edited,
        )
        self._active_card = None

    def _on_cancel_clicked(self, review_id: str) -> None:
        card = self._card_for(review_id)
        if card is None:
            return
        goal, files, spec, acceptance, summary = card.current_plan()
        self._proxy.resolve_cancelled(review_id)
        self._chat.record_plan_review(
            goal, files, spec, acceptance, summary,
            approved=False, user_edited=self._edited,
        )
        self._active_card = None

    def _on_edit_clicked(self, review_id: str) -> None:
        from aura.gui.plan_edit_dialog import PlanEditDialog

        card = self._card_for(review_id)
        if card is None:
            return
        goal, files, spec, acceptance, summary = card.current_plan()
        dlg = PlanEditDialog(goal, files, spec, acceptance, summary, parent=self._parent_widget)
        if dlg.exec() == PlanEditDialog.DialogCode.Accepted:
            card.update_plan(dlg.goal(), dlg.files(), dlg.spec(), dlg.acceptance(), dlg.summary())
            self._edited = True
            self._chat.scroll_to_bottom(force=True)


__all__ = ["PlanReviewController"]
