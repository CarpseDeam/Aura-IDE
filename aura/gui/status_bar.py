from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QLabel, QSizeGrip, QStatusBar

from aura.config import PROVIDERS, ThinkingMode, cost_usd

_THINKING_LABEL = {"off": "Off", "high": "High", "max": "Max"}


def _compact_number(n: int) -> str:
    """Format large integers with k/M shorthand for compact display."""
    if n >= 1_000_000:
        val = n / 1_000_000
        return f"{val:.1f}M".replace(".0M", "M")
    elif n >= 1_000:
        val = n / 1_000
        return f"{val:.1f}k".replace(".0k", "k")
    return str(n)


def _format_footer_cost(known_cost: float, unknown_count: int, total_models: int) -> str:
    """Format session cost for human-readable footer display (not raw precision)."""
    if total_models == 0:
        return "$—"
    if unknown_count == total_models:
        return "$? —"
    cost_str = f"${known_cost:.2f}" if known_cost >= 0.01 else "< $0.01"
    if unknown_count > 0:
        cost_str += " *"
    return cost_str


class _StatusResizeGrip(QSizeGrip):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self.setToolTip("Resize window")
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setStyleSheet("background: transparent;")

    def paintEvent(self, event) -> None:
        super().paintEvent(event)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(120, 130, 150, 150), 1))

        right = self.width() - 5
        bottom = self.height() - 5
        for offset in (0, 5, 10):
            painter.drawLine(right - offset, bottom, right, bottom - offset)

        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            handle = window.windowHandle() if window is not None else None
            if handle is not None:
                edges = Qt.Edge.RightEdge | Qt.Edge.BottomEdge
                if handle.startSystemResize(edges):
                    event.accept()
                    return

        super().mousePressEvent(event)


class _ClickableLabel(QLabel):
    clicked = Signal()

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class AuraStatusBar(QStatusBar):
    credits_chip_clicked = Signal()

    def __init__(self, parent=None, show_resize_grip: bool = True) -> None:
        super().__init__(parent)

        self._drone_label: QLabel | None = None
        self._resize_grip_allowed = show_resize_grip

        # Left side: workspace path only
        self._status_left = QLabel("")
        self.addWidget(self._status_left, 0)

        # Center: model, thinking, cache, session cost
        self._status_center = QLabel("")
        self._status_center.setToolTip(
            "Session cache usage and estimated cost — does not reflect actual provider billing."
        )
        font_center = QFont()
        font_center.setPointSize(11)
        self._status_center.setFont(font_center)
        self.addWidget(self._status_center, 1)

        # Right: Aura Credits pill
        self._status_balance = _ClickableLabel("")
        self._status_balance.setObjectName("aura_credits_status_chip")
        self._status_balance.setAccessibleName("Aura Credits status")
        self._status_balance.setStyleSheet(
            "QLabel#aura_credits_status_chip {"
            "    padding: 2px 10px;"
            "    border: 1px solid rgba(122, 162, 247, 0.3);"
            "    border-radius: 10px;"
            "    background: rgba(122, 162, 247, 0.08);"
            "    color: #7aa2f7;"
            "    font-weight: 600;"
            "}"
            "QLabel#aura_credits_status_chip:hover {"
            "    border-color: rgba(122, 162, 247, 0.6);"
            "    background: rgba(122, 162, 247, 0.15);"
            "}"
        )
        font_balance = QFont()
        font_balance.setPointSize(11)
        self._status_balance.setFont(font_balance)
        self.addPermanentWidget(self._status_balance)
        self._status_balance.clicked.connect(self.credits_chip_clicked)
        self._status_balance.setVisible(True)

        self.setSizeGripEnabled(False)
        self._resize_grip = _StatusResizeGrip(self)
        self.addPermanentWidget(self._resize_grip)
        self._resize_grip.setVisible(show_resize_grip)
        self._resize_grip.setEnabled(show_resize_grip)

    def set_resize_grip_visible(self, visible: bool) -> None:
        visible = visible and self._resize_grip_allowed
        self._resize_grip.setVisible(visible)
        self._resize_grip.setEnabled(visible)

    def refresh(
        self, 
        workspace_root: str, 
        model_id: str, 
        thinking: ThinkingMode,
        session_usage: dict[str, dict[str, int]],
        has_aura_key: bool = False,
        balance_micros: int | None = None,
        has_provider: bool = False,
    ) -> None:
        # Workspace path truncation (left side)
        ws = workspace_root
        if len(ws) > 64:
            ws = "…" + ws[-63:]
        self._status_left.setText(ws)

        # Model label lookup
        model_label = model_id
        for cfg in PROVIDERS.values():
            if model_id in cfg.models:
                model_label = cfg.models[model_id].label
                break

        thinking_label = _THINKING_LABEL.get(thinking, "Off")

        # Usage and Cost
        total_hit = sum(u["hit"] for u in session_usage.values())
        total_miss = sum(u["miss"] for u in session_usage.values())
        total_out = sum(u["out"] for u in session_usage.values())

        known_cost = 0.0
        unknown_count = 0
        for m_id, u in session_usage.items():
            c = cost_usd(m_id, u["hit"], u["miss"], u["out"])
            if c is None:
                unknown_count += 1
            else:
                known_cost += c

        total_models = len(session_usage)

        # Build center text
        center_parts = [f"{model_label} · Thinking: {thinking_label}"]
        if total_hit + total_miss + total_out > 0:
            hit_str = _compact_number(total_hit)
            miss_str = _compact_number(total_miss)
            out_str = _compact_number(total_out)
            center_parts.append(f"Cache {hit_str} hit · {miss_str} miss · {out_str} out")
        cost_str = _format_footer_cost(known_cost, unknown_count, total_models)
        center_parts.append(f"Session {cost_str}")
        self._status_center.setText(" · ".join(center_parts))

        # Balance display (right pill)
        if has_aura_key:
            if balance_micros is not None:
                self._status_balance.setText(f"Aura Credits · ${balance_micros / 1_000_000:.2f}")
                self._status_balance.setToolTip("Aura Credits balance. Click to open Aura Credits.")
            else:
                self._status_balance.setText("Aura Credits · $—")
                self._status_balance.setToolTip("Aura Credits balance unavailable. Click to open Aura Credits.")
        else:
            if has_provider:
                self._status_balance.setText("Fuel Aura")
                self._status_balance.setToolTip("Set up Aura Credits to fuel your workflow. Click to open.")
            else:
                self._status_balance.setText("Set up Aura")
                self._status_balance.setToolTip("Set up Aura Credits and an API provider. Click to open.")
        self._status_balance.setVisible(True)
