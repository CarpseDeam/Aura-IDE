"""Private child model loop, transcript projection, and usage aggregation."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from aura.agents.child_prompt import compose_child_system_prompt
from aura.agents.delegation import (
    DelegationFailure,
    DelegationResult,
    DelegationStatus,
    DelegationUsage,
)
from aura.agents.local_state import AgentPermission
from aura.agents.model_resolution import ResolvedTarget
from aura.agents.roster import AgentRosterEntry
from aura.agents.worktree import AgentWorktree
from aura.client import ApiError, ContentDelta, Done, Event, Usage
from aura.config import redact_secrets
from aura.conversation.agent_loop import AgentLoop, LoopStop
from aura.conversation.history import History
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools._types import ApprovalDecision, ApprovalRequest
from aura.conversation.tools.catalog import child_agent_tool_defs
from aura.conversation.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)
_CHILD_STREAM_LABEL = "agent_child_stream"


def _refuse_approval(_request: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(action="reject", note="Delegated agents cannot write.")


def _approve_isolated_write(_request: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(
        action="approve",
        note="Approved by the frozen isolated-worktree Agent grant.",
    )


class ChildTranscript:
    """Keep only terminal prose, aggregate usage, and redacted provider errors."""

    def __init__(self) -> None:
        self._round_parts: list[str] = []
        self._terminal_seen = False
        self._terminal_text = ""
        self._prompt = 0
        self._completion = 0
        self._hit = 0
        self._miss = 0
        self.api_errors: list[str] = []

    def __call__(self, event: Event) -> None:
        if isinstance(event, ContentDelta):
            self._round_parts.append(event.text)
            return
        if isinstance(event, Usage):
            self._prompt += int(event.prompt_tokens or 0)
            self._completion += int(event.completion_tokens or 0)
            self._hit += int(event.cache_hit_tokens or 0)
            self._miss += int(event.cache_miss_tokens or 0)
            return
        if isinstance(event, ApiError):
            self.api_errors.append(redact_secrets(str(event.message or "")))
            return
        if not isinstance(event, Done):
            return

        message = event.full_message if isinstance(event.full_message, dict) else {}
        if message.get("tool_calls"):
            # Prose on a tool-call round is a private working note. Starting
            # the next round discards it rather than letting it become a result.
            self._round_parts.clear()
            return
        content = message.get("content")
        self._terminal_text = (
            content.strip()
            if isinstance(content, str)
            else "".join(self._round_parts).strip()
        )
        self._terminal_seen = True
        self._round_parts.clear()

    def answer(self, stop: LoopStop) -> str:
        if stop is LoopStop.COMPLETED:
            # An empty terminal response is authoritative. Never fall back to
            # prose from a prior tool-call round.
            return self._terminal_text if self._terminal_seen else ""
        return "".join(self._round_parts).strip()

    @property
    def usage(self) -> DelegationUsage | None:
        usage = DelegationUsage(
            prompt_tokens=self._prompt,
            completion_tokens=self._completion,
            cache_hit_tokens=self._hit,
            cache_miss_tokens=self._miss,
        )
        return None if usage.is_empty else usage


class ChildExecutor:
    """Construct and run one fresh private child conversation."""

    def __init__(
        self,
        *,
        backend_factory: Callable[[str], Any],
        registry_factory: Callable[[Path], ToolRegistry] | None = None,
    ) -> None:
        self._backend_factory = backend_factory
        self._registry_factory = registry_factory

    def run(
        self,
        entry: AgentRosterEntry,
        task: str,
        resolved: ResolvedTarget,
        cancel_event: threading.Event | None,
        *,
        workspace_root: Path,
        permission: AgentPermission,
        worktree: AgentWorktree | None = None,
    ) -> tuple[DelegationResult, tuple[dict[str, Any], ...]]:
        definition = entry.definition
        started = time.monotonic()
        history = History()
        history.set_system(
            compose_child_system_prompt(
                definition,
                workspace_root=workspace_root,
                permission=permission,
                change_set_id=worktree.change_set_id if worktree else "",
                base_sha=worktree.base_sha if worktree else "",
            )
        )
        history.append_user_text(task)

        registry = self._registry(workspace_root, permission)
        tool_runner = ToolRunner(history=history, workspace_root=registry.workspace_root)
        tool_round = ToolRoundRunner(
            history=history, tools=registry, tool_runner=tool_runner
        )
        tool_round.begin_turn()
        transcript = ChildTranscript()
        cancel = cancel_event if cancel_event is not None else threading.Event()
        tool_defs = child_agent_tool_defs(permission)

        logger.info(
            "agent_delegation_start agent_id=%s provider=%s model=%s thinking=%s",
            definition.agent_id,
            resolved.provider,
            resolved.model,
            resolved.thinking,
        )
        try:
            backend = self._backend_factory(resolved.provider)
            loop = AgentLoop(
                history=history,
                stream=backend.stream,
                tool_round=tool_round,
                label=_CHILD_STREAM_LABEL,
            )
            outcome = loop.run(
                on_event=transcript,
                approval_cb=(
                    _approve_isolated_write
                    if permission.allows_edit
                    else _refuse_approval
                ),
                cancel_event=cancel,
                model=resolved.model,
                thinking=resolved.thinking,
                tool_defs=tool_defs,
                temperature=0.7,
            )
        finally:
            try:
                tool_runner.close()
            except Exception:  # pragma: no cover
                logger.debug("agents: child runtime teardown failed", exc_info=True)

        duration_ms = int((time.monotonic() - started) * 1000)
        result = _wrap_result(
            entry,
            outcome.stop,
            transcript.answer(outcome.stop),
            transcript,
            resolved,
            duration_ms,
        )
        logger.info(
            "agent_delegation_finished agent_id=%s status=%s duration_ms=%s",
            definition.agent_id,
            result.status.value,
            duration_ms,
        )
        return result, reported_tests(history)

    def _registry(
        self, workspace_root: Path, permission: AgentPermission
    ) -> ToolRegistry:
        if self._registry_factory is not None:
            registry = self._registry_factory(workspace_root)
            registry.set_read_only(not permission.allows_edit)
            return registry
        return ToolRegistry(
            workspace_root=workspace_root,
            read_only=not permission.allows_edit,
            isolated_agent=permission.allows_edit,
        )


def _wrap_result(
    entry: AgentRosterEntry,
    stop: LoopStop,
    answer: str,
    transcript: ChildTranscript,
    resolved: ResolvedTarget,
    duration_ms: int,
) -> DelegationResult:
    definition = entry.definition
    common: dict[str, Any] = {
        "agent_id": definition.agent_id,
        "agent_name": definition.name,
        "provider": resolved.provider,
        "model": resolved.model,
        "usage": transcript.usage,
        "duration_ms": duration_ms,
    }
    error = transcript.api_errors[-1] if transcript.api_errors else ""
    if stop is LoopStop.CANCELLED:
        return DelegationResult(
            status=DelegationStatus.CANCELLED,
            result=answer,
            failure_class="cancelled",
            error="The user stopped this turn while the agent was running.",
            **common,
        )
    if stop is LoopStop.COMPLETED:
        if not answer:
            return DelegationResult(
                status=DelegationStatus.FAILED,
                failure_class=DelegationFailure.EMPTY_RESULT.value,
                error="The agent finished without writing an answer.",
                **common,
            )
        return DelegationResult(
            status=DelegationStatus.COMPLETED, result=answer, **common
        )

    failure = (
        DelegationFailure.PROVIDER_ERROR
        if stop is LoopStop.API_ERROR
        else DelegationFailure.EMPTY_RESULT
    )
    detail = error or (
        "The agent's provider failed during the run."
        if stop is LoopStop.API_ERROR
        else "The agent's provider ended the stream without a response."
    )
    return DelegationResult(
        status=DelegationStatus.PARTIAL if answer else DelegationStatus.FAILED,
        result=answer,
        failure_class=failure.value,
        error=detail,
        **common,
    )


def reported_tests(history: History) -> tuple[dict[str, Any], ...]:
    """Extract structured validation commands from the private history."""
    tool_names: dict[str, str] = {}
    for message in history.messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict):
                tool_names[str(call.get("id") or "")] = str(function.get("name") or "")

    reported: list[dict[str, Any]] = []
    for message in history.messages:
        call_id = str(message.get("tool_call_id") or "")
        if message.get("role") != "tool" or tool_names.get(call_id) != "shell":
            continue
        try:
            payload = json.loads(str(message.get("content") or "{}"))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or not (
            payload.get("validation_classification")
            or payload.get("counts_as_validation")
        ):
            continue
        reported.append(
            {
                "command": str(payload.get("command") or ""),
                "cwd": str(payload.get("working_directory") or ""),
                "ok": bool(payload.get("ok")),
                "exit_code": payload.get("exit_code"),
                "classification": str(
                    payload.get("validation_classification")
                    or payload.get("terminal_classification")
                    or ""
                ),
            }
        )
    return tuple(reported)


__all__ = ["ChildExecutor", "ChildTranscript", "reported_tests"]
