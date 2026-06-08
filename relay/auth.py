"""Authentication and token management for Relay."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import jwt

logger = logging.getLogger(__name__)

SECRET = os.environ.get("AURA_RELAY_SECRET", "dev-secret-change-in-prod")
TOKEN_TTL_DAYS = 30


def create_device_token(desktop_id: str, device_name: str, role: str = "phone") -> str:
    """Create a signed JWT for a paired phone device."""
    payload = {
        "desktop_id": desktop_id,
        "device_name": device_name,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def verify_token(token: str) -> dict | None:
    """Verify a JWT and return the payload, or None if invalid."""
    try:
        return jwt.decode(token, SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
