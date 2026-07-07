"""Pure helper functions for determining validation truth from raw payloads.

These are the single source of truth for whether a validation attempt
genuinely passed or failed.  No other module should re-implement this logic.

Terminal execution success (``ok=True``) is *never* validation success on its
own — a command can run successfully but produce a failing validation result.
"""
from __future__ import annotations

from typing import Any


def validation_payload_counts_as_validation(payload: dict[str, Any]) -> bool:
    """Return True when *payload* represents a validation attempt.

    Checks the structured validation fields that are attached by the event
    relay's ``_attach_validation_metadata``, plus the legacy
    ``auto_validation`` flag carried from the terminal tracker.
    """
    if payload.get("counts_as_validation") is True:
        return True
    if payload.get("auto_validation") is True:
        return True
    vc = str(payload.get("validation_classification") or "")
    if vc:
        return True
    return False


def validation_payload_passed(payload: dict[str, Any]) -> bool:
    """Return True when the validation genuinely passed.

    Truth rules (in order of precedence):

    1. ``validation_classification == "passed"`` → passed unconditionally.
    2. ``command_outcome_classification == "passed"`` → passed only when the
       payload also counts as a validation attempt.  This field is set by
       the tool runner for *every* command outcome, not just validation,
       so the validation context guard is required.
    3. ``classification == "passed"`` → passed only when the payload
       also counts as a validation attempt.
    4. Every other case → not passed, even when ``ok=True``.
    """
    vc = str(payload.get("validation_classification") or "")
    if vc == "passed":
        return True
    coc = str(payload.get("command_outcome_classification") or "")
    if coc == "passed":
        return validation_payload_counts_as_validation(payload)
    c = str(payload.get("classification") or "")
    if c == "passed":
        return validation_payload_counts_as_validation(payload)
    return False


def validation_payload_failed(payload: dict[str, Any]) -> bool:
    """Return True when the validation failed for *any* reason.

    Failing means the validation attempted to run but produced a failure
    outcome — product failure, infra failure, timeout, etc.

    Truth rules:

    1. ``counts_as_product_failure=True`` → failed.
    2. ``exit_code is not None and exit_code != 0`` → failed.
    3. ``command_success=False`` → failed.
    4. Otherwise → not failed from the payload's own fields.
    """
    if payload.get("counts_as_product_failure") is True:
        return True
    exit_code = payload.get("exit_code")
    if exit_code is not None and exit_code != 0:
        return True
    if payload.get("command_success") is False:
        return True
    return False


def validation_payload_product_failure(payload: dict[str, Any]) -> bool:
    """Return True when the validation failed as a product (code) issue."""
    return payload.get("counts_as_product_failure") is True


def normalize_validation_command_key(command: str, cwd: str = "") -> str:
    """Normalize a validation command into a stable lookup key.

    Strips leading/trailing whitespace, collapses multiple spaces, and
    appends a ``|cwd=<cwd>`` suffix when *cwd* is non-empty.
    """
    key = " ".join(str(command or "").strip().split())
    cwd_str = str(cwd or "").strip()
    if cwd_str:
        key = f"{key}|cwd={cwd_str}"
    return key


__all__ = [
    "normalize_validation_command_key",
    "validation_payload_counts_as_validation",
    "validation_payload_failed",
    "validation_payload_passed",
    "validation_payload_product_failure",
]
