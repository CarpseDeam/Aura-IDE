"""State adapter: converts SyntaxProbeResult into syntax_repair / syntax_validation mutations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.conversation.path_utils import (
    is_validation_scratch_path as _is_validation_scratch_path,
)
from aura.conversation.syntax_repair_state import (
    discard_syntax_validation_path,
    pop_syntax_repair_state,
    set_syntax_repair_state,
    syntax_repair_state_for_path,
)
from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.registry import get_probe


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


def run_post_write_probe(
    workspace_root: Path | None,
    path: str,
    syntax_repair_required: dict[str, dict[str, Any]],
    syntax_validation_required: set[str],
) -> None:
    """Run the registered syntax probe for *path* and update state in place.

    Skips validation scratch paths and paths without a registered probe.
    """
    if workspace_root is None:
        return
    if _is_validation_scratch_path(path):
        return

    probe_cls = get_probe(path)
    if probe_cls is None:
        return

    probe = probe_cls()
    result = probe.check(workspace_root, path)
    # Reuse the original *path* as the state key so that relative-path
    # keys in the repair/validation dicts match the probe result.
    adjusted = SyntaxProbeResult(
        path=path,
        language_id=result.language_id,
        evidence=result.evidence,
        error=result.error,
        line=result.line,
        column=result.column,
        failure_class=result.failure_class,
    )
    apply_syntax_probe_result_to_state(
        adjusted, syntax_repair_required, syntax_validation_required
    )
