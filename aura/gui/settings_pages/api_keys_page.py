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
    get_provider_kind,
    is_external_cli_available,
    save_settings,
    set_api_key,
)
from aura.gui.credits_worker import CreditsCheckoutWorker, CreditsClaimWorker
from aura.providers.registry import provider_registry
from aura.gui.theme import FG_DIM, FG_MUTED, SUCCESS, WARN

logger = logging.getLogger(__name__)


class ApiKeysPage(QWidget):
    credits_claimed = Signal()

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        title = QLabel("Provider Setup")
        title.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", title)

        sub_label = QLabel(
            "API key providers are configured inside Aura. "
            "External CLI providers are configured outside Aura."
        )
        sub_label.setStyleSheet(f"color: {FG_MUTED}; font-size: 11px;")
        sub_label.setWordWrap(True)
        form.addRow("", sub_label)

        self._provider_rows: dict[str, dict[str, object]] = {}
        self._credit_threads: list[QThread] = []

        for pid in provider_registry.ids():
            spec = provider_registry.get(pid)
            kind = get_provider_kind(pid)

            if kind == "api_key":
                row = QHBoxLayout()
                row.setSpacing(6)

                key_input = QLineEdit()
                key_input.setEchoMode(QLineEdit.EchoMode.Password)
                key_input.setPlaceholderText("Paste API key here...")
                row.addWidget(key_input, 1)

                save_btn = QPushButton("Save")
                save_btn.setToolTip("Encrypt and store this key on disk")
                save_btn.clicked.connect(lambda checked=False, p=pid, inp=key_input: self._on_save_key(p, inp))
                row.addWidget(save_btn)

                clear_btn = QPushButton("Clear")
                clear_btn.setToolTip("Remove stored key for this provider")
                clear_btn.clicked.connect(lambda checked=False, p=pid: self._on_clear_key(p))
                row.addWidget(clear_btn)

                row_widget = QWidget()
                row_widget.setLayout(row)
                form.addRow(f"{spec.label}:", row_widget)

                status_label = QLabel("")
                status_label.setWordWrap(True)
                form.addRow("", status_label)

                self._provider_rows[pid] = {
                    "input": key_input,
                    "status": status_label,
                }

                self._refresh_key_status(pid)

                # Aura Credits purchase UI
                if pid == "aura":
                    sep = QLabel("Buy Aura Credits")
                    sep.setStyleSheet(f"color: {FG_DIM}; font-weight: 600; font-size: 11px; letter-spacing: 0.04em;")
                    form.addRow("", sep)

                    email_input = QLineEdit()
                    email_input.setPlaceholderText("Your email address...")
                    form.addRow("Email:", email_input)

                    btn_row = QHBoxLayout()
                    btn_row.setSpacing(6)
                    buy5 = QPushButton("Buy $5 Credits")
                    buy10 = QPushButton("Buy $10 Credits")
                    btn_row.addWidget(buy5)
                    btn_row.addWidget(buy10)
                    buy_widget = QWidget()
                    buy_widget.setLayout(btn_row)
                    form.addRow("", buy_widget)

                    purchase_status = QLabel("")
                    purchase_status.setWordWrap(True)
                    form.addRow("", purchase_status)

                    check_btn = QPushButton("Check Purchase")
                    check_btn.setVisible(False)
                    form.addRow("", check_btn)

                    self._provider_rows[pid].update({
                        "email": email_input,
                        "buy5": buy5,
                        "buy10": buy10,
                        "purchase_status": purchase_status,
                        "check_btn": check_btn,
                    })

                    if self._settings.aura_pending_session_id and self._settings.aura_pending_claim_secret:
                        check_btn.setVisible(True)
                        purchase_status.setText("You have a pending purchase. Complete payment in the browser, then click Check Purchase.")
                        purchase_status.setStyleSheet(f"color: {WARN};")

                    buy5.clicked.connect(lambda: self._on_buy_credits("5"))
                    buy10.clicked.connect(lambda: self._on_buy_credits("10"))
                    check_btn.clicked.connect(self._on_check_purchase)

            elif kind == "external_cli":
                status_row = QHBoxLayout()
                status_row.setSpacing(6)

                status_label = QLabel("")
                status_label.setWordWrap(True)
                status_row.addWidget(status_label, 1)

                refresh_btn = QPushButton("Refresh")
                refresh_btn.setToolTip("Re-check CLI availability")
                refresh_btn.clicked.connect(lambda checked=False, p=pid: self._refresh_key_status(p))
                status_row.addWidget(refresh_btn)

                row_widget = QWidget()
                row_widget.setLayout(status_row)
                form.addRow(f"{spec.label}:", row_widget)

                self._provider_rows[pid] = {
                    "status": status_label,
                }

                self._refresh_key_status(pid)

            elif kind == "local":
                status_label = QLabel("Coming soon")
                status_label.setStyleSheet(f"color: {FG_MUTED}; font-style: italic;")
                status_label.setWordWrap(True)
                form.addRow(f"{spec.label}:", status_label)

                self._provider_rows[pid] = {
                    "status": status_label,
                }

        # Tavily separator
        tavily_sep = QLabel("Web Search (Tavily)")
        tavily_sep.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", tavily_sep)

        tavily_row = QHBoxLayout()
        tavily_row.setSpacing(6)
        self._tavily_key_input = QLineEdit()
        self._tavily_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._tavily_key_input.setPlaceholderText("Paste Tavily API key here...")
        tavily_row.addWidget(self._tavily_key_input, 1)

        self._save_tavily_btn = QPushButton("Save")
        self._save_tavily_btn.clicked.connect(self._on_save_tavily_key)
        tavily_row.addWidget(self._save_tavily_btn)

        self._clear_tavily_btn = QPushButton("Clear")
        self._clear_tavily_btn.clicked.connect(self._on_clear_tavily_key)
        tavily_row.addWidget(self._clear_tavily_btn)

        tavily_widget = QWidget()
        tavily_widget.setLayout(tavily_row)
        form.addRow("Tavily Key:", tavily_widget)

        self._tavily_status = QLabel("")
        self._tavily_status.setWordWrap(True)
        form.addRow("", self._tavily_status)
        self._refresh_tavily_status()

        layout.addLayout(form)
        layout.addStretch()

    # --- Provider key helpers ---

    def _refresh_key_status(self, provider_id: str) -> None:
        row = self._provider_rows[provider_id]
        status_label: QLabel = row["status"]  # type: ignore[assignment]
        cfg = provider_registry.get(provider_id)
        kind = get_provider_kind(provider_id)

        if kind == "api_key":
            if os.environ.get(cfg.env_key):
                text = f"{cfg.label} key loaded from {cfg.env_key}."
                color = SUCCESS
            elif get_api_key(provider_id):
                text = f"{cfg.label} key is stored locally."
                color = SUCCESS
            else:
                text = f"No {cfg.label} key found. Set {cfg.env_key} or save one here."
                color = WARN
        elif kind == "external_cli":
            if is_external_cli_available(provider_id):
                text = f"{cfg.label} — ✓ Available"
                color = SUCCESS
            else:
                text = f"{cfg.label} — Install/sign in to the CLI, then refresh."
                color = WARN
        else:
            text = "Coming soon"
            color = FG_MUTED

        status_label.setText(text)
        status_label.setStyleSheet(f"color: {color};")

    def _on_save_key(self, provider_id: str, key_input: QLineEdit) -> None:
        key = key_input.text().strip()
        if not key:
            QMessageBox.information(self, APP_NAME, "Paste an API key before saving.")
            return
        set_api_key(provider_id, key)
        key_input.clear()
        self._refresh_key_status(provider_id)

    def _on_clear_key(self, provider_id: str) -> None:
        from aura.key_manager import get_key_manager
        get_key_manager().delete_key(provider_id)
        self._refresh_key_status(provider_id)

    # --- Tavily ---

    def _refresh_tavily_status(self) -> None:
        if os.environ.get("TAVILY_API_KEY"):
            text = "Tavily key loaded from TAVILY_API_KEY."
            color = SUCCESS
        elif self._settings.tavily_api_key:
            text = "Tavily key is saved in settings."
            color = SUCCESS
        else:
            text = "No Tavily key saved. Web search will be unavailable."
            color = WARN
        self._tavily_status.setText(text)
        self._tavily_status.setStyleSheet(f"color: {color};")

    def _on_save_tavily_key(self) -> None:
        key = self._tavily_key_input.text().strip()
        if not key:
            QMessageBox.information(self, APP_NAME, "Paste a Tavily key before saving.")
            return
        self._settings.tavily_api_key = key
        self._tavily_key_input.clear()
        self._refresh_tavily_status()

    def _on_clear_tavily_key(self) -> None:
        self._settings.tavily_api_key = ""
        self._refresh_tavily_status()

    # --- Credit checkout / claim ---

    def _on_buy_credits(self, pack_id: str) -> None:
        row = self._provider_rows.get("aura")
        if not row:
            return
        email_input: QLineEdit = row["email"]
        purchase_status: QLabel = row["purchase_status"]
        buy5: QPushButton = row["buy5"]
        buy10: QPushButton = row["buy10"]

        email = email_input.text().strip()
        if not email:
            purchase_status.setText("Enter your email address to buy credits.")
            purchase_status.setStyleSheet(f"color: {WARN};")
            return

        buy5.setEnabled(False)
        buy10.setEnabled(False)
        purchase_status.setText("Starting checkout...")
        purchase_status.setStyleSheet(f"color: {FG_MUTED};")

        base_url = get_provider("aura").base_url

        thread = QThread(self)
        worker = CreditsCheckoutWorker(base_url=base_url, email=email, pack_id=pack_id)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.finished.connect(lambda url, sid, secret, err: self._on_checkout_completed(url, sid, secret, err, pack_id))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._credit_threads.append(thread)
        thread.start()

    def _on_checkout_completed(self, checkout_url: str, session_id: str, claim_secret: str, error: str, pack_id: str) -> None:
        row = self._provider_rows.get("aura")
        if not row:
            return
        purchase_status: QLabel = row["purchase_status"]
        buy5: QPushButton = row["buy5"]
        buy10: QPushButton = row["buy10"]
        check_btn: QPushButton = row["check_btn"]

        buy5.setEnabled(True)
        buy10.setEnabled(True)

        if error:
            purchase_status.setText(f"Checkout failed: {error}")
            purchase_status.setStyleSheet(f"color: {WARN};")
            return

        if not checkout_url or not session_id or not claim_secret:
            purchase_status.setText("Checkout response was incomplete. Try again.")
            purchase_status.setStyleSheet(f"color: {WARN};")
            return

        self._settings.aura_pending_session_id = session_id
        self._settings.aura_pending_claim_secret = claim_secret
        save_settings(self._settings)

        check_btn.setVisible(True)
        purchase_status.setText("Opening checkout in your browser... Complete payment, then click Check Purchase.")
        purchase_status.setStyleSheet(f"color: {FG_DIM};")

        QDesktopServices.openUrl(QUrl(checkout_url))

    def _on_check_purchase(self) -> None:
        row = self._provider_rows.get("aura")
        if not row:
            return
        purchase_status: QLabel = row["purchase_status"]
        check_btn: QPushButton = row["check_btn"]
        buy5: QPushButton = row["buy5"]
        buy10: QPushButton = row["buy10"]

        session_id = self._settings.aura_pending_session_id
        claim_secret = self._settings.aura_pending_claim_secret

        if not session_id or not claim_secret:
            purchase_status.setText("No pending purchase found.")
            purchase_status.setStyleSheet(f"color: {WARN};")
            check_btn.setVisible(False)
            return

        check_btn.setEnabled(False)
        buy5.setEnabled(False)
        buy10.setEnabled(False)
        purchase_status.setText("Checking payment status...")
        purchase_status.setStyleSheet(f"color: {FG_MUTED};")

        base_url = get_provider("aura").base_url

        thread = QThread(self)
        worker = CreditsClaimWorker(base_url=base_url, session_id=session_id, claim_secret=claim_secret)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.finished.connect(lambda aid, bal, tok, err, tok_req: self._on_claim_completed(aid, bal, tok, err, tok_req))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._credit_threads.append(thread)
        thread.start()

    def _on_claim_completed(self, account_id: str, balance_micros: int, token: str, error: str, token_required: bool) -> None:
        row = self._provider_rows.get("aura")
        if not row:
            return
        purchase_status: QLabel = row["purchase_status"]
        check_btn: QPushButton = row["check_btn"]
        buy5: QPushButton = row["buy5"]
        buy10: QPushButton = row["buy10"]

        check_btn.setEnabled(True)
        buy5.setEnabled(True)
        buy10.setEnabled(True)

        if error:
            purchase_status.setText(error)
            purchase_status.setStyleSheet(f"color: {WARN};")
            return

        if token:
            # Save the aura key regardless of token_required flag.
            # Normal first claim returns token with token_required=false.
            set_api_key("aura", token)
            self._settings.aura_pending_session_id = ""
            self._settings.aura_pending_claim_secret = ""
            save_settings(self._settings)
            check_btn.setVisible(False)
            self._refresh_key_status("aura")
            purchase_status.setText("Credits claimed! Aura key has been saved. Balance will refresh.")
            purchase_status.setStyleSheet(f"color: {SUCCESS};")
            self.credits_claimed.emit()
        elif token_required:
            # Backend says this account needs a token but cannot return the old raw key.
            purchase_status.setText("This account already has an Aura key. Use the saved key above.")
            purchase_status.setStyleSheet(f"color: {WARN};")
        else:
            # Successful claim but no token delivered (rare edge case).
            self._settings.aura_pending_session_id = ""
            self._settings.aura_pending_claim_secret = ""
            save_settings(self._settings)
            check_btn.setVisible(False)
            purchase_status.setText("Credits claimed successfully!")
            purchase_status.setStyleSheet(f"color: {SUCCESS};")
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

    # --- Collect ---

    def collect_settings(self, settings: AppSettings) -> None:
        settings.tavily_api_key = self._settings.tavily_api_key
        settings.aura_pending_session_id = self._settings.aura_pending_session_id
        settings.aura_pending_claim_secret = self._settings.aura_pending_claim_secret
