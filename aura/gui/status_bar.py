from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizeGrip,
    QSizePolicy,
    QStatusBar,
    QWidget,
)

from aura.config import ThinkingMode, cost_usd
from aura.conversation.telemetry import LatestContext, context_window_for_model
from aura.gui.theme import (
    BG_RAISED,
    BORDER,
    BORDER_STRONG,
    FG_DIM,
    STATUS_CONTEXT,
    STATUS_METER,
    STATUS_TELEMETRY,
)


def _format_footer_cost(known_cost: float, unknown_count: int, total_models: int) -> str:
    """Format conversation cost for human-readable footer display."""
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


def _format_cache_percentage(total_hit: int, total_miss: int) -> str:
    """Format the conversation prompt-cache hit percentage (output excluded)."""
    if total_hit + total_miss == 0:
        return "—"
    return f"{total_hit / (total_hit + total_miss) * 100:.1f}"


def _format_compact_tokens(tokens: int) -> str:
    """Format token values compactly while retaining useful context precision."""
    tokens = max(0, int(tokens))
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.0f}k"
    return str(tokens)


def _format_context_occupancy(input_tokens: int, context_window_tokens: int) -> tuple[str, int | None]:
    """Return display text and a tenths-percent meter value for a context snapshot."""
    input_tokens = max(0, int(input_tokens))
    context_window_tokens = max(0, int(context_window_tokens))
    if context_window_tokens == 0:
        return f"Context {_format_compact_tokens(input_tokens)} / unknown · —", None
    percentage = input_tokens / context_window_tokens * 100
    return (
        f"Context {_format_compact_tokens(input_tokens)} / "
        f"{_format_compact_tokens(context_window_tokens)} · {percentage:.1f}%",
        min(1000, round(percentage * 10)),
    )


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


class AuraStatusBar(QStatusBar):
    handoff_requested = Signal()

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

        # Center: conversation usage, latest context occupancy, and handoff.
        center_widget = QWidget(self)
        center_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        center_widget.setMinimumWidth(0)
        center_widget.setToolTip(
            "Conversation cache usage, context occupancy, and estimated cost — does not reflect actual provider billing."
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
            f"color: {STATUS_TELEMETRY}; font-weight: 600; padding: 0 2px;"
        )
        center_layout.addWidget(self._status_cache, 1)

        self._status_context_separator = QLabel("│")
        self._status_context_separator.setStyleSheet(f"color: {BORDER_STRONG}; padding: 0 2px;")
        center_layout.addWidget(self._status_context_separator)

        self._status_context = _ElidingLabel("")
        self._status_context.setFont(telemetry_font)
        self._status_context.setStyleSheet(f"color: {STATUS_CONTEXT}; padding: 0 2px;")
        self._status_context.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        center_layout.addWidget(self._status_context)

        self._context_meter = QProgressBar()
        self._context_meter.setRange(0, 1000)
        self._context_meter.setTextVisible(False)
        self._context_meter.setFixedWidth(52)
        self._context_meter.setFixedHeight(7)
        self._context_meter.setToolTip("Latest provider-confirmed context-window occupancy")
        self._context_meter.setStyleSheet(
            f"QProgressBar {{ background: {BG_RAISED}; border: 1px solid {BORDER}; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {STATUS_METER}; border-radius: 2px; }}"
        )
        center_layout.addWidget(self._context_meter)

        self._handoff_btn = QPushButton("Handoff")
        self._handoff_btn.setToolTip(
            "Continue in Fresh Chat — summarize this conversation and continue with a fresh context window."
        )
        self._handoff_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._handoff_btn.setFixedHeight(24)
        self._handoff_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {FG_DIM}; border: 1px solid {BORDER}; "
            "border-radius: 5px; padding: 2px 8px; font-weight: 600; }"
            f"QPushButton:hover {{ background: {BG_RAISED}; border-color: {BORDER_STRONG}; }}"
        )
        self._handoff_btn.clicked.connect(self.handoff_requested.emit)
        center_layout.addWidget(self._handoff_btn)

        self._status_separator = QLabel("│")
        self._status_separator.setStyleSheet(f"color: {BORDER_STRONG}; padding: 0 2px;")
        center_layout.addWidget(self._status_separator)

        self._status_session = QLabel("")
        self._status_session.setFont(telemetry_font)
        self._status_session.setObjectName("statusCost")
        self._status_session.setStyleSheet(f"color: {FG_DIM}; padding: 0 2px;")
        self._status_session.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Preferred,
        )
        center_layout.addWidget(self._status_session)

        self.addWidget(center_widget, 1)

        self.setSizeGripEnabled(False)
        self._resize_grip = _StatusResizeGrip(self)
        self.addPermanentWidget(self._resize_grip)
        self._resize_grip.setVisible(show_resize_grip)
        self._resize_grip.setEnabled(show_resize_grip)

    def set_resize_grip_visible(self, visible: bool) -> None:
        visible = visible and self._resize_grip_allowed
        self._resize_grip.setVisible(visible)
        self._resize_grip.setEnabled(visible)

    def set_execution_active(self, active: bool) -> None:
        """Disable Handoff for the duration of a production run."""
        self._handoff_btn.setEnabled(not active)

    def refresh(
        self, 
        workspace_root: str, 
        model_id: str, 
        thinking: ThinkingMode,
        conversation_usage: dict[str, dict[str, int]],
        latest_context: LatestContext | None = None,
        has_provider: bool = False,
    ) -> None:
        # Workspace path truncation (left side)
        ws = workspace_root
        if len(ws) > 64:
            ws = "…" + ws[-63:]
        self._status_left.setText(ws)

        # Usage and Cost
        total_hit = sum(u["hit"] for u in conversation_usage.values())
        total_miss = sum(u["miss"] for u in conversation_usage.values())
        total_out = sum(u["out"] for u in conversation_usage.values())

        known_cost = 0.0
        unknown_count = 0
        for m_id, u in conversation_usage.items():
            c = cost_usd(m_id, u["hit"], u["miss"], u["out"])
            if c is None:
                unknown_count += 1
            else:
                known_cost += c

        total_models = len(conversation_usage)

        cache_pct = _format_cache_percentage(total_hit, total_miss)
        usage_text = f"{total_hit:,} hit · {total_miss:,} miss · {total_out:,} out · cache {cache_pct}%"
        cost_str = _format_footer_cost(known_cost, unknown_count, total_models)
        self._status_cache.setText(usage_text)
        snapshot = latest_context or LatestContext()
        context_capacity = snapshot.context_window_tokens or context_window_for_model(
            snapshot.model_id or model_id
        )
        context_text, meter_value = _format_context_occupancy(snapshot.input_tokens, context_capacity)
        self._status_context.setText(context_text)
        self._context_meter.setVisible(meter_value is not None)
        if meter_value is not None:
            self._context_meter.setValue(meter_value)
        self._status_session.setText(f"Conversation {cost_str}")
