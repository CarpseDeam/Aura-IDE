from __future__ import annotations

import math
import weakref

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QObject,
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


class AuraPhaseDriver(QObject):
    """One shared, demand-driven animation clock for every desktop Aura halo."""

    BREATH_DURATION_MS = 3200
    BREATH_FLOOR = 0.35
    BREATHS_PER_HUE_ROTATION = 4
    HUE_DURATION_MS = BREATH_DURATION_MS * BREATHS_PER_HUE_ROTATION
    _RAINBOW_SATURATION = 0.72
    _RAINBOW_VALUE = 1.0

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._phase = 0.0
        self._breath = self.BREATH_FLOOR
        self._color = self._rainbow_color(0.0)
        self._registrations: dict[int, weakref.ReferenceType[AuraWidget]] = {}

        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(self.HUE_DURATION_MS)
        self._animation.setLoopCount(-1)
        self._animation.valueChanged.connect(self._on_phase_changed)

    @property
    def phase(self) -> float:
        return self._phase

    @property
    def breath(self) -> float:
        return self._breath

    @property
    def color(self) -> QColor:
        return QColor(self._color)

    @property
    def active_widget_count(self) -> int:
        self._purge_dead_registrations()
        return len(self._registrations)

    def is_registered(self, widget: AuraWidget) -> bool:
        ref = self._registrations.get(id(widget))
        return ref is not None and ref() is widget

    def attach(self, widget: AuraWidget) -> None:
        """Register *widget* once and immediately synchronize its current phase."""
        key = id(widget)
        current_ref = self._registrations.get(key)
        if current_ref is None or current_ref() is not widget:
            widget_ref = weakref.ref(
                widget,
                lambda _ref, registration_key=key: self._remove_registration(
                    registration_key
                ),
            )
            self._registrations[key] = widget_ref
            widget.destroyed.connect(
                lambda _obj=None, registration_key=key: self._remove_registration(
                    registration_key
                )
            )

        if self._animation.state() == QAbstractAnimation.State.Paused:
            self._animation.resume()
        elif self._animation.state() == QAbstractAnimation.State.Stopped:
            self._animation.start()
        self._sync_widget(widget)

    def detach(self, widget: AuraWidget) -> None:
        """Unregister *widget* without disturbing any other halo's phase."""
        key = id(widget)
        ref = self._registrations.get(key)
        if ref is not None and ref() is widget:
            self._remove_registration(key)

    def _remove_registration(self, key: int) -> None:
        self._registrations.pop(key, None)
        if (
            not self._registrations
            and self._animation.state() == QAbstractAnimation.State.Running
        ):
            # Pausing retains the shared phase for a later Aura while producing
            # no frame callbacks when the desktop has no active/fading halo.
            self._animation.pause()

    def _purge_dead_registrations(self) -> None:
        for key, ref in tuple(self._registrations.items()):
            if ref() is None:
                self._registrations.pop(key, None)
        if (
            not self._registrations
            and self._animation.state() == QAbstractAnimation.State.Running
        ):
            self._animation.pause()

    def _on_phase_changed(self, value: float) -> None:
        self._phase = float(value) % 1.0
        breath_phase = (self._phase * self.BREATHS_PER_HUE_ROTATION) % 1.0
        wave = math.sin(breath_phase * math.pi)
        self._breath = self.BREATH_FLOOR + (1.0 - self.BREATH_FLOOR) * wave
        self._color = self._rainbow_color(self._phase)

        for key, ref in tuple(self._registrations.items()):
            widget = ref()
            if widget is None:
                self._registrations.pop(key, None)
                continue
            try:
                self._sync_widget(widget)
            except RuntimeError:
                # PySide can briefly retain a Python wrapper after its C++
                # QWidget has been deleted. It must not keep the clock alive.
                self._registrations.pop(key, None)
        self._purge_dead_registrations()

    def _sync_widget(self, widget: AuraWidget) -> None:
        widget._sync_shared_phase(self._breath, self._color)

    @classmethod
    def _rainbow_color(cls, phase: float) -> QColor:
        return QColor.fromHsvF(
            phase % 1.0,
            cls._RAINBOW_SATURATION,
            cls._RAINBOW_VALUE,
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
    _BREATH_DURATION_MS = AuraPhaseDriver.BREATH_DURATION_MS
    # Lowest point of the breathing cycle (fraction of full intensity) so the
    # halo never fully disappears between pulses while active.
    _BREATH_FLOOR = AuraPhaseDriver.BREATH_FLOOR
    # Duration (ms) of the start/stop ease in/out fade.
    _FADE_DURATION_MS = 450

    def __init__(
        self,
        inner_widget: QWidget,
        phase_driver: AuraPhaseDriver,
        glow_spread: int = 20,
        content_margins: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._phase_driver = phase_driver
        self._glow_color = phase_driver.color
        self._glow_spread = glow_spread
        self._breath: float = phase_driver.breath
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

        # Start/stop ease in/out envelope (single owned instance, replaced
        # rather than stacked on every start/stop).
        self._envelope_anim: QVariantAnimation | None = None
        self._envelope_target = 0.0

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

    def _sync_shared_phase(self, breath: float, color: QColor) -> None:
        self._breath = breath
        self._glow_color = QColor(color)
        self._request_update()

    def start_aura(self) -> None:
        if self._active:
            self._phase_driver.attach(self)
            self._animate_envelope(1.0)
            return
        self._active = True
        self._phase_driver.attach(self)
        self._animate_envelope(1.0)

    def stop_aura(self) -> None:
        if not self._active:
            return
        self._active = False

        def _on_faded_out() -> None:
            if not self._active:
                self._phase_driver.detach(self)
                self._request_update()

        self._animate_envelope(0.0, on_finished=_on_faded_out)

    def _animate_envelope(self, target: float, on_finished=None) -> None:
        if (
            self._envelope_anim is not None
            and self._envelope_anim.state() == QAbstractAnimation.State.Running
            and self._envelope_target == target
        ):
            return
        if self._envelope_anim is not None:
            self._envelope_anim.stop()
        if self._envelope == target:
            self._envelope_target = target
            if on_finished is not None:
                on_finished()
            return

        self._envelope_target = target
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

    def paintEvent(self, event) -> None:
        if self._envelope <= 0.0:
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
