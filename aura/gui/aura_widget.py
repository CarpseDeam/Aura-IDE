"""AuraWidget — animated spinning conic-gradient border for active-streaming indication."""
from __future__ import annotations

from PySide6.QtCore import QAbstractAnimation, QPropertyAnimation, QVariantAnimation
from PySide6.QtGui import QColor, QConicGradient, QPainter, QPainterPath, QRegion
from PySide6.QtWidgets import QVBoxLayout, QWidget


class AuraWidget(QWidget):
    """Wrapper widget that draws a spinning gradient border around an inner widget.

    The inner widget's solid background masks the center of the conic gradient so
    only a thin moving streak is visible at the wrapper's margin area.
    """

    def __init__(
        self,
        inner_widget: QWidget,
        aura_color: str = "#7aa2f7",
        border_thickness: int = 1,
    ) -> None:
        super().__init__()
        self._aura_color = QColor(aura_color)
        self._border_thickness = border_thickness
        self._angle: float = 0.0

        self.setStyleSheet("background: transparent;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            border_thickness, border_thickness,
            border_thickness, border_thickness,
        )
        layout.addWidget(inner_widget)

        # Infinite looping angle animation
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(360.0)
        self._animation.setDuration(2500)
        self._animation.setLoopCount(-1)  # infinite
        self._animation.valueChanged.connect(self._on_angle_changed)

    def _on_angle_changed(self, value: float) -> None:
        self._angle = value
        self.update()

    def start_aura(self) -> None:
        self._animation.start()

    def stop_aura(self) -> None:
        self._animation.stop()
        self.update()  # repaint without gradient

    def paintEvent(self, event) -> None:
        if self._animation.state() != QAbstractAnimation.State.Running:
            # When not animating, draw nothing (transparent background).
            # The inner card's own border shows through.
            super().paintEvent(event)
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()
        radius = 8

        # Clip to rounded rect so the gradient doesn't bleed into sharp corners
        path = QPainterPath()
        path.addRoundedRect(float(rect.x()), float(rect.y()),
                            float(rect.width()), float(rect.height()),
                            radius, radius)
        painter.setClipPath(path)

        # Conic gradient: mostly transparent with a concentrated streak
        # at the leading edge that rotates as _angle increases.
        center = rect.center()
        gradient = QConicGradient(center, -self._angle)
        gradient.setColorAt(0.00, QColor(0, 0, 0, 0))       # transparent
        gradient.setColorAt(0.40, QColor(0, 0, 0, 0))       # transparent
        gradient.setColorAt(0.48, self._aura_color)           # streak start
        gradient.setColorAt(0.50, self._aura_color)           # peak
        gradient.setColorAt(0.52, self._aura_color)           # streak end
        gradient.setColorAt(0.60, QColor(0, 0, 0, 0))       # transparent
        gradient.setColorAt(1.00, QColor(0, 0, 0, 0))       # transparent

        painter.fillRect(rect, gradient)
        painter.end()
