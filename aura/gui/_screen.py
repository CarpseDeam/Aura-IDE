"""Screen geometry helpers — clamp windows to available screen area."""
from __future__ import annotations

from PySide6.QtWidgets import QApplication, QWidget


def clamp_to_screen(widget: QWidget, desired_w: int, desired_h: int) -> None:
    """Resize *widget* to fit within available screen geometry, then centre it.

    The widget is sized to the smaller of (*desired_w*, *desired_h*) and
    92 % of the primary screen's available width/height, then repositioned
    so it is centred within and fully inside the available area.
    """
    screen = QApplication.primaryScreen()
    if screen is None:
        widget.resize(desired_w, desired_h)
        return

    avail = screen.availableGeometry()
    w = min(desired_w, int(avail.width() * 0.92))
    h = min(desired_h, int(avail.height() * 0.92))
    widget.resize(w, h)

    # Centre, then clamp so no edge sticks out past the available rect.
    x = avail.left() + (avail.width() - w) // 2
    y = avail.top() + (avail.height() - h) // 2
    x = max(avail.left(), min(x, avail.right() - w))
    y = max(avail.top(), min(y, avail.bottom() - h))
    widget.move(x, y)
