"""Real-time Drone run progress card — one per active run."""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.drones.definition import DroneDefinition
from aura.drones.receipt import DroneReceipt
from aura.gui.cards._helpers import _MarkdownTextBlock
from aura.gui.markdown_renderer import _render_markdown_with_code
from aura.gui.theme import ACCENT, BG, BG_RAISED, BORDER, DANGER, FG, FG_DIM, FG_MUTED, SUCCESS, WARN

MAX_PREVIEW_CHARS = 8000


class DroneRunCard(QFrame):
    """Displays live progress of a Drone execution with streaming events.

    Layout (running):
      ┌──────────────────────────────────────────────┐
      │  Drone Name                    ● running      │
      │  Elapsed: 12s  ·  Tools: 5                    │
      │                                                │
      │  ┌─ Live Status ─────────────────────────┐    │
      │  │  Streaming response text here...       │    │
      │  └────────────────────────────────────────┘    │
      │                                                │
      │  ▶ Show tool output (5 calls)    [Cancel]     │
      └──────────────────────────────────────────────┘

    Layout (completed):
      ┌──────────────────────────────────────────────┐
      │  Drone Name                    ✓ completed    │
      │  Elapsed: 45s  ·  Tools: 12  ·  Errors: 0    │
      │                                                │
      │  ┌─ Report ───────────────────────────────┐   │
      │  │  (receipt.summary as primary content)   │   │
      │  └─────────────────────────────────────────┘   │
      │                                                │
      │  ▶ Show tool output (12 calls)                │
      │                                                │
      │  [Copy Report]                                 │
      └──────────────────────────────────────────────┘
    """

    cancelRequested = Signal()

    def __init__(self, drone: DroneDefinition, parent: QWidget | None = None, readonly: bool = False) -> None:
        super().__init__(parent)
        self._drone = drone
        self._receipt: DroneReceipt | None = None
        self._is_readonly_view = readonly
        self._started_at = time.time()
        self._tool_count = 0
        self._tool_calls_log: list[tuple[str, str, bool, str]] = []  # (call_id, name, ok, result)
        self._tool_output_widgets: list[QWidget] = []
        self._tool_output_expanded = False
        self._live_content = ""
        self._elapsed_timer: QTimer | None = None
        self._report_placeholder: QLabel | None = None
        self._build_ui()
        self._start_elapsed_timer()

    def set_started_at(self, ts: float) -> None:
        """Override the started-at timestamp for the elapsed counter."""
        self._started_at = ts

    def _start_elapsed_timer(self) -> None:
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._elapsed_timer.start(1000)

    def _update_elapsed(self) -> None:
        if self._receipt is not None:
            self._elapsed_timer.stop()
            return
        elapsed = time.time() - self._started_at
        if elapsed < 60:
            self._meta_elapsed.setText(f"{elapsed:.1f}s")
        else:
            self._meta_elapsed.setText(f"{elapsed / 60:.1f}m")
        self._meta_tool_count.setText(f"· Tools: {self._tool_count}")

    def _build_ui(self) -> None:
        self.setObjectName("droneRunCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._apply_card_style()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        # --- Header row: name + status badge ---
        header = QHBoxLayout()
        header.setSpacing(8)

        name_label = QLabel(self._drone.name)
        name_label.setStyleSheet(
            f"color: {FG}; font-size: 14px; font-weight: 700; background: transparent;"
        )
        header.addWidget(name_label)

        header.addStretch()

        self._status_badge = QLabel("summoning")
        self._status_badge.setStyleSheet(
            f"color: {WARN}; font-size: 11px; font-weight: 600; "
            f"padding: 2px 10px; border-radius: 4px; background: #1a1a24; border: 1px solid {WARN};"
        )
        header.addWidget(self._status_badge)

        layout.addLayout(header)

        # --- Meta row: elapsed + tool count ---
        meta_row = QHBoxLayout()
        meta_row.setSpacing(16)
        self._meta_elapsed = QLabel("0.0s")
        self._meta_elapsed.setStyleSheet(
            f"color: {FG_MUTED}; font-size: 11px; background: transparent;"
        )
        meta_row.addWidget(self._meta_elapsed)

        self._meta_tool_count = QLabel("Tools: 0")
        self._meta_tool_count.setStyleSheet(
            f"color: {FG_MUTED}; font-size: 11px; background: transparent;"
        )
        meta_row.addWidget(self._meta_tool_count)

        meta_row.addStretch()
        layout.addLayout(meta_row)

        # --- Live status area (streaming content) ---
        self._live_area = QFrame()
        self._live_area.setStyleSheet(
            f"QFrame {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 4px; }}"
        )
        live_layout = QVBoxLayout(self._live_area)
        live_layout.setContentsMargins(8, 6, 8, 6)

        self._live_label = QLabel("")
        self._live_label.setWordWrap(True)
        self._live_label.setStyleSheet(f"color: {FG}; font-size: 12px; background: transparent;")
        self._live_label.setMaximumHeight(100)
        live_layout.addWidget(self._live_label)

        layout.addWidget(self._live_area)

        # --- Report area (hidden until receipt ready) ---
        self._report_area = QFrame()
        self._report_area.setStyleSheet(
            f"QFrame {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 4px; }}"
        )
        self._report_layout = QVBoxLayout(self._report_area)
        self._report_layout.setContentsMargins(8, 6, 8, 6)

        self._report_area.hide()
        layout.addWidget(self._report_area)

        # --- Tool output expander ---
        self._tool_toggle = QToolButton()
        self._tool_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._tool_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._tool_toggle.setText("Show tool output (0 calls)")
        self._tool_toggle.setStyleSheet(
            f"QToolButton {{ color: {ACCENT}; font-size: 11px; border: none; "
            f"background: transparent; padding: 2px; }}"
            f"QToolButton:hover {{ color: {FG}; }}"
        )
        self._tool_toggle.clicked.connect(self._toggle_tool_output)
        layout.addWidget(self._tool_toggle)

        self._tool_output_scroll = QScrollArea()
        self._tool_output_scroll.setWidgetResizable(True)
        self._tool_output_scroll.setMaximumHeight(200)
        self._tool_output_scroll.setStyleSheet(
            f"QScrollArea {{ background: {BG}; border: 1px solid {BORDER}; "
            f"border-radius: 4px; }}"
            f"QScrollBar:vertical {{ background: {BG}; width: 8px; }}"
            f"QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; }}"
        )

        self._tool_output_widget = QWidget()
        self._tool_output_layout = QVBoxLayout(self._tool_output_widget)
        self._tool_output_layout.setContentsMargins(8, 4, 8, 4)
        self._tool_output_layout.setSpacing(2)
        self._tool_output_layout.addStretch()

        self._tool_output_scroll.setWidget(self._tool_output_widget)
        self._tool_output_scroll.hide()
        layout.addWidget(self._tool_output_scroll)

        # --- Action buttons ---
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self._copy_btn = QPushButton("Copy Report")
        self._copy_btn.setStyleSheet(
            f"QPushButton {{ background: #1a1a24; color: {FG_DIM}; "
            f"border: 1px solid {BORDER}; border-radius: 6px; "
            f"padding: 4px 16px; font-size: 12px; }}"
            f"QPushButton:hover {{ background: #222230; color: {FG}; }}"
        )
        self._copy_btn.clicked.connect(self._copy_report)
        self._copy_btn.hide()
        btn_layout.addWidget(self._copy_btn)

        btn_layout.addStretch()

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setStyleSheet(
            f"QPushButton {{ background: #2a1a1a; color: {DANGER}; "
            f"border: 1px solid {DANGER}; border-radius: 6px; "
            f"padding: 4px 16px; font-size: 12px; }}"
            f"QPushButton:hover {{ background: #3a2020; }}"
        )
        self._cancel_btn.clicked.connect(self.cancelRequested.emit)
        btn_layout.addWidget(self._cancel_btn)



        layout.addLayout(btn_layout)

        # Initial visibility
        if self._is_readonly_view:
            self._cancel_btn.hide()
            self._live_area.hide()
        else:
            self._cancel_btn.show()

    # -- Expander logic --

    def _toggle_tool_output(self) -> None:
        self._tool_output_expanded = not self._tool_output_expanded
        if self._tool_output_expanded:
            self._tool_toggle.setArrowType(Qt.ArrowType.DownArrow)
            self._tool_toggle.setText(f"Hide tool output ({self._tool_count} calls)")
            self._rebuild_tool_output()
            self._tool_output_scroll.show()
        else:
            self._tool_toggle.setArrowType(Qt.ArrowType.RightArrow)
            self._tool_toggle.setText(f"Show tool output ({self._tool_count} calls)")
            self._tool_output_scroll.hide()

    def _rebuild_tool_output(self) -> None:
        # Clear existing widgets
        for w in self._tool_output_widgets:
            self._tool_output_layout.removeWidget(w)
            w.deleteLater()
        self._tool_output_widgets.clear()

        # Rebuild from stored log entries
        for call_id, name, ok, result in self._tool_calls_log:
            status = "✓" if ok else "✗"
            color = SUCCESS if ok else DANGER
            label = QLabel(f"{status} {name}")
            label.setStyleSheet(
                f"color: {color}; font-size: 11px; font-weight: 600; "
                f"background: transparent;"
            )
            label.setWordWrap(True)
            self._tool_output_layout.insertWidget(
                self._tool_output_layout.count() - 1, label
            )
            self._tool_output_widgets.append(label)

            if result:
                result_text = result[:300] + "…" if len(result) > 300 else result
                res = QLabel(f"  {result_text}")
                res.setStyleSheet(f"color: {FG_DIM}; font-size: 11px; background: transparent;")
                res.setWordWrap(True)
                self._tool_output_layout.insertWidget(
                    self._tool_output_layout.count() - 1, res
                )
                self._tool_output_widgets.append(res)

    # -- State transitions --

    def set_cancelling(self) -> None:
        """Disable Cancel button and show 'Cancelling...'."""
        if not self._cancel_btn.isEnabled():
            return
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setText("Cancelling...")

    # -- Event handlers called from MainWindow --

    def on_status_changed(self, status: str) -> None:
        normalized = status.strip().lower().replace(" ", "_").replace("-", "_")
        if normalized in {"waiting", "approval", "waiting_approval"}:
            normalized = "waiting_for_approval"
        self._status_badge.setText(normalized.replace("_", " "))
        if normalized in {"summoning", "waiting_for_approval"}:
            color = WARN if normalized == "waiting_for_approval" else ACCENT
            self._status_badge.setStyleSheet(
                f"color: {color}; font-size: 11px; font-weight: 600; "
                f"padding: 2px 10px; border-radius: 4px; "
                f"background: #1a1a24; border: 1px solid {color};"
            )
        elif normalized == "running":
            self._status_badge.setStyleSheet(
                f"color: {SUCCESS}; font-size: 11px; font-weight: 600; "
                f"padding: 2px 10px; border-radius: 4px; "
                f"background: #0a1a10; border: 1px solid {SUCCESS};"
            )
        elif normalized == "completed":
            self._status_badge.setStyleSheet(
                f"color: {SUCCESS}; font-size: 11px; font-weight: 600; "
                f"padding: 2px 10px; border-radius: 4px; "
                f"background: #0a1a10; border: 1px solid {SUCCESS};"
            )
            self._cancel_btn.hide()
        elif normalized == "cancelled":
            self._status_badge.setStyleSheet(
                f"color: {FG_MUTED}; font-size: 11px; font-weight: 600; "
                f"padding: 2px 10px; border-radius: 4px; "
                f"background: #18191f; border: 1px solid {FG_MUTED};"
            )
            self._cancel_btn.hide()
        elif normalized in ("failed", "timed_out"):
            self._status_badge.setStyleSheet(
                f"color: {DANGER}; font-size: 11px; font-weight: 600; "
                f"padding: 2px 10px; border-radius: 4px; "
                f"background: #1a0a0a; border: 1px solid {DANGER};"
            )
            self._cancel_btn.hide()

    def on_content_delta(self, text: str) -> None:
        """Append streaming content to the live status area."""
        self._live_content += text
        self._live_label.setText(self._live_content)

    def on_tool_call_start(self, index: int, call_id: str, name: str) -> None:
        """Increment tool count and log for expander."""
        self._tool_count += 1
        self._tool_calls_log.append((call_id, name, True, ""))
        self._meta_tool_count.setText(f"· Tools: {self._tool_count}")
        self._tool_toggle.setText(f"Show tool output ({self._tool_count} calls)")

        # If expanded, append live to the tool output area
        if self._tool_output_expanded:
            label = QLabel(f"🔧 {name}")
            label.setStyleSheet(
                f"color: {ACCENT}; font-size: 11px; font-weight: 600; "
                f"background: transparent;"
            )
            label.setWordWrap(True)
            self._tool_output_layout.insertWidget(
                self._tool_output_layout.count() - 1, label
            )
            self._tool_output_widgets.append(label)

    def on_tool_call_args(self, index: int, args_chunk: str) -> None:
        """Append tool args to expander (only when expanded)."""
        args_stripped = args_chunk.strip().rstrip(",")
        if args_stripped and self._tool_output_expanded:
            label = QLabel(f"  args: {args_stripped}")
            label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px; background: transparent;")
            label.setWordWrap(True)
            self._tool_output_layout.insertWidget(
                self._tool_output_layout.count() - 1, label
            )
            self._tool_output_widgets.append(label)

    def on_tool_result(self, call_id: str, name: str, ok: bool, result: str) -> None:
        """Store tool result in internal log for the expander."""
        # Update last entry with matching call_id
        for i in range(len(self._tool_calls_log) - 1, -1, -1):
            if self._tool_calls_log[i][0] == call_id:
                self._tool_calls_log[i] = (call_id, name, ok, result)
                break
        else:
            self._tool_calls_log.append((call_id, name, ok, result))

        # When expanded, show the result
        if self._tool_output_expanded:
            status = "✓" if ok else "✗"
            color = SUCCESS if ok else DANGER
            result_text = result[:300] + "…" if len(result) > 300 else result
            label = QLabel(f"  {status} {result_text}")
            label.setStyleSheet(f"color: {color}; font-size: 11px; background: transparent;")
            label.setWordWrap(True)
            self._tool_output_layout.insertWidget(
                self._tool_output_layout.count() - 1, label
            )
            self._tool_output_widgets.append(label)

    def on_api_error(self, status_code: int, message: str) -> None:
        """Show API error in live status area."""
        self._live_content += f"\n⚠ API Error ({status_code}): {message}"
        self._live_label.setText(self._live_content)

    def _clear_report_content(self) -> None:
        """Remove all widgets from the report layout."""
        while self._report_layout.count():
            item = self._report_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def on_receipt_ready(self, receipt: DroneReceipt) -> None:
        """Transform card into final-report mode."""
        logger.debug("[DroneRunCard] on_receipt_ready start run_id=%s", receipt.run_id)
        self._receipt = receipt
        if self._elapsed_timer is not None:
            self._elapsed_timer.stop()

        elapsed = receipt.elapsed_seconds or (time.time() - self._started_at)
        dur_str = f"{elapsed:.0f}s" if elapsed < 60 else f"{elapsed / 60:.1f}m"
        self._meta_elapsed.setText(dur_str)
        self._meta_tool_count.setText(
            f"Tools: {receipt.tool_calls_made}  ·  Errors: {receipt.tool_errors}"
        )

        # Switch from live to report
        self._live_area.hide()
        self._clear_report_content()
        if receipt.summary:
            self._report_placeholder = QLabel("Loading report...")
            self._report_placeholder.setStyleSheet(f"color: {FG_DIM}; font-size: 13px; background: transparent;")
            self._report_layout.addWidget(self._report_placeholder)
            self._report_area.show()
            QTimer.singleShot(0, self._render_receipt_report)
        else:
            label = QLabel("(no summary)")
            label.setStyleSheet(f"color: {FG_DIM}; font-size: 13px; background: transparent;")
            self._report_layout.addWidget(label)
            self._report_area.show()

        self._copy_btn.show()
        self._cancel_btn.hide()
        logger.debug("[DroneRunCard] on_receipt_ready end run_id=%s", receipt.run_id)

    def _render_receipt_report(self) -> None:
        """Deferred: render the full receipt summary markdown into the report area."""
        if self._receipt is None or not self._receipt.summary:
            return
        summary = self._receipt.summary
        capped = len(summary) > MAX_PREVIEW_CHARS
        if capped:
            summary = summary[:MAX_PREVIEW_CHARS]
        logger.debug(
            "[DroneRunCard] _render_receipt_report start run_id=%s len=%d capped=%s",
            self._receipt.run_id,
            len(self._receipt.summary),
            capped,
        )
        # Remove placeholder
        if hasattr(self, '_report_placeholder') and self._report_placeholder is not None:
            self._report_layout.removeWidget(self._report_placeholder)
            self._report_placeholder.deleteLater()
            self._report_placeholder = None
        html = _render_markdown_with_code(summary)
        md_block = _MarkdownTextBlock(html, self._report_area)
        self._report_layout.addWidget(md_block)
        if capped:
            note = QLabel(f"(Report preview capped at {MAX_PREVIEW_CHARS} characters — use Copy Report for full content)")
            note.setStyleSheet(f"color: {FG_MUTED}; font-size: 11px; background: transparent; margin-top: 4px;")
            self._report_layout.addWidget(note)
        logger.debug("[DroneRunCard] _render_receipt_report end run_id=%s", self._receipt.run_id)

    def _copy_report(self) -> None:
        """Copy receipt summary to clipboard."""
        if self._receipt is not None and self._receipt.summary:
            QApplication.clipboard().setText(self._receipt.summary)

    # -- Read-only history view --

    def populate_from_receipt(self, receipt: DroneReceipt) -> None:
        """Fill the run card from a saved receipt (read-only view)."""
        self._receipt = receipt
        self._is_readonly_view = True

        elapsed = receipt.elapsed_seconds or 0
        dur_str = f"{elapsed:.0f}s" if elapsed < 60 else f"{elapsed / 60:.1f}m"
        self._meta_elapsed.setText(dur_str)
        self._meta_tool_count.setText(
            f"Tools: {receipt.tool_calls_made}  ·  Errors: {receipt.tool_errors}"
        )

        # Status badge
        status = receipt.status
        status_color = {
            "completed": SUCCESS,
            "failed": DANGER,
            "cancelled": WARN,
        }.get(status, FG_MUTED)
        self._status_badge.setText(status.upper())
        self._status_badge.setStyleSheet(
            f"color: {status_color}; font-size: 11px; font-weight: 600; "
            f"padding: 2px 10px; border-radius: 4px; "
            f"background: {status_color}22; border: 1px solid {status_color};"
        )

        # Hide live, show report
        self._live_area.hide()
        self._clear_report_content()
        if receipt.summary:
            html = _render_markdown_with_code(receipt.summary)
            md_block = _MarkdownTextBlock(html, self._report_area)
            self._report_layout.addWidget(md_block)
        else:
            label = QLabel("(no summary)")
            label.setStyleSheet(f"color: {FG_DIM}; font-size: 13px; background: transparent;")
            self._report_layout.addWidget(label)
        self._report_area.show()

        self._cancel_btn.hide()
        self._copy_btn.show()

        # Populate tool calls for the expander
        for tc in receipt.tool_calls:
            name = tc.get("name", "?")
            result = tc.get("result", "")
            ok = tc.get("ok", True)
            call_id = tc.get("call_id", "")
            self._tool_calls_log.append((call_id, name, ok, result))
        self._tool_count = len(self._tool_calls_log)
        self._tool_toggle.setText(f"Show tool output ({self._tool_count} calls)")

    # -- Properties --

    @property
    def receipt(self) -> DroneReceipt | None:
        return self._receipt

    def highlight_focus(self) -> None:
        """Briefly accent the card when a rail pip focuses it."""
        self._apply_card_style(focused=True)
        QTimer.singleShot(900, self._apply_card_style)

    def _apply_card_style(self, focused: bool = False) -> None:
        border = ACCENT if focused else BORDER
        self.setStyleSheet(
            f"QFrame#droneRunCard {{ background: {BG_RAISED}; border: 1px solid {border}; "
            f"border-radius: 8px; padding: 0px; }}"
        )
