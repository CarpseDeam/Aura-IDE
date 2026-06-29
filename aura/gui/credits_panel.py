from __future__ import annotations

import logging
import os

from PySide6.QtCore import QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aura.config import (
    APP_NAME,
    AppSettings,
    get_api_key,
    get_provider,
    save_settings,
    set_api_key,
)
from aura.gui.balance_fetcher import BalanceWorker
from aura.gui.credits_worker import CreditsCheckoutWorker, CreditsClaimWorker
from aura.gui.theme import ACCENT_HOVER, BG, BG_ALT, BORDER, FG, FG_DIM, FG_MUTED, SUCCESS, WARN

logger = logging.getLogger(__name__)


class AuraCreditsPanel(QWidget):
    credits_claimed = Signal()
    credits_changed = Signal()

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

        self._credit_threads: list[QThread] = []
        self._credit_workers: list = []

        self._balance_micros: int | None = None
        self._balance_inflight = False
        self._balance_thread: QThread | None = None
        self._balance_worker: BalanceWorker | None = None
        self._balance_timer = QTimer(self)
        self._balance_timer.setInterval(60000)
        self._balance_timer.timeout.connect(self._refresh_balance)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self._build_hero_card(layout)
        self._build_buy_card(layout)
        self._build_key_card(layout)
        layout.addStretch()

        self._refresh_key_status()
        self._update_balance_status_text()
        self._set_balance_placeholder()
        self._sync_pending_purchase_card()

        QTimer.singleShot(0, self._maybe_start_balance_refresh)

    def _build_card(self) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 14, 14, 14)
        card_layout.setSpacing(8)
        return card, card_layout

    def _add_section_title(self, layout: QVBoxLayout, text: str) -> QLabel:
        title = QLabel(text)
        title.setStyleSheet(f"color: {FG_DIM}; font-weight: bold; font-size: 13px;")
        layout.addWidget(title)
        return title

    def _build_hero_card(self, layout: QVBoxLayout) -> None:
        hero_card, hero_layout = self._build_card()
        hero_card.setStyleSheet(
            "QFrame#card {"
            "background: rgba(18, 24, 35, 0.76);"
            f"border: 1px solid rgba(122, 162, 247, 0.34);"
            "border-radius: 10px;"
            "}"
        )

        self._add_section_title(hero_layout, "Aura Credits")

        self._headline_label = QLabel("Add credits to run Aura instantly")
        self._headline_label.setWordWrap(True)
        self._headline_label.setStyleSheet(f"color: {FG}; font-size: 18px; font-weight: 700;")
        hero_layout.addWidget(self._headline_label)

        copy = QLabel("No API keys. No provider setup. Just add credits and start building.")
        copy.setWordWrap(True)
        copy.setStyleSheet(f"color: {FG_DIM}; font-size: 12px;")
        hero_layout.addWidget(copy)

        trust_label = QLabel(
            "No provider setup  \u00b7  Pay as you go  \u00b7  Bring your own key anytime"
        )
        trust_label.setStyleSheet(
            f"color: {ACCENT_HOVER}; font-size: 11px; padding: 4px 0 2px 0;"
        )
        hero_layout.addWidget(trust_label)

        state_row = QHBoxLayout()
        state_row.setSpacing(8)

        self._balance_label = QLabel("")
        self._balance_label.setStyleSheet(f"color: {FG}; font-size: 14px; font-weight: 700;")
        state_row.addWidget(self._balance_label, 1)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setToolTip("Check current balance")
        refresh_btn.setStyleSheet(
            "QPushButton {"
            f"background: rgba(122, 162, 247, 0.08); color: {FG_DIM}; "
            f"border: 1px solid rgba(122, 162, 247, 0.22); "
            "border-radius: 5px; padding: 3px 9px; font-size: 11px;"
            "}"
            "QPushButton:hover {"
            "background: rgba(122, 162, 247, 0.14);"
            "}"
        )
        refresh_btn.clicked.connect(self._refresh_balance)
        state_row.addWidget(refresh_btn)
        hero_layout.addLayout(state_row)

        self._balance_status = QLabel("")
        self._balance_status.setWordWrap(True)
        self._balance_status.setStyleSheet("font-size: 12px;")
        hero_layout.addWidget(self._balance_status)

        layout.addWidget(hero_card)

    def _build_buy_card(self, layout: QVBoxLayout) -> None:
        buy_card, buy_card_layout = self._build_card()
        self._add_section_title(buy_card_layout, "Buy Credits")

        buy_desc = QLabel(
            "$10 can last weeks for normal coding chats and small repo tasks. "
            "Heavy autonomous runs and long drone loops use more."
        )
        buy_desc.setStyleSheet(f"color: {FG_MUTED}; font-size: 12px;")
        buy_desc.setWordWrap(True)
        buy_card_layout.addWidget(buy_desc)

        # Email row
        email_row = QHBoxLayout()
        email_row.setSpacing(6)
        email_label = QLabel("Receipt email")
        email_label.setStyleSheet(f"color: {FG_DIM}; font-weight: 600;")
        self._email_input = QLineEdit()
        self._email_input.setPlaceholderText("you@example.com")
        email_row.addWidget(email_label)
        email_row.addWidget(self._email_input, 1)
        buy_card_layout.addLayout(email_row)

        # Credit pack buttons \u2014 2x2 grid
        self._buy_buttons: dict[str, QPushButton] = {}
        packs = ["5", "10", "20", "50"]

        for row_idx in range(2):
            row = QHBoxLayout()
            row.setSpacing(6)
            for col_idx in range(2):
                pid = packs[row_idx * 2 + col_idx]
                if pid == "10":
                    btn = QPushButton("$10\nRecommended")
                    btn.setStyleSheet(
                        "QPushButton {"
                        f"background: rgba(122, 162, 247, 0.92); color: {BG}; "
                        f"border: 1px solid {ACCENT_HOVER}; "
                        "border-radius: 8px; padding: 8px 8px; "
                        "font-size: 15px; font-weight: 800;"
                        "}"
                        "QPushButton:hover {"
                        f"background: {ACCENT_HOVER};"
                        "}"
                        "QPushButton:disabled {"
                        f"background: {BG_ALT}; color: {FG_MUTED}; border-color: {BORDER};"
                        "}"
                    )
                else:
                    btn = QPushButton(f"${pid}")
                    btn.setStyleSheet(
                        "QPushButton {"
                        "background: rgba(28, 28, 34, 0.72);"
                        f"border: 1px solid {BORDER}; color: {FG}; "
                        "border-radius: 8px; padding: 8px 8px; "
                        "font-size: 15px; font-weight: 700;"
                        "}"
                        "QPushButton:hover {"
                        "background: rgba(49, 55, 66, 0.86);"
                        f"border-color: rgba(122, 162, 247, 0.36);"
                        "}"
                    )
                btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                btn.setFixedHeight(52)
                self._buy_buttons[pid] = btn
                btn.clicked.connect(lambda checked, pid=pid: self._on_buy_credits(pid))
                row.addWidget(btn)
            buy_card_layout.addLayout(row)

        # Pending purchase card
        self._pending_card = QFrame()
        self._pending_card.setStyleSheet(
            "QFrame {"
            f"background: rgba(224, 175, 104, 0.10); border: 1px solid {WARN}; "
            "border-radius: 8px;"
            "}"
        )
        pending_layout = QVBoxLayout(self._pending_card)
        pending_layout.setContentsMargins(12, 10, 12, 10)
        pending_layout.setSpacing(8)

        pending_title = QLabel("Pending purchase")
        pending_title.setStyleSheet(f"color: {WARN}; font-weight: 700;")
        pending_layout.addWidget(pending_title)

        pending_desc = QLabel(
            "Complete payment in the browser, then click Check Purchase to claim "
            "credits on this device."
        )
        pending_desc.setWordWrap(True)
        pending_desc.setStyleSheet(f"color: {FG_DIM}; font-size: 12px;")
        pending_layout.addWidget(pending_desc)

        self._check_btn = QPushButton("Check Purchase")
        self._check_btn.clicked.connect(self._on_check_purchase)
        pending_layout.addWidget(self._check_btn)
        buy_card_layout.addWidget(self._pending_card)

        self._purchase_status = QLabel("")
        self._purchase_status.setWordWrap(True)
        buy_card_layout.addWidget(self._purchase_status)

        # Service margin trust note
        trust_note = QLabel(
            "Aura Credits include a small service margin that helps fund hosting, "
            "Stripe fees, relay infrastructure, and Aura development."
        )
        trust_note.setWordWrap(True)
        trust_note.setStyleSheet(f"color: {FG_MUTED}; font-size: 11px; padding-top: 2px;")
        buy_card_layout.addWidget(trust_note)

        layout.addWidget(buy_card)

    def _build_key_card(self, layout: QVBoxLayout) -> None:
        key_card, key_card_layout = self._build_card()
        key_card.setStyleSheet(
            "QFrame#card {"
            "background: rgba(20, 20, 24, 0.42);"
            f"border: 1px solid rgba(75, 83, 105, 0.50);"
            "border-radius: 10px;"
            "}"
        )

        # Subdued section title
        title = QLabel("Advanced: Use an existing Aura key")
        title.setStyleSheet(f"color: {FG_DIM}; font-size: 12px; font-weight: bold;")
        key_card_layout.addWidget(title)

        key_desc = QLabel("Already have an Aura key? Paste it here.")
        key_desc.setStyleSheet(f"color: {FG_MUTED}; font-size: 11px;")
        key_desc.setWordWrap(True)
        key_card_layout.addWidget(key_desc)

        key_row = QHBoxLayout()
        key_row.setSpacing(6)

        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("Paste Aura key here...")
        key_row.addWidget(self._key_input, 1)

        save_btn = QPushButton("Save")
        save_btn.setToolTip("Encrypt and store this key on disk")
        save_btn.setStyleSheet("padding: 4px 10px; font-size: 12px;")
        save_btn.clicked.connect(self._on_save_key)
        key_row.addWidget(save_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.setToolTip("Remove stored key")
        clear_btn.setStyleSheet("padding: 4px 10px; font-size: 12px;")
        clear_btn.clicked.connect(self._on_clear_key)
        key_row.addWidget(clear_btn)

        key_card_layout.addLayout(key_row)

        self._key_status = QLabel("")
        self._key_status.setWordWrap(True)
        key_card_layout.addWidget(self._key_status)

        layout.addWidget(key_card)

    def _refresh_balance(self) -> None:
        if self._balance_inflight:
            return

        api_key = get_api_key("aura")
        if not api_key:
            self._balance_label.setText("No Aura Credits balance yet")
            self._balance_micros = None
            self._update_balance_status_text()
            return

        self._balance_inflight = True
        self._balance_label.setText("Checking balance...")

        base_url = get_provider("aura").base_url

        thread = QThread(self)
        worker = BalanceWorker(base_url=base_url, api_key=api_key)
        worker.moveToThread(thread)
        self._balance_worker = worker
        self._balance_thread = thread

        thread.started.connect(worker.run)
        worker.finished.connect(self._on_balance_fetched)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        _thread = thread

        def _cleanup() -> None:
            if self._balance_thread is _thread:
                self._balance_thread = None
                self._balance_worker = None
            self._balance_inflight = False

        thread.finished.connect(_cleanup)
        thread.start()

    def _on_balance_fetched(self, balance_micros: int, error_msg: str) -> None:
        if error_msg:
            self._balance_label.setText("Balance unavailable")
            self._balance_micros = None
        else:
            self._balance_micros = balance_micros
            if balance_micros >= 0:
                dollars = balance_micros / 1_000_000
                self._balance_label.setText(f"Balance: ${dollars:.2f}")
            else:
                self._balance_label.setText("Balance unavailable")

        self._update_balance_status_text()
        self.credits_changed.emit()

    def _set_balance_placeholder(self) -> None:
        if get_api_key("aura"):
            self._balance_label.setText("Balance unavailable")
        else:
            self._balance_label.setText("No Aura Credits balance yet")

    def _update_balance_status_text(self) -> None:
        has_key = bool(get_api_key("aura"))
        is_aura_active = (
            self._settings.planner_provider == "aura"
            or self._settings.worker_provider == "aura"
        )

        if has_key and is_aura_active:
            self._headline_label.setText("Aura Credits are ready")
            self._balance_status.setText("Aura Credits are active.")
            self._balance_status.setStyleSheet(f"color: {SUCCESS}; font-size: 12px;")
        elif has_key and not is_aura_active:
            self._headline_label.setText("Aura Credits key saved")
            self._balance_status.setText(
                "Aura key saved. Select Aura as Planner or Worker provider to use credits."
            )
            self._balance_status.setStyleSheet(f"color: {FG_MUTED}; font-size: 12px;")
        else:
            self._headline_label.setText("Add credits to run Aura instantly")
            self._balance_status.setText(
                "Run Aura without API keys or provider setup."
            )
            self._balance_status.setStyleSheet(f"color: {FG_MUTED}; font-size: 12px;")

    def _maybe_start_balance_refresh(self) -> None:
        self._balance_timer.stop()
        if get_api_key("aura"):
            self._refresh_balance()
            self._balance_timer.start()
        elif self._balance_micros is None:
            self._set_balance_placeholder()

    def _refresh_key_status(self) -> None:
        cfg = get_provider("aura")
        if os.environ.get(cfg.env_key):
            text = f"{cfg.label} key loaded from {cfg.env_key}."
            color = SUCCESS
        elif get_api_key("aura"):
            text = f"{cfg.label} key is stored locally."
            color = SUCCESS
        else:
            text = "No Aura Credits key saved. Add credits above or paste an existing key here."
            color = FG_MUTED
        self._key_status.setText(text)
        self._key_status.setStyleSheet(f"color: {color};")

    def _on_save_key(self) -> None:
        key = self._key_input.text().strip()
        if not key:
            QMessageBox.information(self, APP_NAME, "Paste an API key before saving.")
            return
        set_api_key("aura", key)
        self._key_input.clear()
        self._refresh_key_status()
        self._update_balance_status_text()
        self._maybe_start_balance_refresh()
        self.credits_changed.emit()

    def _on_clear_key(self) -> None:
        from aura.key_manager import get_key_manager

        get_key_manager().delete_key("aura")
        self._refresh_key_status()
        self._update_balance_status_text()
        self._balance_timer.stop()
        self._balance_micros = None
        self._balance_label.setText("No Aura Credits balance yet")
        self.credits_changed.emit()

    def _sync_pending_purchase_card(self) -> None:
        has_pending = bool(
            self._settings.aura_pending_session_id
            and self._settings.aura_pending_claim_secret
        )
        self._pending_card.setVisible(has_pending)
        self._check_btn.setVisible(has_pending)
        self._check_btn.setEnabled(True)
        if has_pending and not self._purchase_status.text():
            self._purchase_status.setText(
                "A checkout is waiting to be claimed on this device."
            )
            self._purchase_status.setStyleSheet(f"color: {WARN};")

    def _set_buy_buttons_enabled(self, enabled: bool) -> None:
        for btn in self._buy_buttons.values():
            btn.setEnabled(enabled)

    def _on_buy_credits(self, pack_id: str) -> None:
        email = self._email_input.text().strip()
        if not email:
            self._purchase_status.setText("Enter your email address to buy credits.")
            self._purchase_status.setStyleSheet(f"color: {WARN};")
            return

        self._set_buy_buttons_enabled(False)
        self._purchase_status.setText("Starting checkout...")
        self._purchase_status.setStyleSheet(f"color: {FG_MUTED};")

        base_url = get_provider("aura").base_url

        thread = QThread(self)
        worker = CreditsCheckoutWorker(base_url=base_url, email=email, pack_id=pack_id)
        worker.moveToThread(thread)
        self._credit_workers.append(worker)

        thread.started.connect(worker.run)
        worker.finished.connect(
            lambda url, sid, secret, err: self._on_checkout_completed(
                url, sid, secret, err, pack_id
            )
        )
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._credit_threads.append(thread)

        _thread = thread
        _worker = worker

        def _cleanup() -> None:
            if _worker in self._credit_workers:
                self._credit_workers.remove(_worker)
            if _thread in self._credit_threads:
                self._credit_threads.remove(_thread)

        thread.finished.connect(_cleanup)
        thread.start()

    def _on_checkout_completed(
        self,
        checkout_url: str,
        session_id: str,
        claim_secret: str,
        error: str,
        pack_id: str,
    ) -> None:
        self._set_buy_buttons_enabled(True)

        if error:
            self._purchase_status.setText(f"Checkout failed: {error}")
            self._purchase_status.setStyleSheet(f"color: {WARN};")
            return

        if not checkout_url or not session_id or not claim_secret:
            self._purchase_status.setText("Checkout response was incomplete. Try again.")
            self._purchase_status.setStyleSheet(f"color: {WARN};")
            return

        self._settings.aura_pending_session_id = session_id
        self._settings.aura_pending_claim_secret = claim_secret
        save_settings(self._settings)
        self.credits_changed.emit()

        self._purchase_status.setText(
            "Opening checkout in your browser... Complete payment, then click Check Purchase."
        )
        self._purchase_status.setStyleSheet(f"color: {FG_DIM};")
        self._sync_pending_purchase_card()

        QDesktopServices.openUrl(QUrl(checkout_url))

    def _on_check_purchase(self) -> None:
        session_id = self._settings.aura_pending_session_id
        claim_secret = self._settings.aura_pending_claim_secret

        if not session_id or not claim_secret:
            self._purchase_status.setText("No pending purchase found.")
            self._purchase_status.setStyleSheet(f"color: {WARN};")
            self._sync_pending_purchase_card()
            return

        self._check_btn.setEnabled(False)
        self._set_buy_buttons_enabled(False)
        self._purchase_status.setText("Checking payment status...")
        self._purchase_status.setStyleSheet(f"color: {FG_MUTED};")

        base_url = get_provider("aura").base_url

        thread = QThread(self)
        worker = CreditsClaimWorker(
            base_url=base_url, session_id=session_id, claim_secret=claim_secret
        )
        worker.moveToThread(thread)
        self._credit_workers.append(worker)

        thread.started.connect(worker.run)
        worker.finished.connect(
            lambda aid, bal, tok, err, tok_req: self._on_claim_completed(
                aid, bal, tok, err, tok_req
            )
        )
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._credit_threads.append(thread)

        _thread = thread
        _worker = worker

        def _cleanup() -> None:
            if _worker in self._credit_workers:
                self._credit_workers.remove(_worker)
            if _thread in self._credit_threads:
                self._credit_threads.remove(_thread)

        thread.finished.connect(_cleanup)
        thread.start()

    def _on_claim_completed(
        self,
        account_id: str,
        balance_micros: int,
        token: str,
        error: str,
        token_required: bool,
    ) -> None:
        self._check_btn.setEnabled(True)
        self._set_buy_buttons_enabled(True)

        if error:
            self._purchase_status.setText(error)
            self._purchase_status.setStyleSheet(f"color: {WARN};")
            return

        if token:
            set_api_key("aura", token)
            message = "Credits claimed! Aura key has been saved. Balance will refresh."
        elif token_required:
            message = "Credits claimed! Your existing Aura key is still valid. Balance will refresh."
        else:
            message = "Credits claimed successfully!"

        self._settings.aura_pending_session_id = ""
        self._settings.aura_pending_claim_secret = ""
        save_settings(self._settings)
        self._sync_pending_purchase_card()
        self._refresh_key_status()
        self._purchase_status.setText(message)
        self._purchase_status.setStyleSheet(f"color: {SUCCESS};")
        self.credits_claimed.emit()
        self.credits_changed.emit()
        self._show_claimed_balance(balance_micros)
        self._update_balance_status_text()
        self._maybe_start_balance_refresh()

    def _show_claimed_balance(self, balance_micros: int) -> None:
        self._balance_micros = balance_micros
        if balance_micros >= 0:
            dollars = balance_micros / 1_000_000
            self._balance_label.setText(f"Balance: ${dollars:.2f}")
        else:
            self._set_balance_placeholder()

    def cleanup_threads(self) -> None:
        self._balance_timer.stop()

        if self._balance_thread is not None:
            try:
                if self._balance_thread.isRunning():
                    self._balance_thread.quit()
                    if not self._balance_thread.wait(5000):
                        logger.warning("Balance thread did not stop cleanly")
                        self._balance_thread.wait()
            except RuntimeError:
                pass
            self._balance_thread = None
            self._balance_worker = None
            self._balance_inflight = False

        for thread in list(self._credit_threads):
            try:
                if thread.isRunning():
                    thread.quit()
                    if not thread.wait(5000):
                        logger.warning("Credit thread did not stop cleanly")
                        thread.wait()
            except RuntimeError:
                pass
        self._credit_threads.clear()
        self._credit_workers.clear()
