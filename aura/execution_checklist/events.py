"""Event helpers for the execution checklist projector."""

from __future__ import annotations

from typing import Any

from aura.events import AuraEvent

CHECKLIST_PAYLOAD_KEYS = (
    "items",
    "objectives",
    "checklist",
    "execution_checklist",
    "todo_checklist",
)


def campaign_id_from_event(event: AuraEvent) -> str:
    """Return the best campaign/tool-call identity carried by *event*."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    return str(
        event.campaign_id
        or payload.get("campaign_id")
        or payload.get("tool_call_id")
        or event.run_id
        or payload.get("run_id")
        or ""
    )


def step_id_from_event(event: AuraEvent) -> str:
    """Return the best step identity carried by *event*."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    return str(event.step_id or payload.get("step_id") or "")


def checklist_rows_from_event(event: AuraEvent) -> list[Any]:
    """Return raw checklist rows carried by a checklist_declared event."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    for key in CHECKLIST_PAYLOAD_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return list(value)
    return []


__all__ = [
    "CHECKLIST_PAYLOAD_KEYS",
    "campaign_id_from_event",
    "checklist_rows_from_event",
    "step_id_from_event",
]
