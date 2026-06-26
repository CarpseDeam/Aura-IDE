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

_PHASE_COLORS = {
    "preparing": FG_DIM,
    "working": ACCENT,
    "repairing": WARN,
    "validating": ACCENT,
    "launch_check": ACCENT,
    "committing": SUCCESS,
    "planning": ACCENT,
}


def _accent_color_for_status(status: str) -> str:
    if status in ("running",):
        return ACCENT
    if status == "completed":
        return SUCCESS
    if status in ("failed", "timed_out"):
        return DANGER
    if status == "cancelled":
        return FG_MUTED
    return WARN  # waiting, summoning, approval etc.


def _map_phase_from_line(line: str) -> str | None:
    """Detect a phase keyword from a content delta line. Returns phase or None."""
    stripped = line.strip()
    if stripped in ("Preparing unattended lap…", "⏳ Starting unattended lap…",
                    "▶ Preparing unattended lap…"):
        return "preparing"
    if stripped.startswith("Target:"):
        return None  # phase stays the same, target updates
    if "Running planner → worker lap…" in stripped:
        return "working"
    if "Validation failed. Starting repair attempt" in stripped:
        return "repairing"
    if "Verifying app launches…" in stripped:
        return "launch_check"
    if "repairing" in stripped.lower():
        return "repairing"
    if "Validation passed, committing…" in stripped:
        return "committing"
    return None


def _extract_target_from_line(line: str) -> str | None:
    stripped = line.strip()
    if stripped.startswith("Target:"):
        return stripped[len("Target:"):].strip()
    return None


class DroneRunCard(QFrame):
    """Displays live progress of a Drone execution with dashboard-style layout.

    Layout (running, collapsed):
      ┌──────────────────────────────────────────────┐
      │  Drone Name           ● running        [x]    │
      │  Elapsed: 12s                                 │
      │  ┌─ Lap Info ──────────────────────────┐     │
      │  │  Phase: working                      │     │
      │  │  Target: src/main.py                 │     │
      │  │  Last event: (dim italic)            │     │
      │  └──────────────────────────────────────┘     │
      │  ┌─ Timeline ───────────────────────────┐     │
      │  │  Backing up…                         │     │
      │  │  Running planner → worker lap…       │     │
      │  └──────────────────────────────────────┘     │
      │  ▶ Show tool output (N calls)    [Cancel]     │
      └──────────────────────────────────────────────┘

    Layout (completed, collapsed):
      ┌──────────────────────────────────────────────┐
      │  Drone Name                    ✓ completed    │
      │  Elapsed: 45s                                 │
      │  ┌─ Summary ────────────────────────────┐    │
      │  │  🎯 Target: src/main.py               │    │
      │  │  📄 Changed: 3 — src/main.py, …       │    │
      │  │  Commit: abcdef12                     │    │
      │  └───────────────────────────────────────┘    │
      │  ▶ Show details                    [Copy]     │
      └──────────────────────────────────────────────┘
    """

    cancelRequested = Signal()

    def __init__(self, drone: DroneDefinition, parent: QWidget | None = None,
                 readonly: bool = False) -> None:
        super().__init__(parent)
        self._drone = drone
        self._receipt: DroneReceipt | None = None
        self._is_readonly_view = readonly
        self._started_at = time.time()
        self._tool_count = 0
        self._tool_calls_log: list[tuple[str, str, bool, str]] = []
        self._details_expanded = False
        self._phase: str = "preparing"
        self._target_path: str = ""
        self._last_event: str = ""
        self._timeline_labels: list[QLabel] = []
        self._completed = False

        self._build_ui()
        self._start_elapsed_timer()

    # -- Phase / timeline state helpers --

    def _set_phase(self, phase: str) -> None:
        self._phase = phase
        color = _PHASE_COLORS.get(phase, FG_DIM)
        self._phase_value_label.setStyleSheet(
            f"color: {color}; font-size: 12px; font-weight: 600; background: transparent;"
        )
        self._phase_value_label.setText(phase.replace("_", " ").title())

    def _append_timeline_event(self, text: str) -> None:
        label = QLabel(text)
        label.setWordWrap(False)
        label.setStyleSheet(
            f"color: {FG_DIM}; font-size: 11px; background: transparent;"
        )
        label.setToolTip(text)
        # Clip overflow with fixed pixel width
        label.setFixedWidth(self._timeline_container.width() - 20)
        self._timeline_layout.insertWidget(self._timeline_layout.count() - 1, label)
        self._timeline_labels.append(label)

        # Keep at most 8 events
        while len(self._timeline_labels) > 8:
            oldest = self._timeline_labels.pop(0)
            self._timeline_layout.removeWidget(oldest)
            oldest.deleteLater()

    def _set_details_expanded(self, expanded: bool) -> None:
        self._details_expanded = expanded
        if expanded:
            self._details_content_area.show()
            self._details_toggle.setArrowType(Qt.ArrowType.DownArrow)
            if self._completed:
                self._details_toggle.setText("Hide details")
            else:
                self._details_toggle.setText(
                    f"Hide tool output ({self._tool_count} calls)"
                )
        else:
            self._details_content_area.hide()
            self._details_toggle.setArrowType(Qt.ArrowType.RightArrow)
            if self._completed:
                self._details_toggle.setText("Show details")
            else:
                self._details_toggle.setText(
                    f"Show tool output ({self._tool_count} calls)"
                )

    # -- Public interface --

    def set_started_at(self, ts: float) -> None:
        """Override the started-at timestamp for the elapsed counter."""
        self._started_at = ts

    def _start_elapsed_timer(self) -> None:
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._elapsed_timer.start(1000)

    def _update_elapsed(self) -> None:
        if self._completed:
            self._elapsed_timer.stop()
            return
        elapsed = time.time() - self._started_at
        if elapsed < 60:
            self._meta_elapsed.setText(f"{elapsed:.1f}s")
        else:
            self._meta_elapsed.setText(f"{elapsed / 60:.1f}m")

    def _build_ui(self) -> None:
        self.setObjectName("droneRunCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._apply_accent_rail(ACCENT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        # ── 1) Header row ────────────────────────────────────────────────
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

        # Cancel button in header (far right, shown when running)
        self._cancel_btn = QPushButton("✕")
        self._cancel_btn.setFixedSize(24, 24)
        self._cancel_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {FG_DIM}; "
            f"border: 1px solid {BORDER}; border-radius: 4px; font-size: 12px; padding: 0; }}"
            f"QPushButton:hover {{ background: {DANGER}22; color: {DANGER}; "
            f"border-color: {DANGER}; }}"
        )
        self._cancel_btn.clicked.connect(self.cancelRequested.emit)
        header.addWidget(self._cancel_btn)

        layout.addLayout(header)

        # ── 2) Meta row: elapsed ─────────────────────────────────────────
        meta_row = QHBoxLayout()
        meta_row.setSpacing(16)
        self._meta_elapsed = QLabel("0.0s")
        self._meta_elapsed.setStyleSheet(
            f"color: {FG_MUTED}; font-size: 11px; background: transparent;"
        )
        meta_row.addWidget(self._meta_elapsed)
        meta_row.addStretch()
        layout.addLayout(meta_row)

        # ── 3) Lap Info frame (running only) ─────────────────────────────
        self._lap_frame = QFrame()
        self._lap_frame.setFixedHeight(75)
        self._lap_frame.setStyleSheet(
            f"QFrame {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 6px; }}"
        )
        lap_layout = QVBoxLayout(self._lap_frame)
        lap_layout.setContentsMargins(8, 4, 8, 4)
        lap_layout.setSpacing(1)

        phase_row = QHBoxLayout()
        phase_label = QLabel("Phase:")
        phase_label.setStyleSheet(
            f"color: {FG_DIM}; font-size: 12px; background: transparent;"
        )
        phase_row.addWidget(phase_label)
        self._phase_value_label = QLabel("Preparing")
        self._set_phase("preparing")
        phase_row.addWidget(self._phase_value_label)
        phase_row.addStretch()
        lap_layout.addLayout(phase_row)

        target_row = QHBoxLayout()
        target_label = QLabel("Target:")
        target_label.setStyleSheet(
            f"color: {FG_DIM}; font-size: 12px; background: transparent;"
        )
        target_row.addWidget(target_label)
        self._target_value_label = QLabel("—")
        self._target_value_label.setStyleSheet(
            f"color: {FG}; font-size: 12px; background: transparent;"
        )
        self._target_value_label.setWordWrap(False)
        target_row.addWidget(self._target_value_label, 1)
        lap_layout.addLayout(target_row)

        self._last_event_label = QLabel("")
        self._last_event_label.setStyleSheet(
            f"color: {FG_DIM}; font-size: 11px; font-style: italic; background: transparent;"
        )
        self._last_event_label.setWordWrap(False)
        lap_layout.addWidget(self._last_event_label)

        layout.addWidget(self._lap_frame)

        # ── 4) Timeline container (running only) ─────────────────────────
        self._timeline_container = QFrame()
        self._timeline_container.setMaximumHeight(160)
        self._timeline_container.setStyleSheet(
            f"QFrame {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 4px; }}"
        )
        self._timeline_layout = QVBoxLayout(self._timeline_container)
        self._timeline_layout.setContentsMargins(8, 4, 8, 4)
        self._timeline_layout.setSpacing(1)
        # Stretch at end to keep events top-aligned
        self._timeline_layout.addStretch()
        layout.addWidget(self._timeline_container)

        # ── 5) Summary frame (completed only) ────────────────────────────
        self._summary_frame = QFrame()
        self._summary_frame.setStyleSheet(
            f"QFrame {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 6px; }}"
        )
        self._summary_layout = QVBoxLayout(self._summary_frame)
        self._summary_layout.setContentsMargins(8, 6, 8, 6)
        self._summary_layout.setSpacing(3)
        self._summary_frame.hide()
        layout.addWidget(self._summary_frame)

        # ── 6) Details toggle ────────────────────────────────────────────
        toggle_row = QHBoxLayout()
        self._details_toggle = QToolButton()
        self._details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._details_toggle.setText(f"Show tool output ({self._tool_count} calls)")
        self._details_toggle.setStyleSheet(
            f"QToolButton {{ color: {FG_DIM}; font-size: 11px; background: transparent; "
            f"border: none; padding: 0px; }}"
            f"QToolButton:hover {{ color: {FG}; }}"
        )
        self._details_toggle.clicked.connect(self._toggle_details)
        toggle_row.addWidget(self._details_toggle)
        toggle_row.addStretch()
        layout.addLayout(toggle_row)

        # ── 7) Details content area ──────────────────────────────────────
        self._details_content_area = QFrame()
        self._details_content_area.setObjectName("detailsContent")
        self._details_content_area.setStyleSheet(
            "QFrame#detailsContent { background: transparent; border: none; }"
        )
        self._details_content_layout = QVBoxLayout(self._details_content_area)
        self._details_content_layout.setContentsMargins(0, 0, 0, 0)
        self._details_content_layout.setSpacing(4)

        # details internal scroll area
        self._details_scroll = QScrollArea()
        self._details_scroll.setWidgetResizable(True)
        self._details_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._details_scroll.setMaximumHeight(400)
        self._details_scroll.setStyleSheet(
            f"QScrollArea {{ background: {BG}; border: 1px solid {BORDER}; "
            f"border-radius: 6px; }}"
        )
        self._details_scroll_inner = QWidget()
        self._details_scroll_layout = QVBoxLayout(self._details_scroll_inner)
        self._details_scroll_layout.setContentsMargins(8, 4, 8, 4)
        self._details_scroll_layout.setSpacing(2)
        self._details_scroll_layout.addStretch()
        self._details_scroll.setWidget(self._details_scroll_inner)
        self._details_content_layout.addWidget(self._details_scroll)

        self._details_content_area.hide()
        layout.addWidget(self._details_content_area)

        # ── 8) Action buttons row ────────────────────────────────────────
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
        layout.addLayout(btn_layout)

        # Initial visibility
        if self._is_readonly_view:
            self._cancel_btn.hide()
        else:
            self._cancel_btn.show()

        # Running card max height constraint
        self._apply_running_height_constraint()

    def _apply_running_height_constraint(self) -> None:
        """Set max height for running collapsed state (~340px)."""
        self.setMaximumHeight(340)

    def _set_completed_height_free(self) -> None:
        """Remove max height constraint for completed cards."""
        self.setMaximumHeight(10000)

    def _apply_accent_rail(self, color: str) -> None:
        """Set the left accent rail border color on the card."""
        self.setStyleSheet(
            f"QFrame#droneRunCard {{ background: {BG_RAISED}; "
            f"border-left: 3px solid {color}; "
            f"border-top: 1px solid {BORDER}; "
            f"border-right: 1px solid {BORDER}; "
            f"border-bottom: 1px solid {BORDER}; "
            f"border-radius: 8px; padding: 0px; }}"
        )

    # -- Details expander logic --

    def _toggle_details(self) -> None:
        self._set_details_expanded(not self._details_expanded)
        if self._details_expanded:
            self._rebuild_details_content()

    def _rebuild_details_content(self) -> None:
        """Rebuild the interior of the details scroll area based on state."""
        self._clear_details_inner()

        if self._completed:
            if self._receipt is not None:
                art = self._build_artifact_summary_widget(self._receipt)
                if art is not None:
                    self._details_scroll_layout.insertWidget(
                        self._details_scroll_layout.count() - 1, art
                    )
                if self._receipt.summary:
                    html = _render_markdown_with_code(self._receipt.summary)
                    md_block = _MarkdownTextBlock(html, self._details_scroll)
                    self._details_scroll_layout.insertWidget(
                        self._details_scroll_layout.count() - 1, md_block
                    )
            self._rebuild_tool_output_in_details()
        else:
            self._rebuild_tool_output_in_details()

    def _clear_details_inner(self) -> None:
        """Remove all widgets from details scroll layout except trailing stretch."""
        while self._details_scroll_layout.count() > 1:
            item = self._details_scroll_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _rebuild_tool_output_in_details(self) -> None:
        """Populate the tool call log into the details scroll area."""
        for call_id, name, ok, result in self._tool_calls_log:
            status = "✓" if ok else "✗"
            color = SUCCESS if ok else DANGER
            label = QLabel(f"{status} {name}")
            label.setStyleSheet(
                f"color: {color}; font-size: 11px; font-weight: 600; "
                f"background: transparent;"
            )
            label.setWordWrap(True)
            self._details_scroll_layout.insertWidget(
                self._details_scroll_layout.count() - 1, label
            )
            if result:
                result_text = result[:300] + "…" if len(result) > 300 else result
                res = QLabel(f"  {result_text}")
                res.setStyleSheet(
                    f"color: {FG_DIM}; font-size: 11px; background: transparent;"
                )
                res.setWordWrap(True)
                self._details_scroll_layout.insertWidget(
                    self._details_scroll_layout.count() - 1, res
                )

    # -- State transitions --

    def set_cancelling(self) -> None:
        """Disable Cancel button and show 'Cancelling...'."""
        if not self._cancel_btn.isEnabled():
            return
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setText("…")

    # -- Event handlers called from MainWindow --

    def on_status_changed(self, status: str) -> None:
        normalized = status.strip().lower().replace(" ", "_").replace("-", "_")
        if normalized.startswith("repairing"):
            self._status_badge.setText(status)
            self._status_badge.setStyleSheet(
                f"color: {WARN}; font-size: 11px; font-weight: 600; "
                f"padding: 2px 10px; border-radius: 4px; "
                f"background: #1a1a24; border: 1px solid {WARN};"
            )
            self._set_phase("repairing")
            return
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
            self._apply_accent_rail(color)
        elif normalized == "running":
            self._status_badge.setStyleSheet(
                f"color: {SUCCESS}; font-size: 11px; font-weight: 600; "
                f"padding: 2px 10px; border-radius: 4px; "
                f"background: #0a1a10; border: 1px solid {SUCCESS};"
            )
            self._apply_accent_rail(ACCENT)
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
            self._apply_accent_rail(FG_MUTED)
        elif normalized in ("failed", "timed_out"):
            self._status_badge.setStyleSheet(
                f"color: {DANGER}; font-size: 11px; font-weight: 600; "
                f"padding: 2px 10px; border-radius: 4px; "
                f"background: #1a0a0a; border: 1px solid {DANGER};"
            )
            self._cancel_btn.hide()
            self._apply_accent_rail(DANGER)

    def on_content_delta(self, text: str) -> None:
        """Append streaming content and update phase/timeline state."""
        for line in text.split("\n"):
            if not line:
                continue
            # Phase detection
            phase = _map_phase_from_line(line)
            if phase is not None:
                self._set_phase(phase)
            # Target extraction
            target = _extract_target_from_line(line)
            if target is not None:
                self._target_path = target
                self._target_value_label.setText(target)
            # Update last event
            self._last_event = line
            self._last_event_label.setText(line)
            # Timeline
            self._append_timeline_event(line)

    def on_tool_call_start(self, index: int, call_id: str, name: str) -> None:
        """Increment tool count and log for expander."""
        self._tool_count += 1
        self._tool_calls_log.append((call_id, name, True, ""))
        if not self._completed:
            self._details_toggle.setText(
                f"Show tool output ({self._tool_count} calls)"
            )

    def on_tool_call_args(self, index: int, args_chunk: str) -> None:
        """Append tool args (only when details expanded)."""
        args_stripped = args_chunk.strip().rstrip(",")
        if args_stripped and self._details_expanded and not self._completed:
            label = QLabel(f"  args: {args_stripped}")
            label.setStyleSheet(
                f"color: {FG_DIM}; font-size: 11px; background: transparent;"
            )
            label.setWordWrap(True)
            self._details_scroll_layout.insertWidget(
                self._details_scroll_layout.count() - 1, label
            )

    def on_tool_result(self, call_id: str, name: str, ok: bool, result: str) -> None:
        """Store tool result in internal log for the expander."""
        for i in range(len(self._tool_calls_log) - 1, -1, -1):
            if self._tool_calls_log[i][0] == call_id:
                self._tool_calls_log[i] = (call_id, name, ok, result)
                break
        else:
            self._tool_calls_log.append((call_id, name, ok, result))

    def on_api_error(self, status_code: int, message: str) -> None:
        """Show API error in last event."""
        line = f"⚠ API Error ({status_code}): {message}"
        self._last_event = line
        self._last_event_label.setText(line)
        self._append_timeline_event(line)

    # -- Summary helpers --

    def _summarize_receipt(self, receipt: DroneReceipt) -> None:
        """Build compact summary labels from the receipt's produced_artifact."""
        artifact = receipt.produced_artifact
        if not artifact:
            label = QLabel("(no summary)")
            label.setStyleSheet(
                f"color: {FG_DIM}; font-size: 12px; background: transparent;"
            )
            self._summary_layout.addWidget(label)
            return

        labels_data: list[tuple[str, str]] = []

        target = artifact.get("attempted_target")
        if target:
            labels_data.append((f"🎯 Target: {target}", ACCENT))

        changed = artifact.get("changed_files")
        if changed and isinstance(changed, list) and len(changed) > 0:
            count = len(changed)
            first_few = changed[:5]
            parts = ", ".join(first_few)
            if count > 5:
                parts += f", …and {count - 5} more"
            labels_data.append((f"📄 Changed: {count} — {parts}", SUCCESS))

        sha = artifact.get("commit_sha")
        if sha and isinstance(sha, str) and len(sha) >= 8:
            labels_data.append((f"Commit: {sha[:8]}", ACCENT))

        rollback = artifact.get("rollback_status")
        if rollback and rollback != "no_changes_to_revert":
            labels_data.append((f"↩ Rollback: {rollback}", WARN))

        errors = artifact.get("worker_errors")
        if errors and isinstance(errors, list) and len(errors) > 0:
            labels_data.append((f"❌ Errors: {len(errors)}", DANGER))

        # Browse needs_login reauth hint
        if artifact.get("kind") == "browse" and artifact.get("status") == "needs_login":
            profile = artifact.get("browser_profile")
            reauth = artifact.get("reauth_request")
            if reauth and profile:
                labels_data.append((
                    f"Login required for profile '{profile}'. "
                    f"Run a visible login session, then retry this Browse Drone.",
                    WARN,
                ))
            elif not profile:
                labels_data.append((
                    "Login required — no browser_profile set for reauth.",
                    WARN,
                ))

        if not labels_data:
            label = QLabel("(no summary)")
            label.setStyleSheet(
                f"color: {FG_DIM}; font-size: 12px; background: transparent;"
            )
            self._summary_layout.addWidget(label)
            return

        for text, color in labels_data:
            label = QLabel(text)
            label.setWordWrap(False)
            label.setStyleSheet(
                f"color: {color}; font-size: 11px; background: transparent;"
            )
            self._summary_layout.addWidget(label)

    def _clear_report_content(self) -> None:
        """Remove all widgets from the report layout (kept for compat)."""
        pass

    def _build_artifact_summary_widget(self, receipt: DroneReceipt) -> QFrame | None:
        """Build a compact artifact summary frame from receipt.produced_artifact."""
        artifact = receipt.produced_artifact
        if not artifact:
            return None

        labels_data: list[tuple[str, str]] = []

        target = artifact.get("attempted_target")
        if target:
            labels_data.append((f"🎯 Target: {target}", ACCENT))

        changed = artifact.get("changed_files")
        if changed and isinstance(changed, list) and len(changed) > 0:
            count = len(changed)
            first_few = changed[:5]
            parts = ", ".join(first_few)
            if count > 5:
                parts += f", …and {count - 5} more"
            labels_data.append((f"📄 Changed: {count} — {parts}", SUCCESS))

        wstatus = artifact.get("worker_status")
        if wstatus:
            if wstatus == "completed":
                wcolor = SUCCESS
            elif "fail" in str(wstatus).lower():
                wcolor = DANGER
            else:
                wcolor = WARN
            labels_data.append((f"Worker: {wstatus}", wcolor))

        errors = artifact.get("worker_errors")
        if errors and isinstance(errors, list) and len(errors) > 0:
            first = str(errors[0])
            truncated = first[:200] + "…" if len(first) > 200 else first
            suffix = f" …and {len(errors) - 1} more" if len(errors) > 1 else ""
            labels_data.append((f"❌ {truncated}{suffix}", DANGER))

        rollback = artifact.get("rollback_status")
        if rollback and rollback != "no_changes_to_revert":
            labels_data.append((f"↩ Rollback: {rollback}", WARN))

        sha = artifact.get("commit_sha")
        if sha and isinstance(sha, str) and len(sha) >= 8:
            labels_data.append((f"Commit: {sha[:8]}", ACCENT))

        repairs = artifact.get("repair_attempts")
        if repairs and isinstance(repairs, list) and len(repairs) > 0:
            labels_data.append((f"🔄 Repairs: {len(repairs)}", WARN))

        violations = artifact.get("policy_violations")
        if violations and isinstance(violations, list) and len(violations) > 0:
            text = "; ".join(str(v) for v in violations[:2])
            if len(violations) > 2:
                text += "; …"
            labels_data.append((f"⚠ {text}", DANGER))

        # Browse needs_login reauth hint
        if artifact.get("kind") == "browse" and artifact.get("status") == "needs_login":
            profile = artifact.get("browser_profile")
            reauth = artifact.get("reauth_request")
            if reauth and profile:
                labels_data.append((
                    f"Login required for profile '{profile}'. "
                    f"Run a visible login session, then retry this Browse Drone.",
                    WARN,
                ))
            elif not profile:
                labels_data.append((
                    "Login required — no browser_profile set for reauth.",
                    WARN,
                ))

        if not labels_data:
            return None

        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame {{ background: {BG}; border: 1px solid {BORDER}; "
            f"border-radius: 6px; }}"
        )
        flayout = QVBoxLayout(frame)
        flayout.setContentsMargins(8, 6, 8, 6)
        flayout.setSpacing(3)

        for text, color in labels_data:
            label = QLabel(text)
            label.setWordWrap(True)
            label.setStyleSheet(
                f"color: {color}; font-size: 11px; background: transparent;"
            )
            flayout.addWidget(label)

        return frame

    def on_receipt_ready(self, receipt: DroneReceipt) -> None:
        """Transform card from running to final-report mode."""
        logger.debug("[DroneRunCard] on_receipt_ready start run_id=%s", receipt.run_id)
        self._receipt = receipt
        self._completed = True

        if self._elapsed_timer is not None:
            self._elapsed_timer.stop()

        elapsed = receipt.elapsed_seconds or (time.time() - self._started_at)
        dur_str = f"{elapsed:.0f}s" if elapsed < 60 else f"{elapsed / 60:.1f}m"
        self._meta_elapsed.setText(dur_str)

        # Hide running sections
        self._lap_frame.hide()
        self._timeline_container.hide()

        # Show summary frame
        self._summary_frame.show()
        self._summarize_receipt(receipt)

        # Update status badge to final status
        status = receipt.status
        status_color = _accent_color_for_status(status)
        self._status_badge.setText(status.upper())
        self._status_badge.setStyleSheet(
            f"color: {status_color}; font-size: 11px; font-weight: 600; "
            f"padding: 2px 10px; border-radius: 4px; "
            f"background: {status_color}22; border: 1px solid {status_color};"
        )
        self._apply_accent_rail(status_color)

        # Action buttons
        self._copy_btn.show()
        self._cancel_btn.hide()

        # Details toggle text
        self._details_toggle.setText("Show details")

        # Remove max height constraint
        self._set_completed_height_free()

        # Collapse details by default
        self._details_expanded = True  # force toggle to collapse
        self._set_details_expanded(False)

        logger.debug("[DroneRunCard] on_receipt_ready end run_id=%s", receipt.run_id)

    def _render_receipt_report(self) -> None:
        """Deferred: render the full receipt summary markdown (kept for compat)."""
        pass

    def _copy_report(self) -> None:
        """Copy receipt summary to clipboard."""
        if self._receipt is not None and self._receipt.summary:
            QApplication.clipboard().setText(self._receipt.summary)

    # -- Read-only history view --

    def populate_from_receipt(self, receipt: DroneReceipt) -> None:
        """Fill the run card from a saved receipt (read-only view)."""
        self._receipt = receipt
        self._is_readonly_view = True
        self._completed = True

        elapsed = receipt.elapsed_seconds or 0
        dur_str = f"{elapsed:.0f}s" if elapsed < 60 else f"{elapsed / 60:.1f}m"
        self._meta_elapsed.setText(dur_str)

        # Status badge
        status = receipt.status
        status_color = _accent_color_for_status(status)
        self._status_badge.setText(status.upper())
        self._status_badge.setStyleSheet(
            f"color: {status_color}; font-size: 11px; font-weight: 600; "
            f"padding: 2px 10px; border-radius: 4px; "
            f"background: {status_color}22; border: 1px solid {status_color};"
        )
        self._apply_accent_rail(status_color)

        # Hide running sections, show summary
        self._lap_frame.hide()
        self._timeline_container.hide()
        self._summary_frame.show()
        self._summarize_receipt(receipt)

        self._cancel_btn.hide()
        self._copy_btn.show()
        self._details_toggle.setText("Show details")

        # Populate tool calls for the expander
        for tc in receipt.tool_calls:
            name = tc.get("name", "?")
            result = tc.get("result", "")
            ok = tc.get("ok", True)
            call_id = tc.get("call_id", "")
            self._tool_calls_log.append((call_id, name, ok, result))
        self._tool_count = len(self._tool_calls_log)

        # Remove max height constraint
        self._set_completed_height_free()

        # Collapse details by default
        self._details_expanded = True
        self._set_details_expanded(False)

    # -- Properties --

    @property
    def receipt(self) -> DroneReceipt | None:
        return self._receipt

    def highlight_focus(self) -> None:
        """Briefly accent the card when a rail pip focuses it."""
        color = _accent_color_for_status(
            self._receipt.status if self._receipt else "running"
        )
        self._apply_accent_rail(ACCENT)
        QTimer.singleShot(900, lambda: self._apply_accent_rail(color))
