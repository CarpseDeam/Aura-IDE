"""Pure helper functions for Worker validation selector, factored out of dispatch.py.

This module provides the core functions used by the Worker final gate to select
and combine validation commands.  The primary entry point is
refresh_validation_selector_plan(), which is called after each Worker tool
result to determine whether a new validation plan is needed based on changed
files.

The selector works by comparing the current set of changed files against a
cached key.  When files have changed, a new ValidationPlan is built via
select_validation_plan from aura.validation.selector.  The resulting commands
are merged with any Planner-provided commands via combine_validation_commands.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aura.conversation.path_utils import is_validation_scratch_path, normalize_worker_path
from aura.validation.selector import ValidationPlan, select_validation_plan

_log = logging.getLogger(__name__)


def combine_validation_commands(
    planner_commands: list[Any] | tuple[Any, ...] | None,
    selector_commands: list[Any] | tuple[Any, ...] | None,
) -> list[Any]:
    """Combine validation commands without stringifying structured specs.

    Planner commands are commonly ``ValidationCommandSpec`` instances.  They
    must stay structured so their command/cwd/expected outcome survive into
    the final gate.  Selector commands are plain strings and remain so.
    """
    combined: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for entry in [*(planner_commands or []), *(selector_commands or [])]:
        if isinstance(entry, str):
            command = entry.strip()
            cwd = ""
            kept = command
        else:
            command = str(getattr(entry, "command", "") or "").strip()
            cwd = str(getattr(entry, "cwd", "") or "").strip()
            kept = entry
        key = (" ".join(command.split()), cwd.replace("\\", "/").rstrip("/"))
        if not command or key in seen:
            continue
        combined.append(kept)
        seen.add(key)
    return combined


def validation_selector_commands(plan: ValidationPlan | None) -> list[str]:
    if not isinstance(plan, dict):
        return []
    commands = plan.get("commands")
    if not isinstance(commands, list):
        return []
    return [str(command).strip() for command in commands if str(command).strip()]


def validation_selector_changed_files(relay: Any) -> list[str]:
    """Return applied Worker write paths for focused selector validation."""
    write_results = getattr(relay, "write_results", [])
    raw_files: list[str] = []
    if isinstance(write_results, list) and write_results:
        for write in write_results:
            if not isinstance(write, dict):
                continue
            path = write.get("path")
            if (
                write.get("applied") is True
                and not write.get("deleted")
                and isinstance(path, str)
                and path
            ):
                raw_files.append(path)
    else:
        touched = getattr(relay, "touched_files", set())
        if isinstance(touched, set):
            raw_files = sorted(str(path) for path in touched)
        elif isinstance(touched, list):
            raw_files = [str(path) for path in touched]

    files: list[str] = []
    seen: set[str] = set()
    for raw in raw_files:
        path = normalize_worker_path(str(raw or ""))
        if not path or is_validation_scratch_path(path) or path in seen:
            continue
        files.append(path)
        seen.add(path)
    return files


def build_worker_validation_selector_plan(
    *,
    changed_files: list[str],
    task_kind: str,
    context_gearbox: dict[str, Any],
    workspace_root: Path | None,
) -> ValidationPlan:
    """Build the data-only selector plan used by the Worker final gate."""
    if not changed_files:
        return select_validation_plan(
            target_files=[],
            changed_files=None,
            task_kind=task_kind,
            context_gearbox=None,
            workspace_root=workspace_root,
        )
    return select_validation_plan(
        target_files=changed_files,
        changed_files=changed_files,
        task_kind=task_kind,
        context_gearbox=context_gearbox,
        workspace_root=workspace_root,
    )


# ── Main refresh entry point ──


def refresh_validation_selector_plan(
    *,
    relay: Any,
    task_spec_validation_commands: list[str],
    task_kind: str,
    context_gearbox: dict[str, Any],
    workspace_root: Path | None,
    final_validation_commands: list[Any],
    validation_selector: ValidationPlan | None,
    validation_selector_key: tuple[str, ...] | None,
    validation_selector_failed: bool,
) -> tuple[ValidationPlan | None, tuple[str, ...] | None, bool]:
    """Check whether changed files warrant a fresh validation plan.

    Called after each Worker tool result.  Compares the current changed-file
    set against *validation_selector_key*; if unchanged and a plan already
    exists, returns the cached plan.  Otherwise builds a new plan via
    build_worker_validation_selector_plan and merges its commands with the
    Planner-provided commands.
    """
    changed_files = validation_selector_changed_files(relay)
    key = tuple(changed_files)
    if validation_selector is not None and key == validation_selector_key:
        return validation_selector, validation_selector_key, validation_selector_failed
    validation_selector_key = key
    try:
        validation_selector = build_worker_validation_selector_plan(
            changed_files=changed_files,
            task_kind=task_kind,
            context_gearbox=context_gearbox,
            workspace_root=workspace_root,
        )
        final_validation_commands[:] = combine_validation_commands(
            task_spec_validation_commands,
            validation_selector_commands(validation_selector),
        )
    except Exception:
        if not validation_selector_failed:
            _log.exception("Failed to build validation selector plan")
        validation_selector_failed = True
        final_validation_commands[:] = list(task_spec_validation_commands)
    return validation_selector, validation_selector_key, validation_selector_failed
