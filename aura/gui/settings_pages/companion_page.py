"""Companion settings page — pairing, status, and connection config.

Layout:
    [ Connection status pill ]                       [ Enable switch ]

    Connection:
        Desktop Name ..................

    Phone Pairing (visible when connected):
        [ Pair Phone button ]
        (Pairing card: QR, code, expiry, Cancel — when pairing)
        (Paired status — after pairing complete)

    Advanced / Self-hosting (collapsible, collapsed by default):
        Relay URL .......................
        Companion Web URL ................
        Desktop ID (read-only) ...........
"""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aura.companion.auth import get_device_display_name, get_device_id
from aura.gui.cards._collapsible import _CollapsibleSection
from aura.gui.theme import (
    ACCENT,
    BG_ALT,
    BG_RAISED,
    BORDER,
    BORDER_STRONG,
    DANGER,
    FG,
    FG_DIM,
    FG_MUTED,
    SUCCESS,
    WARN,
)
from aura.gui.widgets.glass_switch import GlassSwitch
from aura.gui.widgets.qr_widget import QrCodeLabel
from aura.settings import AppSettings


_STATUS_STYLES = {
    "disabled":   ("Disabled",   FG_MUTED),
    "connecting": ("Connecting", WARN),
    "connected":  ("Connected",  SUCCESS),
    "error":      ("Offline",    DANGER),
}


class _Card(QFrame):
    """Glass-like card frame for sections."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CompanionCard")
        self.setStyleSheet(
            f"#CompanionCard {{"
            f"  background: {BG_ALT};"
            f"  border: 1px solid {BORDER};"
            f"  border-radius: 12px;"
            f"}}"
        )


class CompanionPage(QWidget):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._manager: Optional[object] = None
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._tick_countdown)
        self._pairing_expires_at: float = 0.0
        self._pair_url: str = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        # ── Header row: status pill + enable switch ─────────────
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        self._status_pill = QLabel("● Disabled")
        self._status_pill.setStyleSheet(self._pill_style(FG_MUTED))
        header_row.addWidget(self._status_pill)
        header_row.addStretch()
        self._enabled_switch = GlassSwitch("Enable Companion", self._settings.companion_enabled)
        header_row.addWidget(self._enabled_switch)
        outer.addLayout(header_row)

        # ── Connection section ──────────────────────────────────
        conn_card = _Card(self)
        conn_layout = QVBoxLayout(conn_card)
        conn_layout.setContentsMargins(16, 14, 16, 14)
        conn_layout.setSpacing(8)

        conn_title = QLabel("Connection")
        conn_title.setStyleSheet(f"color: {FG}; font-size: 13px; font-weight: 600;")
        conn_layout.addWidget(conn_title)

        name_row = QHBoxLayout()
        name_row.setSpacing(10)
        name_label = QLabel("Desktop Name")
        name_label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        name_label.setFixedWidth(100)
        name_row.addWidget(name_label)
        self._display_name_edit = QLineEdit(self._settings.companion_display_name or get_device_display_name())
        self._display_name_edit.setPlaceholderText("Auto from hostname")
        name_row.addWidget(self._display_name_edit, 1)
        conn_layout.addLayout(name_row)

        outer.addWidget(conn_card)

        # ── Pairing section (visible when connected) ────────────
        self._pairing_section = QWidget(self)
        self._pairing_section.setVisible(False)
        pairing_outer = QVBoxLayout(self._pairing_section)
        pairing_outer.setContentsMargins(0, 0, 0, 0)
        pairing_outer.setSpacing(8)

        pair_card = _Card(self._pairing_section)
        pair_card_layout = QVBoxLayout(pair_card)
        pair_card_layout.setContentsMargins(16, 14, 16, 14)
        pair_card_layout.setSpacing(8)

        pair_header = QHBoxLayout()
        pair_title = QLabel("Phone Pairing")
        pair_title.setStyleSheet(f"color: {FG}; font-size: 13px; font-weight: 600;")
        pair_header.addWidget(pair_title)
        pair_header.addStretch()
        self._pair_button = QPushButton("Pair Phone")
        self._pair_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pair_button.setStyleSheet(self._primary_button_style())
        self._pair_button.clicked.connect(self._on_pair_clicked)
        pair_header.addWidget(self._pair_button)
        pair_card_layout.addLayout(pair_header)

        pairing_outer.addWidget(pair_card)

        # Pairing card (QR + code + expiry)
        self._pair_card = _Card(self._pairing_section)
        self._pair_card.setVisible(False)
        pair_layout = QHBoxLayout(self._pair_card)
        pair_layout.setContentsMargins(16, 16, 16, 16)
        pair_layout.setSpacing(18)

        self._qr_label = QrCodeLabel(220, self._pair_card)
        pair_layout.addWidget(self._qr_label, 0, Qt.AlignmentFlag.AlignTop)

        info_col = QVBoxLayout()
        info_col.setSpacing(8)

        pair_subtitle = QLabel("Scan with your phone")
        pair_subtitle.setStyleSheet(f"color: {FG}; font-size: 14px; font-weight: 600;")
        info_col.addWidget(pair_subtitle)

        pair_sub = QLabel("Open the Aura Companion web app, then point your camera at the QR.\nOr type the code below if you're testing locally.")
        pair_sub.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        pair_sub.setWordWrap(True)
        info_col.addWidget(pair_sub)

        code_caption = QLabel("Pairing code")
        code_caption.setStyleSheet(f"color: {FG_MUTED}; font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em;")
        info_col.addWidget(code_caption)

        self._code_text = QLabel("------")
        code_font = QFont("JetBrains Mono")
        code_font.setStyleHint(QFont.StyleHint.Monospace)
        code_font.setPointSize(22)
        code_font.setBold(True)
        self._code_text.setFont(code_font)
        self._code_text.setStyleSheet(
            f"color: {ACCENT}; letter-spacing: 0.32em; background: {BG_RAISED};"
            f" border: 1px solid {BORDER_STRONG}; border-radius: 8px;"
            " padding: 10px 14px;"
        )
        self._code_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._code_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info_col.addWidget(self._code_text)

        self._expiry_label = QLabel("")
        self._expiry_label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        info_col.addWidget(self._expiry_label)

        url_caption = QLabel("Pair URL (open on phone)")
        url_caption.setStyleSheet(f"color: {FG_MUTED}; font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em;")
        info_col.addWidget(url_caption)

        self._url_label = QLabel("")
        self._url_label.setStyleSheet(
            f"color: {FG}; background: {BG_RAISED}; border: 1px solid {BORDER};"
            f" border-radius: 6px; padding: 6px 10px; font-size: 10px;"
            " font-family: 'JetBrains Mono', 'Consolas', monospace;"
        )
        self._url_label.setWordWrap(True)
        self._url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._url_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        info_col.addWidget(self._url_label)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self._copy_url_btn = QPushButton("Copy URL")
        self._copy_url_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_url_btn.setStyleSheet(self._ghost_button_style())
        self._copy_url_btn.clicked.connect(self._on_copy_url)
        button_row.addWidget(self._copy_url_btn)

        self._cancel_pair_btn = QPushButton("Cancel")
        self._cancel_pair_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_pair_btn.setStyleSheet(self._danger_button_style())
        self._cancel_pair_btn.clicked.connect(self._on_cancel_pair)
        button_row.addWidget(self._cancel_pair_btn)
        button_row.addStretch()
        info_col.addLayout(button_row)
        info_col.addStretch()

        pair_layout.addLayout(info_col, 1)
        pairing_outer.addWidget(self._pair_card)

        # Paired status
        self._paired_status_label = QLabel("No phone paired yet.")
        self._paired_status_label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px; padding: 2px 4px;")
        pairing_outer.addWidget(self._paired_status_label)

        outer.addWidget(self._pairing_section)

        # ── Advanced / Self-hosting (collapsible) ───────────────
        advanced_body = QWidget(self)
        advanced_body_layout = QVBoxLayout(advanced_body)
        advanced_body_layout.setContentsMargins(0, 4, 0, 0)
        advanced_body_layout.setSpacing(8)

        adv_form = QFormLayout()
        adv_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        adv_form.setHorizontalSpacing(12)
        adv_form.setVerticalSpacing(8)

        self._relay_url_edit = QLineEdit(self._settings.companion_relay_url)
        adv_form.addRow(self._dim_label("Relay URL"), self._relay_url_edit)

        self._web_url_edit = QLineEdit(self._settings.companion_web_url)
        self._web_url_edit.setPlaceholderText("http://<your-lan-ip>:5173")
        adv_form.addRow(self._dim_label("Companion Web URL"), self._web_url_edit)

        device_id = get_device_id()
        device_id_label = QLabel(device_id)
        device_id_label.setStyleSheet(
            f"color: {FG_DIM}; background: {BG_RAISED}; border: 1px solid {BORDER};"
            f" border-radius: 6px; padding: 4px 8px; font-family: 'JetBrains Mono', 'Consolas', monospace;"
            " font-size: 11px;"
        )
        device_id_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        adv_form.addRow(self._dim_label("Desktop ID"), device_id_label)

        advanced_body_layout.addLayout(adv_form)

        note = QLabel("Configure these for custom relay deployments")
        note.setStyleSheet(f"color: {FG_MUTED}; font-size: 10px; padding: 2px 0;")
        note.setWordWrap(True)
        advanced_body_layout.addWidget(note)

        advanced_card = _Card(self)
        advanced_card_layout = QVBoxLayout(advanced_card)
        advanced_card_layout.setContentsMargins(0, 0, 0, 0)
        advanced_card_layout.setSpacing(0)

        self._advanced_section = _CollapsibleSection(
            "Advanced / Self-hosting",
            advanced_body,
            start_open=False,
            parent=advanced_card,
        )
        advanced_card_layout.addWidget(self._advanced_section)
        advanced_card_layout.addStretch()

        outer.addWidget(advanced_card)

        outer.addStretch()

    # ── Manager wiring ───────────────────────────────────────

    def set_manager(self, manager: object) -> None:
        from aura.companion import CompanionManager as _CM
        if not isinstance(manager, _CM):
            return
        self._manager = manager
        manager.connection_status_changed.connect(self._on_connection_status)
        manager.pairing_code_invalidated.connect(self._on_pairing_invalidated)
        manager.pairing_complete.connect(self._on_pairing_complete)

    # ── Status / Pairing slots ───────────────────────────────

    def _on_connection_status(self, status: str) -> None:
        label, color = _STATUS_STYLES.get(status, ("Unknown", FG_DIM))
        self._status_pill.setText(f"● {label}")
        self._status_pill.setStyleSheet(self._pill_style(color))
        self._pairing_section.setVisible(status == "connected")

    def _on_pair_clicked(self) -> None:
        if self._manager is None:
            return
        try:
            info = self._manager.start_pairing()  # type: ignore[attr-defined]
        except Exception:
            return
        code = info.get("code", "")
        expires_at = float(info.get("expires_at", 0.0))
        pair_url = info.get("pair_url", "")
        self._pair_url = pair_url
        self._pairing_expires_at = expires_at
        self._code_text.setText(code)
        self._url_label.setText(pair_url)
        self._qr_label.set_data(pair_url)
        self._pair_card.setVisible(True)
        self._countdown_timer.start()
        self._tick_countdown()

    def _on_copy_url(self) -> None:
        if self._pair_url:
            QGuiApplication.clipboard().setText(self._pair_url)
            self._copy_url_btn.setText("Copied!")
            QTimer.singleShot(1200, lambda: self._copy_url_btn.setText("Copy URL"))

    def _on_cancel_pair(self) -> None:
        if self._manager is not None:
            try:
                self._manager.cancel_pairing()  # type: ignore[attr-defined]
            except Exception:
                pass
        self._pair_card.setVisible(False)
        self._countdown_timer.stop()
        self._pair_url = ""

    def _on_pairing_invalidated(self) -> None:
        self._pair_card.setVisible(False)
        self._countdown_timer.stop()

    def _on_pairing_complete(self, device_name: str) -> None:
        self._pair_card.setVisible(False)
        self._countdown_timer.stop()
        self._paired_status_label.setText(f"✔  Paired with {device_name}")
        self._paired_status_label.setStyleSheet(f"color: {SUCCESS}; font-size: 11px; padding: 2px 4px;")

    def _tick_countdown(self) -> None:
        if not self._pairing_expires_at:
            self._expiry_label.setText("")
            return
        secs = int(max(0, self._pairing_expires_at - time.time()))
        if secs <= 0:
            self._expiry_label.setText("Code expired — generate a new one")
            self._expiry_label.setStyleSheet(f"color: {DANGER}; font-size: 11px;")
            self._countdown_timer.stop()
            return
        mm = secs // 60
        ss = secs % 60
        self._expiry_label.setText(f"Expires in {mm}:{ss:02d}")
        color = WARN if secs < 60 else FG_DIM
        self._expiry_label.setStyleSheet(f"color: {color}; font-size: 11px;")

    # ── Settings collection ──────────────────────────────────

    def collect_settings(self, settings: AppSettings) -> None:
        settings.companion_enabled = self._enabled_switch.isChecked()
        settings.companion_display_name = self._display_name_edit.text().strip()
        settings.companion_relay_url = self._relay_url_edit.text().strip() or "ws://localhost:8765"
        settings.companion_web_url = self._web_url_edit.text().strip() or "http://localhost:5173"

    # ── Style helpers ────────────────────────────────────────

    @staticmethod
    def _dim_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        return lbl

    @staticmethod
    def _pill_style(color: str) -> str:
        return (
            f"color: {color}; background: rgba(255,255,255,0.04);"
            f" border: 1px solid {BORDER}; border-radius: 999px;"
            " padding: 3px 10px; font-size: 11px; font-weight: 600;"
        )

    @staticmethod
    def _primary_button_style() -> str:
        return (
            f"QPushButton {{"
            f"  background: {ACCENT}; color: #0a0f1f;"
            f"  border: none; border-radius: 8px;"
            f"  padding: 8px 18px; font-weight: 600;"
            f"}}"
            f"QPushButton:hover {{ background: #94b6ff; }}"
            f"QPushButton:disabled {{ background: {BORDER}; color: {FG_MUTED}; }}"
        )

    @staticmethod
    def _ghost_button_style() -> str:
        return (
            f"QPushButton {{"
            f"  background: transparent; color: {FG};"
            f"  border: 1px solid {BORDER_STRONG}; border-radius: 8px;"
            f"  padding: 6px 14px; font-weight: 500;"
            f"}}"
            f"QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}"
        )

    @staticmethod
    def _danger_button_style() -> str:
        return (
            f"QPushButton {{"
            f"  background: transparent; color: {DANGER};"
            f"  border: 1px solid {DANGER}; border-radius: 8px;"
            f"  padding: 6px 14px; font-weight: 500;"
            f"}}"
            f"QPushButton:hover {{ background: rgba(247,118,142,0.12); }}"
        )
