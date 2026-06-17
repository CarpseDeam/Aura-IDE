from __future__ import annotations

import logging
import math
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsObject,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QMenu,
)

from aura.drones.chain import ChainDefinition
from aura.drones.definition import DroneDefinition
from aura.gui.theme import (
    ACCENT,
    DANGER,
    FG_MUTED,
)

from aura.gui.drones.chain_canvas_items import PortItem, ChainEdgeItem
from aura.gui.drones.chain_node_item import ChainNodeItem

logger = logging.getLogger(__name__)

PORT_RADIUS = 3
PORT_DIAMETER = PORT_RADIUS * 2
MISSION_CORE_WIDTH = 240
MISSION_CORE_HEIGHT = 120
ASSIGNMENT_HEIGHT = 24


def _qt_color(value, fallback="#ffffff"):
    """Return a QColor from a string token or QColor, falling back on invalid."""
    if isinstance(value, QColor):
        return value
    color = QColor(str(value))
    if not color.isValid():
        color = QColor(fallback)
    return color


def _is_valid_item(item) -> bool:
    """Return True if the PySide6 C++ object backing item is still alive."""
    if item is None:
        return False
    try:
        import shiboken6
        return shiboken6.isValid(item)
    except Exception:
        # shiboken6 unavailable — fall back to scene membership as a proxy
        try:
            return item.scene() is not None
        except Exception:
            return False


def _populate_planet_from_drone(drone: DroneDefinition) -> tuple[str, str]:
    """Derive a deterministic (title, objective) pair from a DroneDefinition."""
    if drone.name:
        title = f"{drone.name} Target"
    else:
        desc_words = (drone.description or "").split()
        if desc_words:
            title = " ".join(desc_words[:5])
        else:
            title = "Mission Goal"

    if drone.description:
        objective = drone.description
    elif drone.output_contract and drone.output_contract.get("description"):
        objective = drone.output_contract["description"]
    elif drone.instructions:
        objective = drone.instructions
    elif drone.name:
        objective = f"Complete the mission for {drone.name}"
    else:
        objective = "Complete the assigned task"

    return title, objective






class MissionCoreItem(QGraphicsObject):
    """A canvas card representing the mission command center."""

    missionCoreChanged = Signal()
    runRequested = Signal()

    def __init__(self, node_id: str, canvas: ChainCanvas):
        super().__init__()
        self._node_id = node_id
        self._canvas = canvas
        self._title = "Mission Control"
        self._goal = ""
        self._assigned_drone_ids: list[str] = []
        self._cargo_count = 0
        self._output_status = "idle"
        self._run_btn_rect = QRectF()
        self._run_btn_hovered = False
        self._drag_hovered = False

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(900)
        self._pulse_timer.timeout.connect(self._on_pulse_tick)
        self._pulse_timer.start()
        self._pulse_phase = 0.0

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptDrops(True)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, value: str) -> None:
        self._title = value
        self.missionCoreChanged.emit()
        self.update()

    @property
    def goal(self) -> str:
        return self._goal

    @goal.setter
    def goal(self, value: str) -> None:
        self._goal = value
        self.missionCoreChanged.emit()
        self.update()

    @property
    def assigned_drone_ids(self) -> list[str]:
        return self._assigned_drone_ids

    @assigned_drone_ids.setter
    def assigned_drone_ids(self, value: list[str]) -> None:
        self._assigned_drone_ids = list(value)
        self.missionCoreChanged.emit()
        self.update()

    def add_assigned_drone(self, drone_id: str) -> None:
        self._assigned_drone_ids.append(drone_id)
        self.missionCoreChanged.emit()
        self.update()

    def remove_assigned_drone(self, drone_id: str) -> None:
        if drone_id in self._assigned_drone_ids:
            self._assigned_drone_ids.remove(drone_id)
        self.missionCoreChanged.emit()
        self.update()

    def boundingRect(self) -> QRectF:
        w = MISSION_CORE_WIDTH
        h = MISSION_CORE_HEIGHT
        return QRectF(-w / 2, -h / 2, w, h)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rect = self.boundingRect()
        w = MISSION_CORE_WIDTH
        h = MISSION_CORE_HEIGHT

        # Card body
        painter.setBrush(QBrush(QColor("#1a1a24")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 8, 8)

        # Glow border
        glow_color = QColor(_qt_color(ACCENT))
        if self._output_status == "running":
            pulse_val = (math.sin(self._pulse_phase) + 1) / 2
            glow_alpha = 20 + int(pulse_val * 30)
        else:
            glow_alpha = 25
        if self.isSelected():
            glow_alpha = 60
        if self._drag_hovered:
            glow_alpha = 80
        glow_color.setAlpha(glow_alpha)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(glow_color, 4))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 7, 7)

        # Border
        border_color = QColor("#3a3a4a")
        if self.isSelected():
            border_color = _qt_color(ACCENT)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(border_color, 1.5))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 7, 7)

        # Drag hover highlight
        if self._drag_hovered:
            highlight_color = QColor(_qt_color(ACCENT))
            highlight_color.setAlpha(40)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(highlight_color, 1, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), 5, 5)

        # Title
        font_title = QFont()
        font_title.setPixelSize(12)
        font_title.setBold(True)
        painter.setFont(font_title)
        painter.setPen(QPen(_qt_color(ACCENT)))
        painter.drawText(
            QRectF(-w / 2 + 12, -h / 2 + 10, w - 24, 18),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._title,
        )

        # Header beacon diamond
        beacon_color = QColor(_qt_color(ACCENT))
        if self._output_status == "running":
            pulse_val = (math.sin(self._pulse_phase) + 1) / 2
            beacon_alpha = 180 + int(pulse_val * 40)
        else:
            beacon_alpha = 200
        beacon_color.setAlpha(int(beacon_alpha))
        painter.setBrush(QBrush(beacon_color))
        painter.setPen(Qt.PenStyle.NoPen)
        beacon_path = QPainterPath()
        beacon_path.moveTo(QPointF(w / 2 - 24, -h / 2 + 16))
        beacon_path.lineTo(QPointF(w / 2 - 21, -h / 2 + 19))
        beacon_path.lineTo(QPointF(w / 2 - 24, -h / 2 + 22))
        beacon_path.lineTo(QPointF(w / 2 - 27, -h / 2 + 19))
        beacon_path.closeSubpath()
        painter.drawPath(beacon_path)

        # Section labels: Launch Bay & Cargo Bay
        font_section = QFont()
        font_section.setPixelSize(9)
        painter.setFont(font_section)
        painter.setPen(QPen(QColor("#a8aebb")))
        drone_count = len(self._assigned_drone_ids)
        launch_bay_text = f"\u25c7  Launch Bay: {drone_count} drones"
        painter.drawText(
            QRectF(-w / 2 + 12, -h / 2 + 34, w - 24, 14),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            launch_bay_text,
        )
        cargo_bay_text = f"\u25c6  Cargo Bay: {self._cargo_count} items"
        painter.drawText(
            QRectF(-w / 2 + 12, -h / 2 + 48, w - 24, 14),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            cargo_bay_text,
        )

        # Metrics row
        font_metrics = QFont()
        font_metrics.setPixelSize(9)
        painter.setFont(font_metrics)

        _status_map = {
            "completed": ("\u2713 completed", _qt_color(ACCENT)),
            "running": ("\u25ce running", _qt_color(ACCENT)),
            "failed": ("\u2717 failed", _qt_color(DANGER)),
            "idle": ("\u25cb idle", _qt_color(FG_MUTED)),
        }
        status_text, status_color = _status_map.get(
            self._output_status, ("\u25cb idle", _qt_color(FG_MUTED))
        )
        metrics_text = f"\u2699 {drone_count}  \U0001f4e6 {self._cargo_count}  {status_text}"

        painter.setPen(QPen(status_color))
        metrics_y = h / 2 - 42
        painter.drawText(
            QRectF(-w / 2 + 12, metrics_y, w - 24, 16),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics_text,
        )

        # "Run Mission" button (bottom-right)
        btn_w = 100
        btn_h = 22
        btn_x = w / 2 - 8 - btn_w
        btn_y = h / 2 - 8 - btn_h
        self._run_btn_rect = QRectF(btn_x, btn_y, btn_w, btn_h)

        btn_alpha = 150 if self._run_btn_hovered else 80
        btn_color = QColor(_qt_color(ACCENT))
        btn_color.setAlpha(btn_alpha)
        painter.setBrush(QBrush(btn_color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self._run_btn_rect, 5, 5)

        font_btn = QFont()
        font_btn.setPixelSize(10)
        font_btn.setBold(True)
        painter.setFont(font_btn)
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(
            self._run_btn_rect,
            Qt.AlignmentFlag.AlignCenter,
            "\u25b6 Run Mission",
        )

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat("application/x-aura-drone-id"):
            self._drag_hovered = True
            self.update()
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        event.ignore()
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if event.mimeData().hasFormat("application/x-aura-drone-id"):
            self._drag_hovered = False
            self.update()
            drone_id = bytes(event.mimeData().data("application/x-aura-drone-id")).decode("utf-8")
            self._canvas._handle_mission_core_drop(self, drone_id)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def dragLeaveEvent(self, event) -> None:
        self._drag_hovered = False
        self.update()
        super().dragLeaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._run_btn_rect.contains(event.pos()):
            self.runRequested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def hoverMoveEvent(self, event) -> None:
        hovered = self._run_btn_rect.contains(event.pos())
        if hovered != self._run_btn_hovered:
            self._run_btn_hovered = hovered
            self.update()
        super().hoverMoveEvent(event)

    def hoverEnterEvent(self, event) -> None:
        self._run_btn_hovered = False
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self._run_btn_hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def _on_pulse_tick(self) -> None:
        self._pulse_phase += 0.15
        self.update()

    def to_dict(self) -> dict:
        return {
            "title": self._title,
            "goal": self._goal,
            "position": [self.pos().x(), self.pos().y()],
            "assigned_drone_ids": list(self._assigned_drone_ids),
        }

    def from_dict(self, data: dict) -> None:
        self._title = data.get("title", "Mission Control")
        self._goal = data.get("goal", "")
        pos = data.get("position")
        if pos and len(pos) == 2:
            self.setPos(pos[0], pos[1])
        self._assigned_drone_ids = list(data.get("assigned_drone_ids", []))

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._canvas._on_node_moved()
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._canvas._on_selection_changed()
        return super().itemChange(change, value)


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



class ChainCanvas(QGraphicsView):
    """QGraphicsView-based canvas for building drone workflow chains."""

    canvasChanged = Signal()
    runMissionRequested = Signal()
    statusMessage = Signal(str, str)  # (text, level)
    renameWorkflowRequested = Signal()

    def __init__(self, parent=None):
        # Set all attributes before super().__init__() because Qt paint events
        # (drawForeground / drawBackground) fire during construction and will
        # AttributeError if these are missing.
        self._nodes: dict[str, ChainNodeItem] = {}
        self._edges: list[ChainEdgeItem] = []
        self._drawing_source_port: PortItem | None = None
        self._rubber_band: QGraphicsLineItem | None = None
        self._drawing_cancelled = False
        self._empty_text: QGraphicsTextItem | None = None
        self._mission_core: MissionCoreItem | None = None
        self._goal_planets: dict[str, GoalPlanetItem] = {}
        self._space_bg_cache: QPixmap | None = None
        self._last_click_time: float = 0.0
        self._last_click_pos: QPointF = QPointF()

        super().__init__(parent)

        self._scene = QGraphicsScene(-2000, -2000, 4000, 4000, self)
        self.setScene(self._scene)

        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setInteractive(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setAcceptDrops(True)

    def _update_empty_text(self) -> None:
        if self._empty_text is not None:
            try:
                self._scene.removeItem(self._empty_text)
            except RuntimeError:
                pass  # C++ object already deleted
            self._empty_text = None

        if not self._nodes and self._mission_core is None and not self._goal_planets:
            text = QGraphicsTextItem(
                "Right-click to add a Mission Core and Goal Planets."
            )
            font = QFont()
            font.setPixelSize(11)
            text.setFont(font)
            text.setDefaultTextColor(_qt_color(FG_MUTED))
            text.setPos(-120, -15)
            self._scene.addItem(text)
            self._empty_text = text


    def load_chain(self, chain: ChainDefinition, drone_lookup: dict[str, DroneDefinition], mission_core_data: dict | None = None, goal_planets_data: list[dict] | None = None) -> None:
        """Populate canvas from a ChainDefinition."""
        self._scene.clear()
        self._empty_text = None  # scene.clear() deleted the C++ object
        self._nodes.clear()
        self._edges.clear()
        self._drawing_source_port = None
        self._rubber_band = None
        self._mission_core = None
        self._goal_planets.clear()

        for node_data in chain.nodes:
            drone_id = node_data.drone_id
            drone = drone_lookup.get(drone_id)
            item = ChainNodeItem(
                node_id=node_data.id,
                drone=drone,
                goal_template=node_data.goal_template,
                canvas=self,
                is_draft=node_data.is_draft,
                draft_name=node_data.draft_name,
                draft_accepts=node_data.draft_accepts,
                draft_produces=node_data.draft_produces,
                draft_brief=node_data.draft_brief,
                is_assignment=node_data.is_assignment,
                goal_id=node_data.goal_id,
            )
            if drone is None and not node_data.is_draft:
                item.missing = True
            if node_data.position and len(node_data.position) == 2:
                item.setPos(node_data.position[0], node_data.position[1])
            self._scene.addItem(item)
            self._nodes[node_data.id] = item

        for edge_data in chain.edges:
            src = self._nodes.get(edge_data.from_node)
            tgt = self._nodes.get(edge_data.to_node)
            if src and tgt:
                edge = ChainEdgeItem(
                    source_port=src.output_port,
                    target_port=tgt.input_port,
                    canvas=self,
                )
                self._scene.addItem(edge)
                self._edges.append(edge)

        # Mission core
        self._mission_core = None
        if mission_core_data:
            mission_item = MissionCoreItem(
                node_id=f"mission-core-{uuid.uuid4().hex[:4]}",
                canvas=self,
            )
            mission_item.from_dict(mission_core_data)
            self._scene.addItem(mission_item)
            self._mission_core = mission_item
            mission_item.runRequested.connect(self.runMissionRequested.emit)

        # Goal planets — iterate chain.goals, apply matching data from goal_planets_data
        goals_data_by_id: dict[str, dict] = {}
        if goal_planets_data:
            for gd in goal_planets_data:
                gid = gd.get("id", gd.get("goal_id", ""))
                if gid:
                    goals_data_by_id[gid] = gd

        for goal in chain.goals:
            gp_item = GoalPlanetItem(
                node_id=f"goal-planet-{uuid.uuid4().hex[:4]}",
                canvas=self,
                goal_id=goal.id,
            )
            if goal.position and len(goal.position) == 2:
                gp_item.setPos(goal.position[0], goal.position[1])
            gp_item.objective = goal.objective
            gp_item.title = goal.title

            # Apply any saved canvas data for this goal (e.g. seed, style, position override)
            match_data = goals_data_by_id.get(goal.id)
            if match_data:
                gp_item.from_dict(match_data)
            self._scene.addItem(gp_item)
            self._goal_planets[goal.id] = gp_item

        self._update_empty_text()

        # Auto-create default mission core for empty chains
        if self._mission_core is None and not self._goal_planets and not self._nodes:
            self._canvas_add_mission_core(QPointF(-160, 0))

        # Auto-fit after a short delay
        QTimer.singleShot(100, self._fit_view)

    def _fit_view(self) -> None:
        node_count = len(self._nodes)
        if node_count == 0 and self._mission_core is None and not self._goal_planets:
            self.resetTransform()
            self.centerOn(0, 0)
        elif node_count == 1 and self._mission_core is None and not self._goal_planets:
            self.resetTransform()
            node = next(iter(self._nodes.values()))
            self.centerOn(node.sceneBoundingRect().center())
        elif node_count == 0 and self._mission_core is not None:
            self.resetTransform()
            self.centerOn(self._mission_core.sceneBoundingRect().center())
        elif node_count == 0 and self._mission_core is None and self._goal_planets:
            first_planet = next(iter(self._goal_planets.values()))
            self.resetTransform()
            self.centerOn(first_planet.sceneBoundingRect().center())
        else:
            items_rect = self._scene.itemsBoundingRect()
            self.fitInView(items_rect.adjusted(-40, -40, 40, 40), Qt.AspectRatioMode.KeepAspectRatio)

        # Clamp minimum zoom so canvas doesn't zoom out too far
        current_scale = self.transform().m11()
        if current_scale < 0.35:
            factor = 0.35 / current_scale
            self.scale(factor, factor)

    def to_chain_nodes_and_edges(self) -> tuple[list[dict], list[dict], dict | None, list[dict]]:
        """Snapshot current canvas state to serializable dicts.

        Returns (nodes, edges, mission_core_dict, goals_list).
        """
        nodes = []
        for node_id, item in self._nodes.items():
            pos = item.pos()
            nodes.append({
                "id": node_id,
                "drone_id": item.drone_id,
                "goal_template": item.goal_template,
                "position": [pos.x(), pos.y()],
                "is_draft": item.is_draft,
                "draft_name": item.draft_name,
                "draft_accepts": item.draft_accepts,
                "draft_produces": item.draft_produces,
                "draft_brief": item.draft_brief,
                "is_assignment": item.is_assignment,
                "goal_id": item.goal_id,
            })

        edges = []
        for edge in self._edges:
            edges.append({
                "from_node": edge.from_node_id,
                "to_node": edge.to_node_id,
            })

        mission_dict = self._mission_core.to_dict() if self._mission_core else None

        goals = []
        for gp in self._goal_planets.values():
            goals.append(gp.to_dict())

        return nodes, edges, mission_dict, goals

    @property
    def goal_planets_data(self) -> list[dict]:
        return [gp.to_dict() for gp in self._goal_planets.values()]

    # ---- Edge drawing ----

    def _start_edge_draw(self, port: PortItem) -> None:
        self._drawing_source_port = port
        scene_pos = port.center_scene()
        self._rubber_band = self._scene.addLine(
            QLineF(scene_pos, scene_pos),
            QPen(_qt_color(ACCENT), 2, Qt.PenStyle.DashLine),
        )
        # Set mouse tracking so we get move events
        self.setMouseTracking(True)

    def _update_rubber_band(self, scene_pos: QPointF) -> None:
        if self._rubber_band and self._drawing_source_port:
            src = self._drawing_source_port.center_scene()
            self._rubber_band.setLine(QLineF(src, scene_pos))

    def _complete_edge(self, target_port: PortItem) -> None:
        if not self._drawing_source_port:
            return
        src_node = self._drawing_source_port.parent_node
        tgt_node = target_port.parent_node
        if src_node == tgt_node:
            self._cancel_edge_draw()
            return
        # Check for duplicate edge
        for edge in self._edges:
            if edge.from_node_id == src_node.node_id and edge.to_node_id == tgt_node.node_id:
                self._cancel_edge_draw()
                return
        edge = ChainEdgeItem(
            source_port=self._drawing_source_port,
            target_port=target_port,
            canvas=self,
        )
        self._scene.addItem(edge)
        self._edges.append(edge)
        self._cleanup_edge_draw()
        self.canvasChanged.emit()

    def _cancel_edge_draw(self) -> None:
        self._cleanup_edge_draw()

    def _cleanup_edge_draw(self) -> None:
        if self._rubber_band:
            self._scene.removeItem(self._rubber_band)
            self._rubber_band = None
        self._drawing_source_port = None
        self.setMouseTracking(False)

    def _find_output_port(self, items: list[QGraphicsItem]) -> PortItem | None:
        for item in items:
            if isinstance(item, PortItem) and not item.is_input:
                return item
        return None

    def _find_input_port(self, items: list[QGraphicsItem]) -> PortItem | None:
        for item in items:
            if isinstance(item, PortItem) and item.is_input:
                return item
        return None

    # ---- Event overrides ----

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(event.position().toPoint())

            # Double-click on empty canvas → fit view
            now = time.monotonic()
            dx = abs(scene_pos.x() - self._last_click_pos.x())
            dy = abs(scene_pos.y() - self._last_click_pos.y())
            if (now - self._last_click_time < 0.4 and dx < 15 and dy < 15):
                items = self._scene.items(scene_pos)
                if not items or all(isinstance(i, QGraphicsTextItem) for i in items):
                    self._fit_view()
                    self._last_click_time = 0.0
                    event.accept()
                    return
            self._last_click_time = now
            self._last_click_pos = scene_pos

            items = self._scene.items(scene_pos)
            output_port = self._find_output_port(items)
            if output_port is not None:
                self._start_edge_draw(output_port)
                event.accept()
                return
            # Also check if clicking on input port while drawing
            if self._drawing_source_port is not None:
                input_port = self._find_input_port(items)
                if input_port is not None and input_port.parent_node != self._drawing_source_port.parent_node:
                    self._complete_edge(input_port)
                    event.accept()
                    return
                elif input_port is not None:
                    self._cancel_edge_draw()
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drawing_source_port is not None:
            scene_pos = self.mapToScene(event.position().toPoint())
            self._update_rubber_band(scene_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._drawing_source_port is not None:
            scene_pos = self.mapToScene(event.position().toPoint())
            items = self._scene.items(scene_pos)
            input_port = self._find_input_port(items)
            if input_port is not None and input_port.parent_node != self._drawing_source_port.parent_node:
                self._complete_edge(input_port)
            else:
                self._cancel_edge_draw()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self._delete_selected()
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:
        """Zoom around cursor with plain scroll wheel, clamped to safe range."""
        factor = 1.12
        if event.angleDelta().y() > 0:
            s = factor
        else:
            s = 1 / factor

        current = self.transform().m11()
        new_scale = current * s
        if new_scale < 0.15:
            s = 0.15 / current
        elif new_scale > 4.0:
            s = 4.0 / current

        self.scale(s, s)
        event.accept()

    # ---- Drag & drop from palette ----

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
            # Route background canvas drop to mission core if available
            if self._mission_core is None:
                scene_pos = self.mapToScene(event.position().toPoint())
                self._canvas_add_mission_core(scene_pos)
            self._handle_mission_core_drop(self._mission_core, drone_id)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def _get_workspace_root(self) -> Path:
        """Walk up parents to find ChainEditor's workspace_root."""
        p = self.parent()
        while p is not None:
            ws = getattr(p, "workspace_root", None)
            if ws:
                return ws
            p = p.parent()
        return Path.cwd()

    # ---- Deletions ----

    def _delete_selected(self) -> None:
        to_remove = [item for item in self._scene.selectedItems()
                     if isinstance(item, (ChainNodeItem, ChainEdgeItem, MissionCoreItem, GoalPlanetItem))]
        for item in to_remove:
            if isinstance(item, GoalPlanetItem):
                removed_id = item.goal_id
                if removed_id in self._goal_planets:
                    del self._goal_planets[removed_id]
                # Reassign any assignment nodes targeting this goal
                remaining_goals = list(self._goal_planets.keys())
                for node in self._nodes.values():
                    if node.is_assignment and node.goal_id == removed_id:
                        node.goal_id = remaining_goals[0] if remaining_goals else ""
                self._scene.removeItem(item)
                self._update_empty_text()
                self.canvasChanged.emit()
            elif isinstance(item, MissionCoreItem):
                self._mission_core = None
                self._scene.removeItem(item)
                self._update_empty_text()
                self.canvasChanged.emit()
            elif isinstance(item, ChainNodeItem):
                self._remove_node(item)
            elif isinstance(item, ChainEdgeItem):
                self._remove_edge(item)

    def _remove_node(self, node: ChainNodeItem) -> None:
        # Clean up mission core assignment
        if self._mission_core is not None:
            self._mission_core.remove_assigned_drone(node.drone_id)
        # Remove incident edges
        incident = [
            e for e in self._edges
            if e.from_node_id == node.node_id or e.to_node_id == node.node_id
        ]
        for edge in incident:
            self._remove_edge(edge)
        if node.node_id in self._nodes:
            del self._nodes[node.node_id]
        self._scene.removeItem(node)
        self._update_empty_text()
        self.canvasChanged.emit()

    def _remove_edge(self, edge: ChainEdgeItem) -> None:
        if edge in self._edges:
            self._edges.remove(edge)
        self._scene.removeItem(edge)
        self.canvasChanged.emit()

    # ---- Node callbacks ----

    def _on_node_moved(self) -> None:
        for edge in self._edges:
            edge._adjust()
        self.canvasChanged.emit()

    def _on_selection_changed(self) -> None:
        # Re-paint for selection state
        self.viewport().update()
        self.canvasChanged.emit()

    def contextMenuEvent(self, event) -> None:
        scene_pos = self.mapToScene(event.pos())
        items = self._scene.items(scene_pos)
        if not items or all(isinstance(i, (QGraphicsTextItem,)) for i in items):
            menu = QMenu()
            add_mission_action = menu.addAction("Add Mission Core")
            if self._mission_core is not None:
                add_mission_action.setEnabled(False)
            add_goal_action = menu.addAction("Add Goal Planet")
            menu.addSeparator()
            rename_action = menu.addAction("Rename Workflow")
            action = menu.exec(event.globalPos())
            if action == add_mission_action:
                self._canvas_add_mission_core(scene_pos)
            elif action == add_goal_action:
                self._canvas_add_goal_planet(scene_pos)
            elif action == rename_action:
                self.renameWorkflowRequested.emit()
            return
        super().contextMenuEvent(event)

    # ---- Mission core ----

    def _canvas_add_mission_core(self, scene_pos: QPointF) -> None:
        if self._mission_core is not None:
            return
        node_id = f"mission-core-{uuid.uuid4().hex[:4]}"
        item = MissionCoreItem(node_id=node_id, canvas=self)
        item.setPos(scene_pos)
        self._scene.addItem(item)
        self._mission_core = item
        item.runRequested.connect(self.runMissionRequested.emit)
        self._update_empty_text()
        self.canvasChanged.emit()

    def _canvas_add_goal_planet(self, scene_pos: QPointF) -> None:
        self._canvas_add_goal_planet_with_data(scene_pos)

    def _canvas_add_goal_planet_with_data(self, scene_pos: QPointF, title: str = "", objective: str = "") -> GoalPlanetItem:
        goal_id = f"goal-{uuid.uuid4().hex[:6]}"
        gp_item = GoalPlanetItem(
            node_id=f"goal-planet-{uuid.uuid4().hex[:4]}",
            canvas=self,
            goal_id=goal_id,
        )
        gp_item.setPos(scene_pos)
        if title:
            gp_item.title = title
        if objective:
            gp_item.objective = objective
        self._scene.addItem(gp_item)
        self._goal_planets[goal_id] = gp_item
        self._update_empty_text()
        self.canvasChanged.emit()
        return gp_item
    def _create_goal_planet_for_drone(self, drone: DroneDefinition) -> GoalPlanetItem:
        title, objective = _populate_planet_from_drone(drone)
        if self._mission_core is not None:
            mc_pos = self._mission_core.pos()
            base_x = mc_pos.x() + 200
            base_y = mc_pos.y() - 40
        else:
            base_x = 160
            base_y = 0
        scene_pos = QPointF(base_x, base_y + len(self._goal_planets) * 100)
        return self._canvas_add_goal_planet_with_data(scene_pos, title, objective)

    def _ensure_goal_for_drone(self, drone: DroneDefinition, preferred_goal_id: str = "") -> str:
        if preferred_goal_id and preferred_goal_id in self._goal_planets:
            return preferred_goal_id
        selected = [gp for gp in self._goal_planets.values() if gp.isSelected()]
        if len(selected) == 1:
            return selected[0].goal_id
        if len(self._goal_planets) == 1:
            return next(iter(self._goal_planets))
        return self._create_goal_planet_for_drone(drone).goal_id

    def _create_drone_assignment(self, drone: DroneDefinition, goal_id: str) -> ChainNodeItem:
        if self._mission_core is None:
            self._canvas_add_mission_core(QPointF(-160, 0))
        node_id = f"{drone.id}-{uuid.uuid4().hex[:4]}"
        item = ChainNodeItem(
            node_id=node_id,
            drone=drone,
            goal_template="",
            canvas=self,
            is_assignment=True,
            goal_id=goal_id,
        )
        mc = self._mission_core
        mc_pos = mc.pos()
        assignment_index = sum(1 for n in self._nodes.values() if n.is_assignment)
        x = mc_pos.x() + MISSION_CORE_WIDTH / 2 + 4
        y = mc_pos.y() - MISSION_CORE_HEIGHT / 2 + 6 + assignment_index * (ASSIGNMENT_HEIGHT + 4)
        item.setPos(x, y)
        self._scene.addItem(item)
        self._nodes[node_id] = item
        mc.add_assigned_drone(drone.id)
        self._scene.clearSelection()
        item.setSelected(True)
        self._update_empty_text()
        self.canvasChanged.emit()
        return item

    def _handle_mission_core_drop(self, mission_item: MissionCoreItem, drone_id: str) -> None:
        from aura.drones.store import DroneStore
        drone = DroneStore.load_drone(self._get_workspace_root(), drone_id)
        if drone is None:
            return
        goal_id = self._ensure_goal_for_drone(drone, "")
        self._create_drone_assignment(drone, goal_id)

    def _handle_goal_planet_drop(self, planet_item: GoalPlanetItem, drone_id: str) -> None:
        from aura.drones.store import DroneStore
        drone = DroneStore.load_drone(self._get_workspace_root(), drone_id)
        if drone is None:
            return
        self._create_drone_assignment(drone, planet_item.goal_id)

    # ---- Space background ----

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.save()
        painter.resetTransform()

        viewport_rect = self.viewport().rect()
        painter.fillRect(viewport_rect, QColor("#060608"))

        cache = self._space_bg_cache
        if cache is None or cache.size() != viewport_rect.size():
            cache = self._build_space_cache(viewport_rect.size())
            self._space_bg_cache = cache
        painter.drawPixmap(viewport_rect.topLeft(), cache)
        painter.restore()

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        """Draw assignment connection lines from Goal Planets to their assignments."""
        try:
            line_color = _qt_color(ACCENT)
            line_color.setAlpha(25)
            pen = QPen(line_color)
            pen.setWidthF(0.5)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)

            for gp in list(self._goal_planets.values()):
                if not _is_valid_item(gp):
                    continue
                if gp.scene() is None:
                    continue
                gp_rect = gp.sceneBoundingRect()
                source_pt = QPointF(gp_rect.right(), gp_rect.center().y())

                for node in list(self._nodes.values()):
                    if not _is_valid_item(node):
                        continue
                    if node.scene() is None:
                        continue
                    if not node.is_assignment:
                        continue
                    if node.goal_id != gp.goal_id:
                        continue
                    node_rect = node.sceneBoundingRect()
                    target_pt = QPointF(node_rect.left(), node_rect.center().y())
                    painter.drawLine(source_pt, target_pt)

            # Fallback: assignments with no goal get a faint line from Mothership
            mc = self._mission_core
            if mc and _is_valid_item(mc) and mc.scene() is not None:
                mc_rect = mc.sceneBoundingRect()
                fallback_color = _qt_color(FG_MUTED)
                fallback_color.setAlpha(15)
                fallback_pen = QPen(fallback_color)
                fallback_pen.setWidthF(0.3)
                fallback_pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(fallback_pen)
                for node in list(self._nodes.values()):
                    if not _is_valid_item(node) or node.scene() is None:
                        continue
                    if not node.is_assignment:
                        continue
                    if node.goal_id:
                        continue
                    source_pt = QPointF(mc_rect.right(), mc_rect.center().y())
                    node_rect = node.sceneBoundingRect()
                    target_pt = QPointF(node_rect.left(), node_rect.center().y())
                    painter.drawLine(source_pt, target_pt)
        except Exception:
            logger.exception("drawForeground error — suppressed to protect Qt paint cycle")

    def _build_space_cache(self, size) -> QPixmap:
        import random
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = size.width(), size.height()

        # Radial gas clouds (layered additive)
        # Violet main bloom
        g1 = QRadialGradient(QPointF(0.30 * w, 0.58 * h), 0.46 * w)
        g1.setColorAt(0.0, QColor(157, 124, 216, 72))
        g1.setColorAt(0.45, QColor(150, 115, 205, 34))
        g1.setColorAt(1.0, QColor(157, 124, 216, 0))
        p.fillRect(0, 0, w, h, g1)

        # Warm magenta pocket
        g2 = QRadialGradient(QPointF(0.52 * w, 0.68 * h), 0.40 * w)
        g2.setColorAt(0.0, QColor(247, 118, 142, 55))
        g2.setColorAt(0.5, QColor(205, 95, 140, 24))
        g2.setColorAt(1.0, QColor(247, 118, 142, 0))
        p.fillRect(0, 0, w, h, g2)

        # Blue upper drift
        g3 = QRadialGradient(QPointF(0.62 * w, 0.34 * h), 0.42 * w)
        g3.setColorAt(0.0, QColor(122, 162, 247, 40))
        g3.setColorAt(0.5, QColor(110, 140, 220, 18))
        g3.setColorAt(1.0, QColor(122, 162, 247, 0))
        p.fillRect(0, 0, w, h, g3)

        # Cyan accent
        g4 = QRadialGradient(QPointF(0.80 * w, 0.55 * h), 0.28 * w)
        g4.setColorAt(0.0, QColor(125, 207, 255, 28))
        g4.setColorAt(1.0, QColor(125, 207, 255, 0))
        p.fillRect(0, 0, w, h, g4)

        # Subtle static stars
        rng = random.Random(42)
        for _ in range(250):
            sx = rng.uniform(0, w)
            sy = rng.uniform(0, h)
            sr = rng.uniform(0.4, 1.5)
            sa = rng.randint(20, 80)
            c = QColor(200, 208, 240, sa)
            p.setBrush(QBrush(c))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(sx, sy), sr, sr)

        p.end()
        return pixmap
