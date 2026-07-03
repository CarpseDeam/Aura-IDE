"""Worker TODO snapshot renderer."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from aura.gui.theme import BG_ALT, BORDER, FG, FG_DIM, SUCCESS, WARN

_log = logging.getLogger(__name__)


@dataclass
class _TodoRow:
    widget: QWidget
    icon: QLabel
    text: QLabel
    status: str = ""
    text_value: str = ""


class WorkerTodoWidget(QFrame):
    """Passive renderer for full Worker TODO snapshots."""

    _ICON_BY_STATUS = {
        "pending": "□",
        "active": "⟳",
        "done": "✓",
    }

    _COLOR_BY_STATUS = {
        "pending": FG_DIM,
        "active": WARN,
        "done": SUCCESS,
    }

    _TEXT_COLOR_BY_STATUS = {
        "pending": FG_DIM,
        "active": WARN,
        "done": FG_DIM,
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("workerTodo")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.setStyleSheet(
            f"""
            QFrame#workerTodo {{
                background: {BG_ALT};
                border-bottom: 1px solid {BORDER};
            }}
            QLabel#workerTodoTitle {{
                color: {FG_DIM};
                font-size: 11px;
                font-weight: 700;
                padding: 0;
            }}
            QLabel#workerTodoText {{
                color: {FG};
                font-size: 12px;
                padding: 0;
            }}
            QLabel#workerTodoIcon {{
                font-size: 13px;
                font-weight: 700;
                padding: 0;
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(4)

        title = QLabel("Worker TODO", self)
        title.setObjectName("workerTodoTitle")
        layout.addWidget(title)

        self._rows_host = QWidget(self)
        self._rows_layout = QVBoxLayout(self._rows_host)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(2)
        layout.addWidget(self._rows_host)

        self._rows: dict[str, _TodoRow] = {}
        self.hide()

    def update_snapshot(self, items: list[dict[str, str]]) -> None:
        """Render a full snapshot, reusing existing rows by stable id.

        Only repositions a row when its index actually changes — avoids
        unnecessary Qt layout churn that causes visible blink on every
        snapshot update.
        """
        if not items:
            self.clear()
            return

        item_ids = [str(item.get("id") or "") for item in items]
        live_ids = {item_id for item_id in item_ids if item_id}
        for stale_id in list(self._rows):
            if stale_id not in live_ids:
                self._remove_row(stale_id)

        for index, item in enumerate(items):
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            row = self._rows.get(item_id)
            if row is None:
                row = self._create_row()
                self._rows[item_id] = row
                self._rows_layout.insertWidget(index, row.widget)
            else:
                current_index = self._rows_layout.indexOf(row.widget)
                if current_index != -1 and current_index != index:
                    self._rows_layout.insertWidget(index, row.widget)
            self._apply_item(row, item)

        if self.isHidden():
            self.show()

    def clear(self) -> None:
        _log.info("DIAGNOSTIC WorkerTodoWidget.clear called row_count=%d", len(self._rows))
        for item_id in list(self._rows):
            self._remove_row(item_id)
        self.hide()

    def _create_row(self) -> _TodoRow:
        row_widget = QWidget(self._rows_host)
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        icon = QLabel(row_widget)
        icon.setObjectName("workerTodoIcon")
        icon.setFixedWidth(18)
        icon.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        row_layout.addWidget(icon)

        text = QLabel(row_widget)
        text.setObjectName("workerTodoText")
        text.setWordWrap(True)
        text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row_layout.addWidget(text, 1)

        return _TodoRow(widget=row_widget, icon=icon, text=text)

    def _apply_item(self, row: _TodoRow, item: dict[str, str]) -> None:
        status = str(item.get("status") or "pending")
        text = str(item.get("text") or "")
        if row.status != status:
            color = self._COLOR_BY_STATUS.get(status, FG_DIM)
            text_color = self._TEXT_COLOR_BY_STATUS.get(status, FG_DIM)
            row.icon.setText(self._ICON_BY_STATUS.get(status, "□"))
            row.icon.setStyleSheet(f"color: {color};")
            row.text.setStyleSheet(f"color: {text_color};")
            font = row.text.font()
            font.setStrikeOut(status == "done")
            row.text.setFont(font)
            row.status = status
        if row.text_value != text:
            row.text.setText(text)
            row.text_value = text

    def _remove_row(self, item_id: str) -> None:
        row = self._rows.pop(item_id, None)
        if row is None:
            return
        self._rows_layout.removeWidget(row.widget)
        row.widget.deleteLater()
