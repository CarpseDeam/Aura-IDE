from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizeGrip, QSizePolicy, QStatusBar, QWidget

from aura.config import ThinkingMode, cost_usd


def _format_footer_cost(known_cost: float, unknown_count: int, total_models: int) -> str:
    """Format session cost for human-readable footer display (not raw precision)."""
    if total_models == 0:
        return "$—"
    if unknown_count == total_models:
        return "$—"
    if known_cost <= 0:
        cost_str = "$0.0000"
    elif known_cost < 0.0001:
        cost_str = "< $0.0001"
    else:
        cost_str = f"${known_cost:.4f}"
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


class _ElidingLabel(QLabel):
    def __init__(
        self,
        text: str = "",
        parent=None,
        elide_mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
    ) -> None:
        super().__init__("", parent)
        self._full_text = ""
        self._elide_mode = elide_mode
        self.setText(text)

    def setText(self, text: str) -> None:  # type: ignore[override]
        self._full_text = text
        self._update_elided_text()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update_elided_text()

    def _update_elided_text(self) -> None:
        width = max(0, self.contentsRect().width())
        if width <= 0:
            super().setText(self._full_text)
            return
        super().setText(self.fontMetrics().elidedText(self._full_text, self._elide_mode, width))


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
        self._status_left = _ElidingLabel("", elide_mode=Qt.TextElideMode.ElideLeft)
        self._status_left.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Preferred,
        )
        self._status_left.setMinimumWidth(0)
        self._status_left.setMaximumWidth(360)
        self.addWidget(self._status_left, 0)

        # Center: cache and session telemetry only
        center_widget = QWidget(self)
        center_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        center_widget.setMinimumWidth(0)
        center_widget.setToolTip(
            "Session cache usage and estimated cost — does not reflect actual provider billing."
        )
        center_layout = QHBoxLayout(center_widget)
        center_layout.setContentsMargins(8, 0, 8, 0)
        center_layout.setSpacing(8)

        telemetry_font = QFont("Geist Mono, JetBrains Mono, Consolas, monospace")
        telemetry_font.setStyleHint(QFont.StyleHint.Monospace)
        telemetry_font.setPointSize(11)

        self._status_cache = _ElidingLabel("")
        self._status_cache.setFont(telemetry_font)
        self._status_cache.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self._status_cache.setMinimumWidth(0)
        self._status_cache.setStyleSheet(
            "color: #7dcfff; font-weight: 600; padding: 0 2px;"
        )
        center_layout.addWidget(self._status_cache, 1)

        self._status_separator = QLabel("│")
        self._status_separator.setStyleSheet("color: #4b5369; padding: 0 2px;")
        center_layout.addWidget(self._status_separator)

        self._status_session = QLabel("")
        self._status_session.setFont(telemetry_font)
        self._status_session.setStyleSheet("color: #a8aebb; padding: 0 2px;")
        self._status_session.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Preferred,
        )
        center_layout.addWidget(self._status_session)

        self.addWidget(center_widget, 1)

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

        usage_text = f"{total_hit:,} hit · {total_miss:,} miss · {total_out:,} out"
        cost_str = _format_footer_cost(known_cost, unknown_count, total_models)
        self._status_cache.setText(usage_text)
        self._status_session.setText(f"Session {cost_str}")

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
                self._status_balance.setText("Add Credits")
                self._status_balance.setToolTip(
                    "Add Aura Credits. The easiest way to run Aura without provider setup."
                )
            else:
                self._status_balance.setText("Set up AI")
                self._status_balance.setToolTip("Set up Aura Credits or an API provider. Click to open.")
        self._status_balance.setVisible(True)
