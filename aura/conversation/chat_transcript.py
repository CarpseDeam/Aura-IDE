"""Durable chat transcript items.

This module models what the chat should render. API messages and worker
dispatch records remain runtime/model artifacts and are not transcript source.
"""
from __future__ import annotations

import copy
from typing import Any

USER = "user"
PLANNER = "planner"
WORKER_COMPLETE = "worker_complete"


def user_item(text: str, image_b64s: list[str] | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": USER, "text": str(text)}
    if image_b64s:
        item["image_b64s"] = [str(v) for v in image_b64s if isinstance(v, str)]
    return item


def planner_item(text: str) -> dict[str, Any]:
    return {"kind": PLANNER, "text": str(text)}


def worker_complete_item(
    *,
    tool_call_id: str,
    goal: str,
    summary: str,
    status: str | None,
    ok: bool,
    needs_followup: bool,
) -> dict[str, Any]:
    return {
        "kind": WORKER_COMPLETE,
        "tool_call_id": str(tool_call_id),
        "goal": str(goal),
        "summary": str(summary),
        "status": str(status) if status is not None else None,
        "ok": bool(ok),
        "needs_followup": bool(needs_followup),
    }


def normalize_chat_item(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    kind = data.get("kind")
    if kind == USER:
        text = data.get("text", "")
        if not isinstance(text, str):
            return None
        images = data.get("image_b64s")
        if isinstance(images, list):
            return user_item(text, [str(v) for v in images if isinstance(v, str)])
        return user_item(text)
    if kind == PLANNER:
        text = data.get("text", "")
        if not isinstance(text, str):
            return None
        return planner_item(text)
    if kind == WORKER_COMPLETE:
        return worker_complete_item(
            tool_call_id=str(data.get("tool_call_id", "")),
            goal=str(data.get("goal", "Worker task")),
            summary=str(data.get("summary", "")),
            status=(
                str(data.get("status"))
                if data.get("status") is not None
                else None
            ),
            ok=bool(data.get("ok", False)),
            needs_followup=bool(data.get("needs_followup", False)),
        )
    return None


def normalize_chat_items(raw_items: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        item = normalize_chat_item(raw)
        if item is not None:
            items.append(item)
    return items


def clone_chat_items(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return normalize_chat_items(copy.deepcopy(items or []))


def legacy_chat_items_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a narrow legacy transcript from API messages.

    This is only for old conversation files that do not have ``chat_items``.
    It intentionally skips tool messages, internal messages, and worker
    dispatch records.
    """
    items: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("aura_internal"):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "tool":
            continue
        if role == "user":
            text = _content_text(content)
            if text:
                items.append(user_item(text))
        elif role == "assistant":
            text = _content_text(content)
            if text:
                items.append(planner_item(text))
    return items


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)
    return ""
