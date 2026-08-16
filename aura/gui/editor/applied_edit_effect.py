"""Transient visual pulse over already-correct, already-committed text.

Never mutates document content -- it only paints a short-lived background
highlight through ``DiffOverlay``'s extra-selection mechanism and clears
itself on a timer. Applying a new pulse to an editor that already has one
running cancels the previous timer first, so an interrupted or superseded
effect can never corrupt or delay the correct text underneath it.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QPlainTextEdit

from aura.gui.editor.diff_overlay import DiffOverlay

PULSE_DURATION_MS = 500


def changed_range(old_text: str, new_text: str) -> tuple[int, int]:
    """Return the ``[start, end)`` range in *new_text* that differs from *old_text*."""
    if not old_text:
        return 0, len(new_text)
    max_common = min(len(old_text), len(new_text))
    prefix = 0
    while prefix < max_common and old_text[prefix] == new_text[prefix]:
        prefix += 1
    max_suffix = max_common - prefix
    suffix = 0
    while (
        suffix < max_suffix
        and old_text[len(old_text) - 1 - suffix] == new_text[len(new_text) - 1 - suffix]
    ):
        suffix += 1
    return prefix, len(new_text) - suffix


class AppliedEditEffect:
    """Owns one cancellable highlight timer per editor.

    Purely decorative: it is applied only *after* the caller has already set
    the correct committed text, and dropping or replacing it can never
    affect what the editor displays.
    """

    def __init__(self) -> None:
        self._timers: dict[QPlainTextEdit, QTimer] = {}

    def pulse(self, editor: QPlainTextEdit, old_text: str, new_text: str) -> None:
        self.cancel(editor)
        start, end = changed_range(old_text, new_text)
        if end > start:
            DiffOverlay.mark_inserted(editor, start, end)
        else:
            DiffOverlay.clear(editor)
        timer = QTimer(editor)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda e=editor: self._clear(e))
        timer.start(PULSE_DURATION_MS)
        self._timers[editor] = timer

    def cancel(self, editor: QPlainTextEdit) -> None:
        timer = self._timers.pop(editor, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        try:
            DiffOverlay.clear(editor)
        except RuntimeError:
            pass

    def cancel_all(self) -> None:
        for editor in list(self._timers):
            self.cancel(editor)

    def _clear(self, editor: QPlainTextEdit) -> None:
        self._timers.pop(editor, None)
        try:
            DiffOverlay.clear(editor)
        except RuntimeError:
            pass


__all__ = ["AppliedEditEffect", "changed_range", "PULSE_DURATION_MS"]
