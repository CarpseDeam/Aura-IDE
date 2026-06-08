"""Pydantic models for Relay API and WebSocket messages."""
from __future__ import annotations

from pydantic import BaseModel


class DeviceInfo(BaseModel):
    device_id: str
    display_name: str
    device_type: str = "desktop"  # "desktop" or "phone"
    last_seen: str = ""
    paired: bool = False


class Envelope(BaseModel):
    id: str
    type: str
    desktop_id: str = ""
    project_id: str = ""
    conversation_id: str = ""
    in_response_to: str = ""
    payload: dict = {}


class PairRequest(BaseModel):
    pairing_code: str
    device_name: str


class PairResponse(BaseModel):
    token: str
    desktop_id: str
    display_name: str


class RevokeRequest(BaseModel):
    device_id: str


class HealthResponse(BaseModel):
    status: str = "ok"
    online_desktops: int = 0
    online_phones: int = 0
