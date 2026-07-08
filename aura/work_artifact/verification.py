"""WorkArtifact item verification — the sole authority for done/retry/pause/cancel.

This module owns all WorkArtifact item truth.  No other module decides
whether an item passes, fails, retries, or pauses.  The runner calls
``classify_item_attempt``; the runner never short-circuits that judgment.

Core invariant
--------------
- Passing structured evidence → done.
- Failed structured evidence → retry the same item.
- Missing structured evidence → retry the same item.
- Provider/API/harness unavailable → pause unfinished.
- User cancellation → stop.

Raw ``WorkerDispatchResult.ok`` is never item completion authority.
"""
from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Any

from aura.conversation.detected_validation import normalize_command as _normalize_command
from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
from aura.conversation.validation_truth import (
    validation_payload_passed,
    validation_payload_product_failure,
)
from aura.work_artifact.model import ValidationCommandSpec, WorkArtifactItem

_log = logging.getLogger(__name__)

__all__ = [
    "WorkArtifactAttemptOutcome",
    "add_retry_context",
    "classify_item_attempt",
    "declared_validation_commands",
    "derive_scoped_validation_commands",
    "ensure_item_verification_source",
    "evidence_records",
    "is_infrastructure_failure",
    "validation_satisfied",
]


# ── Outcome enum ──────────────────────────────────────────────────────────────────


class WorkArtifactAttemptOutcome(Enum):
    """Classification of a single WorkArtifact item attempt."""

    done = "done"
    retry = "retry"
    pause = "pause"
    cancelled = "cancelled"


# ── Evidence extraction ───────────────────────────────────────────────────────────


def declared_validation_commands(req: WorkerDispatchRequest) -> list[str]:
    """Return the list of non-empty declared validation command strings."""
    if not req.validation_commands:
        return []
    return [vc.command for vc in req.validation_commands if vc.command.strip()]


def evidence_records(result: WorkerDispatchResult) -> list[dict]:
    """Collect all structured evidence records from a Worker result.

    Reads from both ``validation_results`` and ``terminal_results`` in
    ``result.extras``, preserving order.
    """
    extras = result.extras if isinstance(result.extras, dict) else {}
    evidence: list[dict] = []
    for src in ("validation_results", "terminal_results"):
        raw = extras.get(src, [])
        if isinstance(raw, list):
            evidence.extend(raw)
    return evidence


# ── Validation truth ──────────────────────────────────────────────────────────────


def validation_satisfied(
    req: WorkerDispatchRequest,
    result: WorkerDispatchResult,
) -> bool:
    """Return True when validation evidence shows the item passed.

    This is the SOLE authority for WorkArtifact item completion.

    When the item declares validation commands, each declared command
    must have a matching passing evidence record in ``validation_results``
    or ``terminal_results`` (using ``validation_payload_passed``).

    When no validation commands are declared, the result must still
    carry at least one passing validation or terminal evidence record
    from the Worker's finalization path.  No evidence is not satisfied.

    Matching uses command normalization (whitespace-collapsed, lowercased).

    A product-failure match for a declared command prevents the satisfied
    outcome for that command.  Commands with no evidence are not satisfied.
    """
    declared = declared_validation_commands(req)
    evidence = evidence_records(result)

    if declared:
        # ── Declared commands: each must have a passing match ─────────────
        declared_normal: dict[str, str] = {
            _normalize_command(cmd): cmd for cmd in declared
        }

        satisfied: set[str] = set()
        blocked: set[str] = set()

        for record in evidence:
            if not isinstance(record, dict):
                continue
            record_cmd = str(record.get("command", "") or "")
            if not record_cmd:
                continue
            normal = _normalize_command(record_cmd)
            if normal not in declared_normal:
                continue  # unrelated evidence

            if validation_payload_product_failure(record):
                blocked.add(normal)
            elif validation_payload_passed(record):
                satisfied.add(normal)

        for normal in declared_normal:
            if normal in blocked:
                return False
            if normal not in satisfied:
                return False
        return True

    # ── No declared commands: require ANY passing evidence ──────────────
    for record in evidence:
        if not isinstance(record, dict):
            continue
        if validation_payload_passed(record):
            return True
    return False


# ── Infrastructure failure detection ─────────────────────────────────────────────


def is_infrastructure_failure(result: WorkerDispatchResult) -> bool:
    """True for harness/provider/auth/network failures.

    These pause the job rather than retrying the item, and the job
    can be resumed later when the infrastructure is healthy.
    """
    from aura.conversation.dispatch import WorkerOutcomeStatus

    if result.status == WorkerOutcomeStatus.harness_error.value:
        return True
    extras = result.extras if isinstance(result.extras, dict) else {}
    if extras.get("api_errors"):
        return True
    fc = str(extras.get("failure_class", "") or "")
    if any(marker in fc for marker in ("provider", "network", "auth", "api_error", "unavailable")):
        return True
    return False


# ── Attempt classification ───────────────────────────────────────────────────────


def classify_item_attempt(
    item_req: WorkerDispatchRequest,
    result: WorkerDispatchResult,
) -> WorkArtifactAttemptOutcome:
    """Classify a single WorkArtifact item attempt.

    Validation is the SOLE authority for "done".
    No side channels (path overlap, receipt status, baseline
    fingerprints, UI projection, Worker prose, or hard-blocker
    checks) influence the outcome.
    """
    # ── Cancelled ──────────────────────────────────────────────────────────
    if result.cancelled:
        return WorkArtifactAttemptOutcome.cancelled

    # ── External / infrastructure pause ────────────────────────────────────
    if is_infrastructure_failure(result):
        return WorkArtifactAttemptOutcome.pause

    # ── Done — validation passed ───────────────────────────────────────────
    if validation_satisfied(item_req, result):
        return WorkArtifactAttemptOutcome.done

    # ── Retry same item (everything else) ──────────────────────────────────
    return WorkArtifactAttemptOutcome.retry


# ── Scoped verification derivation ───────────────────────────────────────────────


def derive_scoped_validation_commands(
    item: WorkArtifactItem,
    workspace_root: Path | None,
) -> list[ValidationCommandSpec]:
    """Derive a safe scoped validation command when the item declares none.

    Rules (from the architecture fix):
    1. If ``item.validation_commands`` exists and contains non-empty commands,
       return those commands unchanged.
    2. If ``item.validation_commands`` is empty, derive a safe scoped
       verification command before running the item.
    3. For Python target files, use ``python -m py_compile <target files>``.
    4. Do not blindly inherit broad top-level job commands.
    5. If no safe scoped verification can be derived, return an empty list.

    The caller (``ensure_item_verification_source``) decides what to do with
    an empty result — the item cannot complete from raw ``ok=True``.
    """
    # If the item already has declared commands, use those as-is.
    if item.validation_commands:
        valid = [vc for vc in item.validation_commands if vc.command.strip()]
        if valid:
            return valid

    # Attempt to derive a scoped command from target files.
    if not item.target_files or not workspace_root:
        return []

    # Check for Python target files.
    py_files = [f for f in item.target_files if f.endswith(".py")]
    if py_files:
        py_compile_cmd = f"python -m py_compile {' '.join(py_files)}"
        return [ValidationCommandSpec(command=py_compile_cmd)]

    return []


def ensure_item_verification_source(
    item_req: WorkerDispatchRequest,
    item: WorkArtifactItem,
    workspace_root: Path | None,
) -> WorkerDispatchRequest:
    """Guarantee a verification evidence path before a WorkArtifact item runs.

    If the item has no declared validation commands and a safe scoped
    command can be derived (e.g. ``py_compile`` for Python files), this
    injects that scoped command into the item request so the Worker will
    execute it and produce structured evidence.

    If no safe scoped command can be derived, the item request is returned
    unchanged — the first attempt runs without a declared command, but
    ``classify_item_attempt`` will still require structured evidence for
    ``done``.  Missing evidence causes same-item retry with explicit
    instructions (built by ``add_retry_context``) to choose and run
    scoped verification.
    """
    declared = declared_validation_commands(item_req)
    if declared:
        return item_req  # Already has commands — nothing to inject.

    scoped = derive_scoped_validation_commands(item, workspace_root)
    if not scoped:
        return item_req  # Cannot derive anything — item will retry on missing evidence.

    from dataclasses import replace

    _log.info(
        "Injecting scoped validation command for item %s: %s",
        item.id, [vc.command for vc in scoped],
    )
    return replace(item_req, validation_commands=scoped)


# ── Retry context construction ────────────────────────────────────────────────────


def add_retry_context(
    item_req: WorkerDispatchRequest,
    result: WorkerDispatchResult,
    item: WorkArtifactItem,
    attempt: int,
) -> WorkerDispatchRequest:
    """Build structured retry context and append it to the item spec.

    The retry context tells the Worker:
    - this is the same WorkArtifact item
    - do not move to another item
    - previous attempt did not satisfy verification
    - what command was expected, if any
    - whether evidence was missing or failed
    - include relevant output excerpt from structured evidence, if present
    - rerun or choose scoped verification
    - item is not complete until structured validation evidence passes
    """
    from dataclasses import replace

    declared_cmds = declared_validation_commands(item_req)
    validation_cmd_str = declared_cmds[0] if declared_cmds else "(no declared validation command)"

    extras = result.extras if isinstance(result.extras, dict) else {}

    # Extract output excerpt from validation/terminal results.
    output_excerpt = ""
    for src in ("validation_results", "terminal_results"):
        records = extras.get(src, [])
        if isinstance(records, list) and records:
            last = records[-1]
            if isinstance(last, dict):
                output_excerpt = str(last.get("output", last.get("stdout", last.get("stderr", ""))))
                break

    recovery_note = (
        f"\n\n--- Recovery attempt {attempt} for WorkArtifact item ---\n"
        f"\n"
        f"Previous attempt failed validation for the current WorkArtifact item.\n"
        f"\n"
        f"Item: {item.id} - {item.title}\n"
        f"Attempt: {attempt}\n"
        f"Worker status: {result.status or 'unknown'}\n"
        f"Failure class: {extras.get('failure_class', 'unknown')}\n"
        f"\n"
        f"Validation command:\n"
        f"{validation_cmd_str}\n"
        f"\n"
        f"Exit code: {extras.get('exit_code', 'N/A')}\n"
        f"\n"
        f"Failure summary:\n"
        f"{result.summary or '(no summary)'}\n"
        f"\n"
    )
    if output_excerpt:
        recovery_note += (
            f"Relevant output:\n"
            f"{output_excerpt[:2000]}\n"
            f"\n"
        )
    recovery_note += (
        f"Files modified last attempt:\n"
        f"{', '.join(result.modified_files) if result.modified_files else '(none)'}\n"
        f"\n"
        f"Instruction:\n"
        f"Continue the same item only ({item.title}).\n"
        f"Fix the validation failure. Do not move to another item.\n"
        f"Rerun the required validation. The item is not complete until validation passes.\n"
        f"Aura will continue the approved job after this item succeeds."
    )
    return replace(item_req, spec=item_req.spec + recovery_note)
