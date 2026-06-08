"""Real-time Drone run progress card — one per active run."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from aura.drones.definition import DroneDefinition
from aura.drones.receipt import DroneReceipt
from aura.gui.theme import ACCENT, BG, BG_RAISED, BORDER, DANGER, FG, FG_DIM, FG_MUTED, SUCCESS, WARN


class DroneRunCard(QFrame):
    """Displays live progress of a Drone execution with streaming events.

    Layout:
      ┌─────────────────────────────────────┐
      │ Drone Name                ● running │
      │ ┌─────────────────────────────────┐ │
      │ │ Tool call: read_file(foo.py)   │ │
      │ │ Result: OK (123 bytes)         │ │
      │ │ Tool call: grep_search(...)    │ │
      │ │ ...                            │ │
      │ └─────────────────────────────────┘ │
      │           [Cancel]                  │
      └─────────────────────────────────────┘
    """

    cancelRequested = Signal()
    closeRequested = Signal()

    def __init__(self, drone: DroneDefinition, parent: QWidget | None = None, readonly: bool = False) -> None:
        super().__init__(parent)
        self._drone = drone
        self._receipt: DroneReceipt | None = None
        self._is_readonly_view = readonly
        self._build_ui()

    def _build_ui(self) -> None:
        self.setObjectName("droneRunCard")
        self.setStyleSheet(
            f"QFrame#droneRunCard {{ background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 8px; padding: 0px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        # Header row: name + status badge
        header = QHBoxLayout()
        header.setSpacing(8)

        name_label = QLabel(self._drone.name)
        name_label.setStyleSheet(f"color: {FG}; font-size: 14px; font-weight: 700; background: transparent;")
        header.addWidget(name_label)

        header.addStretch()

        self._status_badge = QLabel("summoning")
        self._status_badge.setStyleSheet(
            f"color: {WARN}; font-size: 11px; font-weight: 600; "
            f"padding: 2px 8px; border-radius: 4px; background: #1a1a24; border: 1px solid {WARN};"
        )
        header.addWidget(self._status_badge)

        layout.addLayout(header)

        # Scrollable log area
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            f"QScrollArea {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 4px; }}"
            f"QScrollBar:vertical {{ background: {BG}; width: 8px; }}"
            f"QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; }}"
        )

        self._log_widget = QWidget()
        self._log_layout = QVBoxLayout(self._log_widget)
        self._log_layout.setContentsMargins(8, 4, 8, 4)
        self._log_layout.setSpacing(2)
        self._log_layout.addStretch()

        scroll.setWidget(self._log_widget)
        layout.addWidget(scroll, 1)

        # Action buttons row
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setStyleSheet(
            f"QPushButton {{ background: #2a1a1a; color: {DANGER}; border: 1px solid {DANGER}; "
            f"border-radius: 4px; padding: 4px 16px; font-size: 12px; }}"
            f"QPushButton:hover {{ background: #3a2020; }}"
        )
        self._cancel_btn.clicked.connect(self.cancelRequested.emit)
        btn_layout.addWidget(self._cancel_btn)

        self._close_btn = QPushButton("Close")
        self._close_btn.setStyleSheet(
            f"QPushButton {{ background: #1a1a24; color: {FG_DIM}; border: 1px solid {BORDER}; "
            f"border-radius: 4px; padding: 4px 16px; font-size: 12px; }}"
            f"QPushButton:hover {{ background: #222230; color: {FG}; }}"
        )
        self._close_btn.clicked.connect(self.closeRequested.emit)
        self._close_btn.hide()  # hidden until run completes
        btn_layout.addWidget(self._close_btn)

        layout.addLayout(btn_layout)

        # Start with cancel visible, close hidden (unless readonly)
        if self._is_readonly_view:
            self._cancel_btn.hide()
            self._close_btn.show()
        else:
            self._cancel_btn.show()
            self._close_btn.hide()

    # --- Event handlers called from MainWindow ---

    def on_status_changed(self, status: str) -> None:
        self._status_badge.setText(status)
        if status == "running":
            self._status_badge.setStyleSheet(
                f"color: {SUCCESS}; font-size: 11px; font-weight: 600; "
                f"padding: 2px 8px; border-radius: 4px; background: #0a1a10; border: 1px solid {SUCCESS};"
            )
        elif status == "completed":
            self._status_badge.setStyleSheet(
                f"color: {SUCCESS}; font-size: 11px; font-weight: 600; "
                f"padding: 2px 8px; border-radius: 4px; background: #0a1a10; border: 1px solid {SUCCESS};"
            )
            self._cancel_btn.hide()
            self._close_btn.show()
        elif status in ("failed", "cancelled", "timed_out"):
            self._status_badge.setStyleSheet(
                f"color: {DANGER}; font-size: 11px; font-weight: 600; "
                f"padding: 2px 8px; border-radius: 4px; background: #1a0a0a; border: 1px solid {DANGER};"
            )
            self._cancel_btn.hide()
            self._close_btn.show()

    def on_content_delta(self, text: str) -> None:
        """Append a text chunk to the log."""
        self._add_log_entry(f"  {text}", FG)

    def on_tool_call_start(self, index: int, call_id: str, name: str) -> None:
        self._add_log_entry(f"🔧 {name}", ACCENT, bold=True)

    def on_tool_call_args(self, index: int, args_chunk: str) -> None:
        args_stripped = args_chunk.strip().rstrip(",")
        if args_stripped:
            self._add_log_entry(f"  args: {args_stripped}", FG_DIM)

    def on_tool_result(self, call_id: str, name: str, ok: bool, result: str) -> None:
        status = "✓" if ok else "✗"
        color = SUCCESS if ok else DANGER
        # Truncate long results
        result_text = result[:200] if len(result) > 200 else result
        self._add_log_entry(f"  {status} {result_text}", color)

    def on_api_error(self, status_code: int, message: str) -> None:
        self._add_log_entry(f"⚠ API Error ({status_code}): {message}", DANGER)

    def on_receipt_ready(self, receipt: DroneReceipt) -> None:
        self._receipt = receipt
        summary = (
            f"\n── Run complete ──\n"
            f"  Status: {receipt.status}\n"
            f"  Tool calls: {receipt.tool_calls_made}\n"
            f"  Errors: {receipt.tool_errors}\n"
        )
        self._add_log_entry(summary, FG_MUTED)

    def _add_log_entry(self, text: str, color: str, bold: bool = False) -> None:
        label = QLabel(text)
        fw = "font-weight: 700;" if bold else ""
        label.setStyleSheet(
            f"color: {color}; font-size: 11px; background: transparent; {fw}"
        )
        label.setWordWrap(True)
        # Insert before the stretch
        self._log_layout.insertWidget(self._log_layout.count() - 1, label)

    def populate_from_receipt(self, receipt: DroneReceipt) -> None:
        """Fill the run card from a saved receipt (read-only view)."""
        self._receipt = receipt
        self._is_readonly_view = True

        # Set status badge
        status = receipt.status
        status_color = {
            "completed": SUCCESS,
            "failed": DANGER,
            "cancelled": WARN,
        }.get(status, FG_MUTED)
        self._status_badge.setText(status.upper())
        self._status_badge.setStyleSheet(
            f"color: {status_color}; font-size: 11px; font-weight: 600; "
            f"padding: 2px 8px; border-radius: 4px; "
            f"background: {status_color}22; border: 1px solid {status_color};"
        )

        # Show Close, hide Cancel
        self._cancel_btn.hide()
        self._close_btn.show()

        # Populate tool calls
        for tc in receipt.tool_calls:
            name = tc.get("name", "?")
            args = tc.get("args", {})
            result = tc.get("result", "")

            self._add_log_entry(f"── {name} ──", ACCENT, bold=True)
            if args:
                import json
                self._add_log_entry(f"  Args: {json.dumps(args, indent=2)}", FG_DIM)
            if result:
                self._add_log_entry(f"  Result: {result[:500]}", FG)
            self._add_log_entry("", FG_MUTED)

        # Show errors
        for err in receipt.errors:
            self._add_log_entry(f"\u26A0 Error: {err}", DANGER)

        # Summary line
        elapsed = receipt.elapsed_seconds
        dur_str = f"{elapsed:.0f}s" if elapsed < 60 else f"{elapsed/60:.1f}m"
        summary = (
            f"\n── Run complete ──\n"
            f"  Status: {receipt.status}\n"
            f"  Duration: {dur_str}\n"
            f"  Tool calls: {receipt.tool_calls_made}\n"
            f"  Errors: {receipt.tool_errors}\n"
        )
        self._add_log_entry(summary, FG_MUTED)

    @property
    def receipt(self) -> DroneReceipt | None:
        return self._receipt
