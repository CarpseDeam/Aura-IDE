from __future__ import annotations

import os
import logging

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
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
from aura.gui.theme import FG, FG_DIM, FG_MUTED, SUCCESS, WARN

logger = logging.getLogger(__name__)


class AuraPage(QWidget):
    credits_claimed = Signal()

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

        self._credit_threads: list[QThread] = []
        self._credit_workers: list = []

        # Balance state
        self._balance_micros: int | None = None
        self._balance_inflight = False
        self._balance_thread: QThread | None = None
        self._balance_worker: BalanceWorker | None = None
        self._balance_timer = QTimer(self)
        self._balance_timer.setInterval(60000)
        self._balance_timer.timeout.connect(self._refresh_balance)

        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # ── Card 1: Balance Section ────────────────────────────────────

        balance_card = QFrame()
        balance_card.setObjectName("card")
        balance_card_layout = QVBoxLayout(balance_card)
        balance_card_layout.setContentsMargins(14, 14, 14, 14)
        balance_card_layout.setSpacing(6)

        balance_title = QLabel("Aura Credits")
        balance_title.setStyleSheet(
            f"color: {FG_DIM}; font-weight: bold; font-size: 13px;"
        )
        balance_card_layout.addWidget(balance_title)

        balance_desc = QLabel(
            "Use Aura without bringing your own model API key."
        )
        balance_desc.setStyleSheet(f"color: {FG_MUTED}; font-size: 12px;")
        balance_desc.setWordWrap(True)
        balance_card_layout.addWidget(balance_desc)

        self._balance_label = QLabel("Checking balance\u2026")
        self._balance_label.setStyleSheet(
            f"color: {FG}; font-size: 28px; font-weight: 700;"
        )
        balance_card_layout.addWidget(self._balance_label)

        refresh_row = QHBoxLayout()
        refresh_row.setSpacing(6)
        refresh_btn = QPushButton("\u21bb Refresh")
        refresh_btn.setToolTip("Check current balance")
        refresh_btn.clicked.connect(self._refresh_balance)
        refresh_row.addWidget(refresh_btn)
        refresh_row.addStretch()
        balance_card_layout.addLayout(refresh_row)

        self._balance_status = QLabel("")
        self._balance_status.setWordWrap(True)
        self._balance_status.setStyleSheet("font-size: 11px;")
        balance_card_layout.addWidget(self._balance_status)

        layout.addWidget(balance_card)

        # ── Card 2: Buy Credits Section ────────────────────────────────

        buy_card = QFrame()
        buy_card.setObjectName("card")
        buy_card_layout = QVBoxLayout(buy_card)
        buy_card_layout.setContentsMargins(14, 14, 14, 14)
        buy_card_layout.setSpacing(6)

        buy_title = QLabel("Buy Credits")
        buy_title.setStyleSheet(
            f"color: {FG_DIM}; font-weight: bold; font-size: 13px;"
        )
        buy_card_layout.addWidget(buy_title)

        buy_desc = QLabel(
            "After checkout, click Check Purchase to claim your credits on this device."
        )
        buy_desc.setStyleSheet(f"color: {FG_MUTED}; font-size: 12px;")
        buy_desc.setWordWrap(True)
        buy_card_layout.addWidget(buy_desc)

        # Email row
        email_row = QHBoxLayout()
        email_row.setSpacing(6)
        email_label = QLabel("Email:")
        self._email_input = QLineEdit()
        self._email_input.setPlaceholderText("Your email address...")
        email_row.addWidget(email_label)
        email_row.addWidget(self._email_input, 1)
        buy_card_layout.addLayout(email_row)

        # Buy buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self._buy5 = QPushButton("Buy $5 Credits")
        self._buy10 = QPushButton("Buy $10 Credits")
        btn_row.addWidget(self._buy5)
        btn_row.addWidget(self._buy10)
        btn_row.addStretch()
        buy_card_layout.addLayout(btn_row)

        self._purchase_status = QLabel("")
        self._purchase_status.setWordWrap(True)
        buy_card_layout.addWidget(self._purchase_status)

        self._check_btn = QPushButton("Check Purchase")
        self._check_btn.setVisible(False)
        buy_card_layout.addWidget(self._check_btn)

        if self._settings.aura_pending_session_id and self._settings.aura_pending_claim_secret:
            self._check_btn.setVisible(True)
            self._purchase_status.setText(
                "You have a pending purchase. Complete payment in the browser, "
                "then click Check Purchase."
            )
            self._purchase_status.setStyleSheet(f"color: {WARN};")

        self._buy5.clicked.connect(lambda: self._on_buy_credits("5"))
        self._buy10.clicked.connect(lambda: self._on_buy_credits("10"))
        self._check_btn.clicked.connect(self._on_check_purchase)

        layout.addWidget(buy_card)

        # ── Card 3: Aura Key Section ───────────────────────────────────

        key_card = QFrame()
        key_card.setObjectName("card")
        key_card_layout = QVBoxLayout(key_card)
        key_card_layout.setContentsMargins(14, 14, 14, 14)
        key_card_layout.setSpacing(6)

        key_title = QLabel("Aura Key")
        key_title.setStyleSheet(
            f"color: {FG_DIM}; font-weight: bold; font-size: 13px;"
        )
        key_card_layout.addWidget(key_title)

        key_desc = QLabel(
            "Manually paste an Aura API key. You can also obtain one by buying credits above."
        )
        key_desc.setStyleSheet(f"color: {FG_MUTED}; font-size: 12px;")
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
        save_btn.clicked.connect(self._on_save_key)
        key_row.addWidget(save_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.setToolTip("Remove stored key")
        clear_btn.clicked.connect(self._on_clear_key)
        key_row.addWidget(clear_btn)

        key_card_layout.addLayout(key_row)

        self._key_status = QLabel("")
        self._key_status.setWordWrap(True)
        key_card_layout.addWidget(self._key_status)

        layout.addWidget(key_card)
        layout.addStretch()

        # Initial state
        self._refresh_key_status()
        self._update_balance_status_text()
        self._set_balance_placeholder()

    # ── Balance ────────────────────────────────────────────────────────

    def _refresh_balance(self) -> None:
        if self._balance_inflight:
            return

        api_key = get_api_key("aura")
        if not api_key:
            self._balance_label.setText("Balance unavailable")
            self._balance_micros = None
            self._update_balance_status_text()
            return

        self._balance_inflight = True
        self._balance_label.setText("Checking balance\u2026")

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

        def _cleanup():
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

    def _set_balance_placeholder(self) -> None:
        if get_api_key("aura"):
            self._balance_label.setText("Balance not loaded")
        else:
            self._balance_label.setText("Balance unavailable")

    def _update_balance_status_text(self) -> None:
        has_key = bool(get_api_key("aura"))
        is_aura_active = (
            self._settings.planner_provider == "aura"
            or self._settings.worker_provider == "aura"
        )

        if has_key and is_aura_active:
            self._balance_status.setText("Aura Credits are active.")
            self._balance_status.setStyleSheet(
                f"color: {SUCCESS}; font-size: 11px;"
            )
        elif has_key and not is_aura_active:
            self._balance_status.setText(
                "Aura key saved. Select Aura as Planner or Worker provider "
                "to use credits."
            )
            self._balance_status.setStyleSheet(
                f"color: {WARN}; font-size: 11px;"
            )
        else:
            self._balance_status.setText(
                "No Aura key saved yet. Buy credits or paste an Aura key below."
            )
            self._balance_status.setStyleSheet(
                f"color: {FG_MUTED}; font-size: 11px;"
            )

    def _maybe_start_balance_refresh(self) -> None:
        self._balance_timer.stop()
        if self._balance_micros is None:
            self._set_balance_placeholder()

    # ── Key helpers ────────────────────────────────────────────────────

    def _refresh_key_status(self) -> None:
        cfg = get_provider("aura")
        if os.environ.get(cfg.env_key):
            text = f"{cfg.label} key loaded from {cfg.env_key}."
            color = SUCCESS
        elif get_api_key("aura"):
            text = f"{cfg.label} key is stored locally."
            color = SUCCESS
        else:
            text = f"No {cfg.label} key found. Set {cfg.env_key} or save one here."
            color = WARN
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

    def _on_clear_key(self) -> None:
        from aura.key_manager import get_key_manager

        get_key_manager().delete_key("aura")
        self._refresh_key_status()
        self._update_balance_status_text()
        self._maybe_start_balance_refresh()
        self._balance_micros = None
        self._balance_label.setText("Balance unavailable")

    # ── Credit checkout / claim ────────────────────────────────────────

    def _on_buy_credits(self, pack_id: str) -> None:
        email = self._email_input.text().strip()
        if not email:
            self._purchase_status.setText("Enter your email address to buy credits.")
            self._purchase_status.setStyleSheet(f"color: {WARN};")
            return

        self._buy5.setEnabled(False)
        self._buy10.setEnabled(False)
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

        def _cleanup():
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
        self._buy5.setEnabled(True)
        self._buy10.setEnabled(True)

        if error:
            self._purchase_status.setText(f"Checkout failed: {error}")
            self._purchase_status.setStyleSheet(f"color: {WARN};")
            return

        if not checkout_url or not session_id or not claim_secret:
            self._purchase_status.setText(
                "Checkout response was incomplete. Try again."
            )
            self._purchase_status.setStyleSheet(f"color: {WARN};")
            return

        self._settings.aura_pending_session_id = session_id
        self._settings.aura_pending_claim_secret = claim_secret
        save_settings(self._settings)

        self._check_btn.setVisible(True)
        self._purchase_status.setText(
            "Opening checkout in your browser... Complete payment, "
            "then click Check Purchase."
        )
        self._purchase_status.setStyleSheet(f"color: {FG_DIM};")

        QDesktopServices.openUrl(QUrl(checkout_url))

    def _on_check_purchase(self) -> None:
        session_id = self._settings.aura_pending_session_id
        claim_secret = self._settings.aura_pending_claim_secret

        if not session_id or not claim_secret:
            self._purchase_status.setText("No pending purchase found.")
            self._purchase_status.setStyleSheet(f"color: {WARN};")
            self._check_btn.setVisible(False)
            return

        self._check_btn.setEnabled(False)
        self._buy5.setEnabled(False)
        self._buy10.setEnabled(False)
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

        def _cleanup():
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
        self._buy5.setEnabled(True)
        self._buy10.setEnabled(True)

        if error:
            self._purchase_status.setText(error)
            self._purchase_status.setStyleSheet(f"color: {WARN};")
            return

        if token:
            set_api_key("aura", token)
            self._settings.aura_pending_session_id = ""
            self._settings.aura_pending_claim_secret = ""
            save_settings(self._settings)
            self._check_btn.setVisible(False)
            self._refresh_key_status()
            self._purchase_status.setText(
                "Credits claimed! Aura key has been saved. Balance will refresh."
            )
            self._purchase_status.setStyleSheet(f"color: {SUCCESS};")
            self.credits_claimed.emit()
            self._show_claimed_balance(balance_micros)
            self._update_balance_status_text()
        elif token_required:
            self._settings.aura_pending_session_id = ""
            self._settings.aura_pending_claim_secret = ""
            save_settings(self._settings)
            self._check_btn.setVisible(False)
            self._refresh_key_status()
            self._purchase_status.setText(
                "Credits claimed! Your existing Aura key is still valid. Balance will refresh."
            )
            self._purchase_status.setStyleSheet(f"color: {SUCCESS};")
            self.credits_claimed.emit()
            self._show_claimed_balance(balance_micros)
            self._update_balance_status_text()
        else:
            self._settings.aura_pending_session_id = ""
            self._settings.aura_pending_claim_secret = ""
            save_settings(self._settings)
            self._check_btn.setVisible(False)
            self._purchase_status.setText("Credits claimed successfully!")
            self._purchase_status.setStyleSheet(f"color: {SUCCESS};")
            self.credits_claimed.emit()
            self._show_claimed_balance(balance_micros)
            self._update_balance_status_text()

    # ── Cleanup ────────────────────────────────────────────────────────

    def _show_claimed_balance(self, balance_micros: int) -> None:
        self._balance_micros = balance_micros
        if balance_micros >= 0:
            dollars = balance_micros / 1_000_000
            self._balance_label.setText(f"Balance: ${dollars:.2f}")
        else:
            self._set_balance_placeholder()

    def cleanup_threads(self) -> None:
        # Stop balance timer
        self._balance_timer.stop()

        # Clean up balance thread
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

        # Clean up credit threads
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

    # ── Collect ────────────────────────────────────────────────────────

    def collect_settings(self, settings: AppSettings) -> None:
        settings.aura_pending_session_id = self._settings.aura_pending_session_id
        settings.aura_pending_claim_secret = self._settings.aura_pending_claim_secret
