"""Protocol validation helpers for the Relay."""
from __future__ import annotations

import logging

from relay.models import Envelope

logger = logging.getLogger(__name__)


def validate_envelope(msg: dict) -> bool:
    """Check that a message dict has the required envelope fields."""
    if not isinstance(msg, dict):
        return False
    required = ("id", "type", "desktop_id", "payload")
    return all(k in msg for k in required)


def parse_envelope(msg: dict) -> Envelope | None:
    """Parse a dict into an Envelope model. Returns None if invalid."""
    try:
        return Envelope(**msg)
    except Exception as exc:
        logger.debug("Failed to parse envelope: %s", exc)
        return None
