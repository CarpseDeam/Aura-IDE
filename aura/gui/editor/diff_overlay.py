"""Diff overlay — paints red/green background highlights on editor text."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit


class DiffOverlay:
    """Stateless helper that applies red (deleted) / green (inserted)
    extra-selection highlights to a QPlainTextEdit."""

    DELETED_COLOR = QColor(247, 118, 142, 58)
    INSERTED_COLOR = QColor(158, 206, 106, 48)

    @staticmethod
    def mark_deleted(editor: QPlainTextEdit, start: int, end: int) -> None:
        DiffOverlay._set_mark(editor, start, end, DiffOverlay.DELETED_COLOR)

    @staticmethod
    def mark_inserted(editor: QPlainTextEdit, start: int, end: int) -> None:
        DiffOverlay._set_mark(editor, start, end, DiffOverlay.INSERTED_COLOR)

    @staticmethod
    def clear(editor: QPlainTextEdit) -> None:
        editor.setExtraSelections([])

    @staticmethod
    def _set_mark(editor: QPlainTextEdit, start: int, end: int, color: QColor) -> None:
        if end <= start:
            DiffOverlay.clear(editor)
            return
        text_len = len(editor.toPlainText())
        cursor = QTextCursor(editor.document())
        cursor.setPosition(max(0, min(start, text_len)))
        cursor.setPosition(max(0, min(end, text_len)), QTextCursor.MoveMode.KeepAnchor)
        selection = QTextEdit.ExtraSelection()
        fmt = QTextCharFormat()
        fmt.setBackground(color)
        selection.format = fmt
        selection.cursor = cursor
        editor.setExtraSelections([selection])
