"""WorkArtifactRunner — ordered item orchestration for WorkArtifact jobs.

This module owns the item loop only.  It does not decide validation truth
(that is ``verification.py``'s job) and it does not run Workers directly
(that is ``WorkerDispatchRunner``'s job via the ``run_worker`` callback).

Core invariant
--------------
The runner calls ``verification.classify_item_attempt`` for each Worker
result and acts on the classification.  The runner never short-circuits
that judgment.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
from aura.conversation.dispatch import WorkerOutcomeStatus
from aura.work_artifact.controller import WorkArtifactController
from aura.work_artifact.model import ValidationCommandSpec, WorkItemStatus
from aura.work_artifact.projection import WorkArtifactProjection
from aura.work_artifact.verification import (
    WorkArtifactAttemptOutcome,
    add_retry_context,
    classify_item_attempt,
    ensure_item_verification_source,
)

_log = logging.getLogger(__name__)

__all__ = ["WorkArtifactRunner"]


class WorkArtifactRunner:
    """Orchestrates ordered WorkArtifact item execution for one job.

    Does one job per ``run()`` call.  Callers create a fresh runner (or
    reuse one configured with the same callbacks) and call ``run()`` with
    the artifact id and the approved request.
    """

    def __init__(
        self,
        *,
        controller: WorkArtifactController,
        run_worker: Callable[[str, WorkerDispatchRequest], WorkerDispatchResult],
        emit_projection: Callable[[str], None],
        workspace_root: Path | None = None,
        capture_baseline: Callable[[str], None] | None = None,
    ) -> None:
        """Constructor.

        Parameters
        ----------
        controller
            The WorkArtifactController that owns artifact state.
        run_worker
            Callable that executes one Worker attempt.  Receives
            ``(tool_call_id, item_request)`` and returns a
            ``WorkerDispatchResult``.  The ``pending`` object is
            expected to be bound into this callable by the caller.
        emit_projection
            Callable that emits an artifact projection update for the
            GUI.  Receives the tool_call_id.
        workspace_root
            Optional workspace root path, used for scoped verification
            command derivation.
        capture_baseline
            Optional callable that captures validation baseline
            fingerprints.  Receives the tool_call_id.  Called once
            before any item runs.
        """
        self._controller = controller
        self._run_worker = run_worker
        self._emit_projection = emit_projection
        self._workspace_root = workspace_root
        self._capture_baseline = capture_baseline

    def run(
        self,
        artifact_id: str,
        approved_req: WorkerDispatchRequest,
        cancel_event: threading.Event,
    ) -> WorkerDispatchResult:
        """Run all WorkArtifact items for one approved job.

        This is the main entry point.  It iterates through pending items
        in order, executing each via ``run_worker``, classifying outcomes
        via ``verification.classify_item_attempt``, and acting on the
        classification.

        Parameters
        ----------
        artifact_id
            The tool_call_id that identifies the artifact.
        approved_req
            The top-level approved dispatch request.
        cancel_event
            A threading.Event that, when set, signals cancellation.

        Returns
        -------
        WorkerDispatchResult
            The aggregated result for the whole job.
        """
        # ── Capture baseline once before any item runs ─────────────────────
        if self._capture_baseline is not None:
            self._capture_baseline(artifact_id)

        item_results: list[tuple[str, WorkerDispatchResult]] = []
        recovered_item_ids: list[str] = []
        failed_attempts: dict[str, int] = {}

        artifact_obj = self._controller.get_artifact(artifact_id)
        total = len(artifact_obj.work_items) if artifact_obj else 0

        while True:
            # ── Check for external cancellation between items ──────────────
            if cancel_event.is_set():
                return self._aggregate_artifact_results(
                    artifact_id, approved_req, item_results,
                    recovered_item_ids, failed_attempts, total,
                    terminal_override=WorkerDispatchResult(
                        ok=False,
                        summary="WorkArtifact job cancelled during internal items.",
                        cancelled=True,
                        extras={"work_artifact_job": True, "work_artifact_cancelled": True},
                    ),
                )

            unfinished = self._controller.unfinished_items(artifact_id)
            if not unfinished:
                return self._aggregate_artifact_results(
                    artifact_id, approved_req, item_results,
                    recovered_item_ids, failed_attempts, total,
                )

            item = unfinished[0]

            # Compute the item's 1-based index across all work_items.
            artifact_obj = self._controller.get_artifact(artifact_id)
            if artifact_obj is not None:
                idx_in_all = next(
                    (i for i, wi in enumerate(artifact_obj.work_items) if wi.id == item.id),
                    0,
                )
                item_index = idx_in_all + 1
            else:
                item_index = 1

            _log.info(
                "WorkArtifact internal item %d/%d artifact_id=%s item=%s",
                item_index, total, artifact_id, item.id,
            )

            # Mark this exact item active.
            self._controller.mark_item_active(artifact_id, item.id)
            self._emit_projection(artifact_id)

            # Build a bounded WorkerDispatchRequest for this item.
            item_req = self._build_artifact_item_request(
                artifact_id, approved_req, item, item_index, total,
            )

            # ── Guarantee a verification evidence path before running ─────────
            # If the item has no declared validation commands and a safe scoped
            # command can be derived (e.g. py_compile for Python files), inject
            # it now so the Worker produces structured evidence.
            item_req = ensure_item_verification_source(
                item_req, item, self._workspace_root,
            )

            # ── Inner loop for this item — retry indefinitely ──────────────
            attempt = 0
            while True:
                item_result = self._run_worker(artifact_id, item_req)
                attempt += 1

                outcome = classify_item_attempt(item_req, item_result)

                if outcome == WorkArtifactAttemptOutcome.cancelled:
                    _log.info(
                        "WorkArtifact item %s cancelled (attempt %d)",
                        item.id, attempt,
                    )
                    self._controller.attach_receipt(
                        artifact_id, item_result, item_id=item.id,
                    )
                    item_results.append((item.id, item_result))
                    return self._aggregate_artifact_results(
                        artifact_id, approved_req, item_results,
                        recovered_item_ids, failed_attempts, total,
                        terminal_override=WorkerDispatchResult(
                            ok=False,
                            summary="WorkArtifact job cancelled during internal items.",
                            cancelled=True,
                            extras={"work_artifact_job": True, "work_artifact_cancelled": True},
                        ),
                    )

                if outcome == WorkArtifactAttemptOutcome.pause:
                    _log.info(
                        "WorkArtifact item %s infrastructure failure — "
                        "pausing job artifact_id=%s",
                        item.id, artifact_id,
                    )
                    self._controller.attach_receipt(
                        artifact_id, item_result, item_id=item.id,
                    )
                    item_results.append((item.id, item_result))
                    return self._aggregate_artifact_results(
                        artifact_id, approved_req, item_results,
                        recovered_item_ids, failed_attempts, total,
                        terminal_override=item_result,
                        infrastructure_pause=True,
                    )

                if outcome == WorkArtifactAttemptOutcome.done:
                    _log.info(
                        "WorkArtifact item %s done (attempt %d)",
                        item.id, attempt,
                    )
                    # Mark item done explicitly — validation authority, not receipt status.
                    self._controller.mark_item_done(artifact_id, item.id)
                    # Attach receipt as audit record only (display status "ok"
                    # for consistency in the projection).
                    self._controller.attach_receipt(
                        artifact_id, item_result, item_id=item.id,
                        status_override="ok",
                    )
                    item_results.append((item.id, item_result))
                    self._emit_projection(artifact_id)
                    break  # Inner loop — outer loop picks next item.

                # outcome == retry
                recovered_item_ids.append(item.id)
                failed_attempts[item.id] = attempt

                # Build structured retry context via verification module.
                item_req = add_retry_context(item_req, item_result, item, attempt)
                _log.info(
                    "WorkArtifact item %s retry attempt %d",
                    item.id, attempt,
                )

    # ── Item request construction ─────────────────────────────────────────────

    def _build_artifact_item_request(
        self,
        tool_call_id: str,
        approved_req: WorkerDispatchRequest,
        item: Any,
        index: int,
        total: int,
    ) -> WorkerDispatchRequest:
        """Build a bounded WorkerDispatchRequest for one artifact item.

        Preserves the approved top-level context while scoping to the item.
        If the item carries a non-ok, non-continuing receipt (from a prior
        failed attempt), appends that receipt's status and summary to the
        spec so resumed items have context.
        """
        spec_parts = [
            f"WorkArtifact Item {index}/{total}: {item.title}",
            "",
            f"Approved job goal: {approved_req.goal}",
            f"Top-level constraints: {approved_req.spec}",
            "",
            f"Item intent: {item.intent}",
            "",
            "This is one bounded item inside an already approved WorkArtifact job.",
            "Complete only this item. Do not execute other artifact items.",
            "Other items of this approved job may have already modified files "
            "in the workspace. Those changes are NOT yours. Do not inspect, "
            "verify, revert, re-implement, or report on them. They are approved "
            "background, identical to any other pre-existing code. When this "
            "item's acceptance criteria are met and its validation commands "
            "pass, report done immediately. Do not continue checking other "
            "items or the overall job goal — the harness owns job-level "
            "completion, not you.",
            "Aura will continue the approved job after this item succeeds.",
        ]
        spec = "\n".join(spec_parts)

        # Append prior-attempt receipt context if the item carries one.
        if item.receipt is not None and item.receipt.status not in ("ok", "continuing"):
            receipt = item.receipt
            spec += (
                f"\n\n--- Previous attempt on this item ---\n"
                f"Status: {receipt.status}\n"
                f"Summary: {receipt.summary}\n"
                f"Modified files: {', '.join(receipt.modified_files) if receipt.modified_files else '(none)'}"
            )

        # Append manifest of prior done items so each Worker knows what already
        # changed in the workspace and why — derived from verified receipts, not
        # from Planner narration.
        artifact = self._controller.get_artifact(tool_call_id)
        if artifact is not None:
            done_items = [
                it for it in artifact.work_items
                if it.status == WorkItemStatus.done and it.receipt is not None
            ]
            if done_items:
                manifest_parts = [
                    "",
                    "--- Changes already made by prior items of this job ---",
                ]
                for done_item in done_items:
                    files_str = (
                        ", ".join(done_item.receipt.modified_files)
                        if done_item.receipt.modified_files
                        else "(none recorded)"
                    )
                    manifest_parts.append(
                        f"Item: {done_item.title}\n"
                        f"  Modified files: {files_str}\n"
                        f"  Summary: {done_item.receipt.summary}"
                    )
                manifest_parts.append(
                    "These changes are complete, verified, and expected in the working tree "
                    "and in git status/diff output. Treat them as existing code. Do not revert, "
                    "re-verify, or re-implement them."
                )
                spec += "\n".join(manifest_parts)

        # Use only the item's own validation_commands — never inherit
        # top-level/job-wide validation commands into an item request.
        item_vcs = list(getattr(item, "validation_commands", []) or [])

        return WorkerDispatchRequest(
            goal=item.intent or approved_req.goal,
            files=list(item.target_files) if item.target_files else list(approved_req.files),
            spec=spec,
            acceptance=item.acceptance or approved_req.acceptance,
            summary=item.title or approved_req.summary,
            artifact_id=tool_call_id,
            artifact_item_id=item.id,
            validation_commands=item_vcs,
        )

    # ── Aggregation ───────────────────────────────────────────────────────────

    def _aggregate_artifact_results(
        self,
        tool_call_id: str,
        approved_req: WorkerDispatchRequest,
        item_results: list[tuple[str, WorkerDispatchResult]],
        recovered_item_ids: list[str],
        failed_attempts: dict[str, int],
        total_items: int,
        terminal_override: WorkerDispatchResult | None = None,
        infrastructure_pause: bool = False,
    ) -> WorkerDispatchResult:
        """Aggregate per-item results into one outcome.

        Completion is derived from artifact state alone (``item.status == done``),
        never from receipt status.  Receipts are audit records only.

        Outcomes
        --------
        1. **completed** — all items done (``all_required_items_done``).
        2. **cancelled** — user cancelled the job.
        3. **infrastructure-paused** — harness/provider/auth/network failure;
           resumable later (``work_artifact_unfinished: true``).

        There is no "retry-cap-reached" outcome — ordinary repeated failures
        do not pause the job.
        """
        # ── Derive completion from artifact state ────────────────────────────
        artifact = self._controller.get_artifact(tool_call_id)

        if artifact is not None:
            completed_items = [
                wi.id
                for wi in artifact.work_items
                if wi.status == WorkItemStatus.done
            ]
            all_ok = self._controller.all_required_items_done(tool_call_id)
            pending_ids = [
                it.id
                for it in self._controller.unfinished_items(tool_call_id)
            ]
        else:
            # Fallback when no artifact is registered (standalone helper tests).
            completed_items = [item_id for item_id, r in item_results if r.ok]
            all_ok = all(r.ok for _item_id, r in item_results)
            pending_ids = []

        all_cancelled = any(r.cancelled for _item_id, r in item_results)
        first_not_ok = next(
            ((item_id, r) for item_id, r in item_results if not r.ok and not r.cancelled),
            None,
        )
        cancelled_item_id = next(
            (item_id for item_id, r in item_results if r.cancelled),
            None,
        )

        # ── Terminal overrides ──────────────────────────────────────────────

        if terminal_override is not None and infrastructure_pause:
            # ── Infrastructure pause: resumable ──
            paused_extras: dict[str, Any] = dict(terminal_override.extras or {})
            paused_extras.update({
                "work_artifact_job": True,
                "work_artifact_unfinished": True,
                "completed_items": completed_items,
                "pending_item_ids": pending_ids,
                "total_items": total_items,
                "current_item_id": item_results[-1][0] if item_results else "",
            })
            return WorkerDispatchResult(
                ok=False,
                summary=(
                    f"WorkArtifact job paused: "
                    f"{terminal_override.summary or 'Infrastructure issue'}. "
                    f"Job will resume when the provider is reachable. "
                    f"{len(completed_items)}/{total_items} items completed."
                ),
                cancelled=False,
                modified_files=list(terminal_override.modified_files)
                if terminal_override.modified_files else [],
                recoverable=True,
                status=terminal_override.status or WorkerOutcomeStatus.harness_error.value,
                extras=paused_extras,
            )

        if terminal_override is not None:
            # ── Cancellation: passed through directly ──
            return terminal_override

        # Collect modified files (ordered union).
        modified_files: list[str] = []
        seen_files: set[str] = set()
        for _item_id, r in item_results:
            for f in (r.modified_files or []):
                if f not in seen_files:
                    seen_files.add(f)
                    modified_files.append(f)

        # Collect validation summaries.
        validation_parts: list[str] = []
        for _item_id, r in item_results:
            if r.validation:
                validation_parts.append(r.validation)
        validation = "\n".join(validation_parts) if validation_parts else None

        item_summaries = {
            item_id: r.summary for item_id, r in item_results
        }
        audit_base: dict[str, Any] = {
            "work_artifact_job": True,
            "completed_items": completed_items,
            "total_items": total_items,
            "recovered_item_ids": list(recovered_item_ids),
            "failed_attempts": dict(failed_attempts),
            "item_summaries": item_summaries,
        }

        if all_ok:
            # All items done — no stale failure metadata from raw non-ok results.
            return WorkerDispatchResult(
                ok=True,
                summary=f"WorkArtifact job completed: {total_items} item(s) done.",
                modified_files=modified_files,
                validation=validation,
                status=WorkerOutcomeStatus.completed.value,
                extras={
                    **audit_base,
                    "current_item_id": "",
                },
            )

        # ── Not fully complete: derive metadata from raw results ──────────────
        failed_item_id = first_not_ok[0] if first_not_ok else None
        cancel_item_id = cancelled_item_id if all_cancelled else None
        current_item_id = failed_item_id or cancel_item_id or (item_results[-1][0] if item_results else "")
        extras: dict[str, Any] = {
            **audit_base,
            "current_item_id": current_item_id,
        }
        if failed_item_id:
            extras["failed_item_id"] = failed_item_id

        if all_cancelled:
            return WorkerDispatchResult(
                ok=False,
                summary="WorkArtifact job cancelled during internal items.",
                cancelled=True,
                modified_files=modified_files,
                status=WorkerOutcomeStatus.cancelled.value,
                extras=extras,
            )

        # ── Incomplete: some items did not complete ──
        summary_parts = [
            f"WorkArtifact job incomplete: {len(completed_items)}/{total_items} items completed."
        ]
        for item_id, r in item_results:
            if r.ok:
                summary_parts.append(f"✓ {item_id}: {r.summary}")
            else:
                summary_parts.append(f"✗ {item_id}: {r.summary}")
        return WorkerDispatchResult(
            ok=False,
            summary=" ".join(summary_parts),
            modified_files=modified_files,
            validation=validation,
            recoverable=True,
            status=WorkerOutcomeStatus.harness_error.value,
            extras=extras,
        )
