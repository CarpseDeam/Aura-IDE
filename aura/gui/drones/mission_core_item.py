from __future__ import annotations

import math
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

from aura.gui.theme import (
    ACCENT,
    DANGER,
    FG_MUTED,
)

if TYPE_CHECKING:
    from aura.gui.drones.chain_canvas import ChainCanvas


MISSION_CORE_WIDTH = 240
MISSION_CORE_HEIGHT = 120


def _qt_color(value, fallback="#ffffff"):
    """Return a QColor from a string token or QColor, falling back on invalid."""
    if isinstance(value, QColor):
        return value
    color = QColor(str(value))
    if not color.isValid():
        color = QColor(fallback)
    return color


class MissionCoreItem(QGraphicsObject):
    """A canvas card representing the mission command center."""

    missionCoreChanged = Signal()
    runRequested = Signal()
    loopToggled = Signal(bool)

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
        self._loop_enabled: bool = False
        self._loop_toggle_rect: QRectF = QRectF()

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

    @property
    def border_color(self) -> QColor:
        return QColor("#8b9eeb")

    @property
    def is_draft(self) -> bool:
        return False

    @property
    def loop_enabled(self) -> bool:
        return self._loop_enabled

    @loop_enabled.setter
    def loop_enabled(self, value: bool) -> None:
        self._loop_enabled = value
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

        # Loop toggle pill (left side, below cargo bay)
        loop_btn_w = 60
        loop_btn_h = 20
        loop_btn_x = -w / 2 + 12
        loop_btn_y = h / 2 - 34
        self._loop_toggle_rect = QRectF(loop_btn_x, loop_btn_y, loop_btn_w, loop_btn_h)

        # Toggle background
        if self._loop_enabled:
            toggle_bg = QColor("#9ece6a")
            toggle_bg.setAlpha(200)
        else:
            toggle_bg = QColor("#3a3a4a")
            toggle_bg.setAlpha(120)
        painter.setBrush(QBrush(toggle_bg))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self._loop_toggle_rect, 4, 4)

        # Toggle label
        font_toggle = QFont()
        font_toggle.setPixelSize(9)
        font_toggle.setBold(True)
        painter.setFont(font_toggle)
        toggle_label_color = QColor("#ffffff") if self._loop_enabled else QColor("#a8aebb")
        painter.setPen(QPen(toggle_label_color))
        painter.drawText(
            self._loop_toggle_rect,
            Qt.AlignmentFlag.AlignCenter,
            "Loop",
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
        if event.button() == Qt.MouseButton.LeftButton and self._loop_toggle_rect.contains(event.pos()):
            self._loop_enabled = not self._loop_enabled
            self.loopToggled.emit(self._loop_enabled)
            self.update()
            event.accept()
            return
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
            "loop_enabled": self._loop_enabled,
        }

    def from_dict(self, data: dict) -> None:
        self._title = data.get("title", "Mission Control")
        self._goal = data.get("goal", "")
        pos = data.get("position")
        if pos and len(pos) == 2:
            self.setPos(pos[0], pos[1])
        self._assigned_drone_ids = list(data.get("assigned_drone_ids", []))
        self._loop_enabled = bool(data.get("loop_enabled", False))

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._canvas._on_node_moved()
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._canvas._on_selection_changed()
        return super().itemChange(change, value)
