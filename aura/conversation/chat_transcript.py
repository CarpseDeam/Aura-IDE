"""Durable chat transcript items.

This module models what the chat should render. API messages and execution
tool-call records remain runtime/model artifacts and are not transcript source.
"""
from __future__ import annotations

import copy
from typing import Any

USER = "user"
ASSISTANT = "assistant"
EXECUTION_COMPLETE = "execution_complete"
ERROR = "error"
PLAN_REVIEW = "plan_review"

#: Legacy chat error title, retired: production runs no longer synthesize a
#: generic "Needs follow-up" card. Old persisted conversations may still
#: contain an ERROR item with exactly this title; it is dropped on load (and
#: therefore on the next save) so it stops rendering, without touching any
#: other historical error.
_LEGACY_NEEDS_FOLLOWUP_ERROR_TITLE = "Needs follow-up"


def user_item(text: str, image_b64s: list[str] | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": USER, "text": str(text)}
    if image_b64s:
        item["image_b64s"] = [str(v) for v in image_b64s if isinstance(v, str)]
    return item


def assistant_item(text: str) -> dict[str, Any]:
    return {"kind": ASSISTANT, "text": str(text)}


def error_item(title: str, message: str, show_retry: bool = False) -> dict[str, Any]:
    """A failure the user saw in chat (API error, harness error, cancellation).

    Errors are part of the durable conversation: what went wrong must still be
    visible after a restart, not only in the ephemeral workspace.
    """
    return {
        "kind": ERROR,
        "title": str(title),
        "message": str(message),
        "show_retry": bool(show_retry),
    }


def execution_complete_item(
    *,
    tool_call_id: str,
    goal: str,
    summary: str,
    status: str | None,
    ok: bool,
    needs_followup: bool,
) -> dict[str, Any]:
    return {
        "kind": EXECUTION_COMPLETE,
        "tool_call_id": str(tool_call_id),
        "goal": str(goal),
        "summary": str(summary),
        "status": str(status) if status is not None else None,
        "ok": bool(ok),
        "needs_followup": bool(needs_followup),
    }


def plan_review_item(
    *,
    goal: str,
    files: list[str],
    spec: str,
    acceptance: str,
    summary: str,
    approved: bool,
    user_edited: bool = False,
) -> dict[str, Any]:
    """A resolved Plan Review decision — durable conversation state.

    Only ever recorded *resolved*: there is no persisted "pending" plan
    review, so replay never recreates a pending review or executes anything.
    """
    return {
        "kind": PLAN_REVIEW,
        "goal": str(goal),
        "files": [str(f) for f in files],
        "spec": str(spec),
        "acceptance": str(acceptance),
        "summary": str(summary),
        "approved": bool(approved),
        "user_edited": bool(user_edited),
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
    if kind == ASSISTANT:
        text = data.get("text", "")
        if not isinstance(text, str):
            return None
        return assistant_item(text)
    if kind == ERROR:
        title = data.get("title", "")
        message = data.get("message", "")
        if not isinstance(title, str) or not isinstance(message, str):
            return None
        if title == _LEGACY_NEEDS_FOLLOWUP_ERROR_TITLE:
            return None
        return error_item(title, message, bool(data.get("show_retry", False)))
    if kind == EXECUTION_COMPLETE:
        return execution_complete_item(
            tool_call_id=str(data.get("tool_call_id", "")),
            goal=str(data.get("goal", "Execution task")),
            summary=str(data.get("summary", "")),
            status=(
                str(data.get("status"))
                if data.get("status") is not None
                else None
            ),
            ok=bool(data.get("ok", False)),
            needs_followup=bool(data.get("needs_followup", False)),
        )
    if kind == PLAN_REVIEW:
        raw_files = data.get("files")
        files = [str(f) for f in raw_files] if isinstance(raw_files, list) else []
        return plan_review_item(
            goal=str(data.get("goal", "")),
            files=files,
            spec=str(data.get("spec", "")),
            acceptance=str(data.get("acceptance", "")),
            summary=str(data.get("summary", "")),
            approved=bool(data.get("approved", False)),
            user_edited=bool(data.get("user_edited", False)),
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
    It intentionally skips tool messages, internal messages, and execution
    tool-call records.
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
                items.append(assistant_item(text))
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
