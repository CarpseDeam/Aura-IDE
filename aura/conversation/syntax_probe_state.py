"""State adapter: converts SyntaxProbeResult into syntax_repair / syntax_validation mutations."""

from __future__ import annotations

from typing import Any

from aura.syntax_probe.models import SyntaxProbeResult
from aura.conversation.syntax_repair_state import (
    discard_syntax_validation_path,
    pop_syntax_repair_state,
    set_syntax_repair_state,
    syntax_repair_state_for_path,
)


def apply_syntax_probe_result_to_state(
    result: SyntaxProbeResult,
    syntax_repair_required: dict[str, dict[str, Any]],
    syntax_validation_required: set[str],
    stale_validation_notes: list[str] | None = None,
) -> None:
    """Mutate *syntax_repair_required* and *syntax_validation_required* in
    place based on the evidence carried by *result*.

    *stale_validation_notes* is an optional list that gets a note appended
    when a ``"pass"`` result clears a previously recorded repair state.
    """
    if result.evidence == "pass":
        prior = syntax_repair_state_for_path(syntax_repair_required, result.path)
        if prior and stale_validation_notes is not None:
            stale_validation_notes.append(
                f"Stale validation cleared: syntax probe passed for {result.path}."
            )
        pop_syntax_repair_state(syntax_repair_required, result.path)
        discard_syntax_validation_path(syntax_validation_required, result.path)

    elif result.evidence == "fail":
        prior = syntax_repair_state_for_path(syntax_repair_required, result.path)
        repair_failed = bool(
            prior.get("repair_attempted") or prior.get("awaiting_validation")
        )
        failed_repairs = int(prior.get("failed_repairs", 0)) + (
            1 if repair_failed else 0
        )
        state: dict[str, Any] = {
            "error": result.error,
            "line": result.line,
            "column": result.column,
            "language_id": result.language_id,
            "failure_class": result.failure_class or "syntax_invalid",
            "probe_evidence": "fail",
            "awaiting_validation": False,
            "repair_failed": repair_failed,
            "failed_repairs": failed_repairs,
        }
        set_syntax_repair_state(syntax_repair_required, result.path, state)
        discard_syntax_validation_path(syntax_validation_required, result.path)

    # evidence == "no_evidence": do nothing.
