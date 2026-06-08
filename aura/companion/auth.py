"""Authentication and device identity for Companion.

This module is self-contained — it does not depend on the relay package, so
the desktop build remains shippable without the relay code bundled.
"""
from __future__ import annotations

import json
import logging
import os
import random
import secrets
import socket
import string
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt

from aura.paths import data_dir

logger = logging.getLogger(__name__)

_DEVICE_ID_FILE = "companion_device.json"

# Shared secret with the relay. Must match relay.auth.SECRET.
SECRET = os.environ.get("AURA_RELAY_SECRET", "dev-secret-change-in-prod")
TOKEN_TTL_DAYS = 30


def create_device_token(desktop_id: str, device_name: str, role: str = "phone") -> str:
    """Create a signed JWT for a paired phone device.

    Mirrors relay.auth.create_device_token so the desktop does not depend on
    the relay package at runtime.
    """
    payload = {
        "desktop_id": desktop_id,
        "device_name": device_name,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def get_device_id() -> str:
    """Return a stable device identifier for this desktop instance.

    Persisted as a UUID in the Aura data directory.
    """
    path = data_dir() / _DEVICE_ID_FILE
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if "device_id" in data:
                return data["device_id"]
        except Exception as exc:
            logger.warning("Failed to read companion device ID, regenerating: %s", exc)
    # Generate new persistent ID
    device_id = f"desktop_{uuid.uuid4().hex[:12]}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"device_id": device_id, "hostname": socket.gethostname()}))
    return device_id


def get_device_display_name() -> str:
    """Return a human-readable display name for this desktop."""
    return socket.gethostname()


# In-memory pairing code store (thread-safe)
_pairing_codes: dict[str, dict] = {}
_pairing_lock = threading.Lock()
CODE_TTL = 300  # 5 minutes


def generate_pairing_code() -> str:
    """Generate a 6-character alphanumeric pairing code (valid 5 min).

    Avoids visually ambiguous characters (0/O, 1/I) so users can type the
    fallback code from the QR card without squinting.
    """
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(random.choices(alphabet, k=6))
    with _pairing_lock:
        _pairing_codes[code] = {
            "expires_at": time.time() + CODE_TTL,
            "created_at": time.time(),
        }
    return code


def pairing_code_expiry(code: str) -> float | None:
    """Return the expiry timestamp for a pairing code, or None if unknown."""
    with _pairing_lock:
        entry = _pairing_codes.get(code)
        return float(entry["expires_at"]) if entry else None


def validate_pairing_code(code: str) -> bool:
    """Validate a pairing code (consumes it on success — single use)."""
    with _pairing_lock:
        entry = _pairing_codes.get(code)
        if not entry:
            return False
        if time.time() > entry["expires_at"]:
            _pairing_codes.pop(code, None)
            return False
        _pairing_codes.pop(code, None)  # Single-use
        return True


def invalidate_pairing_code(code: str) -> None:
    """Invalidate a pairing code before it expires."""
    with _pairing_lock:
        _pairing_codes.pop(code, None)


def clear_expired_codes() -> None:
    """Remove expired codes from the store."""
    now = time.time()
    with _pairing_lock:
        for code, entry in list(_pairing_codes.items()):
            if now > entry["expires_at"]:
                _pairing_codes.pop(code, None)


# In-memory ticket store (thread-safe)
TICKET_BYTES = 24  # 192 bits of entropy
TICKET_TTL = 300   # 5 minutes

_tickets: dict[str, dict] = {}
_tickets_lock = threading.Lock()


def generate_ticket(desktop_id: str, pairing_code: str, *,
                    desktop_name: str = "",
                    project_id: str = "",
                    conversation_id: str = "") -> str:
    """Generate an opaque short-lived ticket bound to the given context."""
    ticket = secrets.token_urlsafe(TICKET_BYTES)
    with _tickets_lock:
        _tickets[ticket] = {
            "desktop_id": desktop_id,
            "code": pairing_code,
            "desktop_name": desktop_name,
            "project_id": project_id,
            "conversation_id": conversation_id,
            "created_at": time.time(),
            "expires_at": time.time() + TICKET_TTL,
        }
    return ticket


def pop_ticket(ticket: str) -> dict | None:
    """Return and remove ticket data if valid, or None if expired/invalid."""
    with _tickets_lock:
        data = _tickets.pop(ticket, None)
        if data is None:
            return None
    if time.time() > data.get("expires_at", 0):
        return None
    return data


def cleanup_expired_tickets() -> None:
    """Remove expired tickets."""
    now = time.time()
    with _tickets_lock:
        expired = [k for k, v in _tickets.items() if now > v.get("expires_at", 0)]
        for k in expired:
            _tickets.pop(k, None)
