"""Syntax repair state helpers for worker recovery."""

from __future__ import annotations

from typing import Any

from aura.conversation.path_utils import normalize_worker_path


def syntax_repair_paths(
    syntax_repair_required: dict[str, dict[str, Any]],
) -> set[str]:
    return {
        normalize_worker_path(path)
        for path, state in syntax_repair_required.items()
        if not state.get("awaiting_validation")
    }


def syntax_repair_state_for_path(
    syntax_repair_required: dict[str, dict[str, Any]],
    path: str,
) -> dict[str, Any]:
    normalized = normalize_worker_path(path)
    state = syntax_repair_required.get(normalized)
    if state is not None:
        return state
    for existing_path, existing_state in syntax_repair_required.items():
        if normalize_worker_path(existing_path) == normalized:
            return existing_state
    return {}


def set_syntax_repair_state(
    syntax_repair_required: dict[str, dict[str, Any]],
    path: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    normalized = normalize_worker_path(path)
    for existing_path in list(syntax_repair_required):
        if normalize_worker_path(existing_path) == normalized and existing_path != normalized:
            syntax_repair_required.pop(existing_path, None)
    syntax_repair_required[normalized] = state
    return state


def pop_syntax_repair_state(
    syntax_repair_required: dict[str, dict[str, Any]],
    path: str,
) -> None:
    normalized = normalize_worker_path(path)
    syntax_repair_required.pop(normalized, None)
    for existing_path in list(syntax_repair_required):
        if normalize_worker_path(existing_path) == normalized:
            syntax_repair_required.pop(existing_path, None)


def discard_syntax_validation_path(
    syntax_validation_required: set[str],
    path: str,
) -> None:
    normalized = normalize_worker_path(path)
    syntax_validation_required.discard(normalized)
    for existing_path in set(syntax_validation_required):
        if normalize_worker_path(existing_path) == normalized:
            syntax_validation_required.discard(existing_path)


def has_terminal_syntax_failure(
    syntax_repair_required: dict[str, dict[str, Any]],
) -> bool:
    return any(state.get("repair_failed") for state in syntax_repair_required.values())
