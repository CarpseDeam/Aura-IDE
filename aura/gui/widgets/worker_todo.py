"""Worker TODO snapshot renderer."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import Qt  # pyright: ignore
from PySide6.QtWidgets import (  # pyright: ignore
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aura.gui.theme import ACCENT, BG_ALT, BORDER, FG, FG_DIM, FG_MUTED, SUCCESS, WARN

_log = logging.getLogger(__name__)


@dataclass
class _TodoRow:
    widget: QFrame
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
        "pending": FG_MUTED,
        "active": ACCENT,
        "done": FG_MUTED,
    }

    _TEXT_COLOR_BY_STATUS = {
        "pending": FG_MUTED,
        "active": FG,
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

        self._title = QLabel("WORKER TODO", self)
        self._title.setObjectName("workerTodoTitle")
        layout.addWidget(self._title)

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

        # Update progress title
        done_count = sum(1 for item in items if item.get("status") == "done")
        total = len(items)
        self._title.setText(f"WORKER TODO \u00b7 {done_count}/{total}")

        item_ids = [str(item.get("id") or "") for item in items]
        live_ids = {item_id for item_id in item_ids if item_id}

        current_ids = set(self._rows.keys())
        if current_ids and live_ids and not (current_ids & live_ids):
            self._apply_step_transition(items, item_ids)
        else:
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
        row_widget = QFrame(self._rows_host)
        row_widget.setFrameShape(QFrame.Shape.NoFrame)
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
            # Active gets an accent left edge; done/pending get no border
            if status == "active":
                row.widget.setStyleSheet(
                    f"QFrame {{ background: transparent; border-left: 3px solid {ACCENT}; padding-left: 6px; }}"
                )
            else:
                row.widget.setStyleSheet(
                    "QFrame { background: transparent; border-left: none; }"
                )
        if row.text_value != text:
            row.text.setText(text)
            row.text_value = text

    def _apply_step_transition(self, items: list[dict[str, str]], item_ids: list[str]) -> None:
        """Reuse row widgets by position when step transitions (all IDs changed)."""
        old_rows: list[_TodoRow] = []
        for i in range(self._rows_layout.count()):
            w = self._rows_layout.itemAt(i).widget()
            if w is not None:
                for row in self._rows.values():
                    if row.widget is w:
                        old_rows.append(row)
                        break

        num_old = len(old_rows)
        num_new = len(items)

        self._rows.clear()

        for index in range(min(num_old, num_new)):
            item_id = item_ids[index]
            if not item_id:
                continue
            row = old_rows[index]
            self._rows[item_id] = row
            self._apply_item(row, items[index])

        for index in range(num_new, num_old):
            row = old_rows[index]
            self._rows_layout.removeWidget(row.widget)
            row.widget.deleteLater()

        for index in range(num_old, num_new):
            item_id = item_ids[index]
            if not item_id:
                continue
            row = self._create_row()
            self._rows[item_id] = row
            self._rows_layout.insertWidget(index, row.widget)
            self._apply_item(row, items[index])

    def _remove_row(self, item_id: str) -> None:

        row = self._rows.pop(item_id, None)
        if row is None:
            return
        self._rows_layout.removeWidget(row.widget)
        row.widget.deleteLater()
