"""AuraWidget — soft breathing glow effect for active-streaming indication."""
from __future__ import annotations

import math

from PySide6.QtCore import QAbstractAnimation, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPainterPath, QRadialGradient
from PySide6.QtWidgets import QVBoxLayout, QWidget


class AuraWidget(QWidget):
    """Wrapper widget that draws a soft breathing radial glow underneath an inner card.

    The glow pulsates: it expands outward and fades in, then contracts and fades out,
    creating a low-key neon-light effect beneath the card.
    """

    def __init__(
        self,
        inner_widget: QWidget,
        glow_color: str = "#6d28d9",
        glow_spread: int = 16,
    ) -> None:
        super().__init__()
        self._glow_color = QColor(glow_color)
        self._glow_spread = glow_spread
        self._breath: float = 0.0

        self.setStyleSheet("background: transparent;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            glow_spread, glow_spread, glow_spread, glow_spread,
        )
        layout.addWidget(inner_widget)

        # Breathing animation: cycles 0.0 → 1.0 infinitely
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(2500)
        self._animation.setLoopCount(-1)
        self._animation.valueChanged.connect(self._on_breath_changed)

    def _on_breath_changed(self, value: float) -> None:
        # Sine shaping: 0 → 1 → 0 for smooth breathe-in / breathe-out
        self._breath = math.sin(value * math.pi)
        self.update()

    def start_aura(self) -> None:
        self._animation.start()

    def stop_aura(self) -> None:
        self._animation.stop()
        self._breath = 0.0
        self.update()

    def paintEvent(self, event) -> None:
        if self._animation.state() != QAbstractAnimation.State.Running:
            # No glow when idle — fully transparent
            super().paintEvent(event)
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()
        if rect.isEmpty():
            painter.end()
            return

        b = self._breath
        if b < 0.005:
            painter.end()
            return

        # Radial gradient centered on the widget
        center = rect.center()
        max_r = max(rect.width(), rect.height()) * 0.5
        radius = max_r * (0.2 + 0.8 * b)  # expands/contracts with breath

        alpha = int(90 * b)  # fades in/out with breath

        c = self._glow_color
        inner = QColor(c.red(), c.green(), c.blue(), alpha)
        mid = QColor(c.red(), c.green(), c.blue(), alpha // 2)
        outer = QColor(0, 0, 0, 0)

        gradient = QRadialGradient(center, radius)
        gradient.setColorAt(0.0, inner)
        gradient.setColorAt(0.5, mid)
        gradient.setColorAt(1.0, outer)

        # Clip to rounded rect matching the app's corner radius
        path = QPainterPath()
        path.addRoundedRect(float(rect.x()), float(rect.y()),
                            float(rect.width()), float(rect.height()),
                            8, 8)
        painter.setClipPath(path)

        painter.fillRect(rect, gradient)
        painter.end()
