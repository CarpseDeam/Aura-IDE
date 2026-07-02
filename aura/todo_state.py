"""Shared TODO list normalization helpers."""

from __future__ import annotations

from typing import Any

TODO_DESCRIPTION_KEYS = ("description", "content", "text", "task")
TODO_STATUS_KEYS = ("status", "state")


def todo_task_description(task: Any) -> str:
    if isinstance(task, dict):
        for key in TODO_DESCRIPTION_KEYS:
            if key in task:
                return str(task.get(key) or "")
        return ""
    return str(task)


def todo_task_status(task: Any) -> str:
    raw_status = ""
    if isinstance(task, dict):
        for key in TODO_STATUS_KEYS:
            if key in task:
                raw_status = str(task.get(key) or "").lower().strip()
                break
    if raw_status in {"done", "completed", "complete"}:
        return "done"
    if raw_status in {"active", "in_progress", "doing", "current"}:
        return "active"
    if raw_status in {"failed", "fail", "error"}:
        return "failed"
    if raw_status in {"skipped", "skip", "cancelled", "canceled"}:
        return "skipped"
    return "pending"


def normalize_todo_tasks(tasks: list[Any]) -> list[dict[str, Any]]:
    """Normalize user-provided task lists into a standard display format."""
    normalized: list[dict[str, Any]] = []
    if not isinstance(tasks, list):
        return normalized

    for task in tasks:
        if not isinstance(task, (dict, str)):
            continue
        description = todo_task_description(task)
        if len(description) > 220:
            description = description[:217] + "..."
        normalized.append(
            {
                "description": description,
                "status": todo_task_status(task),
            }
        )

    return normalized


def todo_signature(tasks: list[Any]) -> tuple[tuple[str, str], ...]:
    """Return the normalized signature used to suppress redundant UI updates."""
    return tuple(
        (task["description"], task["status"])
        for task in normalize_todo_tasks(tasks)
    )
