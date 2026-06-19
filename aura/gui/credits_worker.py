from __future__ import annotations

import logging

import httpx
from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


class CreditsCheckoutWorker(QObject):
    finished = Signal(str, str, str, str)  # checkout_url, session_id, claim_secret, error_msg

    def __init__(self, base_url: str, email: str, pack_id: str):
        super().__init__()
        self._base_url = base_url
        self._email = email
        self._pack_id = pack_id

    def run(self):
        try:
            url = self._base_url.rstrip("/") + "/credits/checkout"
            resp = httpx.post(
                url,
                json={"email": self._email, "pack_id": self._pack_id},
                timeout=15.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                checkout_url = data.get("checkout_url", "")
                session_id = data.get("session_id", "")
                claim_secret = data.get("claim_secret", "")
                self.finished.emit(checkout_url, session_id, claim_secret, "")
            else:
                self.finished.emit("", "", "", f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            logger.warning("Credits checkout failed: %s", exc)
            self.finished.emit("", "", "", str(exc))


class CreditsClaimWorker(QObject):
    finished = Signal(str, int, str, str, bool)  # account_id, balance_micros, token, error_msg, token_required

    def __init__(self, base_url: str, session_id: str, claim_secret: str):
        super().__init__()
        self._base_url = base_url
        self._session_id = session_id
        self._claim_secret = claim_secret

    def run(self):
        try:
            url = self._base_url.rstrip("/") + "/credits/claim"
            resp = httpx.post(
                url,
                json={"session_id": self._session_id, "claim_secret": self._claim_secret},
                timeout=15.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                account_id = str(data.get("account_id", ""))
                balance_micros = int(data.get("balance_micros", 0))
                token = str(data.get("token", ""))
                token_required = bool(data.get("token_required", False))
                self.finished.emit(account_id, balance_micros, token, "", token_required)
            elif resp.status_code == 409:
                self.finished.emit("", 0, "", "Payment is not complete yet.", False)
            else:
                self.finished.emit("", 0, "", f"HTTP {resp.status_code}: {resp.text[:200]}", False)
        except Exception as exc:
            logger.warning("Credits claim failed: %s", exc)
            self.finished.emit("", 0, "", str(exc), False)
