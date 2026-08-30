"""Running one delegated child agent, in the foreground, one level deep.

This is the whole of child execution.  It builds a child that shares no
conversation state with the root; read-only children use the primary workspace
and writable children use a linked worktree owned by AgentWorktreeManager:

* a **fresh private History** — a child-only system prompt and one user
  message carrying the parent-authored task;
* a **dedicated ToolRegistry** rooted to the effective workspace, with no MCP
  servers, dynamic tools, Skills state, or delegation;
* a **frozen child tool catalog** matching the frozen grant — read surface,
  optional path-scoped file editing, and optional terminal — which is also the
  enforcement boundary: :class:`~aura.conversation.manager_tool_round.
  ToolRoundRunner` refuses calls absent from the request's exact catalog;
* an **injected backend stream** for the resolved provider, handed straight to
  :class:`~aura.conversation.agent_loop.AgentLoop`.  No process-global stream
  hook is registered, swapped, or read — the root's production hook is not
  touched;
* the **root turn's cancellation event**, relayed rather than replaced, so
  Stop stops the child too and there is only ever one cancellation authority.

Delegation is serialized: a runner refuses a second concurrent run rather than
letting two children share a foreground.  The child's messages, reasoning, and
tool results stay here and are discarded when the run ends; only the
:class:`~aura.agents.delegation.DelegationResult` leaves.

Nothing here retries, falls back to another provider, or substitutes a model.
A provider or model that cannot be resolved is reported as a delegation
failure and the run does not happen.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from aura.agents.child_prompt import compose_child_system_prompt, compose_child_task_message
from aura.agents.delegation import (
    DelegationFailure,
    DelegationResult,
    DelegationStatus,
    DelegationUsage,
)
from aura.agents.local_state import AgentPermission
from aura.agents.models import AgentThinking, ModelTarget
from aura.agents.roster import AgentRosterEntry
from aura.agents.worktree import (
    AgentChangeSet,
    AgentWorktree,
    AgentWorktreeError,
    AgentWorktreeManager,
)
from aura.client import ApiError, ContentDelta, Event, Usage
from aura.conversation.agent_loop import AgentLoop, LoopStop
from aura.conversation.history import History
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools._types import ApprovalDecision, ApprovalRequest
from aura.conversation.tools.catalog import child_agent_tool_defs
from aura.conversation.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_CHILD_STREAM_LABEL = "agent_child_stream"


def _refuse_approval(request: ApprovalRequest) -> ApprovalDecision:
    """A child never asks the user for anything.

    Its catalog contains nothing that can request approval, so reaching this
    is a bug elsewhere — and the answer is still no.
    """
    return ApprovalDecision(action="reject", note="Delegated agents cannot write.")


def _approve_isolated_write(request: ApprovalRequest) -> ApprovalDecision:
    """The user's frozen writable grant authorizes worktree-local file edits."""
    return ApprovalDecision(
        action="approve",
        note="Approved by the frozen isolated-worktree Agent grant.",
    )


class ResolvedTarget:
    """The provider, model, and thinking mode one child will actually run with."""

    __slots__ = ("provider", "model", "thinking")

    def __init__(self, provider: str, model: str, thinking: str) -> None:
        self.provider = provider
        self.model = model
        self.thinking = thinking


def resolve_model_target(
    target: ModelTarget,
    thinking: AgentThinking,
    *,
    inherited_provider: str,
    inherited_model: str,
    inherited_thinking: str,
) -> tuple[ResolvedTarget | None, DelegationFailure | None, str]:
    """Resolve a definition's target to a real provider and model, or refuse.

    Two shapes are valid and nothing else is: inherit Aura's current provider
    *and* model together, or name both explicitly. Half a target is a mistake
    the store already refuses to save, and it is refused here too rather than
    being completed with a guess. There is no fallback: an unknown provider,
    an unconfigured one, or one this build cannot drive as an agent backend
    stops the delegation and says which.
    """
    from aura.config import has_usable_provider_configuration
    from aura.providers.registry import provider_registry

    if target.inherits:
        provider = str(inherited_provider or "").strip()
        model = str(inherited_model or "").strip()
        if not provider or not model:
            return None, DelegationFailure.MODEL_TARGET_INCOMPLETE, (
                "This agent inherits Aura's provider and model, but the current "
                "turn has no resolved provider/model to inherit."
            )
    elif not target.is_complete:
        return None, DelegationFailure.MODEL_TARGET_INCOMPLETE, (
            "This agent names only half a model target. A definition must either "
            "inherit both the provider and the model, or name both."
        )
    else:
        provider = target.provider.strip()
        model = target.model.strip()

    if not provider_registry.has(provider):
        return None, DelegationFailure.PROVIDER_UNKNOWN, (
            f"This build does not know a provider called '{provider}'."
        )

    kind = provider_registry.get(provider).kind
    if kind != "api_key":
        return None, DelegationFailure.PROVIDER_UNSUPPORTED, (
            f"Provider '{provider}' is a '{kind}' provider. Agents currently run "
            "only on hosted API providers."
        )
    if not has_usable_provider_configuration(provider):
        return None, DelegationFailure.PROVIDER_NOT_CONFIGURED, (
            f"No API key is configured for '{provider}'. Add one in "
            "Settings → API Keys, or point this agent at a provider that has one."
        )

    resolved_thinking = (
        str(inherited_thinking or "off") if thinking.inherits else thinking.value
    )
    return ResolvedTarget(provider, model, resolved_thinking), None, ""


class _ChildTranscript:
    """The child's private event sink.

    It keeps only what the structured result needs — the streamed answer text
    and whatever usage the provider reported — and drops everything else. The
    child's reasoning and tool results are never forwarded anywhere: they die
    with this object when the run returns.
    """

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.usage: DelegationUsage | None = None
        self.api_errors: list[str] = []

    def __call__(self, event: Event) -> None:
        if isinstance(event, ContentDelta):
            self.text_parts.append(event.text)
        elif isinstance(event, Usage):
            self.usage = DelegationUsage(
                prompt_tokens=int(event.prompt_tokens or 0),
                completion_tokens=int(event.completion_tokens or 0),
                cache_hit_tokens=int(event.cache_hit_tokens or 0),
                cache_miss_tokens=int(event.cache_miss_tokens or 0),
            )
        elif isinstance(event, ApiError):
            self.api_errors.append(str(event.message or ""))

    @property
    def text(self) -> str:
        return "".join(self.text_parts).strip()


class AgentDelegationRunner:
    """Runs delegated child agents for one root turn's provider and model.

    ``inherited_provider``/``inherited_model``/``inherited_thinking`` are the
    root turn's own choices, used by an agent whose definition inherits. They
    are supplied by the caller that owns the turn, so nothing here reads a
    setting or a global.

    ``backend_factory`` and ``registry_factory`` exist for testing, exactly as
    the Agents store's ``personal_dir`` does: production always builds an
    :class:`~aura.backends.api.APIAgentBackend` and a dedicated registry whose
    authority is fixed by the roster entry.
    """

    def __init__(
        self,
        *,
        workspace_root: Path | str | None,
        inherited_provider: str = "",
        inherited_model: str = "",
        inherited_thinking: str = "off",
        backend_factory: Callable[[str], Any] | None = None,
        registry_factory: Callable[[Path], ToolRegistry] | None = None,
        worktree_manager: AgentWorktreeManager | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root) if workspace_root is not None else None
        self._inherited_provider = str(inherited_provider or "")
        self._inherited_model = str(inherited_model or "")
        self._inherited_thinking = str(inherited_thinking or "off")
        self._backend_factory = backend_factory or _default_backend_factory
        self._registry_factory = registry_factory
        self._worktrees = worktree_manager or AgentWorktreeManager(workspace_root)
        # Delegation is foreground and serialized. The tool round already
        # refuses to run a COMMAND-effect call beside anything else, so this
        # is the second, unconditional guarantee: two children never share a
        # foreground, whatever route a call arrives by.
        self._lock = threading.Lock()

    # ---- inherited turn facts ---------------------------------------------

    def set_inherited_target(
        self, *, provider: str, model: str, thinking: str
    ) -> None:
        """Point inheritance at the root turn's own provider, model, and mode."""
        self._inherited_provider = str(provider or "")
        self._inherited_model = str(model or "")
        self._inherited_thinking = str(thinking or "off")

    def set_workspace_root(self, root: Path | str | None) -> None:
        self._workspace_root = Path(root) if root is not None else None
        self._worktrees.set_workspace_root(root)

    @property
    def worktree_manager(self) -> AgentWorktreeManager:
        return self._worktrees

    # ---- the delegated run -------------------------------------------------

    def run(
        self,
        entry: AgentRosterEntry,
        task: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> DelegationResult:
        """Run one child to completion and return its structured result."""
        definition = entry.definition
        agent_id = definition.agent_id
        # This is the only permission value the invocation will use.  It came
        # from the turn's immutable roster entry and is never recomputed after
        # provider/model resolution or during a later round.
        permission = AgentPermission(entry.permission)
        brief = compose_child_task_message(task)
        if not brief:
            return DelegationResult.failure(
                agent_id,
                DelegationFailure.TASK_MISSING,
                "No task was given to the agent.",
                agent_name=definition.name,
            )

        resolved, failure, message = resolve_model_target(
            definition.target,
            definition.thinking,
            inherited_provider=self._inherited_provider,
            inherited_model=self._inherited_model,
            inherited_thinking=self._inherited_thinking,
        )
        if resolved is None:
            return DelegationResult.failure(
                agent_id,
                failure or DelegationFailure.INTERNAL_ERROR,
                message,
                agent_name=definition.name,
                provider=definition.target.provider,
                model=definition.target.model,
            )

        if not self._lock.acquire(blocking=False):
            return DelegationResult.failure(
                agent_id,
                DelegationFailure.DELEGATION_BUSY,
                "Another agent is already running. Delegation is one at a time.",
                agent_name=definition.name,
                provider=resolved.provider,
                model=resolved.model,
            )
        try:
            if permission.allows_edit:
                return self._run_writable(
                    entry, brief, resolved, cancel_event, permission
                )
            result, _tests = self._run_child(
                entry,
                brief,
                resolved,
                cancel_event,
                workspace_root=self._child_root(),
                permission=permission,
            )
            return result
        except Exception as exc:
            from aura.config import redact_secrets

            logger.exception("agents: delegated run failed for %s", agent_id)
            return DelegationResult.failure(
                agent_id,
                DelegationFailure.INTERNAL_ERROR,
                redact_secrets(f"{type(exc).__name__}: {exc}"),
                agent_name=definition.name,
                provider=resolved.provider,
                model=resolved.model,
            )
        finally:
            self._lock.release()

    def _run_writable(
        self,
        entry: AgentRosterEntry,
        task: str,
        resolved: ResolvedTarget,
        cancel_event: threading.Event | None,
        permission: AgentPermission,
    ) -> DelegationResult:
        definition = entry.definition
        try:
            worktree = self._worktrees.create(definition.agent_id)
        except AgentWorktreeError as exc:
            return DelegationResult(
                status=DelegationStatus.FAILED,
                agent_id=definition.agent_id,
                agent_name=definition.name,
                failure_class=exc.failure_class,
                error=str(exc),
                provider=resolved.provider,
                model=resolved.model,
                permission=permission.value,
                change_set_id=exc.change_set_id,
                base_sha=exc.base_sha,
                result_sha=exc.result_sha,
                final_report="",
                extras={"recovery_path": exc.recovery_path} if exc.recovery_path else {},
            )

        child_result: DelegationResult
        tests: tuple[dict[str, Any], ...] = ()
        try:
            child_result, tests = self._run_child(
                entry,
                task,
                resolved,
                cancel_event,
                workspace_root=worktree.path,
                permission=permission,
                worktree=worktree,
            )
        except Exception as exc:
            from aura.config import redact_secrets

            logger.exception("agents: writable child failed for %s", definition.agent_id)
            child_result = DelegationResult(
                status=DelegationStatus.FAILED,
                agent_id=definition.agent_id,
                agent_name=definition.name,
                failure_class=DelegationFailure.INTERNAL_ERROR.value,
                error=redact_secrets(f"{type(exc).__name__}: {exc}"),
                provider=resolved.provider,
                model=resolved.model,
            )

        # _run_child closes the ToolRunner in a finally block.  At this point
        # the model has stopped and its full terminal process tree has been
        # terminated; only then may stable edits be staged and checkpointed.
        try:
            checkpoint = self._worktrees.recover(worktree)
        except AgentWorktreeError as exc:
            status = child_result.status
            if status is DelegationStatus.COMPLETED:
                status = (
                    DelegationStatus.PARTIAL
                    if child_result.result
                    else DelegationStatus.FAILED
                )
            detail = str(exc)
            if child_result.error:
                detail = f"{child_result.error} Checkpoint recovery also failed: {detail}"
            return replace(
                child_result,
                status=status,
                failure_class=exc.failure_class,
                error=detail,
                permission=permission.value,
                change_set_id=worktree.change_set_id,
                base_sha=worktree.base_sha,
                result_sha=exc.result_sha,
                changed_paths=(),
                diffstat="",
                tests_reported=tests,
                final_report=child_result.result,
                extras={
                    **child_result.extras,
                    "recovery_path": exc.recovery_path or str(worktree.path),
                },
            )
        return self._attach_checkpoint(
            child_result, checkpoint, permission=permission, tests=tests
        )

    @staticmethod
    def _attach_checkpoint(
        result: DelegationResult,
        checkpoint: AgentChangeSet,
        *,
        permission: AgentPermission,
        tests: tuple[dict[str, Any], ...],
    ) -> DelegationResult:
        extras = dict(result.extras)
        if checkpoint.failure_class:
            extras["lifecycle_warning"] = {
                "failure_class": checkpoint.failure_class,
                "error": checkpoint.error,
                "recovery_path": checkpoint.worktree_path,
            }
        return replace(
            result,
            permission=permission.value,
            change_set_id=checkpoint.change_set_id,
            base_sha=checkpoint.base_sha,
            result_sha=checkpoint.result_sha,
            changed_paths=checkpoint.changed_paths,
            diffstat=checkpoint.diffstat,
            tests_reported=tests,
            final_report=result.result,
            extras=extras,
        )

    def _run_child(
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

        # A fresh private History. It is created here, used here, and dropped
        # when this method returns: no child state is persisted, and a second
        # run of the same agent starts exactly where this one did.
        history = History()
        history.set_system(
            compose_child_system_prompt(
                definition,
                workspace_root=workspace_root,
                permission=permission,
                change_set_id=worktree.change_set_id if worktree is not None else "",
                base_sha=worktree.base_sha if worktree is not None else "",
            )
        )
        history.append_user_text(task)

        registry = self._child_registry(workspace_root, permission)
        tool_runner = ToolRunner(
            history=history, workspace_root=registry.workspace_root
        )
        tool_round = ToolRoundRunner(
            history=history, tools=registry, tool_runner=tool_runner
        )
        tool_round.begin_turn()

        backend = self._backend_factory(resolved.provider)
        loop = AgentLoop(
            history=history,
            stream=backend.stream,
            tool_round=tool_round,
            label=_CHILD_STREAM_LABEL,
        )

        transcript = _ChildTranscript()
        # The root turn's own cancel event, relayed. A child never creates a
        # second cancellation authority; when the user stops the turn, the
        # child's provider stream and tool round stop with it.
        cancel = cancel_event if cancel_event is not None else threading.Event()

        # The child's frozen catalog is resolved from the frozen grant once and
        # passed to every round, so the surface cannot move mid-run. It is the
        # exposure boundary the tool round enforces against.
        tool_defs = child_agent_tool_defs(permission)

        logger.info(
            "agent_delegation_start agent_id=%s provider=%s model=%s thinking=%s",
            definition.agent_id, resolved.provider, resolved.model, resolved.thinking,
        )
        try:
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
            except Exception:  # pragma: no cover - teardown cannot mask recovery
                logger.debug("agents: child runtime teardown failed", exc_info=True)

        duration_ms = int((time.monotonic() - started) * 1000)
        answer = transcript.text or _final_assistant_text(history)
        result = self._wrap(
            entry, outcome.stop, answer, transcript, resolved, duration_ms
        )
        logger.info(
            "agent_delegation_finished agent_id=%s status=%s duration_ms=%s",
            definition.agent_id, result.status.value, duration_ms,
        )
        return result, _reported_tests(history)

    def _child_root(self) -> Path:
        return self._workspace_root if self._workspace_root is not None else Path.home()

    def _child_registry(
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

    def _wrap(
        self,
        entry: AgentRosterEntry,
        stop: LoopStop,
        answer: str,
        transcript: _ChildTranscript,
        resolved: ResolvedTarget,
        duration_ms: int,
    ) -> DelegationResult:
        """Turn a loop outcome and the child's text into the one reported shape.

        A run that produced text but did not finish cleanly is ``partial``, not
        ``failed``: partial findings are still findings, and calling them a
        failure would throw away work the user paid for. A run that produced
        nothing makes no claim at all.
        """
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
                    result="",
                    failure_class=DelegationFailure.EMPTY_RESULT.value,
                    error="The agent finished without writing an answer.",
                    **common,
                )
            return DelegationResult(
                status=DelegationStatus.COMPLETED, result=answer, **common
            )

        # API_ERROR or NO_RESPONSE: the child's provider failed it. That is
        # the child's failure, reported here — never raised into the root
        # conversation as a harness error, and never retried.
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
        if answer:
            return DelegationResult(
                status=DelegationStatus.PARTIAL,
                result=answer,
                failure_class=failure.value,
                error=detail,
                **common,
            )
        return DelegationResult(
            status=DelegationStatus.FAILED,
            result="",
            failure_class=failure.value,
            error=detail,
            **common,
        )


def _final_assistant_text(history: History) -> str:
    """The last assistant prose in the child's private history, if any.

    The streamed text is the normal source; this covers a backend that reports
    only a final message without content deltas.
    """
    for message in reversed(history.messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _reported_tests(history: History) -> tuple[dict[str, Any], ...]:
    """Extract structured validation commands from the private child history."""
    tool_names: dict[str, str] = {}
    for message in history.messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
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


def _default_backend_factory(provider: str) -> Any:
    from aura.backends import APIAgentBackend

    return APIAgentBackend(provider=provider)


def _default_child_registry(workspace_root: Path) -> ToolRegistry:
    """A dedicated child registry: rooted to the workspace, read-only, alone.

    Nothing is connected to it — no MCP server, no dynamic tool directory
    scan, no Skills turn state, no roster — and ``read_only`` shuts off every
    mutation handler underneath the catalog as well.
    """
    return ToolRegistry(workspace_root=Path(workspace_root), read_only=True)


__all__ = [
    "AgentDelegationRunner",
    "ResolvedTarget",
    "resolve_model_target",
]
