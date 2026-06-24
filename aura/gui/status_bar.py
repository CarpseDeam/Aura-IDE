from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QLabel, QSizeGrip, QStatusBar

from aura.config import PROVIDERS, ThinkingMode, cost_usd

_THINKING_LABEL = {"off": "Off", "high": "High", "max": "Max"}


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


class AuraStatusBar(QStatusBar):
    def __init__(self, parent=None, show_resize_grip: bool = True) -> None:
        super().__init__(parent)

        self._drone_label: QLabel | None = None
        self._resize_grip_allowed = show_resize_grip

        # Left side: workspace, model, thinking
        self._status_left = QLabel("")
        self.addWidget(self._status_left, 1)        
        # Right side: tokens, cost
        self._status_tokens = QLabel("0 hit · 0 miss · 0 out")
        self.addPermanentWidget(self._status_tokens)

        self._status_cost = QLabel("$—")
        self._status_cost.setObjectName("statusCost")
        self.addPermanentWidget(self._status_cost)

        self._status_balance = QLabel("")
        self._status_balance.setObjectName("statusBalance")
        self.addPermanentWidget(self._status_balance)
        self._status_balance.setVisible(False)

        self.setSizeGripEnabled(False)
        self._resize_grip = _StatusResizeGrip(self)
        self.addPermanentWidget(self._resize_grip)
        self._resize_grip.setVisible(show_resize_grip)
        self._resize_grip.setEnabled(show_resize_grip)

        # Monospace for numbers
        mono_font = QFont("Geist Mono, JetBrains Mono, Consolas, monospace")
        mono_font.setStyleHint(QFont.StyleHint.Monospace)
        mono_font.setPointSize(11)
        self._status_tokens.setFont(mono_font)
        self._status_cost.setFont(mono_font)
        self._status_balance.setFont(mono_font)

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
        show_balance: bool = False,
        balance_micros: int | None = None,
    ) -> None:
        # Workspace path truncation
        ws = workspace_root
        if len(ws) > 64:
            ws = "…" + ws[-63:]
            
        # Model label lookup
        model_label = model_id
        for cfg in PROVIDERS.values():
            if model_id in cfg.models:
                model_label = cfg.models[model_id].label
                break
                
        thinking_label = _THINKING_LABEL.get(thinking, "Off")
        self._status_left.setText(f"{ws}    ·    {model_label}    ·    Thinking: {thinking_label}")

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

        self._status_tokens.setText(
            f"{total_hit:,} hit · {total_miss:,} miss · {total_out:,} out"
        )

        total_models = len(session_usage)
        if total_models == 0:
            self._status_cost.setText("$—")
            self._status_cost.setToolTip("")
        elif unknown_count == total_models:
            self._status_cost.setText("$?.??????")
            self._status_cost.setToolTip(
                "Session cost estimate — pricing unknown for all models used."
            )
        elif unknown_count > 0:
            self._status_cost.setText(f"${known_cost:.6f}*")
            self._status_cost.setToolTip(
                "Session cost estimate — some model pricing unknown, actual may differ from provider billing."
            )
        else:
            self._status_cost.setText(f"${known_cost:.6f}")
            self._status_cost.setToolTip(
                "Session cost estimate — does not reflect actual provider billing."
            )

        # Balance display
        if show_balance:
            if balance_micros is not None:
                self._status_balance.setText(f"Credits: ${balance_micros / 1_000_000:.2f}")
                self._status_balance.setToolTip("")
            else:
                self._status_balance.setText("Credits: $—")
                self._status_balance.setToolTip("Balance not loaded yet.")
            self._status_balance.setVisible(True)
        else:
            self._status_balance.setVisible(False)
            self._status_balance.setToolTip("")
