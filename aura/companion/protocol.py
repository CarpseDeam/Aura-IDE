"""Message protocol for Aura Companion — envelope format, DTOs, helpers."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal
from uuid import uuid4

__all__ = [
    "CompanionProject",
    "CompanionThread",
    "ActiveRunSummary",
    "ReceiptSummary",
    "make_envelope",
    "parse_command",
]


@dataclass
class CompanionProject:
    id: str
    name: str
    updated_at: str
    thread_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CompanionThread:
    id: str
    title: str
    updated_at: str
    is_current: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ActiveRunSummary:
    run_id: str
    kind: Literal["worker", "drone"]
    label: str
    status: str
    started_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReceiptSummary:
    run_id: str
    kind: Literal["drone", "worker"]
    label: str
    status: str
    completed_at: str
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def make_envelope(
    msg_type: str,
    payload: dict,
    *,
    desktop_id: str | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    in_response_to: str | None = None,
) -> dict:
    """Build a protocol envelope dict for sending to Relay -> phone."""
    return {
        "id": f"evt_{uuid4().hex[:12]}",
        "type": msg_type,
        "desktop_id": desktop_id or "",
        "project_id": project_id or "",
        "conversation_id": conversation_id or "",
        "in_response_to": in_response_to or "",
        "payload": payload,
    }


def parse_command(raw: dict) -> tuple[str, dict] | None:
    """Validate an incoming command envelope and return (type, payload).

    Returns None if the envelope is malformed.
    """
    if not isinstance(raw, dict):
        return None
    if "type" not in raw or "payload" not in raw:
        return None
    return (raw["type"], raw["payload"])
