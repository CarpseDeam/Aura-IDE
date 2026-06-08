"""Companion-specific settings helpers."""
from __future__ import annotations

import socket


def default_display_name() -> str:
    """Return a default display name for this desktop (hostname)."""
    return socket.gethostname()


def default_relay_url() -> str:
    return "ws://localhost:8765"
