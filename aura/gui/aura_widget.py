"""AuraWidget — soft breathing glow effect for active-streaming indication."""
from __future__ import annotations

import math

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QRectF, QVariantAnimation
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
        glow_spread: int = 20,
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
        self._animation.setDuration(2000)
        self._animation.setLoopCount(-1)
        self._animation.valueChanged.connect(self._on_breath_changed)

    def _on_breath_changed(self, value: float) -> None:
        # Sine shaping: 0 → 1 → 0 for smooth breathe-in / breathe-out
        self._breath = math.sin(value * math.pi)
        self.update()

    def start_aura(self) -> None:
        if self._animation.state() != QAbstractAnimation.State.Running:
            self._animation.start()

    def stop_aura(self) -> None:
        self._animation.stop()
        self._breath = 0.0
        self.update()

    def transition_glow_color(self, new_color: str, duration: int = 600) -> None:
        """Animate the glow color from its current value to *new_color*."""
        target = QColor(new_color)
        start = QColor(self._glow_color)
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(duration)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        def _on_value(v: float) -> None:
            r = int(start.red() + (target.red() - start.red()) * v)
            g = int(start.green() + (target.green() - start.green()) * v)
            b = int(start.blue() + (target.blue() - start.blue()) * v)
            a = int(start.alpha() + (target.alpha() - start.alpha()) * v)
            self._glow_color = QColor(r, g, b, a)
            self.update()
        anim.valueChanged.connect(_on_value)
        anim.start()

    def set_glow_state(self, state: str) -> None:
        """Transition the glow to a semantic colour state."""
        colors = {
            "thinking": "#9b30ff",
            "coding": "#00e5ff",
        }
        color = colors.get(state)
        if color is not None:
            self.transition_glow_color(color)
            self.start_aura()

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

        # Build a ring-shaped clip path: outer rounded rect minus inner
        # rounded rect, so the glow only appears in the margin around the card.
        s = self._glow_spread
        outer_rect = QRectF(rect)
        inner_rect = QRectF(
            rect.x() + s, rect.y() + s,
            rect.width() - 2 * s, rect.height() - 2 * s,
        )
        outer_path = QPainterPath()
        outer_path.addRoundedRect(outer_rect, 8, 8)
        inner_path = QPainterPath()
        inner_path.addRoundedRect(inner_rect, 8, 8)
        ring_path = outer_path.subtracted(inner_path)
        painter.setClipPath(ring_path)

        # Radial gradient centered on the widget — still pulses with breath
        center = rect.center()
        max_r = max(rect.width(), rect.height()) * 0.5
        radius = max_r * (0.3 + 0.7 * b)  # expands/contracts with breath

        alpha = int(220 * b)  # fades in/out with breath

        c = self._glow_color
        inner = QColor(c.red(), c.green(), c.blue(), alpha)
        mid = QColor(c.red(), c.green(), c.blue(), alpha // 2)
        outer = QColor(0, 0, 0, 0)

        gradient = QRadialGradient(center, radius)
        # Position the strongest colour at the inner edge of the ring so the
        # glow fades naturally from the card border outward.
        inner_pos = 0.0
        if max_r > 0:
            inner_pos = max(0.0, (min(rect.width(), rect.height()) * 0.5 - s) / max_r)
        inner_pos = min(inner_pos, 0.99)
        gradient.setColorAt(inner_pos, inner)
        gradient.setColorAt(inner_pos + (1.0 - inner_pos) * 0.5, mid)
        gradient.setColorAt(1.0, outer)

        painter.fillRect(rect, gradient)
        painter.end()
