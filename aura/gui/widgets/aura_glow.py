from __future__ import annotations

import math

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QRectF,
    QVariantAnimation,
)
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPainterPath,
    QPen,
    QRegion,
)
from PySide6.QtWidgets import (
    QVBoxLayout,
    QWidget,
)


class AuraWidget(QWidget):
    """Wrapper widget that draws a soft breathing halo around an inner card.

    The halo is a fixed-geometry ring hugging the inner widget's boundary,
    built from a crisp accent edge plus progressively wider, fainter
    feathered outlines. Only its opacity breathes/fades; its geometry never
    expands or contracts, so wide and tall panes get an even halo along all
    four edges instead of a radial gradient's directional banding.
    """

    # --- Tunable constants -------------------------------------------------
    # Number of concentric outline layers making up the feathered halo.
    # Layer 0 is the crisp accent edge right at the card boundary; the rest
    # fan outward through the margin, each wider and fainter than the last.
    _HALO_LAYERS = 5
    # Pen width (px) of the innermost crisp accent edge.
    _CRISP_EDGE_WIDTH = 1.6
    # Corner radius (px) of the inner card boundary, matched by the halo.
    _HALO_BASE_RADIUS = 8.0
    # Peak alpha (0-255) of the halo at full breath/envelope intensity.
    # Kept well below the old 240 ceiling so it reads as a halo, not a wall.
    _MAX_ALPHA = 130
    # Falloff exponent controlling how quickly outer layers fade; higher
    # values keep the glow concentrated near the card edge.
    _ALPHA_FALLOFF_POWER = 1.5
    # Full breathing cycle duration (ms). ~3-3.5s reads as a slow, calm pulse.
    _BREATH_DURATION_MS = 3200
    # Lowest point of the breathing cycle (fraction of full intensity) so the
    # halo never fully disappears between pulses while active.
    _BREATH_FLOOR = 0.35
    # Duration (ms) of the start/stop ease in/out fade.
    _FADE_DURATION_MS = 450

    def __init__(
        self,
        inner_widget: QWidget,
        glow_color: str = "#6d28d9",
        glow_spread: int = 20,
        content_margins: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._glow_color = QColor(glow_color)
        self._glow_spread = glow_spread
        self._breath: float = self._BREATH_FLOOR
        self._envelope: float = 0.0
        self._active: bool = False
        self._cached_ring_path: QPainterPath | None = None
        self._cached_ring_region: QRegion | None = None
        self._cached_halo_layers: list[tuple[QPainterPath, float, float]] = []

        self.setStyleSheet("background: transparent;")

        margin = content_margins if content_margins is not None else glow_spread
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            margin, margin, margin, margin,
        )
        layout.addWidget(inner_widget)

        # Breathing animation: cycles 0.0 -> 1.0 infinitely, drives opacity
        # only. Geometry (built in resizeEvent) never changes with it.
        self._breath_anim = QVariantAnimation(self)
        self._breath_anim.setStartValue(0.0)
        self._breath_anim.setEndValue(1.0)
        self._breath_anim.setDuration(self._BREATH_DURATION_MS)
        self._breath_anim.setLoopCount(-1)
        self._breath_anim.valueChanged.connect(self._on_breath_changed)

        # Start/stop ease in/out envelope (single owned instance, replaced
        # rather than stacked on every start/stop).
        self._envelope_anim: QVariantAnimation | None = None

        # Single owned color-transition animation.
        self._color_anim: QVariantAnimation | None = None

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        rect = self.rect()
        if rect.isEmpty():
            self._cached_ring_path = None
            self._cached_ring_region = None
            self._cached_halo_layers = []
            return

        s = self._glow_spread
        outer_rect = QRectF(rect)
        inner_rect = QRectF(
            rect.x() + s, rect.y() + s,
            rect.width() - 2 * s, rect.height() - 2 * s,
        )
        outer_path = QPainterPath()
        outer_path.addRoundedRect(outer_rect, self._HALO_BASE_RADIUS, self._HALO_BASE_RADIUS)
        inner_path = QPainterPath()
        inner_path.addRoundedRect(inner_rect, self._HALO_BASE_RADIUS, self._HALO_BASE_RADIUS)
        self._cached_ring_path = outer_path.subtracted(inner_path)
        self._cached_ring_region = QRegion(
            self._cached_ring_path.toFillPolygon().toPolygon(),
        )

        self._cached_halo_layers = self._build_halo_layers(inner_rect)

    def _build_halo_layers(self, inner_rect: QRectF) -> list[tuple[QPainterPath, float, float]]:
        s = self._glow_spread
        if s <= 0 or inner_rect.isEmpty():
            return []

        layers: list[tuple[QPainterPath, float, float]] = []

        # Layer 0: crisp accent edge right at the card boundary.
        crisp_path = QPainterPath()
        crisp_path.addRoundedRect(inner_rect, self._HALO_BASE_RADIUS, self._HALO_BASE_RADIUS)
        layers.append((crisp_path, self._CRISP_EDGE_WIDTH, 1.0))

        # Remaining layers fill the margin outward in adjoining, non-
        # overlapping bands so composited (stacked) alpha never exceeds a
        # single layer's own alpha - avoids a "neon wall" from stacked
        # translucent strokes piling up past _MAX_ALPHA, especially at
        # rounded corners where concentric strokes would otherwise overlap.
        segments = max(1, self._HALO_LAYERS - 1)
        band = (s - self._CRISP_EDGE_WIDTH / 2) / segments
        for i in range(segments):
            # Deliberately never reaches t=1.0 - the outermost band keeps a
            # faint sliver of alpha so the feather tapers smoothly into the
            # antialiased stroke edge instead of visibly stepping to zero.
            t = i / segments
            inset = self._CRISP_EDGE_WIDTH / 2 + (i + 0.5) * band
            layer_rect = inner_rect.adjusted(-inset, -inset, inset, inset)
            radius = self._HALO_BASE_RADIUS + inset
            path = QPainterPath()
            path.addRoundedRect(layer_rect, radius, radius)

            alpha_fraction = (1.0 - t) ** self._ALPHA_FALLOFF_POWER
            layers.append((path, band * 1.02, alpha_fraction))
        return layers

    def _request_update(self) -> None:
        if self._cached_ring_region is not None:
            self.update(self._cached_ring_region)
        else:
            self.update()

    def _on_breath_changed(self, value: float) -> None:
        # Smooth sine breathing between the floor and full intensity - no
        # abrupt zero-to-full pulse.
        wave = math.sin(value * math.pi)
        self._breath = self._BREATH_FLOOR + (1.0 - self._BREATH_FLOOR) * wave
        self._request_update()

    def start_aura(self) -> None:
        self._active = True
        if self._breath_anim.state() != QAbstractAnimation.State.Running:
            self._breath_anim.start()
        self._animate_envelope(1.0)

    def stop_aura(self) -> None:
        self._active = False

        def _on_faded_out() -> None:
            if not self._active:
                self._breath_anim.stop()
                self._breath = self._BREATH_FLOOR
                self._request_update()

        self._animate_envelope(0.0, on_finished=_on_faded_out)

    def _animate_envelope(self, target: float, on_finished=None) -> None:
        if self._envelope_anim is not None:
            self._envelope_anim.stop()

        anim = QVariantAnimation(self)
        anim.setStartValue(self._envelope)
        anim.setEndValue(target)
        anim.setDuration(self._FADE_DURATION_MS)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

        def _on_value(v: float) -> None:
            self._envelope = v
            self._request_update()

        anim.valueChanged.connect(_on_value)
        if on_finished is not None:
            anim.finished.connect(on_finished)
        self._envelope_anim = anim
        anim.start()

    def transition_glow_color(self, new_color: str, duration: int = 600) -> None:
        """Animate the glow color from its current value to *new_color*.

        Retains exactly one owned color-transition animation: a new call
        stops and replaces any transition already in flight rather than
        stacking overlapping animations.
        """
        if self._color_anim is not None:
            self._color_anim.stop()

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
            self._request_update()

        anim.valueChanged.connect(_on_value)
        self._color_anim = anim
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
        if self._envelope <= 0.0 and self._breath_anim.state() != QAbstractAnimation.State.Running:
            # Idle - fully transparent.
            super().paintEvent(event)
            return

        if not self._cached_halo_layers:
            super().paintEvent(event)
            return

        intensity = self._breath * self._envelope
        if intensity < 0.001:
            super().paintEvent(event)
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._cached_ring_path is not None:
            painter.setClipPath(self._cached_ring_path)

        c = self._glow_color
        for path, pen_width, alpha_fraction in self._cached_halo_layers:
            alpha = int(self._MAX_ALPHA * alpha_fraction * intensity)
            if alpha <= 0:
                continue
            pen = QPen(QColor(c.red(), c.green(), c.blue(), min(alpha, 255)))
            pen.setWidthF(pen_width)
            painter.setPen(pen)
            painter.drawPath(path)

        painter.end()
