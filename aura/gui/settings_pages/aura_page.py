from __future__ import annotations

import os
import logging

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFormLayout,
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
from aura.gui.credits_worker import CreditsCheckoutWorker, CreditsClaimWorker
from aura.gui.theme import FG_DIM, FG_MUTED, SUCCESS, WARN

logger = logging.getLogger(__name__)


class AuraPage(QWidget):
    credits_claimed = Signal()

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

        self._credit_threads: list[QThread] = []
        self._credit_workers: list = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        # --- Aura API Key row ---

        title = QLabel("Aura Key")
        title.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", title)

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

        key_widget = QWidget()
        key_widget.setLayout(key_row)
        form.addRow("Aura:", key_widget)

        self._key_status = QLabel("")
        self._key_status.setWordWrap(True)
        form.addRow("", self._key_status)

        # --- Aura Credits purchase UI ---

        sep = QLabel("Buy Aura Credits")
        sep.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " letter-spacing: 0.04em;"
        )
        form.addRow("", sep)

        desc = QLabel(
            "Aura Credits let you use Aura without bringing your own API key."
        )
        desc.setStyleSheet(f"color: {FG_MUTED}; font-size: 11px;")
        desc.setWordWrap(True)
        form.addRow("", desc)

        desc2 = QLabel(
            "After checkout, click Check Purchase to claim your credits on this device."
        )
        desc2.setStyleSheet(f"color: {FG_MUTED}; font-size: 11px;")
        desc2.setWordWrap(True)
        form.addRow("", desc2)

        self._email_input = QLineEdit()
        self._email_input.setPlaceholderText("Your email address...")
        form.addRow("Email:", self._email_input)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self._buy5 = QPushButton("Buy $5 Credits")
        self._buy10 = QPushButton("Buy $10 Credits")
        btn_row.addWidget(self._buy5)
        btn_row.addWidget(self._buy10)
        buy_widget = QWidget()
        buy_widget.setLayout(btn_row)
        form.addRow("", buy_widget)

        self._purchase_status = QLabel("")
        self._purchase_status.setWordWrap(True)
        form.addRow("", self._purchase_status)

        self._check_btn = QPushButton("Check Purchase")
        self._check_btn.setVisible(False)
        form.addRow("", self._check_btn)

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

        self._refresh_key_status()

        layout.addLayout(form)
        layout.addStretch()

    # --- Key helpers ---

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

    def _on_clear_key(self) -> None:
        from aura.key_manager import get_key_manager
        get_key_manager().delete_key("aura")
        self._refresh_key_status()

    # --- Credit checkout / claim ---

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
        worker.finished.connect(lambda url, sid, secret, err: self._on_checkout_completed(url, sid, secret, err, pack_id))
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

    def _on_checkout_completed(self, checkout_url: str, session_id: str, claim_secret: str, error: str, pack_id: str) -> None:
        self._buy5.setEnabled(True)
        self._buy10.setEnabled(True)

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

        self._check_btn.setVisible(True)
        self._purchase_status.setText(
            "Opening checkout in your browser... Complete payment, then click Check Purchase."
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
        worker = CreditsClaimWorker(base_url=base_url, session_id=session_id, claim_secret=claim_secret)
        worker.moveToThread(thread)
        self._credit_workers.append(worker)

        thread.started.connect(worker.run)
        worker.finished.connect(lambda aid, bal, tok, err, tok_req: self._on_claim_completed(aid, bal, tok, err, tok_req))
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

    def _on_claim_completed(self, account_id: str, balance_micros: int, token: str, error: str, token_required: bool) -> None:
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
            self._purchase_status.setText("Credits claimed! Aura key has been saved. Balance will refresh.")
            self._purchase_status.setStyleSheet(f"color: {SUCCESS};")
            self.credits_claimed.emit()
        elif token_required:
            self._purchase_status.setText(
                "This account already has an Aura key. Use the saved key above."
            )
            self._purchase_status.setStyleSheet(f"color: {WARN};")
        else:
            self._settings.aura_pending_session_id = ""
            self._settings.aura_pending_claim_secret = ""
            save_settings(self._settings)
            self._check_btn.setVisible(False)
            self._purchase_status.setText("Credits claimed successfully!")
            self._purchase_status.setStyleSheet(f"color: {SUCCESS};")
            self.credits_claimed.emit()

    def cleanup_threads(self) -> None:
        for thread in list(self._credit_threads):
            try:
                if thread.isRunning():
                    thread.quit()
                    if not thread.wait(5000):
                        logger.warning("Credit thread did not stop cleanly")
            except RuntimeError:
                pass
        self._credit_threads.clear()
        self._credit_workers.clear()

    # --- Collect ---

    def collect_settings(self, settings: AppSettings) -> None:
        settings.aura_pending_session_id = self._settings.aura_pending_session_id
        settings.aura_pending_claim_secret = self._settings.aura_pending_claim_secret
