from __future__ import annotations

import math
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

if TYPE_CHECKING:
    from aura.gui.drones.chain_canvas import ChainCanvas


PALETTES: dict[str, tuple[str, str, str]] = {
    "smoky-teal":    ("#6fa8a8", "#b8f3ee", "#172426"),
    "dusk-purple":   ("#8c7bc8", "#d8ccff", "#1d1828"),
    "icy-blue":      ("#7ea5c8", "#d4edff", "#14202b"),
    "sage-glass":    ("#8aa89a", "#d6f0df", "#18231d"),
    "violet-mist":   ("#a09ac8", "#ece8ff", "#1c1a28"),
    "slate-ice":     ("#9aa8b8", "#e4f0ff", "#171d26"),
}


class GoalPlanetItem(QGraphicsObject):
    """A small planet-like node representing the mission goal."""

    planetChanged = Signal()

    def __init__(self, node_id: str, canvas: ChainCanvas, goal_id: str = ""):
        super().__init__()
        self._node_id = node_id
        self._canvas = canvas
        self._goal_id = goal_id
        self._title: str = ""
        self._objective: str = ""
        self._glow_phase = 0.0
        self._seed: int = 0
        self._style: str = "auto"
        self._planet_cache: QPixmap | None = None
        self._cache_key: tuple = ()

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(1200)
        self._pulse_timer.timeout.connect(self._on_tick)
        self._pulse_timer.start()

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptDrops(True)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._ensure_seed()

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def goal_id(self) -> str:
        return self._goal_id

    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, value: str) -> None:
        self._title = value
        self.planetChanged.emit()
        self.update()

    @property
    def objective(self) -> str:
        return self._objective

    @objective.setter
    def objective(self, value: str) -> None:
        self._objective = value
        self.planetChanged.emit()
        self.update()

    @property
    def goal(self) -> str:
        return self._objective

    @goal.setter
    def goal(self, value: str) -> None:
        self._objective = value
        self.planetChanged.emit()
        self.update()

    def _on_tick(self) -> None:
        self._glow_phase += 0.12
        self.update()

    def _ensure_seed(self) -> None:
        import random as _random
        if self._seed == 0:
            self._seed = _random.randint(1, 2**31 - 1)
        self._invalidate_cache()

    def _invalidate_cache(self) -> None:
        self._planet_cache = None
        self._cache_key = ()
        self.update()

    def _palette_for_seed(self) -> tuple:
        return list(PALETTES.values())[self._seed % len(PALETTES)]

    def reroll_seed(self) -> None:
        import random as _random
        self._seed = _random.randint(1, 2**31 - 1)
        self._invalidate_cache()

    def _draw_soft_halo(self, p, center, radius, base):
        g = QRadialGradient(center, radius * 2.0)
        g.setColorAt(0.0, QColor(base.red(), base.green(), base.blue(), 0))
        g.setColorAt(0.45, QColor(base.red(), base.green(), base.blue(), 10))
        g.setColorAt(1.0, QColor(base.red(), base.green(), base.blue(), 0))
        p.setBrush(QBrush(g))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(center, radius * 1.9, radius * 1.9)

    def _draw_body(self, p, center, radius, base, rim, shadow, lx, ly):
        focal = QPointF(lx, ly)
        g_body = QRadialGradient(focal, radius * 1.4)
        g_body.setColorAt(0.00, QColor(shadow.red(), shadow.green(), shadow.blue(), 95))
        g_body.setColorAt(0.45, QColor(base.red(), base.green(), base.blue(), 105))
        g_body.setColorAt(0.75, QColor(base.red(), base.green(), base.blue(), 70))
        g_body.setColorAt(1.00, QColor(shadow.red(), shadow.green(), shadow.blue(), 35))
        p.setBrush(QBrush(g_body))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(center, radius, radius)

    def _draw_gas_bands(self, p, center, radius, rng, base, rim, shadow):
        num_bands = rng.randint(2, 4)
        for i in range(num_bands):
            phi = rng.uniform(-0.65, 0.65)
            y_center = center.y() + radius * math.sin(phi)
            half_w = radius * math.cos(phi)
            top_bow = rng.uniform(1.0, 2.5)
            bot_bow = rng.uniform(1.0, 2.5)
            band_path = QPainterPath()
            band_path.moveTo(center.x() - half_w, y_center - 1.2)
            band_path.cubicTo(
                center.x() - half_w * 0.45, y_center - 1.2 + top_bow,
                center.x() + half_w * 0.45, y_center - 1.2 + top_bow,
                center.x() + half_w, y_center - 1.2,
            )
            band_path.lineTo(center.x() + half_w, y_center + 1.2)
            band_path.cubicTo(
                center.x() + half_w * 0.45, y_center + 1.2 + bot_bow,
                center.x() - half_w * 0.45, y_center + 1.2 + bot_bow,
                center.x() - half_w, y_center + 1.2,
            )
            band_path.closeSubpath()
            if i % 2 == 0:
                band_color = QColor(rim)
                band_color.setAlpha(rng.randint(10, 22))
            else:
                band_color = QColor(shadow)
                band_color.setAlpha(rng.randint(8, 18))
            p.setBrush(QBrush(band_color))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(band_path)

    def boundingRect(self) -> QRectF:
        return QRectF(-44, -44, 88, 88)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        center = QPointF(0, 0)
        radius = 24

        # Cache check
        is_sel = int(self.isSelected())
        key = (self._seed, is_sel)
        if self._planet_cache is None or self._cache_key != key:
            self._planet_cache = self._render_planet(radius)
            self._cache_key = key

        # Draw cached planet body (origin at -radius, -radius since pixmap has padding)
        pm = self._planet_cache
        offset = QPointF(-pm.width() / 2, -pm.height() / 2)
        painter.drawPixmap(offset, pm)

        # Subtle receiver pulse ring (animates)
        _, rim_str, _ = self._palette_for_seed()
        rim_color = QColor(rim_str)
        pulse_norm = (math.sin(self._glow_phase) + 1.0) / 2.0
        if self.isSelected():
            pulse_alpha = int(35 + pulse_norm * 30)
        else:
            pulse_alpha = int(8 + pulse_norm * 8)
        rim_color.setAlpha(max(0, min(255, pulse_alpha)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(rim_color, 1.25))
        painter.drawEllipse(center, radius + 9, radius + 9)

        # Selected — orbit/receiver ring and faint outer glow
        if self.isSelected():
            # Thin orbit ring
            orbit_color = QColor(rim_str)
            orbit_color.setAlpha(60)
            painter.setPen(QPen(orbit_color, 1.0))
            painter.drawEllipse(center, radius + 3, radius + 3)
            # Faint outer glow
            glow_color = QColor(rim_str)
            glow_color.setAlpha(18)
            painter.setPen(QPen(glow_color, 4.0))
            painter.drawEllipse(center, radius + 4, radius + 4)

    def _render_planet(self, radius: int) -> QPixmap:
        import random as _random
        rng = _random.Random(self._seed)

        pad = 18
        scale = 2
        logical_size = radius * 2 + pad * 2
        device_size = logical_size * scale
        pix = QPixmap(device_size, device_size)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(scale, scale)
        center = QPointF(logical_size / 2, logical_size / 2)

        base_str, rim_str, shadow_str = self._palette_for_seed()
        base = QColor(base_str)
        rim = QColor(rim_str)
        shadow = QColor(shadow_str)

        # Light from upper-left; shadow lower-right
        focal_x = center.x() + 0.25 * radius
        focal_y = center.y() + 0.25 * radius

        # a) Soft tactical halo (behind body)
        self._draw_soft_halo(p, center, radius, base)

        # b) Ring BACK arc (optional)
        has_ring = (self._seed % 12) < 2
        ring_radius_val = 0.0
        tilt = 0.4
        rect_ring = QRectF()
        arc_start = 0
        arc_sweep = 0
        if has_ring:
            ring_radius_val = radius + rng.uniform(5, 9)
            tilt = rng.uniform(0.28, 0.48)
            rect_ring = QRectF(
                center.x() - ring_radius_val,
                center.y() - ring_radius_val * tilt,
                ring_radius_val * 2,
                ring_radius_val * 2 * tilt,
            )
            arc_start = int(rng.uniform(0, 360) * 16)
            arc_sweep = int(rng.uniform(200, 280) * 16)
            back_arc = QColor(rim)
            back_arc.setAlpha(rng.randint(16, 26))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(back_arc, 1.0))
            p.drawArc(rect_ring, arc_start, arc_sweep)

        # c) Planet body — translucent glass
        self._draw_body(p, center, radius, base, rim, shadow, focal_x, focal_y)

        # d) Subtle inner depth (clipped to body)
        clip_path = QPainterPath()
        clip_path.addEllipse(center, radius, radius)
        p.setClipPath(clip_path)

        depth_center = QPointF(center.x() + 0.35 * radius, center.y() + 0.35 * radius)
        g_depth = QRadialGradient(depth_center, radius * 0.7)
        g_depth.setColorAt(0.0, QColor(0, 0, 0, 0))
        g_depth.setColorAt(1.0, QColor(0, 0, 0, rng.randint(35, 55)))
        p.setBrush(QBrush(g_depth))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(center, radius, radius)

        # e) Gas bands (2-4, clipped)
        self._draw_gas_bands(p, center, radius, rng, base, rim, shadow)

        # Un-clip
        p.setClipPath(QPainterPath(), Qt.ClipOperation.NoClip)

        # f) Directional crescent highlight (not clipped)
        if rng.random() < 0.5:
            crescent_start = rng.uniform(35, 70)
        else:
            crescent_start = rng.uniform(115, 155)
        crescent_sweep = rng.uniform(55, 95)
        crescent_start_16 = int(crescent_start * 16)
        crescent_sweep_16 = int(crescent_sweep * 16)

        planet_rect = QRectF(
            center.x() - radius,
            center.y() - radius,
            radius * 2,
            radius * 2,
        )

        # Soft bloom arc behind highlight
        bloom = QColor(rim)
        bloom.setAlpha(rng.randint(18, 28))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(bloom, 3.0))
        p.drawArc(planet_rect, crescent_start_16, crescent_sweep_16)

        # Sharp crescent
        crescent = QColor(rim)
        crescent.setAlpha(rng.randint(70, 110))
        p.setPen(QPen(crescent, rng.uniform(1.0, 1.25)))
        p.drawArc(planet_rect, crescent_start_16, crescent_sweep_16)

        # g) Thin tactical rim (full ellipse)
        rim_line = QColor(rim)
        rim_line.setAlpha(rng.randint(28, 45))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(rim_line, 1.0))
        p.drawEllipse(center, radius, radius)

        # h) Ring FRONT arc (optional)
        if has_ring:
            front_arc = QColor(rim)
            front_arc.setAlpha(rng.randint(30, 52))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(front_arc, rng.uniform(1.0, 1.4)))
            p.drawArc(rect_ring, arc_start, arc_sweep)

        p.end()
        pix = pix.scaled(
            logical_size, logical_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        return pix

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat("application/x-aura-drone-id"):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat("application/x-aura-drone-id"):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if event.mimeData().hasFormat("application/x-aura-drone-id"):
            drone_id = bytes(event.mimeData().data("application/x-aura-drone-id")).decode("utf-8")
            self._canvas._handle_goal_planet_drop(self, drone_id)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def to_dict(self) -> dict:
        return {
            "id": self._goal_id,
            "objective": self._objective,
            "title": self._title,
            "seed": self._seed,
            "style": self._style,
            "position": [self.pos().x(), self.pos().y()],
        }

    def from_dict(self, data: dict) -> None:
        self._goal_id = data.get("id", data.get("goal_id", ""))
        self._title = data.get("title", "")
        self._objective = data.get("objective", data.get("goal", ""))
        self._seed = data.get("seed", 0)
        self._style = data.get("style", "auto")
        if "position" in data and len(data["position"]) == 2:
            self.setPos(data["position"][0], data["position"][1])
        self._ensure_seed()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._canvas._on_node_moved()
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._invalidate_cache()
            self._canvas._on_selection_changed()
        return super().itemChange(change, value)
