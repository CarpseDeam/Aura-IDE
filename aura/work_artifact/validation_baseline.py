"""Baseline capture for WorkArtifact validation attribution.

Captures the failure fingerprints of declared validation commands *before*
any WorkArtifact item mutates the workspace.  The baseline is later compared
against the fingerprints of each item's final explicit validation to
determine whether failures are novel (gate the item) or pre-existing
(recorded in receipt metadata, but do not block completion).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aura.conversation.validation_attribution import compute_failure_fingerprint
from aura.conversation.validation_failure_routing import (
    compute_diagnostics_digest,
)
from aura.conversation.worker_final_validation import (
    run_explicit_validation_commands,
)
from aura.work_artifact.model import ValidationCommandSpec

_log = logging.getLogger(__name__)


def capture_baseline(
    commands: list[ValidationCommandSpec] | list[str],
    workspace_root: Path,
) -> dict[str, list[str]]:
    """Run the declared validation commands once and fingerprint failures.

    The returned dict maps each command key to its failure fingerprints.
    A passing command stores an empty list.  An empty command list returns
    ``{}``.

    If a command fails due to infrastructure/environment issues (rather
    than a product failure), its fingerprints are still stored so the
    baseline is complete — the attribution layer decides gating.

    Parameters
    ----------
    commands
        The union of declared per-item validation commands.
    workspace_root
        The workspace root path, used as the working directory.

    Returns
    -------
    dict[str, list[str]]
        Mapping command key → list of failure fingerprints (empty = passed).
    """
    if not commands:
        return {}

    baseline: dict[str, list[str]] = {}

    for command_entry in commands:
        if isinstance(command_entry, ValidationCommandSpec):
            command_str = command_entry.command
        else:
            command_str = str(command_entry)

        if not command_str.strip():
            continue

        try:
            result = run_explicit_validation_commands(
                workspace_root=workspace_root,
                commands=[command_entry],
                window_seconds=20,
            )
        except Exception as exc:
            _log.warning(
                "Baseline capture threw for command %r: %s",
                command_str, exc,
            )
            # Store empty list — missing baseline degrades to strict gating.
            baseline[command_str] = []
            continue

        if result.ok:
            baseline[command_str] = []
        else:
            fingerprints = _fingerprint_failure(result, command_str)
            _log.info(
                "Baseline fingerprint for %r: %d fingerprint(s)",
                command_str, len(fingerprints),
            )
            baseline[command_str] = fingerprints

    return baseline


def _fingerprint_failure(
    result: Any,
    command_key: str,
) -> list[str]:
    """Extract failure fingerprints from a ``WorkerFinalValidationResult``.

    Tries to fingerprint each failing run independently.  If no runs are
    available, fingerprints the command as a single failure.

    Returns a list of fingerprint strings (may be empty if output is
    unparseable).
    """
    failing_runs = [
        r for r in (getattr(result, "runs", None) or []) if not r.ok
    ]

    if not failing_runs:
        # No structured runs — fingerprint the whole diagnostics blob.
        diagnostics = getattr(result, "diagnostics", "") or ""
        classification = getattr(result, "command", "") or command_key
        return [
            compute_failure_fingerprint(
                command_key=command_key,
                classification=classification,
                diagnostics=diagnostics,
            )
        ]

    fingerprints: list[str] = []
    for run in failing_runs:
        classification = getattr(run, "classification", "") or "failure"
        output = getattr(run, "output", "") or ""

        try:
            fp = compute_failure_fingerprint(
                command_key=command_key,
                classification=classification,
                diagnostics=output,
            )
            fingerprints.append(fp)
        except Exception as exc:
            _log.warning(
                "Failed to fingerprint run for %r: %s",
                command_key, exc,
            )

    return fingerprints


__all__ = ["capture_baseline"]
