"""Thin root tool handlers for the shared Workflow authoring service."""

from __future__ import annotations

import logging

from aura.agents.graph_store import AgentGraphStoreError
from aura.agents.local_state import AgentLocalStateError
from aura.agents.retention import AgentRetentionError
from aura.agents.store import AgentStoreError
from aura.agents.team_spec import parse_workflow_spec
from aura.conversation.tools._types import ToolExecResult
from aura.conversation.tools.effects import ToolEffect

logger = logging.getLogger(__name__)


class WorkflowAuthoringHandlersMixin:
    def _workflow_authoring_call(self, operation, args, reject_all=False):
        service = self._workflow_authoring
        if service is None or self._isolated_agent:
            return ToolExecResult(False, {"ok": False, "error": "Workflow authoring is unavailable here."})
        if operation != "inspect" and (
            self._read_only
            or reject_all
            or self._plan_review.blocks(ToolEffect.MUTATION)
            or (self.active_cancel_event is not None and self.active_cancel_event.is_set())
        ):
            return ToolExecResult(False, {"ok": False, "error": "This turn does not allow Workflow changes."})
        try:
            workflow_id = str(args.get("workflow_id") or "")
            if operation == "inspect":
                return ToolExecResult(True, {"ok": True, **service.inspect(workflow_id)})
            if operation == "undo":
                saved = service.undo(workflow_id, str(args.get("revision") or ""))
            else:
                parsed = parse_workflow_spec(args)
                if not parsed.ok:
                    return ToolExecResult(False, {"ok": False, "errors": parsed.errors})
                saved = (
                    service.create(parsed.spec)
                    if operation == "create"
                    else service.update(workflow_id, str(args.get("revision") or ""), parsed.spec)
                )
        except (AgentGraphStoreError, AgentStoreError, AgentLocalStateError, AgentRetentionError) as exc:
            return ToolExecResult(False, {"ok": False, "error": str(exc)})
        observer = self._workflow_authoring_observer
        if observer is not None:
            try:
                observer.workflow_authored(saved)
            except Exception:
                logger.exception("Could not present the saved Workflow")
        return ToolExecResult(
            True,
            {
                "ok": True,
                "status": saved.status,
                **saved.document.payload(),
                "executed": False,
                "next": "The Workflow is saved. A subsequent Run or user turn can execute it by this exact id.",
            },
        )

    def _handle_inspect_workflow(self, args, approval_cb, reject_all):
        return self._workflow_authoring_call("inspect", args)

    def _handle_create_workflow(self, args, approval_cb, reject_all):
        return self._workflow_authoring_call("create", args, reject_all)

    def _handle_update_workflow(self, args, approval_cb, reject_all):
        return self._workflow_authoring_call("update", args, reject_all)

    def _handle_undo_workflow_edit(self, args, approval_cb, reject_all):
        return self._workflow_authoring_call("undo", args, reject_all)
