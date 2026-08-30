"""Root orchestration handlers plus the Step-scoped helper delegation seam.

This is a thin, deliberate seam.  It resolves the requested id against the
turn's frozen roster, refuses anything that is not on it, and hands the run to
the injected delegation runner.  It does not decide provider, model, prompt,
or tool surface — :mod:`aura.agents.runtime` owns all of that — and it never
lets a child's failure escape as an exception: a delegation that could not
happen is a structured tool result like any other.

An ordinary child or helper registry has no roster, helper context, or runner,
so this handler answers every call there with the same truthful refusal. A
solid workflow Step is configured only with its own frozen helper occurrences.
That is what keeps delegation one level deep at execution as well as catalog
composition.
"""
from __future__ import annotations

from typing import Any

from aura.conversation.tools._types import ApprovalCallback, ToolExecResult


class AgentDelegationHandlersMixin:
    """Handlers for root orchestration and frozen workflow helper calls."""

    def _handle_delegate_agent(
        self, args: dict[str, Any], approval_cb: ApprovalCallback, reject_all: bool
    ) -> ToolExecResult:
        from aura.agents.delegation import DelegationFailure, DelegationResult

        if self._workflow_helpers:
            return self._handle_workflow_helper(args)

        agent_id = str(args.get("agent_id") or "").strip()
        task = str(args.get("task") or "")

        roster = self._turn_agent_roster
        entry = roster.get(agent_id) if roster is not None else None
        if entry is None:
            available = ", ".join(roster.ids) if roster is not None else ""
            result = DelegationResult.failure(
                agent_id,
                DelegationFailure.AGENT_NOT_AVAILABLE,
                (
                    f"No agent with id '{agent_id}' is available on this turn."
                    + (f" Available: {available}." if available else "")
                ),
            )
            return ToolExecResult(ok=False, payload=result.payload())

        runner = self._agent_delegation_runner
        if runner is None:
            result = DelegationResult.failure(
                agent_id,
                DelegationFailure.DELEGATION_UNAVAILABLE,
                "Delegation is not available in this runtime.",
                agent_name=entry.name,
            )
            return ToolExecResult(ok=False, payload=result.payload())

        if entry.permission.allows_edit:
            from aura.conversation.tools.effects import ToolEffect

            mutation_forbidden = self._read_only or self._plan_review.blocks(
                ToolEffect.MUTATION
            )
            if mutation_forbidden:
                result = DelegationResult.failure(
                    agent_id,
                    DelegationFailure.ROOT_MUTATION_FORBIDDEN,
                    "This Agent has a writable grant, but the frozen root turn "
                    "forbids mutation. Aura did not downgrade the grant or start "
                    "the Agent; use a read-only Agent or a mutation-enabled turn.",
                    agent_name=entry.name,
                )
                return ToolExecResult(ok=False, payload=result.payload())

        # The turn's own cancel event, relayed by the tool round — never a
        # second cancellation authority created here.
        result = runner.run(entry, task, cancel_event=self.active_cancel_event)
        extras: dict[str, Any] = {
            "agent_id": result.agent_id,
            "delegation_status": result.status.value,
        }
        if result.usage is not None and not result.usage.is_empty:
            extras["delegation_usage"] = result.usage.as_dict()
            extras["delegation_provider"] = result.provider
            extras["delegation_model"] = result.model
        return ToolExecResult(
            ok=result.ok,
            payload=result.payload(),
            extras=extras,
        )

    def _handle_workflow_helper(self, args: dict[str, Any]) -> ToolExecResult:
        """Run one helper already frozen for this solid workflow Step."""
        from aura.agents.delegation import DelegationFailure, DelegationResult

        helper_node_id = str(args.get("helper_node_id") or "").strip()
        helper = next(
            (
                item
                for item in self._workflow_helpers
                if item.node_id == helper_node_id
            ),
            None,
        )
        if helper is None:
            available = ", ".join(item.node_id for item in self._workflow_helpers)
            result = DelegationResult.failure(
                helper_node_id,
                DelegationFailure.AGENT_NOT_AVAILABLE,
                (
                    f"No helper occurrence with node id '{helper_node_id}' is "
                    "attached to this workflow Step."
                    + (f" Available: {available}." if available else "")
                ),
            )
            return ToolExecResult(ok=False, payload=result.payload())

        runner = self._workflow_helper_runner
        if runner is None:
            result = DelegationResult.failure(
                helper.agent_id,
                DelegationFailure.DELEGATION_UNAVAILABLE,
                "Workflow helper execution is not available in this runtime.",
                agent_name=helper.agent_name,
                provider=helper.resolved.provider,
                model=helper.resolved.model,
            )
            return ToolExecResult(ok=False, payload=result.payload())

        result = runner.run(
            helper,
            str(args.get("task") or ""),
            cancel_event=self.active_cancel_event,
        )
        extras: dict[str, Any] = {
            "agent_id": result.agent_id,
            "helper_node_id": helper.node_id,
            "owning_step_node_id": helper.owning_step_node_id,
            "connection_id": helper.connection_id,
            "delegation_status": result.status.value,
        }
        if result.usage is not None and not result.usage.is_empty:
            extras["delegation_usage"] = result.usage.as_dict()
            extras["delegation_provider"] = result.provider
            extras["delegation_model"] = result.model
        return ToolExecResult(
            ok=result.ok,
            payload=result.payload(),
            extras=extras,
        )

    def _handle_run_agent_workflow(
        self, args: dict[str, Any], approval_cb: ApprovalCallback, reject_all: bool
    ) -> ToolExecResult:
        """Run this turn's one frozen workflow, or refuse truthfully.

        There is no workflow to name: the plan was frozen with the turn, and a
        turn that froze none has no such tool in its catalog at all. Reaching
        here without one therefore means something went wrong, and it is said
        plainly rather than resolved into whatever workflow is open now.
        """
        from aura.agents.delegation import DelegationFailure
        from aura.agents.workflow_runner import WorkflowRunResult

        plan = self._turn_workflow_plan
        if plan is None:
            result = WorkflowRunResult.failure(
                "",
                DelegationFailure.DELEGATION_UNAVAILABLE,
                "No Agent workflow is available on this turn.",
            )
            return ToolExecResult(ok=False, payload=result.payload())

        runner = self._agent_workflow_runner
        if runner is None:
            result = WorkflowRunResult.failure(
                plan.graph_id,
                DelegationFailure.DELEGATION_UNAVAILABLE,
                "Agent workflows are not available in this runtime.",
                workflow_name=plan.name,
            )
            return ToolExecResult(ok=False, payload=result.payload())

        if plan.writable:
            from aura.conversation.tools.effects import ToolEffect

            if self._read_only or self._plan_review.blocks(ToolEffect.MUTATION):
                result = WorkflowRunResult.failure(
                    plan.graph_id,
                    DelegationFailure.ROOT_MUTATION_FORBIDDEN,
                    "This workflow has a solid Step or attached helper with a "
                    "Read / Write grant, but the frozen root turn forbids "
                    "mutation. Aura did not downgrade the grant or start the "
                    "workflow.",
                    workflow_name=plan.name,
                )
                return ToolExecResult(ok=False, payload=result.payload())

        # The turn's own cancel event, relayed by the tool round — never a
        # second cancellation authority created here.
        result = runner.run(
            plan, str(args.get("task") or ""), cancel_event=self.active_cancel_event
        )
        return ToolExecResult(
            ok=result.ok,
            payload=result.payload(),
            extras={
                "workflow_graph_id": result.graph_id,
                "workflow_status": result.status.value,
            },
        )

    def _handle_list_agent_change_sets(
        self, args: dict[str, Any], approval_cb: ApprovalCallback, reject_all: bool
    ) -> ToolExecResult:
        manager = self._agent_worktree_manager
        if manager is None:
            return _change_set_unavailable("list_agent_change_sets")
        try:
            return ToolExecResult(ok=True, payload=manager.list_change_sets())
        except Exception as exc:
            return _change_set_failure("list_agent_change_sets", exc)

    def _handle_inspect_agent_change_set(
        self, args: dict[str, Any], approval_cb: ApprovalCallback, reject_all: bool
    ) -> ToolExecResult:
        manager = self._agent_worktree_manager
        if manager is None:
            return _change_set_unavailable("inspect_agent_change_set")
        try:
            raw_paths = args.get("paths") or []
            payload = manager.inspect(
                str(args.get("change_set_id") or ""),
                paths=tuple(str(path) for path in raw_paths),
            )
        except Exception as exc:
            return _change_set_failure("inspect_agent_change_set", exc)
        return ToolExecResult(ok=True, payload=payload)

    def _handle_apply_agent_change_set(
        self, args: dict[str, Any], approval_cb: ApprovalCallback, reject_all: bool
    ) -> ToolExecResult:
        manager = self._agent_worktree_manager
        if manager is None:
            return _change_set_unavailable("apply_agent_change_set")
        if reject_all:
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "tool": "apply_agent_change_set",
                    "applied": False,
                    "failure_class": "approval_rejected",
                    "error": "User rejected all writes in this turn.",
                },
                extras={"rejected_all": True},
            )
        try:
            payload = manager.apply(
                str(args.get("change_set_id") or ""),
                approval_cb=approval_cb,
                capture_before_write=lambda path: self._capture_before_write(self, path),
            )
        except Exception as exc:
            return _change_set_failure("apply_agent_change_set", exc)
        if payload.get("applied"):
            self._refresh_code_intel_paths(payload.get("changed_paths", []))
        approval = str(payload.get("approval") or "")
        return ToolExecResult(
            ok=bool(payload.get("ok")),
            payload=payload,
            extras={"approval": approval} if approval else {},
        )

    def _handle_discard_agent_change_set(
        self, args: dict[str, Any], approval_cb: ApprovalCallback, reject_all: bool
    ) -> ToolExecResult:
        manager = self._agent_worktree_manager
        if manager is None:
            return _change_set_unavailable("discard_agent_change_set")
        try:
            payload = manager.discard(
                str(args.get("change_set_id") or ""), approval_cb=approval_cb
            )
        except Exception as exc:
            return _change_set_failure("discard_agent_change_set", exc)
        approval = str(payload.get("approval") or "")
        return ToolExecResult(
            ok=bool(payload.get("ok")),
            payload=payload,
            extras={"approval": approval} if approval else {},
        )


def _change_set_unavailable(tool: str) -> ToolExecResult:
    return ToolExecResult(
        ok=False,
        payload={
            "ok": False,
            "tool": tool,
            "status": "failed",
            "failure_class": "agent_worktree_unavailable",
            "error": "Writable Agent change sets are not available in this runtime.",
        },
    )


def _change_set_failure(tool: str, exc: Exception) -> ToolExecResult:
    from aura.agents.worktree import AgentWorktreeError
    from aura.config import redact_secrets

    if isinstance(exc, AgentWorktreeError):
        payload = {"ok": False, "tool": tool, "status": "failed", **exc.payload()}
    else:
        payload = {
            "ok": False,
            "tool": tool,
            "status": "failed",
            "failure_class": "internal_error",
            "error": redact_secrets(f"{type(exc).__name__}: {exc}"),
        }
    if tool == "apply_agent_change_set":
        payload["applied"] = False
    elif tool == "discard_agent_change_set":
        payload["discarded"] = False
    return ToolExecResult(ok=False, payload=payload)


__all__ = ["AgentDelegationHandlersMixin"]
