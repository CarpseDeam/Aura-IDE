"""Authentication and device identity for Companion."""
from __future__ import annotations

import json
import logging
import socket

from aura.paths import data_dir

logger = logging.getLogger(__name__)

_DEVICE_ID_FILE = "companion_device.json"


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
    import uuid

    device_id = f"desktop_{uuid.uuid4().hex[:12]}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"device_id": device_id, "hostname": socket.gethostname()}))
    return device_id


def get_device_display_name() -> str:
    """Return a human-readable display name for this desktop."""
    return socket.gethostname()


import random
import string
import threading
import time

# In-memory pairing code store (thread-safe)
_pairing_codes: dict[str, dict] = {}
_pairing_lock = threading.Lock()
CODE_TTL = 300  # 5 minutes


def generate_pairing_code() -> str:
    """Generate a 6-character alphanumeric pairing code (valid 5 min)."""
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    with _pairing_lock:
        _pairing_codes[code] = {
            "expires_at": time.time() + CODE_TTL,
            "created_at": time.time(),
        }
    return code


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
