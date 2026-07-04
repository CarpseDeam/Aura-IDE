"""Pinned host for active Worker dispatch spec cards."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from aura.gui.cards.spec_card import SpecCard
from aura.gui.theme import BG_ALT, BORDER, FG_DIM


class SpecCardHost(QWidget):
    """Stable, non-scrolling container for active Worker plan cards."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cards: dict[str, SpecCard] = {}
        self.setObjectName("specCardHost")
        self.setVisible(False)
        self.setStyleSheet(
            f"QWidget#specCardHost {{ background: {BG_ALT}; "
            f"border: 1px solid {BORDER}; border-radius: 8px; }}"
        )

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(10, 8, 10, 10)
        self._layout.setSpacing(8)

        self._title = QLabel("Active Plan", parent=self)
        self._title.setStyleSheet(
            f"color: {FG_DIM}; font-size: 10px; font-weight: 700;"
        )
        self._layout.addWidget(self._title)

    def add_spec_card(
        self,
        tool_call_id: str,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
        summary: str = "",
    ) -> SpecCard:
        existing = self._cards.get(tool_call_id)
        if existing is not None:
            existing.update_spec(goal, files, spec, acceptance, summary)
            self.setVisible(True)
            return existing

        for existing_id in list(self._cards):
            self.remove_spec_card(existing_id)

        card = SpecCard(
            tool_call_id,
            goal,
            files,
            spec,
            acceptance,
            summary=summary,
            parent=self,
        )
        self._cards[tool_call_id] = card
        self._layout.addWidget(card)
        self.setVisible(True)
        return card

    def get_spec_card(self, tool_call_id: str) -> SpecCard | None:
        return self._cards.get(tool_call_id)

    def remove_spec_card(self, tool_call_id: str) -> None:
        card = self._cards.pop(tool_call_id, None)
        if card is None:
            return
        self._layout.removeWidget(card)
        card.setParent(None)
        card.deleteLater()
        self.setVisible(bool(self._cards))

    def clear(self) -> None:
        for tool_call_id in list(self._cards):
            self.remove_spec_card(tool_call_id)
