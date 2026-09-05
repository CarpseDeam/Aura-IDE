"""Root orchestration handlers plus direct-child workflow delegation.

This is a thin, deliberate seam.  It resolves the requested id against the
turn's frozen roster, refuses anything that is not on it, and hands the run to
the injected delegation runner.  It does not decide provider, model, prompt,
or tool surface — :mod:`aura.agents.runtime` owns all of that — and it never
lets a child's failure escape as an exception: a delegation that could not
happen is a structured tool result like any other.

An ordinary child registry has no roster, helper context, or runner, so this
handler answers every call there with the same truthful refusal. Every running
workflow Agent is configured only with its own frozen immediate children.
"""
from __future__ import annotations

import logging
from typing import Any

from aura.conversation.tools._types import ApprovalCallback, ToolExecResult

logger = logging.getLogger(__name__)


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

        roster = self._legacy_turn_agent_roster
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
        """Run one helper frozen directly under this workflow Agent."""
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
                    "directly attached to this workflow Agent."
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
            "root_step_node_id": helper.root_step_node_id,
            "immediate_parent_node_id": helper.immediate_parent_node_id,
            "connection_id": helper.connection_id,
            "depth": helper.depth,
            "lineage": list(helper.lineage),
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
        """Run an explicitly selected Workflow from this turn's frozen catalog."""
        from aura.agents.delegation import DelegationFailure
        from aura.agents.workflow_runner import WorkflowRunResult

        workflow_id = str(args.get("workflow_id") or "").strip()
        plan = self._turn_agent_context.workflow(workflow_id)
        if plan is None:
            available = ", ".join(self._turn_agent_context.workflows.ids)
            result = WorkflowRunResult.failure(
                workflow_id,
                DelegationFailure.DELEGATION_UNAVAILABLE,
                (
                    f"No saved Workflow with id '{workflow_id}' is available on "
                    "this turn's frozen catalog."
                    + (f" Available: {available}." if available else "")
                ),
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
                    "This workflow has a solid Step or helper descendant with "
                    "a Read / Write grant, but the frozen root turn forbids "
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
        extras: dict[str, Any] = {
            "workflow_graph_id": result.graph_id,
            "workflow_status": result.status.value,
        }
        if result.usage_groups:
            extras["delegation_usage_groups"] = [
                group.payload() for group in result.usage_groups
            ]
        return ToolExecResult(
            ok=result.ok,
            payload=result.payload(),
            extras=extras,
        )

    def _handle_run_agent_team(
        self, args: dict[str, Any], approval_cb: ApprovalCallback, reject_all: bool
    ) -> ToolExecResult:
        """Compile and run this automatic turn's one temporary native team."""
        from aura.agents.team_compiler import compile_agent_team
        from aura.agents.team_spec import parse_agent_team_spec
        from aura.agents.turn_context import AgentTurnMode
        from aura.conversation.tools.effects import ToolEffect

        context = self._turn_agent_context
        if context.mode is not AgentTurnMode.ENABLED or context.explicit_workflow_id:
            return _agent_team_failure(
                "agent_team_unavailable",
                "Automatic Agent team assembly is not available on this turn.",
            )
        if self._automatic_team_started:
            return _agent_team_failure(
                "agent_team_already_started",
                "Aura already started this turn's automatic Agent team. "
                "Continue from that Aura Result instead of starting another team.",
            )

        parsed = parse_agent_team_spec(args)
        if not parsed.ok or parsed.spec is None:
            return _agent_team_failure(
                "invalid_agent_team",
                "Aura could not understand the proposed Agent team.",
                errors=parsed.errors,
            )

        compiled, errors = compile_agent_team(
            parsed.spec,
            roster=context.roster,
            model_targets=context.model_targets,
            provider=context.root_provider,
            model=context.root_model,
            thinking=context.root_thinking,
        )
        if compiled is None:
            return _agent_team_failure(
                "invalid_agent_team",
                "Aura refused the proposed Agent team because it is not runnable.",
                errors=errors,
            )

        runner = self._agent_workflow_runner
        if runner is None:
            return _agent_team_failure(
                "delegation_unavailable",
                "Automatic Agent teams are not available in this runtime.",
            )

        if compiled.plan.writable and (
            self._read_only or self._plan_review.blocks(ToolEffect.MUTATION)
        ):
            return _agent_team_failure(
                "root_mutation_forbidden",
                "This team contains a Read / Write specialist, but the frozen "
                "root turn forbids mutation. Aura did not downgrade the grant "
                "or start the team.",
            )

        # Compilation and every authority check succeeded. Consume the turn's
        # single launch immediately before entering the existing runner, so a
        # provider/runtime failure cannot cause a second team to be spawned.
        self._automatic_team_started = True
        observer = self._agent_team_run_observer
        _notify_agent_team_observer(observer, "team_accepted", compiled)

        def _on_step(node_id, state) -> None:
            _notify_agent_team_observer(
                observer,
                "step_changed",
                compiled.plan.graph_id,
                node_id,
                state,
            )

        result = runner.run(
            compiled.plan,
            compiled.task,
            cancel_event=self.active_cancel_event,
            on_step=_on_step if observer is not None else None,
        )
        _notify_agent_team_observer(observer, "team_finished", result)
        payload = result.payload()
        payload["tool"] = "run_agent_team"
        payload.pop("workflow", None)
        payload["assembled"] = True
        payload["team"] = compiled.plan.catalog_row()

        extras: dict[str, Any] = {
            "agent_team_graph_id": result.graph_id,
            "agent_team_status": result.status.value,
        }
        if result.usage_groups:
            extras["delegation_usage_groups"] = [
                group.payload() for group in result.usage_groups
            ]
        return ToolExecResult(ok=result.ok, payload=payload, extras=extras)

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


def _agent_team_failure(
    failure_class: str,
    error: str,
    *,
    errors: tuple[str, ...] = (),
) -> ToolExecResult:
    payload: dict[str, Any] = {
        "ok": False,
        "tool": "run_agent_team",
        "status": "failed",
        "failure_class": failure_class,
        "error": error,
    }
    if errors:
        payload["errors"] = list(errors)
    return ToolExecResult(ok=False, payload=payload)


def _notify_agent_team_observer(observer: Any, method: str, *args: Any) -> None:
    """Project one run fact without allowing presentation to affect work."""
    if observer is None:
        return
    try:
        getattr(observer, method)(*args)
    except Exception:  # pragma: no cover - defensive UI boundary
        logger.debug("agents: automatic team observer raised", exc_info=True)


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
