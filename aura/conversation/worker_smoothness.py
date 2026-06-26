"""Pass-level smoothness state to detect worker thrash before it happens.

Tracks meaningful progress vs. non-progress so the caller can trigger a
recoverable phase boundary before the worker burns tool calls on repeated
failed reads, writes, or terminal commands.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from aura.conversation.path_utils import normalize_worker_path
from aura.conversation.tool_limits import WRITE_TOOLS


@dataclass
class WorkerSmoothnessDecision:
    allowed: bool = True
    phase_boundary: bool = False
    reason: str = ""
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkerSmoothnessState:
    """Per-pass counters for detecting non-progress patterns."""

    max_calls_without_progress: int = 8
    max_reads_per_path_without_write: int = 3
    max_write_attempts_per_path: int = 2
    max_same_terminal_command: int = 2

    _total_calls_since_progress: int = 0
    _reads_by_path: dict[str, int] = field(default_factory=dict)
    _write_attempts_by_path: dict[str, int] = field(default_factory=dict)
    _terminal_command_counts: dict[str, int] = field(default_factory=dict)
    _last_progress_note: str = ""


def _payload_ok(payload_text: str) -> bool:
    """Parse a JSON tool result payload and return whether ok is true."""
    try:
        data = json.loads(payload_text)
        if isinstance(data, dict):
            if "ok" in data:
                return bool(data.get("ok"))
            terminal_payload = data.get("_terminal_payload")
            if isinstance(terminal_payload, dict):
                return bool(terminal_payload.get("ok"))
        return False
    except (TypeError, json.JSONDecodeError):
        return False


def _payload_applied(payload_text: str) -> bool:
    """Check whether the payload indicates a write was applied or a file changed."""
    try:
        data = json.loads(payload_text)
        # A write was applied
        if data.get("applied") is True:
            return True
        # A file was changed (delete_file, write_file, etc.)
        if data.get("deleted") is True:
            return True
        # write_file with changed=True
        if data.get("changed") is True:
            return True
        # The payload has a list of applied writes
        applied_writes = data.get("applied_writes")
        if isinstance(applied_writes, list) and len(applied_writes) > 0:
            return True
        # The payload has a list of modified files
        modified_files = data.get("modified_files")
        if isinstance(modified_files, list) and len(modified_files) > 0:
            return True
        return False
    except (TypeError, json.JSONDecodeError):
        return False


def _tool_paths(name: str, args: dict[str, Any]) -> list[str]:
    """Return normalized paths addressed by common worker file tools."""
    if name == "read_files":
        paths = args.get("paths")
        if isinstance(paths, list):
            return [
                normalize_worker_path(str(path))
                for path in paths
                if str(path or "").strip()
            ]
        return []
    raw_path = str(args.get("path") or "")
    return [normalize_worker_path(raw_path)] if raw_path else []


def _terminal_command_key(name: str, args: dict[str, Any]) -> str:
    """Build a stable key for a terminal command based on its command text."""
    command = ""
    if isinstance(args, dict):
        command = str(args.get("command") or args.get("cmd") or "")
    return f"{name}:{command}"


def precheck_tool(
    state: WorkerSmoothnessState,
    *,
    name: str,
    args: dict[str, Any],
) -> WorkerSmoothnessDecision:
    """Check whether a tool should be allowed to execute.

    Evaluates budgets before the tool runs. Returns a decision with
    allowed=False + phase_boundary=True when a budget is exhausted.
    """
    # Overall calls-since-progress budget
    if state._total_calls_since_progress >= state.max_calls_without_progress:
        return WorkerSmoothnessDecision(
            allowed=False,
            phase_boundary=True,
            reason="max_calls_without_progress",
            message=(
                f"No meaningful progress in {state._total_calls_since_progress} "
                f"tool calls (max {state.max_calls_without_progress})."
            ),
            details={
                "total_calls_since_progress": state._total_calls_since_progress,
                "max_calls_without_progress": state.max_calls_without_progress,
                "last_progress_note": state._last_progress_note,
            },
        )

    # Path read repetition
    if name in {"read_file", "read_file_range", "read_files"}:
        for norm_path in _tool_paths(name, args):
            reads = state._reads_by_path.get(norm_path, 0)
            if reads >= state.max_reads_per_path_without_write:
                return WorkerSmoothnessDecision(
                    allowed=False,
                    phase_boundary=True,
                    reason="max_reads_per_path_without_write",
                    message=(
                        f"File '{norm_path}' has been read {reads} times "
                        f"(max {state.max_reads_per_path_without_write}) "
                        "without an intervening write."
                    ),
                    details={
                        "path": norm_path,
                        "reads": reads,
                        "max_reads_per_path_without_write": state.max_reads_per_path_without_write,
                        "last_progress_note": state._last_progress_note,
                    },
                )

    # Write attempt repetition
    if name in WRITE_TOOLS:
        for norm_path in _tool_paths(name, args):
            attempts = state._write_attempts_by_path.get(norm_path, 0)
            if attempts >= state.max_write_attempts_per_path:
                return WorkerSmoothnessDecision(
                    allowed=False,
                    phase_boundary=True,
                    reason="max_write_attempts_per_path",
                    message=(
                        f"File '{norm_path}' has had {attempts} write attempts "
                        f"(max {state.max_write_attempts_per_path}) without success."
                    ),
                    details={
                        "path": norm_path,
                        "attempts": attempts,
                        "max_write_attempts_per_path": state.max_write_attempts_per_path,
                        "last_progress_note": state._last_progress_note,
                    },
                )

    # Terminal command repetition
    if name in {"run_terminal_command", "run_and_watch"}:
        key = _terminal_command_key(name, args)
        count = state._terminal_command_counts.get(key, 0)
        if count >= state.max_same_terminal_command:
            return WorkerSmoothnessDecision(
                allowed=False,
                phase_boundary=True,
                reason="max_same_terminal_command",
                message=(
                    f"Same terminal command has been run {count} times "
                    f"(max {state.max_same_terminal_command}). "
                    "Consider a different approach."
                ),
                details={
                    "command_key": key,
                    "count": count,
                    "max_same_terminal_command": state.max_same_terminal_command,
                    "last_progress_note": state._last_progress_note,
                },
            )

    return WorkerSmoothnessDecision(allowed=True)


def observe_tool_result(
    state: WorkerSmoothnessState,
    *,
    name: str,
    args: dict[str, Any],
    ok: bool,
    payload_text: str,
) -> WorkerSmoothnessDecision:
    """Update counters after a tool has executed and return a decision.

    Call after every tool result in worker mode to update smoothness state.
    Returns a decision that may signal a phase boundary.
    """
    state._total_calls_since_progress += 1

    paths = _tool_paths(name, args)
    norm_path = paths[0] if paths else ""

    # --- Meaningful progress detection ---
    is_write = name in WRITE_TOOLS
    is_terminal = name in {"run_terminal_command", "run_and_watch"}

    progress = False

    if is_write and ok:
        applied = _payload_applied(payload_text)
        if applied:
            progress = True
            for path in paths:
                state._reads_by_path.pop(path, None)
                state._write_attempts_by_path.pop(path, None)
            state._last_progress_note = f"Write applied to '{norm_path}'"
    elif is_terminal and ok:
        payload_ok = _payload_ok(payload_text)
        if payload_ok:
            progress = True
            state._last_progress_note = f"Terminal command succeeded: {_terminal_command_key(name, args)}"
            # Progress on terminal: clear its repeat count
            key = _terminal_command_key(name, args)
            state._terminal_command_counts.pop(key, None)

    if progress:
        state._total_calls_since_progress = 0
        return WorkerSmoothnessDecision(allowed=True)

    # --- Non-progress tracking ---

    # Reads
    if name in {"read_file", "read_file_range", "read_files"}:
        for path in paths:
            current = state._reads_by_path.get(path, 0)
            state._reads_by_path[path] = current + 1

    # Write attempts (even failed ones count toward repetition)
    if is_write:
        for path in paths:
            current = state._write_attempts_by_path.get(path, 0)
            state._write_attempts_by_path[path] = current + 1

    # Terminal command repetition (only track when failing)
    if is_terminal and not ok:
        key = _terminal_command_key(name, args)
        state._terminal_command_counts[key] = state._terminal_command_counts.get(key, 0) + 1

    decision = precheck_tool(state, name=name, args=args)
    if not decision.allowed:
        return decision

    # Check reads-per-path in observe (precheck already catches, but also flag
    # here in case the increment just pushed over the limit)
    if name in {"read_file", "read_file_range", "read_files"}:
        exhausted = [
            (path, state._reads_by_path.get(path, 0))
            for path in paths
            if state._reads_by_path.get(path, 0) >= state.max_reads_per_path_without_write
        ]
        if exhausted:
            exhausted_path, reads = exhausted[0]
            if reads >= state.max_reads_per_path_without_write:
                return WorkerSmoothnessDecision(
                    allowed=False,
                    phase_boundary=True,
                    reason="max_reads_per_path_without_write",
                    message=(
                        f"File '{exhausted_path}' has been read {reads} times "
                        f"(max {state.max_reads_per_path_without_write}) "
                        "without an intervening write."
                    ),
                    details={
                        "path": exhausted_path,
                        "reads": reads,
                        "max_reads_per_path_without_write": state.max_reads_per_path_without_write,
                        "last_progress_note": state._last_progress_note,
                    },
                )

    return WorkerSmoothnessDecision(allowed=True)


def note_validation_result(
    state: WorkerSmoothnessState,
    *,
    gate: str,
    ok: bool,
    diagnostics: str = "",
) -> WorkerSmoothnessDecision:
    """Called when a validation gate result is known.

    If the gate passes (ok=True), this counts as meaningful progress and resets
    non-progress counters. A failure does not count as non-progress but also
    does not reset progress.
    """
    if ok:
        state._total_calls_since_progress = 0
        state._last_progress_note = f"Validation gate '{gate}' passed"
        return WorkerSmoothnessDecision(allowed=True)

    # Gate failed — not progress, but not non-progress either
    return WorkerSmoothnessDecision(allowed=True)


def phase_boundary_payload(decision: WorkerSmoothnessDecision) -> dict[str, Any]:
    """Produce a tool-result payload that the existing recoverable-phase-boundary
    machinery can recognise.
    """
    details = dict(decision.details or {})
    if decision.reason:
        details.setdefault("budget_reason", decision.reason)
    if decision.message:
        details.setdefault("budget_message", decision.message)
    progress_note = str(details.get("last_progress_note") or "none recorded")
    budget_reason = str(details.get("budget_reason") or "budget reached")
    message = (
        "Worker paused before thrashing.\n"
        f"Progress made: {progress_note}\n"
        f"Reason: {budget_reason}\n"
        "Next: planner should redispatch with a narrower target or explicit validation command."
    )
    return {
        "ok": False,
        "recoverable": True,
        "phase_boundary": True,
        "reason": "worker_smoothness_phase_boundary",
        "message": message,
        "details": details,
        "smoothness_phase_boundary": True,
    }


__all__ = [
    "WorkerSmoothnessDecision",
    "WorkerSmoothnessState",
    "precheck_tool",
    "observe_tool_result",
    "note_validation_result",
    "phase_boundary_payload",
]
