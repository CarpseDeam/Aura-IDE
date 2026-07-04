"""WorkArtifactController — owns active artifacts for the current session.

The controller is the single point of coordination between the Planner's
tool payload, the dispatch bridge, and the GUI projection.

Responsibilities:
- Create artifact from Planner tool payload.
- Create a one-item compatibility artifact from a flat dispatch request.
- Return current item as a normal bounded WorkerDispatchRequest.
- Mark item active when user dispatches through SpecCard.
- Attach receipt on Worker finish.
- Emit artifact projection updates for GUI.
- Never run Worker itself.
- Never loop through items internally.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
from aura.work_artifact.model import WorkArtifact, WorkArtifactItem, WorkArtifactReceipt
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

    def current_dispatch_request(
        self,
        tool_call_id: str,
        original_req: WorkerDispatchRequest,
    ) -> WorkerDispatchRequest | None:
        """Return the current item as a bounded WorkerDispatchRequest.

        Returns None if no artifact exists for *tool_call_id* or no current
        item is set.
        """
        artifact = self._artifacts.get(tool_call_id)
        if artifact is None:
            return None
        item = artifact.current_item()
        if item is None:
            return None

        from dataclasses import replace

        return replace(
            original_req,
            goal=item.intent or original_req.goal,
            files=list(item.target_files) if item.target_files else original_req.files,
            spec=original_req.spec,
            acceptance=item.acceptance or original_req.acceptance,
            summary=item.title or original_req.summary,
            artifact_id=tool_call_id,
            artifact_item_id=item.id,
        )

    def mark_item_active(self, tool_call_id: str, item_id: str) -> None:
        """Mark an artifact item as active (user dispatched through SpecCard)."""
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
        *,
        status_override: str | None = None,
    ) -> None:
        """Attach a Worker result receipt to the current artifact item."""
        artifact = self._artifacts.get(tool_call_id)
        if artifact is None:
            _log.warning("attach_receipt: no artifact for tool_call_id=%s", tool_call_id)
            return

        item = artifact.current_item()
        if item is None:
            _log.warning("attach_receipt: no current item for tool_call_id=%s", tool_call_id)
            return

        receipt = worker_result_to_receipt(result, status_override=status_override)
        artifact.attach_receipt(item.id, receipt)
        self._emit_projection(tool_call_id)

    def advance_to_next_item(self, tool_call_id: str) -> bool:
        """Advance artifact to the next pending item for review.

        Returns True if a next item was found, False if the artifact is
        complete (no more pending items).

        This only advances the cursor for review. It must NOT dispatch
        the Worker.
        """
        artifact = self._artifacts.get(tool_call_id)
        if artifact is None:
            return False
        next_item = artifact.advance()
        if next_item is not None:
            _log.info(
                "WorkArtifact advanced tool_call_id=%s next_item=%s",
                tool_call_id,
                next_item.id,
            )
            self._emit_projection(tool_call_id)
            return True

        _log.info("WorkArtifact complete tool_call_id=%s (no more pending items)", tool_call_id)
        self._emit_projection(tool_call_id)
        return False

    def has_more_items(self, tool_call_id: str) -> bool:
        """Return True if the artifact has more pending items."""
        artifact = self._artifacts.get(tool_call_id)
        if artifact is None:
            return False
        return artifact.next_pending_item() is not None

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
