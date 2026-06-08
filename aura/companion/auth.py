"""Authentication and device identity for Companion."""
from __future__ import annotations

import logging
import socket

logger = logging.getLogger(__name__)


def get_device_id() -> str:
    """Return a stable device identifier for this desktop instance.

    Phase 0: uses hostname. Phase 4 will use a persisted UUID.
    """
    return socket.gethostname()


def generate_pairing_code() -> str:
    """Generate a 6-character pairing code. Stub for Phase 0.

    Phase 4 will implement the full time-limited code flow.
    """
    logger.warning("[Companion] generate_pairing_code stub — Phase 4 feature")
    return "STUB42"


def validate_pairing_code(code: str) -> bool:
    """Validate a pairing code. Stub for Phase 0."""
    return code == "STUB42"
