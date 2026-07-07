"""Policy for routing explicit-validation failures to repair, fix_command, or handback.

This module owns all decide-what-happens-on-failure policy.  The gate calls
``route_validation_failure`` and acts on the returned verdict -- it decides
nothing itself.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from aura.conversation.worker_final_validation import WorkerFinalValidationResult


@dataclass(frozen=True)
class ValidationFailureVerdict:
    """What to do about a failed explicit-validation result."""

    action: Literal["repair", "fix_command", "handback"]
    instruction: str = ""
    handback_details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Module-level instruction constants
# ---------------------------------------------------------------------------

VALIDATION_INFRA_FAILURE_INSTRUCTION = (
    "Final acceptance validation failed due to an environment or configuration "
    "issue. Do not edit product code in response. "
    "Rerun the exact validation command with the corrected working directory and "
    "the project venv Python interpreter. "
    "If the command itself is misdeclared for the current project layout, report "
    "it so the acceptance definition can be revised.\n\n"
    "Command: {command}\n\n"
    "Diagnostic output:\n{diagnostics}"
)

VALIDATION_REPAIR_INSTRUCTION = (
    "Final acceptance validation failed. Do not infer the expected value from "
    "prose first. "
    "Run a minimal diagnostic that prints the actual value(s), then patch only "
    "the failing code. "
    "After the patch, rerun the exact validation command. Finish only after it "
    "passes.\n\n"
    "Command: {command}\n\n"
    "Diagnostic output:\n{diagnostics}\n\n"
    "When the failure is a linter complaint about an unused import or name, "
    "first search the repository for downstream references to that name. "
    "If any other file imports it from this module, the name is a compatibility "
    "re-export and must be preserved with a # noqa: F401 comment instead of "
    "deleted. Names are never removed solely to satisfy lint."
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Matches absolute path prefixes on both Windows and POSIX.
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:[a-zA-Z]:[/\\]|/)"  # drive letter (Windows) or root (POSIX)
    r"(?:[^: \t\n\"'()<>|{}\[\]\x00-\x1f]+)"  # path characters
)


# Matches common timestamp / datetime formats.
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?\b"
)


def _normalize_diagnostics(text: str) -> str:
    """Collapse whitespace and strip absolute paths and timestamps."""
    cleaned = _ABSOLUTE_PATH_RE.sub("<path>", text)
    cleaned = _TIMESTAMP_RE.sub("<timestamp>", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _compute_digest(text: str) -> str:
    """SHA-256 hex digest of *text*, truncated to 16 hex characters.

    Uses ``hashlib.sha256`` (not the built-in ``hash()``, which is randomized
    per process) so the fingerprint is stable across process boundaries.
    """
    normalized = _normalize_diagnostics(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Diagnostics preview helper
# ---------------------------------------------------------------------------


def validation_diagnostics_preview(text: str, limit: int = 2000) -> dict[str, object]:
    """Build bounded diagnostics preview fields for handback details.

    The preview keeps failure output actionable without flooding visible
    Planner logs with multi-thousand-line escaped payloads.

    Parameters
    ----------
    text
        The raw diagnostics string to preview.
    limit
        Maximum number of characters to include in the preview.

    Returns
    -------
    dict
        With keys ``diagnostics_preview``, ``diagnostics_truncated``,
        and ``diagnostics_char_count``.
    """
    raw = text or ""
    return {
        "diagnostics_preview": raw[:limit],
        "diagnostics_truncated": len(raw) > limit,
        "diagnostics_char_count": len(raw),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def route_validation_failure(
    val_result: WorkerFinalValidationResult,
    fingerprint_memory: dict[str, str],
    edits_since_last_pass: bool,
) -> ValidationFailureVerdict:
    """Decide how to respond to a failed explicit-validation result.

    Parameters
    ----------
    val_result
        The completed validation result (``ok`` is *False*).
    fingerprint_memory
        State dictionary mapping command-key to last-seen fingerprint string.
        *This function mutates the dict in-place* when progress is detected;
        the caller must supply the live state dict.
    edits_since_last_pass
        Whether the Worker made at least one write attempt since the
        previous explicit-validation pass.

    Returns
    -------
    ValidationFailureVerdict
        With ``action`` set to ``"repair"``, ``"fix_command"``, or
        ``"handback"`` and the corresponding ``instruction`` or
        ``handback_details`` filled in.
    """
    # --- Build the command key -------------------------------------------
    failing_runs = [r for r in (val_result.runs or []) if not r.ok]

    command_key = (
        val_result.command
        or "|".join(r.command for r in failing_runs if r.command)
        or "<unknown>"
    )

    # --- Build the failure fingerprint -----------------------------------
    classifications = (
        ",".join(r.classification for r in failing_runs)
        if failing_runs
        else val_result.diagnostics
    )

    diag_parts = [r.output for r in failing_runs if r.output.strip()]
    if not diag_parts and val_result.diagnostics.strip():
        diag_parts.append(val_result.diagnostics)
    digest = _compute_digest("\n".join(diag_parts))

    fingerprint = f"{command_key}|{classifications}|{digest}"

    # --- Stall detection -------------------------------------------------
    stored = fingerprint_memory.get(command_key)
    progress = (
        stored is None
        or fingerprint != stored
        or edits_since_last_pass
    )

    if progress:
        # Worker is making progress -- keep going indefinitely.
        # This module contains no attempt counters or retry budgets.
        fingerprint_memory[command_key] = fingerprint

        if val_result.infra_only:
            return ValidationFailureVerdict(
                action="fix_command",
                instruction=VALIDATION_INFRA_FAILURE_INSTRUCTION.format(
                    command=val_result.command or command_key,
                    diagnostics=val_result.diagnostics,
                ),
            )
        else:
            return ValidationFailureVerdict(
                action="repair",
                instruction=VALIDATION_REPAIR_INSTRUCTION.format(
                    command=val_result.command or command_key,
                    diagnostics=val_result.diagnostics,
                ),
            )

    # --- Stall -- identical fingerprint, no intervening edit -------------
    # The Worker has replayed itself and all options are exhausted.
    return ValidationFailureVerdict(
        action="handback",
        handback_details={
            "failure_class": "product_validation_failed",
            "error": (
                "Final acceptance validation still fails after one focused "
                "repair attempt."
            ),
            "details": {
                "command": val_result.command,
                **validation_diagnostics_preview(val_result.diagnostics),
                "suggested_next_tool": "dispatch_to_worker",
                "suggested_next_action": (
                    "Redispatch a focused repair, or revise the "
                    "acceptance command if it is misdeclared."
                ),
                "dispatch_mismatch": True,
                "worker_confusion_question": (
                    "Worker could not make acceptance validation "
                    "pass after one focused repair attempt"
                    + (": " + str(val_result.command) if val_result.command else ".")
                ),
            },
        },
    )


__all__ = [
    "ValidationFailureVerdict",
    "route_validation_failure",
    "validation_diagnostics_preview",
    "VALIDATION_INFRA_FAILURE_INSTRUCTION",
    "VALIDATION_REPAIR_INSTRUCTION",
]
