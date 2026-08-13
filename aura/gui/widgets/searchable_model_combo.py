"""A QComboBox whose dropdown is a searchable, filtered list.

Single shared owner for the model-picker search UX used by both
``ModelsPage`` (Settings) and ``LeftPane`` (main window) — see
``aura.providers.model_presentation`` for the data those callers feed in.
This widget owns UI/filter/selection behavior only; it knows nothing about
providers, discovery, or persistence.
"""

from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from aura.gui.widgets.no_wheel_combo import NoWheelComboBox
from aura.providers.model_presentation import ModelPickerItem

__all__ = ["ModelPickerItem", "SearchableModelCombo"]


class _SearchPopup(QFrame):
    """The search box + filtered list shown as the combo's dropdown."""

    item_chosen = Signal(int)  # index into the unfiltered entries passed to load()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search models…")
        layout.addWidget(self.search)

        self.list = QListWidget(self)
        layout.addWidget(self.list)

        self._source: list[tuple[str, object]] = []  # (label, data), combo order

        self.search.textChanged.connect(self._apply_filter)
        self.search.installEventFilter(self)
        self.list.itemActivated.connect(self._choose_item)
        self.list.itemClicked.connect(self._choose_item)

    def load(self, entries: list[tuple[str, object]], current_row: int) -> None:
        """Reset the popup to show *entries* (label, data) with no search text."""
        self._source = entries
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._rebuild(entries)
        if 0 <= current_row < self.list.count():
            self.list.setCurrentRow(current_row)

    def _rebuild(self, entries: list[tuple[str, object]]) -> None:
        self.list.clear()
        for label, data in entries:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, data)
            self.list.addItem(item)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        matches = [
            (label, data)
            for label, data in self._source
            if not needle or needle in label.lower() or needle in str(data).lower()
        ]
        self._rebuild(matches)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _choose_item(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        for index, (_, source_data) in enumerate(self._source):
            if source_data == data:
                self.item_chosen.emit(index)
                break
        self.close()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 — Qt override
        if obj is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Down:
                row = min(self.list.currentRow() + 1, self.list.count() - 1)
                if row >= 0:
                    self.list.setCurrentRow(row)
                return True
            if key == Qt.Key.Key_Up:
                row = max(self.list.currentRow() - 1, 0)
                self.list.setCurrentRow(row)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                item = self.list.currentItem()
                if item is not None:
                    self._choose_item(item)
                return True
            if key == Qt.Key.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, event)


class SearchableModelCombo(NoWheelComboBox):
    """QComboBox whose dropdown is a searchable list filtered by label or id.

    Keeps the ordinary QComboBox contract — ``addItem``/``currentData``/
    ``findData``/``setCurrentIndex``/``blockSignals``/``clear``/``count``/
    ``currentIndexChanged`` — so existing callers that treat it as a plain
    combo keep working; only the dropdown itself is replaced. The selected
    value is always one of the ids handed to ``set_items``: the search field
    filters, it never becomes the value (no ``setEditable(True)``).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._popup = _SearchPopup(self)
        self._popup.item_chosen.connect(self._on_item_chosen)

    def set_items(
        self,
        items: Sequence[ModelPickerItem],
        selected_model_id: str | None = None,
    ) -> None:
        """Replace all entries and select *selected_model_id* if present.

        Falls back to the first item when the requested id is absent. No
        signal fires for the population itself, matching plain-QComboBox
        population code this replaces.
        """
        self.blockSignals(True)
        self.clear()
        for item in items:
            self.addItem(item.label, item.model_id)
        idx = self.findData(selected_model_id) if selected_model_id else -1
        if idx < 0 and self.count():
            idx = 0
        if idx >= 0:
            self.setCurrentIndex(idx)
        self.blockSignals(False)

    def showPopup(self) -> None:  # noqa: N802 — Qt override
        entries = [(self.itemText(i), self.itemData(i)) for i in range(self.count())]
        self._popup.load(entries, self.currentIndex())

        width = max(self.width(), 260)
        height = min(320, 48 + 22 * max(1, len(entries)))
        self._popup.resize(width, height)
        self._popup.move(self.mapToGlobal(self.rect().bottomLeft()))
        self._popup.show()
        self._popup.search.setFocus(Qt.FocusReason.PopupFocusReason)

    def hidePopup(self) -> None:  # noqa: N802 — Qt override
        self._popup.hide()

    def _on_item_chosen(self, row: int) -> None:
        if 0 <= row < self.count():
            self.setCurrentIndex(row)
