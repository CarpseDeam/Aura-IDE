"""Worker tool validation observation — Phase 1 extraction from manager_tool_round.py."""
from __future__ import annotations

from typing import Any


def observe_worker_tool_validation(
    state: Any,
    loop_info: dict[str, Any] | None,
) -> None:
    """Record a validation payload into the state ledger when applicable.

    Only records when in worker mode and the terminal payload exists.
    The ledger's internal ``validation_payload_counts_as_validation``
    guard silently skips non-validation terminal results.
    """
    if _is_not_worker(state):
        return
    payload = _terminal_payload(loop_info)
    if not payload:
        return
    write_snapshot = _current_write_snapshot(state)
    state.validation_ledger.observe_tool_payload(payload, write_snapshot)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _is_not_worker(state: Any) -> bool:
    return getattr(state, "mode", None) != "worker"


def _terminal_payload(loop_info: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(loop_info, dict):
        return {}
    payload = loop_info.get("_terminal_payload")
    return payload if isinstance(payload, dict) else {}


def _current_write_snapshot(state: Any) -> int:
    return state.applied_write_count()
