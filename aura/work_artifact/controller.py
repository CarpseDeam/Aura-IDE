"""WorkArtifactController — owns active artifacts for the current session.

The controller is the single point of coordination between the Planner's
tool payload, the dispatch bridge, and the GUI projection.

Responsibilities:
- Create artifact from Planner tool payload.
- Create a one-item compatibility artifact from a flat dispatch request.
- Track item state for internal artifact execution.
- Mark item active when execution starts.
- Attach receipt on Worker finish.
- Emit artifact projection updates for GUI.
- Never run Worker itself.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
from aura.work_artifact.model import (
    ValidationCommandSpec,
    WorkArtifact,
    WorkArtifactItem,
    WorkArtifactReceipt,
)
from aura.work_artifact.projection import WorkArtifactProjection
from aura.work_artifact.receipt import worker_result_to_receipt

_log = logging.getLogger(__name__)


class WorkArtifactController:
    """Owns active WorkArtifacts for the current conversation/session.

    One controller instance per conversation. Multiple artifacts may exist
    (one active at a time per tool_call_id).
    """

    def __init__(self) -> None:
        self._artifacts: dict[str, WorkArtifact] = {}
        self._on_projection_updated: Callable[[WorkArtifactProjection], None] | None = None

    def set_on_projection_updated(
        self, callback: Callable[[WorkArtifactProjection], None] | None
    ) -> None:
        """Register a callback invoked after every projection update."""
        self._on_projection_updated = callback

    # ── artifact lifecycle ───────────────────────────────────────────────

    def create_artifact_from_payload(
        self,
        tool_call_id: str,
        payload: dict[str, Any],
    ) -> WorkArtifact:
        """Create a WorkArtifact from the Planner's tool payload.

        The payload shape matches the ``work_artifact`` field in the
        dispatch_to_worker schema:

        - goal (str)
        - constraints (list[str])
        - allowed_files (list[str])
        - items (list of {id, title, intent, target_files, acceptance})
        """
        items_raw = payload.get("items") if isinstance(payload.get("items"), list) else []
        work_items = [
            WorkArtifactItem(
                id=str(item.get("id", f"item-{idx}")),
                title=str(item.get("title", "")),
                intent=str(item.get("intent", "")),
                target_files=[str(f) for f in (item.get("target_files") or item.get("files") or [])],
                acceptance=str(item.get("acceptance", "")),
                validation_commands=_parse_validation_specs(item.get("validation_commands")),
            )
            for idx, item in enumerate(items_raw)
        ]

        current_item_id = work_items[0].id if work_items else ""

        artifact = WorkArtifact(
            artifact_id=tool_call_id,
            goal=str(payload.get("goal", "")),
            constraints=[str(c) for c in (payload.get("constraints") or [])],
            allowed_files=[str(f) for f in (payload.get("allowed_files") or [])],
            work_items=work_items,
            current_item_id=current_item_id,
        )

        self._artifacts[tool_call_id] = artifact
        _log.info(
            "WorkArtifact created tool_call_id=%s item_count=%d current_item=%s",
            tool_call_id,
            len(work_items),
            current_item_id,
        )
        self._emit_projection(tool_call_id)
        return artifact

    def create_one_item_artifact(
        self,
        tool_call_id: str,
        req: WorkerDispatchRequest,
    ) -> WorkArtifact:
        """Create a one-item compatibility artifact from a flat dispatch request.

        Used when Planner sends a flat dispatch_to_worker without a
        ``work_artifact`` field. The single item wraps the flat request.
        """
        item = WorkArtifactItem(
            id="item-1",
            title=req.summary or req.goal or "Worker dispatch",
            intent=req.goal,
            target_files=list(req.files),
            acceptance=req.acceptance,
        )
        artifact = WorkArtifact(
            artifact_id=tool_call_id,
            goal=req.goal,
            constraints=list(req.non_goals),
            allowed_files=list(req.files),
            work_items=[item],
            current_item_id=item.id,
        )

        self._artifacts[tool_call_id] = artifact
        _log.info(
            "One-item WorkArtifact created tool_call_id=%s",
            tool_call_id,
        )
        self._emit_projection(tool_call_id)
        return artifact

    def get_artifact(self, tool_call_id: str) -> WorkArtifact | None:
        """Return the active artifact for *tool_call_id*, or None."""
        return self._artifacts.get(tool_call_id)

    def mark_item_active(self, tool_call_id: str, item_id: str) -> None:
        """Mark an artifact item as active (execution started for this item)."""
        artifact = self._artifacts.get(tool_call_id)
        if artifact is None:
            _log.warning("mark_item_active: no artifact for tool_call_id=%s", tool_call_id)
            return
        artifact.mark_active(item_id)
        self._emit_projection(tool_call_id)

    def attach_receipt(
        self,
        tool_call_id: str,
        result: WorkerDispatchResult,
        item_id: str | None = None,
        *,
        status_override: str | None = None,
    ) -> None:
        """Attach a Worker result receipt to an artifact item.

        When *item_id* is provided, attaches to that exact item.
        Otherwise attaches to ``artifact.current_item()``.
        """
        artifact = self._artifacts.get(tool_call_id)
        if artifact is None:
            _log.warning("attach_receipt: no artifact for tool_call_id=%s", tool_call_id)
            return

        if item_id:
            target_id = item_id
        else:
            item = artifact.current_item()
            if item is None:
                _log.warning("attach_receipt: no current item for tool_call_id=%s", tool_call_id)
                return
            target_id = item.id

        receipt = worker_result_to_receipt(result, status_override=status_override)
        artifact.attach_receipt(target_id, receipt)
        self._emit_projection(tool_call_id)

    # ── item query helpers ───────────────────────────────────────────────

    def pending_items(self, tool_call_id: str) -> list[Any]:
        """Return all unfinished work items for the given artifact.

        Returns every item whose status is not ``done``, including items
        that are currently ``active`` (execution in progress) or ``pending``
        (not yet started).  This ensures that an infrastructure-paused job
        can resume the active item it was executing, and that the internal
        loop correctly discovers the next item after marking one ``done``.
        """
        return self.unfinished_items(tool_call_id)

    def unfinished_items(self, tool_call_id: str) -> list[Any]:
        """Return all unfinished work items for the given artifact.

        Same semantics as ``pending_items`` — every item whose status is
        not ``done`` — but with an unambiguous name.  Use this for internal
        dispatch loops and resume logic; ``pending_items`` is kept for
        backward compatibility.
        """
        artifact = self._artifacts.get(tool_call_id)
        if artifact is None:
            return []
        return [item for item in artifact.work_items if item.status.value != "done"]

    def next_pending_item(self, tool_call_id: str) -> Any | None:
        """Return the first pending item, or None if no pending items remain."""
        items = self.pending_items(tool_call_id)
        return items[0] if items else None

    def all_required_items_done(self, tool_call_id: str) -> bool:
        """Return True if every item in the artifact is done."""
        artifact = self._artifacts.get(tool_call_id)
        if artifact is None or not artifact.work_items:
            return False
        return all(item.status.value == "done" for item in artifact.work_items)

    def set_final_receipt(self, tool_call_id: str, result: WorkerDispatchResult) -> None:
        """Set an aggregate final receipt on the artifact for display."""
        artifact = self._artifacts.get(tool_call_id)
        if artifact is None:
            return
        from aura.work_artifact.receipt import worker_result_to_receipt
        artifact.final_receipt = worker_result_to_receipt(result)

    # ── lifecycle ────────────────────────────────────────────────────────

    def remove_artifact(self, tool_call_id: str) -> None:
        """Remove an artifact (conversation reset / teardown)."""
        self._artifacts.pop(tool_call_id, None)

    def clear(self) -> None:
        """Remove all artifacts (conversation reset)."""
        self._artifacts.clear()

    # ── projection ───────────────────────────────────────────────────────

    def _emit_projection(self, tool_call_id: str) -> None:
        """Emit a projection update for the artifact."""
        if self._on_projection_updated is None:
            return
        artifact = self._artifacts.get(tool_call_id)
        if artifact is None:
            return
        projection = WorkArtifactProjection.from_artifact(artifact)
        self._on_projection_updated(projection)


def _parse_validation_specs(raw: Any) -> list[ValidationCommandSpec]:
    """Coerce raw validation_commands from a Planner item payload into specs.

    Supports both the new structured format (list of dicts) and the legacy
    flat-string format (list of strings) for backward compatibility.
    """
    if not isinstance(raw, list):
        return []
    return [ValidationCommandSpec.from_dict(v) for v in raw]
