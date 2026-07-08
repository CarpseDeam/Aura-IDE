"""Attribution of validation failures — does a failure gate an item?

Determines whether validation failures are novel (introduced by the current
item) or pre-existing (already present in the baseline).  Only novel failures
gate the item.  Preexisting failures are recorded in receipt metadata but do
not block completion.

This is a pure set-difference operation.  No counters, no thresholds, no
file-tracing heuristics, no cleverness.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aura.conversation.validation_failure_routing import (
    compute_diagnostics_digest,
)


@dataclass(frozen=True)
class AttributionVerdict:
    """Whether a set of validation failures gates an item.

    Attributes:
        novel_fingerprints: Fingerprints present in the current run but not
            in the baseline — these are new failures introduced by this item.
        preexisting_fingerprints: Fingerprints present in both the current
            run and the baseline — these existed before this item.
        gates_item: True when there is at least one novel fingerprint.
    """
    novel_fingerprints: list[str]
    preexisting_fingerprints: list[str]
    gates_item: bool


def attribute_validation_failures(
    *,
    current_fingerprints: list[str],
    baseline_fingerprints: list[str],
) -> AttributionVerdict:
    """Attribute current validation failures against a baseline.

    Pure set difference — no heuristics, thresholds, or counters.

    Parameters
    ----------
    current_fingerprints
        Failure fingerprints from the current validation run.
    baseline_fingerprints
        Failure fingerprints captured before any item mutated the workspace.

    Returns
    -------
    AttributionVerdict
        With ``gates_item=True`` only when novel failures are found.
    """
    current_set = set(current_fingerprints)
    baseline_set = set(baseline_fingerprints)

    preexisting = sorted(current_set & baseline_set)
    novel = sorted(current_set - baseline_set)
    gates_item = bool(novel)

    return AttributionVerdict(
        novel_fingerprints=list(novel),
        preexisting_fingerprints=list(preexisting),
        gates_item=gates_item,
    )


def compute_failure_fingerprint(
    command_key: str,
    classification: str,
    diagnostics: str,
) -> str:
    """Stable fingerprint for a validation failure.

    Uses the same normalisation/digest pipeline as the failure router so
    fingerprints are comparable across runs and processes.

    Parameters
    ----------
    command_key
        Normalized command key (e.g. ``"pytest"``, ``"ruff check"``).
    classification
        Failure classification string.
    diagnostics
        Raw diagnostic output from the failed command.

    Returns
    -------
    str
        A stable fingerprint string.
    """
    digest = compute_diagnostics_digest(diagnostics)
    return f"{command_key}|{classification}|{digest}"


__all__ = [
    "AttributionVerdict",
    "attribute_validation_failures",
    "compute_failure_fingerprint",
]
